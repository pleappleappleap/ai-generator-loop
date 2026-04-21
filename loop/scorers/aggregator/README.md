# Aggregator

Rust component (AMQP 1.0 / Artemis). Accumulates results from all three
scorers per image using the `scorer_session` SQLite table. Applies cascade
threshold logic and emits verdicts to `scorer.result`.

## Threshold Logic

1. CLIP score below `thresholds.clip` → mark rejected in SQLite, publish
   `cancel.*` to `scorer.events`, emit rejected verdict to `scorer.result`
2. Artifact confidence above `thresholds.artifact` → same cancel and reject flow
3. All three results within bounds → mark candidate in SQLite, emit candidate
   verdict to `scorer.result`

## SQLite State

Uses the `scorer_session` table in `pipeline.db` (owned by coordinator).
Merges results with `BEGIN IMMEDIATE` transactions. Rows expire via
`score_timeout_secs` and are cleaned up by the coordinator's background task.

## Build and Run

```bash
cd ~/ai-image/loop/scorers
cargo build --release -p aggregator
AI_IMAGE_ROOT=~/ai-image ./target/release/aggregator
```

## Address Subscriptions (AMQP 1.0)

| Queue | Source address | Role |
|-------|----------------|------|
| `aggregator.clip.queue` | `scorer.requests` multicast | CLIP results |
| `aggregator.artifact.queue` | `scorer.requests` multicast | Artifact results |
| `aggregator.vlm.queue` | `scorer.requests` multicast | VLM results |

## Address Publications (AMQP 1.0)

| Address | Type | Content |
|---------|------|---------|
| `scorer.result` | anycast | Final verdict with all scorer results |
| `scorer.events` | multicast | Cancel events (`cancel.<image_uuid>`) |
