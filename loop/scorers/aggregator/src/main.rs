//! Aggregator — scorer result collection, threshold logic, and verdict emission.
//! (AMQP 1.0 / Artemis)
//!
//! Subscribes to all three scorer result queues via Artemis AMQP 1.0.
//! Accumulates results in `scorer_session` SQLite using `BEGIN IMMEDIATE`.
//!
//! XA coordinator integration (2PC for AMQP + SQLite) is wired in step 7
//! using the coordinator's internal channel API.

use async_trait::async_trait;
use bytes::Bytes;
use fe2o3_amqp::{Connection, Receiver, Sender, Session};
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

#[async_trait]
#[cfg_attr(test, mockall::automock)]
pub trait Publisher: Send + Sync {
    async fn publish(
        &self,
        address: &str,
        payload: &[u8],
    ) -> Result<(), Box<dyn std::error::Error>>;
}

// ── Handlers (unchanged from step 6 — just Publisher signature updated) ───────

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
        let session = fetch_and_reject(pool, image_uuid, "clip_threshold", &clip_json, "clip").await?;
        if let Some(session) = session {
            publish_cancel(publisher, image_uuid).await?;
            publish_verdict(publisher, image_uuid, &session, "rejected", Some("clip_threshold")).await?;
        }
        return Ok(());
    }

    let updated = merge_scorer_result(pool, image_uuid, "clip", &clip_json, score_timeout_secs).await?;
    if let Some(session) = updated {
        try_aggregate(pool, publisher, image_uuid, &session).await?;
    }
    Ok(())
}

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

// ── SQLite helpers (identical to step 6) ──────────────────────────────────────

async fn merge_scorer_result(
    pool: &SqlitePool,
    image_uuid: &str,
    column: &str,
    json_value: &str,
    score_timeout_secs: i64,
) -> Result<Option<Value>, Box<dyn std::error::Error>> {
    let now = db::now_unix();
    let mut tx = pool.begin().await?;

    let row = sqlx::query!(
        "SELECT image_uuid, session_uuid, sequence_number, prompt, workflow_path,
                clip, artifact, vlm
         FROM scorer_session WHERE image_uuid = ?1 AND expires_at > ?2",
        image_uuid, now
    )
    .fetch_optional(&mut *tx)
    .await?;

    let row = match row { Some(r) => r, None => { tx.rollback().await?; return Ok(None); } };

    let clip     = if column == "clip"     { Some(json_value) } else { row.clip.as_deref() };
    let artifact = if column == "artifact" { Some(json_value) } else { row.artifact.as_deref() };
    let vlm      = if column == "vlm"     { Some(json_value) } else { row.vlm.as_deref() };
    let new_expires = now + score_timeout_secs;

    match column {
        "clip"     => sqlx::query!("UPDATE scorer_session SET clip = ?1, expires_at = ?2 WHERE image_uuid = ?3",
                                   json_value, new_expires, image_uuid).execute(&mut *tx).await?,
        "artifact" => sqlx::query!("UPDATE scorer_session SET artifact = ?1, expires_at = ?2 WHERE image_uuid = ?3",
                                   json_value, new_expires, image_uuid).execute(&mut *tx).await?,
        "vlm"      => sqlx::query!("UPDATE scorer_session SET vlm = ?1, expires_at = ?2 WHERE image_uuid = ?3",
                                   json_value, new_expires, image_uuid).execute(&mut *tx).await?,
        _          => unreachable!(),
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
         FROM scorer_session WHERE image_uuid = ?1 AND expires_at > ?2 AND verdict IS NULL",
        image_uuid, now
    )
    .fetch_optional(&mut *tx)
    .await?;

    let row = match row { Some(r) => r, None => { tx.rollback().await?; return Ok(None); } };

    sqlx::query!("UPDATE scorer_session SET verdict = 'rejected', rejection_reason = ?1 WHERE image_uuid = ?2",
                 reason, image_uuid)
        .execute(&mut *tx).await?;
    tx.commit().await?;

    let clip     = if scorer_column == "clip"     { Some(scorer_json) } else { row.clip.as_deref() };
    let artifact = if scorer_column == "artifact" { Some(scorer_json) } else { row.artifact.as_deref() };
    let vlm      = if scorer_column == "vlm"     { Some(scorer_json) } else { row.vlm.as_deref() };

    Ok(Some(json!({
        "image_uuid":       image_uuid,
        "session_uuid":     row.session_uuid,
        "sequence_number":  row.sequence_number,
        "prompt":           row.prompt.as_deref().unwrap_or(""),
        "workflow_path":    row.workflow_path.as_deref().unwrap_or(""),
        "verdict":          "rejected",
        "rejection_reason": reason,
        "clip":     clip.and_then(|s| serde_json::from_str::<Value>(s).ok()),
        "artifact": artifact.and_then(|s| serde_json::from_str::<Value>(s).ok()),
        "vlm":      vlm.and_then(|s| serde_json::from_str::<Value>(s).ok()),
    })))
}

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
        "UPDATE scorer_session SET verdict = 'candidate' WHERE image_uuid = ?1 AND verdict IS NULL",
        image_uuid
    )
    .execute(&mut *tx).await?.rows_affected();

    if rows_affected == 0 { tx.rollback().await?; return Ok(()); }
    tx.commit().await?;
    publish_verdict(publisher, image_uuid, session, "candidate", None).await?;
    Ok(())
}

