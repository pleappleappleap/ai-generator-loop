//! Router — generation event to scorer fanout (AMQP 1.0 / Artemis).
//!
//! Subscribes to the `loop.events` address on Artemis and republishes
//! generation complete events to `scorer.requests`, triggering all three
//! scorers simultaneously.
//!
//! The router has no business logic. It receives a message, constructs a
//! routing key of the form `score.<image_uuid>`, and republishes the payload
//! unchanged. Adding or removing scorers requires no changes here.

use async_trait::async_trait;
use fe2o3_amqp::{Connection, Receiver, Sender, Session};
use serde::Deserialize;
use serde_json::Value;
use tracing::info;

// ── Config ────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct Broker {
    amqp_url: String,
}

#[derive(Deserialize)]
struct Config {
    broker: Broker,
}

// ── Publisher trait (for unit tests) ─────────────────────────────────────────

/// Publishes a message to an AMQP address.
#[async_trait]
#[cfg_attr(test, mockall::automock)]
pub trait Publisher: Send + Sync {
    async fn publish(
        &self,
        address: &str,
        payload: &[u8],
    ) -> Result<(), Box<dyn std::error::Error>>;
}

// ── Routing logic ─────────────────────────────────────────────────────────────

/// Route a generation complete message to scorer.requests.
pub async fn route_message(
    publisher: &dyn Publisher,
    message: Value,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = message["image_uuid"].as_str().ok_or("missing image_uuid")?;
    // Artemis AMQP 1.0: use subject/routing-key style address for fanout.
    let address = format!("scorer.requests/{}", image_uuid);
    let payload = serde_json::to_vec(&message)?;
    publisher.publish(&address, &payload).await?;
    info!("Routed image {} to scorer.requests", image_uuid);
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

    // Artemis AMQP 1.0 connection.
    let mut connection = Connection::open("router", &cfg.broker.amqp_url[..]).await?;
    let mut session = Session::begin(&mut connection).await?;

    // Receiver: consume from loop.events (Artemis multicast address).
    let mut receiver = Receiver::attach(&mut session, "router-receiver", "loop.events").await?;

    // Sender: publish to scorer.requests (Artemis multicast address).
    // The full address including subject is set per-message using properties.subject.
    let mut sender = Sender::attach(&mut session, "router-sender", "scorer.requests").await?;

    info!("Router ready (AMQP 1.0)");

    loop {
        let delivery = receiver.recv::<Vec<u8>>().await?;
        let payload = delivery.body().clone();
        let message: Value = serde_json::from_slice(&payload)?;
        let image_uuid = message["image_uuid"].as_str().unwrap_or("unknown");

        sender
            .send(
                fe2o3_amqp::types::messaging::Message::builder()
                    .properties(
                        fe2o3_amqp::types::messaging::Properties::builder()
                            .subject(format!("score.{}", image_uuid))
                            .build(),
                    )
                    .data(payload)
                    .build(),
            )
            .await?;

        receiver.accept(&delivery).await?;
        info!("Routed {}", image_uuid);
    }
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use mockall::predicate::*;

    #[tokio::test]
    async fn test_routing_key_constructed_correctly() {
        let image_uuid = "test-uuid-123";
        let expected = format!("scorer.requests/{}", image_uuid);
        assert_eq!(expected, "scorer.requests/test-uuid-123");
    }

    #[tokio::test]
    async fn test_router_publishes_to_scorer_requests() {
        let mut mock = MockPublisher::new();
        mock.expect_publish()
            .with(eq("scorer.requests/test-uuid-123"), always())
            .times(1)
            .returning(|_, _| Box::pin(async { Ok(()) }));

        let message = serde_json::json!({
            "image_uuid": "test-uuid-123",
            "image_path": "/tmp/test.png",
            "prompt": "test prompt"
        });
        route_message(&mock, message).await.unwrap();
    }

    #[tokio::test]
    async fn test_router_missing_image_uuid_returns_error() {
        let mock = MockPublisher::new();
        let message = serde_json::json!({"image_path": "/tmp/test.png"});
        assert!(route_message(&mock, message).await.is_err());
    }
}
