# Router

Rust component (AMQP 1.0 / Artemis). Subscribes to the `loop.events`
multicast address and fans out generation complete events to the
`scorer.requests` multicast address, triggering all three scorers
simultaneously.

No business logic. Message payload is forwarded unchanged with subject
property set to `score.<image_uuid>`.

## Build and Run

```bash
cd ~/ai-image/loop/scorers
cargo build --release -p router
AI_IMAGE_ROOT=~/ai-image ./target/release/router
```

## Address Subscriptions (AMQP 1.0)

| Address | Type | Role |
|---------|------|------|
| `loop.events` | multicast | Receives generation complete events from `comfyui_worker` |

## Address Publications (AMQP 1.0)

| Address | Type | Subject | Role |
|---------|------|---------|------|
| `scorer.requests` | multicast | `score.<image_uuid>` | Fans out to all scorer durable subscriptions |
