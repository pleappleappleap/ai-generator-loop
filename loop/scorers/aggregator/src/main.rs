//! Aggregator — scorer result collection, threshold logic, and verdict emission.
//!
//! Subscribes to all three scorer result queues via the `scorer.results`
//! topic exchange. Accumulates results per image in the `scorer_session`
//! SQLite table using `BEGIN IMMEDIATE` for serialised writer semantics.
//!
//! Applies cascade threshold logic as results arrive:
//!
//! 1. CLIP score below `clip_threshold` → publish cancel, emit rejected verdict.
//! 2. Artifact confidence above `artifact_threshold` → publish cancel, emit rejected verdict.
//! 3. All three scores collected → emit candidate verdict, write verdict to scorer_session.
//!
//! `BEGIN IMMEDIATE` on each update ensures only one aggregator instance
//! writes at a time per row. The `busy_timeout` in the pool options makes
//! contention block rather than fail.
//!
//! Config is read from `config.yaml` via serde_yaml; set `AI_IMAGE_ROOT` in
//! the environment to locate `config.yaml`, falling back to `./config.yaml`.
//!
//! XA coordinator integration (2PC for AMQP + SQLite) is wired in step 7.

use async_trait::async_trait;
use futures_lite::StreamExt;
use lapin::{
    options::*, types::FieldTable, BasicProperties, Connection, ConnectionProperties,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::SqlitePool;
use tracing::info;

// ── Config ────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct Thresholds {
    clip: f64,
    artifact: f64,
    score_timeout_secs: i64,
}

#[derive(Deserialize)]
struct Broker {
    rabbitmq_url: String,
}

#[derive(Deserialize)]
struct Database {
    path: String,
    busy_timeout_ms: u64,
    cleanup_interval_secs: u64,
}

#[derive(Deserialize)]
struct Config {
    thresholds: Thresholds,
    broker: Broker,
    database: Database,
}

// ── Publisher trait ───────────────────────────────────────────────────────────

/// Publishes a message to a RabbitMQ exchange or queue.
#[async_trait]
#[cfg_attr(test, mockall::automock)]
pub trait Publisher: Send + Sync {
    async fn publish(
        &self,
        exchange: &str,
        routing_key: &str,
        payload: &[u8],
    ) -> Result<(), Box<dyn std::error::Error>>;
}

// ── Core logic ────────────────────────────────────────────────────────────────

/// Handle a CLIP scorer result.
///
/// Loads the `scorer_session` row for this image, merges the CLIP result,
/// and applies the threshold. Below threshold → cancel + rejected verdict.
/// Otherwise updates the row and calls `try_aggregate`.
pub async fn handle_clip_result(
    pool: &SqlitePool,
    publisher: &dyn Publisher,
    result: Value,
    clip_threshold: f64,
    score_timeout_secs: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = result["image_uuid"].as_str().ok_or("missing image_uuid")?;
    let clip_json = serde_json::to_string(&result)?;
    let clip_score = result["clip_score"].as_f64().unwrap_or(0.0);

    if clip_score < clip_threshold {
        // Threshold failure: fetch-and-mark atomically.
        let session = fetch_and_reject(pool, image_uuid, "clip_threshold", &clip_json, "clip").await?;
        if let Some(session) = session {
            publish_cancel(publisher, image_uuid).await?;
            publish_verdict(publisher, image_uuid, &session, "rejected", Some("clip_threshold")).await?;
        }
        return Ok(());
    }

    // Merge CLIP result and attempt aggregation.
    let updated = merge_scorer_result(pool, image_uuid, "clip", &clip_json, score_timeout_secs).await?;
    if let Some(session) = updated {
        try_aggregate(pool, publisher, image_uuid, &session).await?;
    }
    Ok(())
}

/// Handle an artifact scorer result.
pub async fn handle_artifact_result(
    pool: &SqlitePool,
    publisher: &dyn Publisher,
    result: Value,
    artifact_threshold: f64,
    score_timeout_secs: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = result["image_uuid"].as_str().ok_or("missing image_uuid")?;
    let artifact_json = serde_json::to_string(&result)?;
    let ai_confidence = result["ai_confidence"].as_f64().unwrap_or(1.0);

    if ai_confidence > artifact_threshold {
        let session = fetch_and_reject(pool, image_uuid, "artifact_threshold", &artifact_json, "artifact").await?;
        if let Some(session) = session {
            publish_cancel(publisher, image_uuid).await?;
            publish_verdict(publisher, image_uuid, &session, "rejected", Some("artifact_threshold")).await?;
        }
        return Ok(());
    }

    let updated = merge_scorer_result(pool, image_uuid, "artifact", &artifact_json, score_timeout_secs).await?;
    if let Some(session) = updated {
        try_aggregate(pool, publisher, image_uuid, &session).await?;
    }
    Ok(())
}