// ── Publications (AMQP 1.0 addresses) ────────────────────────────────────────

async fn publish_cancel(publisher: &dyn Publisher, image_uuid: &str)
    -> Result<(), Box<dyn std::error::Error>>
{
    let payload = serde_json::to_vec(&json!({"image_uuid": image_uuid}))?;
    publisher.publish(&format!("scorer.events/cancel.{}", image_uuid), &payload).await
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
    if let Some(r) = reason { output["reason"] = json!(r); }
    let payload = serde_json::to_vec(&output)?;
    publisher.publish("scorer.result", &payload).await
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

    let pool = db::open_pool(&cfg.database.path, cfg.database.busy_timeout_ms).await?;
    db::init_schema(&pool).await?;
    db::spawn_cleanup_task(pool.clone(), cfg.database.cleanup_interval_secs);

    let mut connection = Connection::open("aggregator", &cfg.broker.rabbitmq_url[..]).await?;
    let mut session = Session::begin(&mut connection).await?;

    let sender = Sender::attach(&mut session, "aggregator-sender", "scorer.result").await?;
    let events_sender = Sender::attach(&mut session, "aggregator-events", "scorer.events").await?;

    let mut clip_receiver = Receiver::attach(
        &mut session, "aggregator-clip", "aggregator.clip.queue").await?;
    let mut artifact_receiver = Receiver::attach(
        &mut session, "aggregator-artifact", "aggregator.artifact.queue").await?;
    let mut vlm_receiver = Receiver::attach(
        &mut session, "aggregator-vlm", "aggregator.vlm.queue").await?;

    // Sender wrapper that maps AMQP 1.0 addresses to the right sender.
    struct AmqpPublisher {
        verdict_sender: std::sync::Arc<tokio::sync::Mutex<Sender>>,
        events_sender:  std::sync::Arc<tokio::sync::Mutex<Sender>>,
    }
    #[async_trait]
    impl Publisher for AmqpPublisher {
        async fn publish(&self, address: &str, payload: &[u8])
            -> Result<(), Box<dyn std::error::Error>>
        {
            let msg = fe2o3_amqp::types::messaging::Message::builder()
                .properties(
                    fe2o3_amqp::types::messaging::Properties::builder()
                        .subject(address.to_string())
                        .build()
                )
                .data(payload.to_vec())
                .build();

            if address.starts_with("scorer.events") {
                self.events_sender.lock().await.send(msg).await??;
            } else {
                self.verdict_sender.lock().await.send(msg).await??;
            }
            Ok(())
        }
    }

    let publisher = std::sync::Arc::new(AmqpPublisher {
        verdict_sender: std::sync::Arc::new(tokio::sync::Mutex::new(sender)),
        events_sender:  std::sync::Arc::new(tokio::sync::Mutex::new(events_sender)),
    });

    info!("Aggregator ready (AMQP 1.0)");

    loop {
        tokio::select! {
            delivery = clip_receiver.recv::<Bytes>() => {
                let delivery = delivery?;
                let result: Value = serde_json::from_slice(delivery.body())?;
                if let Err(e) = handle_clip_result(&pool, publisher.as_ref(), result,
                                                   clip_threshold, score_timeout).await {
                    tracing::error!("clip error: {}", e);
                }
                delivery.accept().await?;
            }
            delivery = artifact_receiver.recv::<Bytes>() => {
                let delivery = delivery?;
                let result: Value = serde_json::from_slice(delivery.body())?;
                if let Err(e) = handle_artifact_result(&pool, publisher.as_ref(), result,
                                                       artifact_threshold, score_timeout).await {
                    tracing::error!("artifact error: {}", e);
                }
                delivery.accept().await?;
            }
            delivery = vlm_receiver.recv::<Bytes>() => {
                let delivery = delivery?;
                let result: Value = serde_json::from_slice(delivery.body())?;
                if let Err(e) = handle_vlm_result(&pool, publisher.as_ref(), result, score_timeout).await {
                    tracing::error!("vlm error: {}", e);
                }
                delivery.accept().await?;
            }
        }
    }
}
