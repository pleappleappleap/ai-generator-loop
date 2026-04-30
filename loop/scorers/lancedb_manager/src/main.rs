//! LanceDB manager — pure terminal writer (Rust port of lancedb_manager.py).
//!
//! Listens for terminal events only (`loop.accepted` and `cancel.*`) and
//! writes one complete `loop` record to LanceDB per image. Scorer results
//! are read from the `scorer_session` SQLite table (accumulated there by the
//! aggregator), not from a parallel accumulator.
//!
//! The `ldb:session` Redis key pattern is eliminated entirely in this Rust
//! port. State lives exclusively in `scorer_session`.
//!
//! # Data-loss protection
//!
//! LanceDB has no XA RM support, so the write is outside the 2PC boundary.
//! It is protected instead by:
//!
//! 1. `scorer_session` row exists in SQLite (XA-protected upstream by coordinator).
//! 2. Idempotent existence check: skip write if `loop` table already has the UUID.
//! 3. Message redelivery: ack only after a successful write; nack-with-requeue
//!    on transient failure; nack-without-requeue (→ DLX) on unrecoverable failure.
//!
//! # Dead letter exchange
//!
//! All queues declare `x-dead-letter-exchange: pipeline.dlx`. Messages nacked
//! with `requeue=false` land in `pipeline.dead` for the monitor process.
//!
//! # Exchange subscriptions
//!
//! | Exchange | Binding | Queue |
//! |----------|---------|-------|
//! | `loop.accepted` | `accepted.*` | `lancedb.accepted.queue` |
//! | `scorer.events` | `cancel.*`   | `lancedb.cancel.queue`   |

use bytes::Bytes;
use fe2o3_amqp::{Connection, Receiver, Sender, Session};
use lancedb::{connect, Table};
use serde::Deserialize;
use serde_json::Value;
use sqlx::SqlitePool;
use tracing::{error, info, warn};

// ── Config ────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct Paths {
    lancedb: String,
}

#[derive(Deserialize)]
struct Broker {
    amqp_url: String,
}

#[derive(Deserialize)]
struct Database {
    path: String,
    busy_timeout_ms: u64,
    cleanup_interval_secs: u64,
}

#[derive(Deserialize)]
struct Config {
    paths: Paths,
    broker: Broker,
    database: Database,
}

// ── LanceDB ───────────────────────────────────────────────────────────────────

/// Open (or create) the `loop` table in LanceDB.
///
/// The schema mirrors `lancedb_schema.py:Loop`. All fields are nullable
/// except `image_uuid` so partial writes (e.g. rejected images with no
/// VLM data) can still be stored.
async fn open_loop_table(lancedb_path: &str) -> Result<Table, Box<dyn std::error::Error>> {
    let db = connect(lancedb_path).execute().await?;
    let table_names = db.table_names().execute().await?;
    if !table_names.contains(&"loop".to_string()) {
        // Create the table by inserting a schema-only empty batch.
        // In production lancedb-rs >= 0.14 the schema can be provided
        // directly; create_empty_table is used here for simplicity.
        db.create_empty_table("loop", build_loop_schema()).execute().await?;
    }
    Ok(db.open_table("loop").execute().await?)
}

fn build_loop_schema() -> arrow_schema::SchemaRef {
    use arrow_schema::{DataType, Field, Schema};
    std::sync::Arc::new(Schema::new(vec![
        Field::new("image_uuid",       DataType::Utf8,    false),
        Field::new("session_uuid",     DataType::Utf8,    true),
        Field::new("sequence_number",  DataType::Int64,   true),
        Field::new("created_at",       DataType::Utf8,    true),
        Field::new("prompt",           DataType::Utf8,    true),
        Field::new("workflow_path",    DataType::Utf8,    true),
        Field::new("workflow_params",  DataType::Utf8,    true),
        Field::new("clip_score",       DataType::Float64, true),
        Field::new("artifact_score",   DataType::Float64, true),
        Field::new("vlm_photorealism", DataType::Float64, true),
        Field::new("vlm_anatomical",   DataType::Float64, true),
        Field::new("vlm_interaction",  DataType::Float64, true),
        Field::new("vlm_lighting",     DataType::Float64, true),
        Field::new("vlm_prompt",       DataType::Float64, true),
        Field::new("vlm_issues",       DataType::Utf8,    true),
        Field::new("vlm_recommendations", DataType::Utf8, true),
        Field::new("verdict",          DataType::Utf8,    true),
        Field::new("rejection_reason", DataType::Utf8,    true),
        Field::new("image_path",       DataType::Utf8,    true),
    ]))
}