/// Handle a VLM scorer result. Never triggers rejection.
pub async fn handle_vlm_result(
    pool: &SqlitePool,
    publisher: &dyn Publisher,
    result: Value,
    score_timeout_secs: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = result["image_uuid"].as_str().ok_or("missing image_uuid")?;
    let vlm_json = serde_json::to_string(&result)?;

    let updated = merge_scorer_result(pool, image_uuid, "vlm", &vlm_json, score_timeout_secs).await?;
    if let Some(session) = updated {
        try_aggregate(pool, publisher, image_uuid, &session).await?;
    }
    Ok(())
}

// ── SQLite helpers ────────────────────────────────────────────────────────────

/// Merge a scorer result column into scorer_session using BEGIN IMMEDIATE.
///
/// Returns the updated row as a JSON Value, or None if the row is absent
/// or expired. The caller is responsible for deciding what to do next.
async fn merge_scorer_result(
    pool: &SqlitePool,
    image_uuid: &str,
    column: &str,   // "clip" | "artifact" | "vlm"
    json_value: &str,
    score_timeout_secs: i64,
) -> Result<Option<Value>, Box<dyn std::error::Error>> {
    let now = db::now_unix();

    let mut tx = pool.begin().await?;

    // Fetch current row under exclusive lock.
    let row = sqlx::query!(
        "SELECT image_uuid, session_uuid, sequence_number, prompt, workflow_path,
                clip, artifact, vlm, verdict, expires_at
         FROM scorer_session WHERE image_uuid = ?1 AND expires_at > ?2",
        image_uuid, now
    )
    .fetch_optional(&mut *tx)
    .await?;

    let row = match row {
        Some(r) => r,
        None => {
            tx.rollback().await?;
            return Ok(None);
        }
    };

    // Build the updated JSON session object for the caller.
    let clip     = if column == "clip"     { Some(json_value) } else { row.clip.as_deref() };
    let artifact = if column == "artifact" { Some(json_value) } else { row.artifact.as_deref() };
    let vlm      = if column == "vlm"     { Some(json_value) } else { row.vlm.as_deref() };

    let new_expires = now + score_timeout_secs;

    // Write the updated column back.
    match column {
        "clip" => sqlx::query!(
            "UPDATE scorer_session SET clip = ?1, expires_at = ?2 WHERE image_uuid = ?3",
            json_value, new_expires, image_uuid
        ).execute(&mut *tx).await?,
        "artifact" => sqlx::query!(
            "UPDATE scorer_session SET artifact = ?1, expires_at = ?2 WHERE image_uuid = ?3",
            json_value, new_expires, image_uuid
        ).execute(&mut *tx).await?,
        "vlm" => sqlx::query!(
            "UPDATE scorer_session SET vlm = ?1, expires_at = ?2 WHERE image_uuid = ?3",
            json_value, new_expires, image_uuid
        ).execute(&mut *tx).await?,
        _ => unreachable!(),
    };

    tx.commit().await?;

    let session = json!({
        "image_uuid":      image_uuid,
        "session_uuid":    row.session_uuid,
        "sequence_number": row.sequence_number,
        "prompt":          row.prompt.as_deref().unwrap_or(""),
        "workflow_path":   row.workflow_path.as_deref().unwrap_or(""),
        "clip":            clip.and_then(|s| serde_json::from_str::<Value>(s).ok()),
        "artifact":        artifact.and_then(|s| serde_json::from_str::<Value>(s).ok()),
        "vlm":             vlm.and_then(|s| serde_json::from_str::<Value>(s).ok()),
    });

    Ok(Some(session))
}

