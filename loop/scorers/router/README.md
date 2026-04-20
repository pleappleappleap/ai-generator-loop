# Router

Rust component. Subscribes to the `loop.events` topic exchange and
fans out generation complete events to the `scorer.requests` topic
exchange, triggering all three scorers simultaneously.

No business logic. Message payload is forwarded unchanged.

## Build and Run

```bash
make build
./target/release/router
```

## Exchange Subscriptions

| Exchange | Binding Key | Role |
|----------|-------------|------|
| `loop.events` | `loop.complete.*` | Receives generation complete events |
| `scorer.requests` | — | Publishes score requests |