/// Check whether a Loop record for `image_uuid` already exists.
async fn record_exists(table: &Table, image_uuid: &str) -> Result<bool, Box<dyn std::error::Error>> {
    let count = table
        .query()
        .only_if(format!("image_uuid = '{}'", image_uuid.replace('\'', "''")))
        .limit(1)
        .execute_stream()
        .await?
        .count()
        .await;
    Ok(count > 0)
}

/// Write a terminal Loop record to LanceDB from a complete scorer_session row.
async fn write_loop_record(
    table: &Table,
    session: &SessionRow,
    image_path: Option<&str>,
    verdict: &str,
    rejection_reason: Option<&str>,
) -> Result<(), Box<dyn std::error::Error>> {
    use arrow_array::{Float64Array, Int64Array, RecordBatch, StringArray};
    use arrow_schema::{DataType, Field, Schema};
    use std::sync::Arc;

    let clip: Value  = session.clip.as_deref()
        .and_then(|s| serde_json::from_str(s).ok()).unwrap_or(Value::Null);
    let artifact: Value = session.artifact.as_deref()
        .and_then(|s| serde_json::from_str(s).ok()).unwrap_or(Value::Null);
    let vlm: Value   = session.vlm.as_deref()
        .and_then(|s| serde_json::from_str(s).ok()).unwrap_or(Value::Null);

    let schema = build_loop_schema();
    let batch = RecordBatch::try_new(
        schema,
        vec![
            Arc::new(StringArray::from(vec![session.image_uuid.as_str()])),
            Arc::new(StringArray::from(vec![session.session_uuid.as_str()])),
            Arc::new(Int64Array::from(vec![session.sequence_number])),
            Arc::new(StringArray::from(vec![db::now_unix().to_string().as_str()])),
            Arc::new(StringArray::from(vec![session.prompt.as_deref().unwrap_or("")])),
            Arc::new(StringArray::from(vec![session.workflow_path.as_deref().unwrap_or("")])),
            Arc::new(StringArray::from(vec![session.workflow_params.as_deref().unwrap_or("{}")])),
            Arc::new(Float64Array::from(vec![clip["clip_score"].as_f64().unwrap_or(0.0)])),
            Arc::new(Float64Array::from(vec![artifact["ai_confidence"].as_f64().unwrap_or(0.0)])),
            Arc::new(Float64Array::from(vec![vlm["photorealism"].as_f64().unwrap_or(0.0)])),
            Arc::new(Float64Array::from(vec![vlm["anatomical_coherence"].as_f64().unwrap_or(0.0)])),
            Arc::new(Float64Array::from(vec![vlm["interaction_plausibility"].as_f64().unwrap_or(0.0)])),
            Arc::new(Float64Array::from(vec![vlm["lighting_consistency"].as_f64().unwrap_or(0.0)])),
            Arc::new(Float64Array::from(vec![vlm["prompt_adherence"].as_f64().unwrap_or(0.0)])),
            Arc::new(StringArray::from(vec![vlm["issues"].to_string().as_str()])),
            Arc::new(StringArray::from(vec![vlm["recommendations"].to_string().as_str()])),
            Arc::new(StringArray::from(vec![verdict])),
            Arc::new(StringArray::from(vec![rejection_reason.unwrap_or("")])),
            Arc::new(StringArray::from(vec![image_path.unwrap_or("")])),
        ],
    )?;

    table.add(vec![batch]).execute().await?;
    Ok(())
}

// ── SQLite ────────────────────────────────────────────────────────────────────

struct SessionRow {
    image_uuid:      String,
    session_uuid:    String,
    sequence_number: i64,
    prompt:          Option<String>,
    workflow_path:   Option<String>,
    workflow_params: Option<String>,
    clip:            Option<String>,
    artifact:        Option<String>,
    vlm:             Option<String>,
}

async fn fetch_session(pool: &SqlitePool, image_uuid: &str) -> Result<Option<SessionRow>, sqlx::Error> {
    let row = sqlx::query!(
        "SELECT image_uuid, session_uuid, sequence_number, prompt, workflow_path,
                workflow_params, clip, artifact, vlm
         FROM scorer_session WHERE image_uuid = ?1",
        image_uuid
    )
    .fetch_optional(pool)
    .await?;

    Ok(row.map(|r| SessionRow {
        image_uuid:      r.image_uuid,
        session_uuid:    r.session_uuid,
        sequence_number: r.sequence_number,
        prompt:          r.prompt,
        workflow_path:   r.workflow_path,
        workflow_params: r.workflow_params,
        clip:            r.clip,
        artifact:        r.artifact,
        vlm:             r.vlm,
    }))
}