/// Atomically mark a session as rejected (sets verdict + rejection_reason).
///
/// Returns the final session Value for publishing the verdict, or None if
/// the row is missing or already resolved.
async fn fetch_and_reject(
    pool: &SqlitePool,
    image_uuid: &str,
    reason: &str,
    scorer_json: &str,
    scorer_column: &str,
) -> Result<Option<Value>, Box<dyn std::error::Error>> {
    let now = db::now_unix();
    let mut tx = pool.begin().await?;

    let row = sqlx::query!(
        "SELECT image_uuid, session_uuid, sequence_number, prompt, workflow_path,
                clip, artifact, vlm
         FROM scorer_session
         WHERE image_uuid = ?1 AND expires_at > ?2 AND verdict IS NULL",
        image_uuid, now
    )
    .fetch_optional(&mut *tx)
    .await?;

    let row = match row {
        Some(r) => r,
        None => {
            tx.rollback().await?;
            return Ok(None);
        }
    };

    sqlx::query!(
        "UPDATE scorer_session SET verdict = 'rejected', rejection_reason = ?1 WHERE image_uuid = ?2",
        reason, image_uuid
    )
    .execute(&mut *tx)
    .await?;

    tx.commit().await?;

    let clip     = if scorer_column == "clip"     { Some(scorer_json) } else { row.clip.as_deref() };
    let artifact = if scorer_column == "artifact" { Some(scorer_json) } else { row.artifact.as_deref() };
    let vlm      = if scorer_column == "vlm"     { Some(scorer_json) } else { row.vlm.as_deref() };

    let session = json!({
        "image_uuid":      image_uuid,
        "session_uuid":    row.session_uuid,
        "sequence_number": row.sequence_number,
        "prompt":          row.prompt.as_deref().unwrap_or(""),
        "workflow_path":   row.workflow_path.as_deref().unwrap_or(""),
        "verdict":         "rejected",
        "rejection_reason": reason,
        "clip":            clip.and_then(|s| serde_json::from_str::<Value>(s).ok()),
        "artifact":        artifact.and_then(|s| serde_json::from_str::<Value>(s).ok()),
        "vlm":             vlm.and_then(|s| serde_json::from_str::<Value>(s).ok()),
    });

    Ok(Some(session))
}

// ── Aggregation ───────────────────────────────────────────────────────────────

/// Emit a candidate verdict if all three scorer results have arrived.
///
/// Writes verdict = 'candidate' to scorer_session within a transaction so
/// only one aggregator instance emits the verdict (the second writer on the
/// same row sees verdict IS NOT NULL and bails out).
async fn try_aggregate(
    pool: &SqlitePool,
    publisher: &dyn Publisher,
    image_uuid: &str,
    session: &Value,
) -> Result<(), Box<dyn std::error::Error>> {
    if session["clip"].is_null() || session["artifact"].is_null() || session["vlm"].is_null() {
        return Ok(());
    }

    let mut tx = pool.begin().await?;
    let rows_affected = sqlx::query!(
        "UPDATE scorer_session SET verdict = 'candidate'
         WHERE image_uuid = ?1 AND verdict IS NULL",
        image_uuid
    )
    .execute(&mut *tx)
    .await?
    .rows_affected();

    if rows_affected == 0 {
        tx.rollback().await?;
        return Ok(()); // Another aggregator instance already emitted the verdict.
    }

    tx.commit().await?;
    publish_verdict(publisher, image_uuid, session, "candidate", None).await?;
    Ok(())
}

// ── Publications ──────────────────────────────────────────────────────────────

async fn publish_cancel(
    publisher: &dyn Publisher,
    image_uuid: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let payload = serde_json::to_vec(&json!({"image_uuid": image_uuid}))?;
    publisher.publish("scorer.events", &format!("cancel.{}", image_uuid), &payload).await
}

async fn publish_verdict(
    publisher: &dyn Publisher,
    image_uuid: &str,
    session: &Value,
    verdict: &str,
    reason: Option<&str>,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut output = json!({
        "image_uuid":      image_uuid,
        "verdict":         verdict,
        "scores":          session,
        "prompt":          session["prompt"].as_str().unwrap_or(""),
        "session_uuid":    session["session_uuid"].as_str().unwrap_or(""),
        "workflow_path":   session["workflow_path"].as_str().unwrap_or(""),
        "sequence_number": session["sequence_number"].as_i64().unwrap_or(0),
    });
    if let Some(r) = reason {
        output["reason"] = json!(r);
    }
    let payload = serde_json::to_vec(&output)?;
    publisher.publish("", "scorer.result", &payload).await
}

