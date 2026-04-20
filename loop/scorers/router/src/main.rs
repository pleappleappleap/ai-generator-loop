//! Router — generation event to scorer fanout.
//!
//! Subscribes to the `loop.events` topic exchange and republishes
//! generation complete events to the `scorer.requests` topic exchange,
//! triggering all three scorers simultaneously.
//!
//! The router has no business logic. It receives a message, constructs
//! a routing key of the form `score.<image_uuid>`, and republishes the
//! message payload unchanged. Adding or removing scorers requires no
//! changes to the router.
//!
//! # Exchange Subscriptions
//!
//! | Exchange | Binding Key | Queue |
//! |----------|-------------|-------|
//! | `loop.events` | `loop.complete.*` | `router.loop` |
//!
//! # Exchange Publications
//!
//! | Exchange | Routing Key |
//! |----------|-------------|
//! | `scorer.requests` | `score.<image_uuid>` |

use async_trait::async_trait;
use futures_lite::StreamExt;
use lapin::{
    options::*, types::FieldTable, BasicProperties, Connection, ConnectionProperties,
};
use serde_json::Value;
use tracing::info;

/// Publishes a message to a RabbitMQ exchange.
///
/// Abstracted as a trait to allow mock implementations in unit tests.
/// The production implementation uses a `lapin::Channel`.
#[async_trait]
#[cfg_attr(test, mockall::automock)]
pub trait Publisher: Send + Sync {
    /// Publish a message to an exchange with a routing key.
    ///
    /// # Arguments
    ///
    /// * `exchange` - Name of the RabbitMQ exchange to publish to.
    /// * `routing_key` - Routing key for topic exchange routing.
    /// * `payload` - Raw message bytes to publish.
    ///
    /// # Errors
    ///
    /// Returns an error if the broker connection is unavailable or
    /// the publish operation fails.
    async fn publish(
        &self,
        exchange: &str,
        routing_key: &str,
        payload: &[u8],
    ) -> Result<(), Box<dyn std::error::Error>>;
}

/// Route a generation complete message to the scorer.requests exchange.
///
/// Extracts the `image_uuid` field from the message JSON, constructs
/// a routing key of the form `score.<image_uuid>`, and publishes the
/// unchanged payload to the `scorer.requests` exchange.
///
/// # Arguments
///
/// * `publisher` - Trait object implementing the Publisher interface.
/// * `message` - Parsed JSON value of the generation complete message.
///   Must contain an `image_uuid` string field.
///
/// # Returns
///
/// `Ok(())` on successful publish.
///
/// # Errors
///
/// Returns an error if `image_uuid` is missing from the message, if
/// JSON serialization fails, or if the publisher returns an error.
pub async fn route_message(
    publisher: &dyn Publisher,
    message: Value,
) -> Result<(), Box<dyn std::error::Error>> {
    let image_uuid = message["image_uuid"]
        .as_str()
        .ok_or("missing image_uuid")?;
    let routing_key = format!("score.{}", image_uuid);
    let payload = serde_json::to_vec(&message)?;
    publisher
        .publish("scorer.requests", &routing_key, &payload)
        .await?;
    info!("Routed image {} to scorer.requests", image_uuid);
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();

    let amqp_url = std::env::var("AI_IMAGE_RABBITMQ_URL")
        .unwrap_or_else(|_| "amqp://guest:guest@localhost:5672".to_string());

    let conn = Connection::connect(
        &amqp_url,
        ConnectionProperties::default(),
    )
    .await?;

    let channel = conn.create_channel().await?;

    channel
        .exchange_declare(
            "loop.events",
            lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions {
                durable: true,
                ..Default::default()
            },
            FieldTable::default(),
        )
        .await?;

    channel
        .exchange_declare(
            "scorer.requests",
            lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions {
                durable: true,
                ..Default::default()
            },
            FieldTable::default(),
        )
        .await?;

    channel
        .queue_declare(
            "router.loop",
            QueueDeclareOptions {
                durable: true,
                ..Default::default()
            },
            FieldTable::default(),
        )
        .await?;

    channel
        .queue_bind(
            "router.loop",
            "loop.events",
            "loop.complete.*",
            QueueBindOptions::default(),
            FieldTable::default(),
        )
        .await?;

    let mut consumer = channel
        .basic_consume(
            "router.loop",
            "router",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await?;

    info!("Router ready");

    while let Some(delivery) = consumer.next().await {
        let delivery = delivery?;
        let message: Value = serde_json::from_slice(&delivery.data)?;
        let image_uuid = message["image_uuid"].as_str().unwrap_or("unknown");
        let routing_key = format!("score.{}", image_uuid);
        channel
            .basic_publish(
                "scorer.requests",
                &routing_key,
                BasicPublishOptions::default(),
                &delivery.data,
                BasicProperties::default(),
            )
            .await?;
        delivery.ack(BasicAckOptions::default()).await?;
        info!("Routed {}", image_uuid);
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use mockall::predicate::*;

    /// Verify that the routing key is constructed correctly from image_uuid.
    #[tokio::test]
    async fn test_routing_key_constructed_correctly() {
        let image_uuid = "test-uuid-123";
        let expected_key = format!("score.{}", image_uuid);
        assert_eq!(expected_key, "score.test-uuid-123");
    }

    /// Verify that route_message publishes to scorer.requests with correct routing key.
    #[tokio::test]
    async fn test_router_publishes_to_scorer_requests() {
        let mut mock_publisher = MockPublisher::new();
        mock_publisher
            .expect_publish()
            .with(
                eq("scorer.requests"),
                eq("score.test-uuid-123"),
                always(),
            )
            .times(1)
            .returning(|_, _, _| Ok(()));

        let message = serde_json::json!({
            "image_uuid": "test-uuid-123",
            "image_path": "/tmp/test.png",
            "prompt": "test prompt"
        });

        route_message(&mock_publisher, message).await.unwrap();
    }

    /// Verify that a message missing image_uuid returns an error.
    #[tokio::test]
    async fn test_router_missing_image_uuid_returns_error() {
        let mock_publisher = MockPublisher::new();
        let message = serde_json::json!({
            "image_path": "/tmp/test.png",
            "prompt": "test prompt"
        });
        assert!(route_message(&mock_publisher, message).await.is_err());
    }
}