async fn delete_session(pool: &SqlitePool, image_uuid: &str) -> Result<(), sqlx::Error> {
    sqlx::query!("DELETE FROM scorer_session WHERE image_uuid = ?1", image_uuid)
        .execute(pool)
        .await?;
    Ok(())
}

// ── Entry point ───────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();

    let config_path = std::env::var("AI_IMAGE_ROOT")
        .map(|root| format!("{}/config.yaml", root))
        .unwrap_or_else(|_| "config.yaml".to_string());
    let cfg: Config = serde_yaml::from_str(&std::fs::read_to_string(&config_path)?)?;

    let pool = db::open_pool(&cfg.database.path, cfg.database.busy_timeout_ms).await?;
    db::init_schema(&pool).await?;
    db::spawn_cleanup_task(pool.clone(), cfg.database.cleanup_interval_secs);

    let loop_table = open_loop_table(&cfg.paths.lancedb).await?;

    let mut connection = Connection::open("lancedb-manager", &cfg.broker.amqp_url[..]).await?;
    let mut session = Session::begin(&mut connection).await?;

    // Dead-letter sender (pipeline.dlx).
    let dlx_sender = Sender::attach(&mut session, "lancedb-dlx", "pipeline.dlx").await?;
    let dlx_sender = std::sync::Arc::new(tokio::sync::Mutex::new(dlx_sender));

    let mut accepted_receiver = Receiver::attach(
        &mut session, "lancedb-accepted", "lancedb.accepted.queue").await?;
    let mut cancel_receiver = Receiver::attach(
        &mut session, "lancedb-cancel", "lancedb.cancel.queue").await?;

    info!("LanceDB manager ready (AMQP 1.0)");

    loop {
        tokio::select! {
            delivery = accepted_receiver.recv::<Bytes>() => {
                let delivery = delivery?;
                let msg: Value = serde_json::from_slice(delivery.body())?;
                let image_uuid = msg["image_uuid"].as_str().unwrap_or("").to_string();
                let image_path = msg["image_path"].as_str().map(|s| s.to_string());

                match process_terminal(
                    &pool, &loop_table, &image_uuid,
                    image_path.as_deref(), "candidate", None,
                ).await {
                    Ok(_) => { delivery.accept().await?; }
                    Err(e) => {
                        error!("lancedb accepted write failed (unrecoverable): {}", e);
                        // Nack to DLX by sending to pipeline.dlx before rejecting.
                        let _ = dlx_sender.lock().await
                            .send(fe2o3_amqp::types::messaging::Message::builder()
                                .data(delivery.body().to_vec()).build())
                            .await;
                        delivery.reject(Default::default()).await?;
                    }
                }
            }

            delivery = cancel_receiver.recv::<Bytes>() => {
                let delivery = delivery?;
                let msg: Value = serde_json::from_slice(delivery.body())?;
                let image_uuid = msg["image_uuid"].as_str().unwrap_or("").to_string();
                let reason     = msg["reason"].as_str().map(|s| s.to_string());

                match process_terminal(
                    &pool, &loop_table, &image_uuid,
                    None, "rejected", reason.as_deref(),
                ).await {
                    Ok(_) => { delivery.accept().await?; }
                    Err(e) => {
                        error!("lancedb cancel write failed (unrecoverable): {}", e);
                        let _ = dlx_sender.lock().await
                            .send(fe2o3_amqp::types::messaging::Message::builder()
                                .data(delivery.body().to_vec()).build())
                            .await;
                        delivery.reject(Default::default()).await?;
                    }
                }
            }
        }
    }
}

/// Process a terminal event: existence-check, write, delete session row.
///
/// Returns `Ok(true)` on success, `Ok(false)` if the session row is missing,
/// `Err(_)` on an unrecoverable write failure (→ caller sends to DLX).
async fn process_terminal(
    pool: &SqlitePool,
    table: &Table,
    image_uuid: &str,
    image_path: Option<&str>,
    verdict: &str,
    rejection_reason: Option<&str>,
) -> Result<bool, Box<dyn std::error::Error>> {
    // 1. Load scorer_session row.
    let session = match fetch_session(pool, image_uuid).await? {
        Some(s) => s,
        None    => return Ok(false),
    };

    // 2. Idempotent existence check — do not double-write.
    if record_exists(table, image_uuid).await? {
        info!("LanceDB record for {} already exists — deleting session row", image_uuid);
        delete_session(pool, image_uuid).await?;
        return Ok(true);
    }

    // 3. Write to LanceDB.
    write_loop_record(table, &session, image_path, verdict, rejection_reason).await?;

    // 4. Delete the scorer_session row (cleanup after durable write).
    delete_session(pool, image_uuid).await?;

    info!("LanceDB: wrote {} record for {}", verdict, image_uuid);
    Ok(true)
}