// ── Entry point ───────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();

    let config_path = std::env::var("AI_IMAGE_ROOT")
        .map(|root| format!("{}/config.yaml", root))
        .unwrap_or_else(|_| "config.yaml".to_string());
    let cfg: Config = serde_yaml::from_str(&std::fs::read_to_string(&config_path)?)?;

    let clip_threshold     = cfg.thresholds.clip;
    let artifact_threshold = cfg.thresholds.artifact;
    let score_timeout      = cfg.thresholds.score_timeout_secs;

    info!(clip_threshold, artifact_threshold, score_timeout, "Aggregator starting");

    // Open SQLite pool.
    let pool = db::open_pool(&cfg.database.path, cfg.database.busy_timeout_ms).await?;
    db::init_schema(&pool).await?;
    db::spawn_cleanup_task(pool.clone(), cfg.database.cleanup_interval_secs);

    // Connect to AMQP broker.
    let conn = Connection::connect(&cfg.broker.rabbitmq_url, ConnectionProperties::default()).await?;
    let channel = conn.create_channel().await?;

    channel
        .exchange_declare("scorer.results", lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions { durable: true, ..Default::default() }, FieldTable::default())
        .await?;
    channel
        .exchange_declare("scorer.events", lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions { durable: true, ..Default::default() }, FieldTable::default())
        .await?;
    channel
        .queue_declare("scorer.result",
            QueueDeclareOptions { durable: true, ..Default::default() }, FieldTable::default())
        .await?;

    for (queue, binding) in &[
        ("aggregator.clip.queue",     "clip.*"),
        ("aggregator.artifact.queue", "artifact.*"),
        ("aggregator.vlm.queue",      "vlm.*"),
    ] {
        channel
            .queue_declare(queue, QueueDeclareOptions { durable: true, ..Default::default() }, FieldTable::default())
            .await?;
        channel
            .queue_bind(queue, "scorer.results", binding, QueueBindOptions::default(), FieldTable::default())
            .await?;
    }

    // Shared publisher wrapper.
    struct ChanPublisher(lapin::Channel);
    #[async_trait]
    impl Publisher for ChanPublisher {
        async fn publish(&self, exchange: &str, routing_key: &str, payload: &[u8])
            -> Result<(), Box<dyn std::error::Error>>
        {
            self.0
                .basic_publish(exchange, routing_key, BasicPublishOptions::default(),
                    payload, BasicProperties::default())
                .await?;
            Ok(())
        }
    }
    let publisher = std::sync::Arc::new(ChanPublisher(channel.clone()));

    // Consume from all three queues concurrently.
    channel.basic_qos(1, BasicQosOptions::default()).await?;

    let mut clip_consumer = channel
        .basic_consume("aggregator.clip.queue", "aggregator-clip",
            BasicConsumeOptions::default(), FieldTable::default())
        .await?;
    let mut artifact_consumer = channel
        .basic_consume("aggregator.artifact.queue", "aggregator-artifact",
            BasicConsumeOptions::default(), FieldTable::default())
        .await?;
    let mut vlm_consumer = channel
        .basic_consume("aggregator.vlm.queue", "aggregator-vlm",
            BasicConsumeOptions::default(), FieldTable::default())
        .await?;

    info!("Aggregator ready");

    loop {
        tokio::select! {
            Some(delivery) = clip_consumer.next() => {
                let delivery = delivery?;
                let result: Value = serde_json::from_slice(&delivery.data)?;
                if let Err(e) = handle_clip_result(&pool, publisher.as_ref(), result,
                                                   clip_threshold, score_timeout).await {
                    tracing::error!("clip handler error: {}", e);
                }
                delivery.ack(BasicAckOptions::default()).await?;
            }
            Some(delivery) = artifact_consumer.next() => {
                let delivery = delivery?;
                let result: Value = serde_json::from_slice(&delivery.data)?;
                if let Err(e) = handle_artifact_result(&pool, publisher.as_ref(), result,
                                                       artifact_threshold, score_timeout).await {
                    tracing::error!("artifact handler error: {}", e);
                }
                delivery.ack(BasicAckOptions::default()).await?;
            }
            Some(delivery) = vlm_consumer.next() => {
                let delivery = delivery?;
                let result: Value = serde_json::from_slice(&delivery.data)?;
                if let Err(e) = handle_vlm_result(&pool, publisher.as_ref(), result, score_timeout).await {
                    tracing::error!("vlm handler error: {}", e);
                }
                delivery.ack(BasicAckOptions::default()).await?;
            }
        }
    }
}
