# Aggregator

Rust component. Accumulates results from all three scorers per image
using Redis for correlation state. Applies cascade threshold logic
and emits verdicts.

## Threshold Logic

1. CLIP score below `CLIP_THRESHOLD` → cancel + rejected
2. Artifact confidence above `ARTIFACT_THRESHOLD` → cancel + rejected
3. All three pass → candidate

## Redis Key Namespace

Uses `agg:session:<image_uuid>` prefix. LanceDB manager uses
`ldb:session:<image_uuid>`. Distinct to avoid collision.

## Build and Run

```bash
make build
./target/release/aggregator
```

## Queue Subscriptions

| Queue | Exchange | Binding |
|-------|----------|---------|
| `aggregator.clip.queue` | `scorer.results` | `clip.*` |
| `aggregator.artifact.queue` | `scorer.results` | `artifact.*` |
| `aggregator.vlm.queue` | `scorer.results` | `vlm.*` |
