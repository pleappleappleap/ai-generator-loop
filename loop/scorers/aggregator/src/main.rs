//! Aggregator — scorer result collection, threshold logic, and verdict emission.
//!
//! Subscribes to all three scorer result queues via the `scorer.results`
//! topic exchange. Accumulates results per image in Redis using the
//! `agg:session:<image_uuid>` key namespace with a configurable TTL.
//!
//! Applies cascade threshold logic as results arrive:
//!
//! 1. CLIP score below `clip_threshold` → publish cancel, emit rejected verdict.
//! 2. Artifact confidence above `artifact_threshold` → publish cancel, emit rejected verdict.
//! 3. All three scores collected within threshold bounds → emit candidate verdict.
//!
//! The `GETDEL` Redis operation provides atomic read-and-delete semantics,
//! preventing duplicate verdicts in scale-out deployments where multiple
//! aggregator instances may process results for the same image concurrently.
//!
//! Thresholds and broker URLs are read from `config.yaml` via serde_yaml.
//! Set `AI_IMAGE_ROOT` in the environment to locate `config.yaml`; falls
//! back to `./config.yaml` if the variable is absent.
//!
//! # Exchange Subscriptions
//!
//! | Exchange | Binding Key | Queue |
//! |----------|-------------|-------|
//! | `scorer.results` | `clip.*` | `aggregator.clip.queue` |
//! | `scorer.results` | `artifact.*` | `aggregator.artifact.queue` |
//! | `scorer.results` | `vlm.*` | `aggregator.vlm.queue` |
//!
//! # Exchange Publications
//!
//! | Target | Type | Condition |
//! |--------|------|-----------|
//! | `scorer.events` | Topic exchange | Threshold failure |
//! | `scorer.result` | Queue | All results collected or threshold failure |

use async_trait::async_trait;
use lapin::{
    options::*, types::FieldTable, Connection, ConnectionProperties,
};
use serde::Deserialize;
use serde_json::{json, Value};
use tracing::info;

// ── Config ────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct Thresholds {
    clip: f64,
    artifact: f64,
    score_timeout_secs: usize,
}

#[derive(Deserialize)]
struct Broker {
    rabbitmq_url: String,
    redis_url: String,
}

#[derive(Deserialize)]
struct Config {
    thresholds: Thresholds,
    broker: Broker,
}

// ── Traits ────────────────────────────────────────────────────────────────────

/// Provides read, atomic-delete, and write operations on a key-value store.
#[async_trait]
#[cfg_attr(test, mockall::automock)]
pub trait RedisClient: Send + Sync {
    /// Retrieve the value at `key`, returning `None` if absent or expired.
    async fn get(&self, key: &str) -> Option<String>;

    /// Atomically retrieve and delete the value at `key`.
    async fn getdel(&self, key: &str) -> Option<String>;

    /// Set `key` to `value` with a TTL of `ttl` seconds.
    async fn setex(&self, key: &str, ttl: usize, value: &str) -> bool;
}

/// Publishes a message to a RabbitMQ exchange or queue.
#[async_trait]
#[cfg_attr(test, mockall::automock)]
pub trait Publisher: Send + Sync {
    /// Publish a message to an exchange or queue.
    ///
    /// * `exchange` — Exchange name, or empty string for the default exchange.
    /// * `routing_key` — Routing key for topic exchanges, or queue name for the default exchange.
    /// * `payload` — Raw message bytes.
    async fn publish(
        &self,
        exchange: &str,
        routing_key: &str,
        payload: &[u8],
    ) -> Result<(), Box<dyn std::error::Error>>;
}

// ── Handlers ──────────────────────────────────────────────────────────────────

/// Handle a CLIP scorer result message.
///
/// Updates the `agg:session:<uuid>` Redis record with the CLIP score.
/// If the score falls below `clip_threshold`, atomically removes the session,
/// publishes a cancel event, and emits a rejected verdict. Otherwise stores
/// the updated record and calls `try_aggregate`.
pub async fn handle_clip_result(
    redis: &dyn RedisClient,
    publisher: &dyn Publisher,
    result: Value,
    clip_threshold: f64,
    score_timeout: usize,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = result["image_uuid"].as_str().ok_or("missing image_uuid")?;
    let key = format!("agg:session:{}", image_uuid);

    let raw = match redis.get(&key).await {
        Some(r) => r,
        None => return Ok(()),
    };

    let mut session: Value = serde_json::from_str(&raw)?;
    session["clip"] = result.clone();

    let clip_score = result["clip_score"].as_f64().unwrap_or(0.0);
    if clip_score < clip_threshold {
        if let Some(raw) = redis.getdel(&key).await {
            let session: Value = serde_json::from_str(&raw)?;
            publish_cancel(publisher, image_uuid).await?;
            publish_verdict(publisher, image_uuid, &session, "rejected", Some("clip_threshold"))
                .await?;
        }
        return Ok(());
    }

    redis.setex(&key, score_timeout, &serde_json::to_string(&session)?).await;
    try_aggregate(redis, publisher, image_uuid, &session).await?;
    Ok(())
}

/// Handle an artifact scorer result message.
///
/// Updates the session record with the artifact confidence. If the confidence
/// exceeds `artifact_threshold`, atomically removes the session, publishes a
/// cancel event, and emits a rejected verdict. Otherwise stores the updated
/// record and calls `try_aggregate`.
pub async fn handle_artifact_result(
    redis: &dyn RedisClient,
    publisher: &dyn Publisher,
    result: Value,
    artifact_threshold: f64,
    score_timeout: usize,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = result["image_uuid"].as_str().ok_or("missing image_uuid")?;
    let key = format!("agg:session:{}", image_uuid);

    let raw = match redis.get(&key).await {
        Some(r) => r,
        None => return Ok(()),
    };

    let mut session: Value = serde_json::from_str(&raw)?;
    session["artifact"] = result.clone();

    let ai_confidence = result["ai_confidence"].as_f64().unwrap_or(1.0);
    if ai_confidence > artifact_threshold {
        if let Some(raw) = redis.getdel(&key).await {
            let session: Value = serde_json::from_str(&raw)?;
            publish_cancel(publisher, image_uuid).await?;
            publish_verdict(
                publisher,
                image_uuid,
                &session,
                "rejected",
                Some("artifact_threshold"),
            )
            .await?;
        }
        return Ok(());
    }

    redis.setex(&key, score_timeout, &serde_json::to_string(&session)?).await;
    try_aggregate(redis, publisher, image_uuid, &session).await?;
    Ok(())
}

/// Handle a VLM scorer result message.
///
/// Updates the session record with the VLM scores and calls `try_aggregate`.
/// VLM results never trigger rejection directly.
pub async fn handle_vlm_result(
    redis: &dyn RedisClient,
    publisher: &dyn Publisher,
    result: Value,
    score_timeout: usize,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = result["image_uuid"].as_str().ok_or("missing image_uuid")?;
    let key = format!("agg:session:{}", image_uuid);

    let raw = match redis.get(&key).await {
        Some(r) => r,
        None => return Ok(()),
    };

    let mut session: Value = serde_json::from_str(&raw)?;
    session["vlm"] = result;

    redis.setex(&key, score_timeout, &serde_json::to_string(&session)?).await;
    try_aggregate(redis, publisher, image_uuid, &session).await?;
    Ok(())
}

// ── Aggregation ───────────────────────────────────────────────────────────────

/// Emit a candidate verdict if all three scorer results have arrived.
///
/// Uses GETDEL for atomic session removal, preventing duplicate verdicts
/// in scale-out deployments.
async fn try_aggregate(
    redis: &dyn RedisClient,
    publisher: &dyn Publisher,
    image_uuid: &str,
    session: &Value,
) -> Result<(), Box<dyn std::error::Error>> {
    let key = format!("agg:session:{}", image_uuid);
    if session["clip"].is_null()
        || session["artifact"].is_null()
        || session["vlm"].is_null()
    {
        return Ok(());
    }
    if let Some(raw) = redis.getdel(&key).await {
        let session: Value = serde_json::from_str(&raw)?;
        publish_verdict(publisher, image_uuid, &session, "candidate", None).await?;
    }
    Ok(())
}

// ── Publications ──────────────────────────────────────────────────────────────

/// Publish a cancel event to the scorer.events topic exchange.
async fn publish_cancel(
    publisher: &dyn Publisher,
    image_uuid: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let payload = serde_json::to_vec(&json!({"image_uuid": image_uuid}))?;
    publisher
        .publish("scorer.events", &format!("cancel.{}", image_uuid), &payload)
        .await
}

/// Publish a verdict to the scorer.result queue.
///
/// Includes prompt, session_uuid, workflow_path, and sequence_number from
/// the session record so downstream consumers (tactical LLM) have full context.
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

    let clip_threshold = cfg.thresholds.clip;
    let artifact_threshold = cfg.thresholds.artifact;
    let score_timeout = cfg.thresholds.score_timeout_secs;

    info!(
        clip_threshold,
        artifact_threshold,
        score_timeout,
        "Aggregator starting"
    );

    let conn = Connection::connect(
        &cfg.broker.rabbitmq_url,
        ConnectionProperties::default(),
    )
    .await?;

    let channel = conn.create_channel().await?;

    channel
        .exchange_declare(
            "scorer.results",
            lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions { durable: true, ..Default::default() },
            FieldTable::default(),
        )
        .await?;

    channel
        .exchange_declare(
            "scorer.events",
            lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions { durable: true, ..Default::default() },
            FieldTable::default(),
        )
        .await?;

    channel
        .queue_declare(
            "scorer.result",
            QueueDeclareOptions { durable: true, ..Default::default() },
            FieldTable::default(),
        )
        .await?;

    for (queue, binding) in &[
        ("aggregator.clip.queue", "clip.*"),
        ("aggregator.artifact.queue", "artifact.*"),
        ("aggregator.vlm.queue", "vlm.*"),
    ] {
        channel
            .queue_declare(
                queue,
                QueueDeclareOptions { durable: true, ..Default::default() },
                FieldTable::default(),
            )
            .await?;
        channel
            .queue_bind(
                queue,
                "scorer.results",
                binding,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await?;
    }

    info!("Aggregator ready");

    // Message consumption loop is wired in step 6 (Redis → SQLite migration).
    // The handler functions are unit-tested independently via the trait abstractions above.
    let _ = (clip_threshold, artifact_threshold, score_timeout);

    Ok(())
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use mockall::predicate::*;

    /// Verify that a CLIP score below threshold triggers cancel and rejected verdict.
    #[tokio::test]
    async fn test_clip_failure_triggers_cancel_and_rejected_verdict() {
        let mut mock_redis = MockRedisClient::new();
        let mut mock_publisher = MockPublisher::new();
        let image_uuid = "test-uuid-123";

        mock_redis.expect_get().returning(|_| {
            Some(json!({"image_uuid": "test-uuid-123", "clip": null, "artifact": null, "vlm": null,
                        "prompt": "", "session_uuid": "", "workflow_path": "", "sequence_number": 0})
                .to_string())
        });
        mock_redis.expect_getdel().times(1).returning(|_| {
            Some(json!({"image_uuid": "test-uuid-123", "clip": {"clip_score": 0.1},
                        "artifact": null, "vlm": null,
                        "prompt": "", "session_uuid": "", "workflow_path": "", "sequence_number": 0})
                .to_string())
        });
        mock_publisher
            .expect_publish()
            .with(eq("scorer.events"), eq(format!("cancel.{}", image_uuid).as_str()), always())
            .times(1)
            .returning(|_, _, _| Ok(()));
        mock_publisher
            .expect_publish()
            .with(eq(""), eq("scorer.result"), always())
            .times(1)
            .returning(|_, _, _| Ok(()));

        handle_clip_result(
            &mock_redis, &mock_publisher,
            json!({"image_uuid": image_uuid, "clip_score": 0.1}),
            0.25, 60,
        )
        .await
        .unwrap();
    }

    /// Verify that artifact confidence above threshold triggers cancel and rejected verdict.
    #[tokio::test]
    async fn test_artifact_failure_triggers_cancel_and_rejected_verdict() {
        let mut mock_redis = MockRedisClient::new();
        let mut mock_publisher = MockPublisher::new();
        let image_uuid = "test-uuid-456";

        mock_redis.expect_get().returning(|_| {
            Some(json!({"image_uuid": "test-uuid-456", "clip": {"clip_score": 0.8},
                        "artifact": null, "vlm": null,
                        "prompt": "", "session_uuid": "", "workflow_path": "", "sequence_number": 0})
                .to_string())
        });
        mock_redis.expect_getdel().times(1).returning(|_| {
            Some(json!({"image_uuid": "test-uuid-456", "clip": {"clip_score": 0.8},
                        "artifact": {"ai_confidence": 0.9}, "vlm": null,
                        "prompt": "", "session_uuid": "", "workflow_path": "", "sequence_number": 0})
                .to_string())
        });
        mock_publisher
            .expect_publish()
            .with(eq("scorer.events"), eq(format!("cancel.{}", image_uuid).as_str()), always())
            .times(1)
            .returning(|_, _, _| Ok(()));
        mock_publisher
            .expect_publish()
            .with(eq(""), eq("scorer.result"), always())
            .times(1)
            .returning(|_, _, _| Ok(()));

        handle_artifact_result(
            &mock_redis, &mock_publisher,
            json!({"image_uuid": image_uuid, "ai_confidence": 0.9}),
            0.5, 60,
        )
        .await
        .unwrap();
    }

    /// Verify that all scores within thresholds emit a candidate verdict.
    #[tokio::test]
    async fn test_all_scores_pass_emits_candidate() {
        let mut mock_redis = MockRedisClient::new();
        let mut mock_publisher = MockPublisher::new();
        let image_uuid = "test-uuid-789";

        mock_redis.expect_get().returning(|_| {
            Some(json!({"image_uuid": "test-uuid-789",
                        "clip": {"clip_score": 0.8}, "artifact": {"ai_confidence": 0.2}, "vlm": null,
                        "prompt": "test", "session_uuid": "sess-1",
                        "workflow_path": "/tmp/wf.json", "sequence_number": 1})
                .to_string())
        });
        mock_redis.expect_setex().returning(|_, _, _| true);
        mock_redis.expect_getdel().times(1).returning(|_| {
            Some(json!({"image_uuid": "test-uuid-789",
                        "clip": {"clip_score": 0.8}, "artifact": {"ai_confidence": 0.2},
                        "vlm": {"photorealism": 8, "anatomical_coherence": 7,
                                "interaction_plausibility": 8, "lighting_consistency": 7,
                                "prompt_adherence": 9, "issues": [], "recommendations": []},
                        "prompt": "test", "session_uuid": "sess-1",
                        "workflow_path": "/tmp/wf.json", "sequence_number": 1})
                .to_string())
        });
        mock_publisher
            .expect_publish()
            .with(eq(""), eq("scorer.result"), always())
            .times(1)
            .returning(|_, _, _| Ok(()));

        handle_vlm_result(
            &mock_redis, &mock_publisher,
            json!({"image_uuid": image_uuid, "photorealism": 8, "anatomical_coherence": 7,
                   "interaction_plausibility": 8, "lighting_consistency": 7,
                   "prompt_adherence": 9, "issues": [], "recommendations": []}),
            60,
        )
        .await
        .unwrap();
    }

    /// Verify that an expired or missing session record is silently ignored.
    #[tokio::test]
    async fn test_expired_session_is_ignored() {
        let mut mock_redis = MockRedisClient::new();
        let mock_publisher = MockPublisher::new();
        mock_redis.expect_get().returning(|_| None);
        handle_clip_result(
            &mock_redis, &mock_publisher,
            json!({"image_uuid": "expired-uuid", "clip_score": 0.8}),
            0.25, 60,
        )
        .await
        .unwrap();
    }
}
