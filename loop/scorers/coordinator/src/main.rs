//! XA Coordinator — 2PC transaction manager and Python budget API.
//!
//! Responsibilities:
//!
//! 1. **XA state machine** — `Begin → Enlist → Prepare → Commit/Rollback`.
//!    Writes durable `xa_log` entries before phase 2. The SQLite FULL-sync
//!    WAL ensures the log survives crashes.
//!
//! 2. **Crash recovery** — on startup, scans `xa_log` for
//!    `state = 'prepared'` entries and completes or rolls back each one.
//!
//! 3. **Unix socket API** — Python processes that cannot participate in
//!    Rust-owned XA transactions (tactical_llm, session.py) send JSON
//!    requests to a Unix domain socket for budget operations.
//!
//! # Unix socket protocol
//!
//! The socket path is `{database.path}.sock` (e.g. `pipeline.db.sock`).
//! Each request is a newline-terminated JSON object; each response is a
//! newline-terminated JSON object.
//!
//! Request variants:
//! ```json
//! {"op":"BudgetInit","session_uuid":"...","max_retries":3,"max_inpaints":2}
//! {"op":"BudgetGet","session_uuid":"..."}
//! {"op":"BudgetUpdate","session_uuid":"...","field":"retries_used"}
//! {"op":"SessionInit","image_uuid":"...","session_uuid":"...","sequence_number":0,
//!  "prompt":"...","workflow_path":"...","workflow_params":"{}","score_timeout_secs":60}
//! ```
//!
//! Response variants:
//! ```json
//! {"ok":true}
//! {"ok":true,"retries_used":1,"inpaints_used":0,"max_retries":3,"max_inpaints":2}
//! {"ok":false,"error":"..."}
//! ```
//!
//! # XA note (step 7 → step 9)
//!
//! True 2PC with Artemis requires AMQP 1.0 XA support via fe2o3-amqp
//! (step 9). In this step, the coordinator implements the SQLite side of
//! XA (xa_log + fsync) and stubs out the Artemis RM enlistment. The
//! `enlist(Resource::Artemis)` call is recorded in xa_log participants but
//! the Artemis publish is done as a best-effort non-XA operation. Full 2PC
//! across both RMs is wired in step 9.

use serde::{Deserialize, Serialize};
use sqlx::SqlitePool;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tokio::sync::Mutex;
use tracing::{error, info, warn};

// ── Config ────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct Database {
    path: String,
    busy_timeout_ms: u64,
    cleanup_interval_secs: u64,
}

#[derive(Deserialize)]
struct Config {
    database: Database,
}

// ── XA state machine ──────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum XaState {
    Active,
    Prepared,
    Committed,
    Rolledback,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
enum Resource {
    Artemis,
    Sqlite,
}

#[derive(Debug)]
struct XaTransaction {
    xid: String,
    state: XaState,
    participants: Vec<String>,
}

/// In-memory XA transaction registry.
type XaRegistry = Arc<Mutex<HashMap<String, XaTransaction>>>;

async fn xa_begin(pool: &SqlitePool, registry: &XaRegistry, xid: String) -> Result<(), Box<dyn std::error::Error>> {
    let now = db::now_unix();
    sqlx::query!(
        "INSERT INTO xa_log (xid, state, participants, created_at) VALUES (?1, 'prepared', '[]', ?2)",
        xid, now
    )
    .execute(pool)
    .await?;

    let tx = XaTransaction { xid: xid.clone(), state: XaState::Active, participants: vec![] };
    registry.lock().await.insert(xid, tx);
    Ok(())
}

async fn xa_enlist(registry: &XaRegistry, xid: &str, resource: Resource) -> Result<(), Box<dyn std::error::Error>> {
    let mut guard = registry.lock().await;
    let tx = guard.get_mut(xid).ok_or("unknown xid")?;
    let name = format!("{:?}", resource);
    if !tx.participants.contains(&name) {
        tx.participants.push(name);
    }
    Ok(())
}

/// Phase 1: write Prepared to xa_log with the participant list.
async fn xa_prepare(pool: &SqlitePool, registry: &XaRegistry, xid: &str) -> Result<(), Box<dyn std::error::Error>> {
    let participants = {
        let mut guard = registry.lock().await;
        let tx = guard.get_mut(xid).ok_or("unknown xid")?;
        tx.state = XaState::Prepared;
        serde_json::to_string(&tx.participants)?
    };

    // fsync-backed write — SqliteSynchronous::Full in open_pool() ensures durability.
    sqlx::query!(
        "UPDATE xa_log SET state = 'prepared', participants = ?1 WHERE xid = ?2",
        participants, xid
    )
    .execute(pool)
    .await?;
    Ok(())
}

/// Phase 2: commit — write Committed to xa_log, remove from registry.
async fn xa_commit(pool: &SqlitePool, registry: &XaRegistry, xid: &str) -> Result<(), Box<dyn std::error::Error>> {
    let now = db::now_unix();
    sqlx::query!(
        "UPDATE xa_log SET state = 'committed', resolved_at = ?1 WHERE xid = ?2",
        now, xid
    )
    .execute(pool)
    .await?;
    registry.lock().await.remove(xid);
    Ok(())
}

async fn xa_rollback(pool: &SqlitePool, registry: &XaRegistry, xid: &str) -> Result<(), Box<dyn std::error::Error>> {
    let now = db::now_unix();
    sqlx::query!(
        "UPDATE xa_log SET state = 'rolledback', resolved_at = ?1 WHERE xid = ?2",
        now, xid
    )
    .execute(pool)
    .await?;
    registry.lock().await.remove(xid);
    Ok(())
}

/// Crash recovery: complete any transactions that were prepared but not resolved.
///
/// Called on startup before accepting new connections. For each prepared entry
/// in xa_log, this implementation rolls back (conservative recovery strategy).
/// When Artemis XA support is wired in step 9, this will query each RM for
/// the transaction status before deciding.
async fn recover_prepared_transactions(pool: &SqlitePool) -> Result<(), Box<dyn std::error::Error>> {
    let prepared = sqlx::query!(
        "SELECT xid FROM xa_log WHERE state = 'prepared'"
    )
    .fetch_all(pool)
    .await?;

    if prepared.is_empty() {
        info!("Crash recovery: no prepared transactions found");
        return Ok(());
    }

    warn!("Crash recovery: {} prepared transaction(s) found — rolling back", prepared.len());
    let now = db::now_unix();
    for row in prepared {
        sqlx::query!(
            "UPDATE xa_log SET state = 'rolledback', resolved_at = ?1 WHERE xid = ?2",
            now, row.xid
        )
        .execute(pool)
        .await?;
        warn!("Crash recovery: rolled back xid={}", row.xid);
    }
    Ok(())
}

// ── Unix socket API ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
#[serde(tag = "op")]
enum ApiRequest {
    BudgetInit {
        session_uuid: String,
        max_retries: i64,
        max_inpaints: i64,
    },
    BudgetGet {
        session_uuid: String,
    },
    BudgetUpdate {
        session_uuid: String,
        field: String, // "retries_used" | "inpaints_used"
    },
    SessionInit {
        image_uuid: String,
        session_uuid: String,
        sequence_number: i64,
        prompt: String,
        workflow_path: String,
        workflow_params: String,
        score_timeout_secs: i64,
    },
}

#[derive(Serialize)]
struct ApiResponse {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    retries_used: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    inpaints_used: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_retries: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_inpaints: Option<i64>,
}

impl ApiResponse {
    fn ok() -> Self {
        Self { ok: true, error: None, retries_used: None, inpaints_used: None,
               max_retries: None, max_inpaints: None }
    }
    fn err(msg: impl Into<String>) -> Self {
        Self { ok: false, error: Some(msg.into()), retries_used: None, inpaints_used: None,
               max_retries: None, max_inpaints: None }
    }
}

async fn handle_api_request(pool: &SqlitePool, req: ApiRequest) -> ApiResponse {
    match req {
        ApiRequest::BudgetInit { session_uuid, max_retries, max_inpaints } => {
            let now = db::now_unix();
            let expires_at = now + 86400; // 24 h session budget lifetime
            match sqlx::query!(
                "INSERT OR IGNORE INTO tactical_budget
                 (session_uuid, max_retries, max_inpaints, created_at, expires_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                session_uuid, max_retries, max_inpaints, now, expires_at
            )
            .execute(pool)
            .await
            {
                Ok(_) => ApiResponse::ok(),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::BudgetGet { session_uuid } => {
            match sqlx::query!(
                "SELECT retries_used, inpaints_used, max_retries, max_inpaints
                 FROM tactical_budget WHERE session_uuid = ?1",
                session_uuid
            )
            .fetch_optional(pool)
            .await
            {
                Ok(Some(row)) => ApiResponse {
                    ok: true,
                    error: None,
                    retries_used:  Some(row.retries_used),
                    inpaints_used: Some(row.inpaints_used),
                    max_retries:   Some(row.max_retries),
                    max_inpaints:  Some(row.max_inpaints),
                },
                Ok(None) => ApiResponse::err("session not found"),
                Err(e)   => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::BudgetUpdate { session_uuid, field } => {
            let result = match field.as_str() {
                "retries_used" => sqlx::query!(
                    "UPDATE tactical_budget SET retries_used = retries_used + 1 WHERE session_uuid = ?1",
                    session_uuid
                ).execute(pool).await,
                "inpaints_used" => sqlx::query!(
                    "UPDATE tactical_budget SET inpaints_used = inpaints_used + 1 WHERE session_uuid = ?1",
                    session_uuid
                ).execute(pool).await,
                _ => return ApiResponse::err(format!("unknown field: {}", field)),
            };
            match result {
                Ok(_) => ApiResponse::ok(),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::SessionInit {
            image_uuid, session_uuid, sequence_number, prompt,
            workflow_path, workflow_params, score_timeout_secs,
        } => {
            let now = db::now_unix();
            let expires_at = now + score_timeout_secs;
            match sqlx::query!(
                "INSERT OR IGNORE INTO scorer_session
                 (image_uuid, session_uuid, sequence_number, prompt, workflow_path,
                  workflow_params, created_at, expires_at)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
                image_uuid, session_uuid, sequence_number, prompt,
                workflow_path, workflow_params, now, expires_at
            )
            .execute(pool)
            .await
            {
                Ok(_) => ApiResponse::ok(),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }
    }
}

async fn run_socket_server(pool: Arc<SqlitePool>, socket_path: &str) -> Result<(), Box<dyn std::error::Error>> {
    // Clean up stale socket from a previous run.
    let _ = std::fs::remove_file(socket_path);
    let listener = UnixListener::bind(socket_path)?;
    info!("Coordinator Unix socket listening at {}", socket_path);

    loop {
        let (stream, _) = listener.accept().await?;
        let pool = pool.clone();
        tokio::spawn(async move {
            let (reader, mut writer) = stream.into_split();
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let response = match serde_json::from_str::<ApiRequest>(&line) {
                    Ok(req) => handle_api_request(&pool, req).await,
                    Err(e)  => ApiResponse::err(format!("parse error: {}", e)),
                };
                let mut resp_bytes = serde_json::to_vec(&response).unwrap_or_default();
                resp_bytes.push(b'\n');
                if let Err(e) = writer.write_all(&resp_bytes).await {
                    error!("Socket write error: {}", e);
                    break;
                }
            }
        });
    }
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

    // Crash recovery before accepting new work.
    recover_prepared_transactions(&pool).await?;

    let socket_path = format!("{}.sock", cfg.database.path);
    let pool = Arc::new(pool);

    info!("Coordinator starting");
    run_socket_server(pool, &socket_path).await?;
    Ok(())
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use sqlx::sqlite::SqlitePoolOptions;

    /// Open a fresh in-memory SQLite pool with the pipeline schema applied.
    async fn make_pool() -> SqlitePool {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .expect("in-memory pool");
        db::init_schema(&pool).await.expect("init_schema");
        pool
    }

    // ── BudgetInit ────────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_budget_init_creates_row() {
        let pool = make_pool().await;
        let resp = handle_api_request(&pool, ApiRequest::BudgetInit {
            session_uuid: "sess-1".into(),
            max_retries:  3,
            max_inpaints: 2,
        }).await;
        assert!(resp.ok);

        // Verify row exists via BudgetGet
        let get = handle_api_request(&pool, ApiRequest::BudgetGet {
            session_uuid: "sess-1".into(),
        }).await;
        assert!(get.ok);
        assert_eq!(get.max_retries,   Some(3));
        assert_eq!(get.max_inpaints,  Some(2));
        assert_eq!(get.retries_used,  Some(0));
        assert_eq!(get.inpaints_used, Some(0));
    }

    #[tokio::test]
    async fn test_budget_init_idempotent() {
        let pool = make_pool().await;
        // First insert
        handle_api_request(&pool, ApiRequest::BudgetInit {
            session_uuid: "sess-1".into(), max_retries: 3, max_inpaints: 2,
        }).await;
        // Second insert with different params — INSERT OR IGNORE keeps first
        let resp = handle_api_request(&pool, ApiRequest::BudgetInit {
            session_uuid: "sess-1".into(), max_retries: 99, max_inpaints: 99,
        }).await;
        assert!(resp.ok);

        let get = handle_api_request(&pool, ApiRequest::BudgetGet {
            session_uuid: "sess-1".into(),
        }).await;
        assert_eq!(get.max_retries, Some(3)); // original value preserved
    }

    // ── BudgetGet ─────────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_budget_get_missing_session_returns_error() {
        let pool = make_pool().await;
        let resp = handle_api_request(&pool, ApiRequest::BudgetGet {
            session_uuid: "nonexistent".into(),
        }).await;
        assert!(!resp.ok);
        assert!(resp.error.is_some());
    }

    // ── BudgetUpdate ──────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_budget_update_retries_increments() {
        let pool = make_pool().await;
        handle_api_request(&pool, ApiRequest::BudgetInit {
            session_uuid: "sess-1".into(), max_retries: 3, max_inpaints: 2,
        }).await;

        handle_api_request(&pool, ApiRequest::BudgetUpdate {
            session_uuid: "sess-1".into(),
            field:        "retries_used".into(),
        }).await;
        handle_api_request(&pool, ApiRequest::BudgetUpdate {
            session_uuid: "sess-1".into(),
            field:        "retries_used".into(),
        }).await;

        let get = handle_api_request(&pool, ApiRequest::BudgetGet {
            session_uuid: "sess-1".into(),
        }).await;
        assert_eq!(get.retries_used,  Some(2));
        assert_eq!(get.inpaints_used, Some(0)); // unchanged
    }

    #[tokio::test]
    async fn test_budget_update_inpaints_increments() {
        let pool = make_pool().await;
        handle_api_request(&pool, ApiRequest::BudgetInit {
            session_uuid: "sess-1".into(), max_retries: 3, max_inpaints: 2,
        }).await;

        handle_api_request(&pool, ApiRequest::BudgetUpdate {
            session_uuid: "sess-1".into(),
            field:        "inpaints_used".into(),
        }).await;

        let get = handle_api_request(&pool, ApiRequest::BudgetGet {
            session_uuid: "sess-1".into(),
        }).await;
        assert_eq!(get.inpaints_used, Some(1));
        assert_eq!(get.retries_used,  Some(0)); // unchanged
    }

    #[tokio::test]
    async fn test_budget_update_unknown_field_returns_error() {
        let pool = make_pool().await;
        handle_api_request(&pool, ApiRequest::BudgetInit {
            session_uuid: "sess-1".into(), max_retries: 3, max_inpaints: 2,
        }).await;

        let resp = handle_api_request(&pool, ApiRequest::BudgetUpdate {
            session_uuid: "sess-1".into(),
            field:        "nonexistent_field".into(),
        }).await;
        assert!(!resp.ok);
    }

    // ── SessionInit ───────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_session_init_inserts_row() {
        let pool = make_pool().await;
        let resp = handle_api_request(&pool, ApiRequest::SessionInit {
            image_uuid:         "img-1".into(),
            session_uuid:       "sess-1".into(),
            sequence_number:    1,
            prompt:             "a tiger".into(),
            workflow_path:      "/workflows/default.json".into(),
            workflow_params:    "{}".into(),
            score_timeout_secs: 60,
        }).await;
        assert!(resp.ok);

        let row = sqlx::query!(
            "SELECT image_uuid, prompt FROM scorer_session WHERE image_uuid = 'img-1'"
        )
        .fetch_optional(&pool).await.unwrap();
        assert!(row.is_some());
        assert_eq!(row.unwrap().prompt.unwrap_or_default(), "a tiger");
    }

    #[tokio::test]
    async fn test_session_init_idempotent() {
        let pool = make_pool().await;
        for _ in 0..3 {
            let resp = handle_api_request(&pool, ApiRequest::SessionInit {
                image_uuid:         "img-1".into(),
                session_uuid:       "sess-1".into(),
                sequence_number:    1,
                prompt:             "a tiger".into(),
                workflow_path:      "/workflows/default.json".into(),
                workflow_params:    "{}".into(),
                score_timeout_secs: 60,
            }).await;
            assert!(resp.ok);
        }
        let count: i32 = sqlx::query_scalar!(
            "SELECT COUNT(*) FROM scorer_session WHERE image_uuid = 'img-1'"
        )
        .fetch_one(&pool).await.unwrap();
        assert_eq!(count, 1);
    }

    // ── recover_prepared_transactions ─────────────────────────────────────────

    #[tokio::test]
    async fn test_recover_no_prepared_is_no_op() {
        let pool = make_pool().await;
        // Insert a committed transaction — should not be touched
        sqlx::query!(
            "INSERT INTO xa_log (xid, state, participants, created_at)
             VALUES ('xid-1', 'committed', '[]', 1000)"
        )
        .execute(&pool).await.unwrap();

        recover_prepared_transactions(&pool).await.unwrap();

        let state: String = sqlx::query_scalar!(
            "SELECT state FROM xa_log WHERE xid = 'xid-1'"
        )
        .fetch_one(&pool).await.unwrap();
        assert_eq!(state, "committed");
    }

    #[tokio::test]
    async fn test_recover_prepared_rolls_back_all() {
        let pool = make_pool().await;
        for xid in &["xid-1", "xid-2"] {
            sqlx::query!(
                "INSERT INTO xa_log (xid, state, participants, created_at)
                 VALUES (?1, 'prepared', '[]', 1000)",
                xid
            )
            .execute(&pool).await.unwrap();
        }

        recover_prepared_transactions(&pool).await.unwrap();

        let states: Vec<String> = sqlx::query_scalar!(
            "SELECT state FROM xa_log WHERE xid IN ('xid-1', 'xid-2') ORDER BY xid"
        )
        .fetch_all(&pool).await.unwrap();
        assert_eq!(states, vec!["rolledback", "rolledback"]);
    }

    // ── XA state machine ──────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_xa_begin_creates_log_entry() {
        let pool = make_pool().await;
        let registry: XaRegistry = Arc::new(Mutex::new(HashMap::new()));
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();

        let row = sqlx::query!("SELECT xid, state FROM xa_log WHERE xid = 'xid-1'")
            .fetch_optional(&pool).await.unwrap();
        assert!(row.is_some());

        let guard = registry.lock().await;
        assert!(guard.contains_key("xid-1"));
        assert_eq!(guard["xid-1"].state, XaState::Active);
    }

    #[tokio::test]
    async fn test_xa_enlist_adds_participant() {
        let pool = make_pool().await;
        let registry: XaRegistry = Arc::new(Mutex::new(HashMap::new()));
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_enlist(&registry, "xid-1", Resource::Artemis).await.unwrap();

        let guard = registry.lock().await;
        assert!(guard["xid-1"].participants.contains(&"Artemis".to_string()));
    }

    #[tokio::test]
    async fn test_xa_prepare_updates_state_to_prepared() {
        let pool = make_pool().await;
        let registry: XaRegistry = Arc::new(Mutex::new(HashMap::new()));
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_prepare(&pool, &registry, "xid-1").await.unwrap();

        let guard = registry.lock().await;
        assert_eq!(guard["xid-1"].state, XaState::Prepared);
    }

    #[tokio::test]
    async fn test_xa_commit_removes_from_registry() {
        let pool = make_pool().await;
        let registry: XaRegistry = Arc::new(Mutex::new(HashMap::new()));
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_prepare(&pool, &registry, "xid-1").await.unwrap();
        xa_commit(&pool, &registry, "xid-1").await.unwrap();

        assert!(!registry.lock().await.contains_key("xid-1"));

        let state: String = sqlx::query_scalar!(
            "SELECT state FROM xa_log WHERE xid = 'xid-1'"
        )
        .fetch_one(&pool).await.unwrap();
        assert_eq!(state, "committed");
    }

    #[tokio::test]
    async fn test_xa_rollback_removes_from_registry() {
        let pool = make_pool().await;
        let registry: XaRegistry = Arc::new(Mutex::new(HashMap::new()));
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_rollback(&pool, &registry, "xid-1").await.unwrap();

        assert!(!registry.lock().await.contains_key("xid-1"));

        let state: String = sqlx::query_scalar!(
            "SELECT state FROM xa_log WHERE xid = 'xid-1'"
        )
        .fetch_one(&pool).await.unwrap();
        assert_eq!(state, "rolledback");
    }

    #[tokio::test]
    async fn test_xa_enlist_unknown_xid_returns_error() {
        let registry: XaRegistry = Arc::new(Mutex::new(HashMap::new()));
        let result = xa_enlist(&registry, "nonexistent", Resource::Sqlite).await;
        assert!(result.is_err());
    }

    // ── JSON round-trip (ApiRequest deserialisation) ──────────────────────────

    #[test]
    fn test_budget_init_request_deserialises() {
        let json = r#"{"op":"BudgetInit","session_uuid":"s1","max_retries":3,"max_inpaints":2}"#;
        let req: ApiRequest = serde_json::from_str(json).unwrap();
        matches!(req, ApiRequest::BudgetInit { .. });
    }

    #[test]
    fn test_budget_get_request_deserialises() {
        let json = r#"{"op":"BudgetGet","session_uuid":"s1"}"#;
        let req: ApiRequest = serde_json::from_str(json).unwrap();
        matches!(req, ApiRequest::BudgetGet { .. });
    }

    #[test]
    fn test_budget_update_request_deserialises() {
        let json = r#"{"op":"BudgetUpdate","session_uuid":"s1","field":"retries_used"}"#;
        let req: ApiRequest = serde_json::from_str(json).unwrap();
        matches!(req, ApiRequest::BudgetUpdate { .. });
    }

    #[test]
    fn test_session_init_request_deserialises() {
        let json = r#"{"op":"SessionInit","image_uuid":"i1","session_uuid":"s1",
                       "sequence_number":1,"prompt":"p","workflow_path":"w",
                       "workflow_params":"{}","score_timeout_secs":60}"#;
        let req: ApiRequest = serde_json::from_str(json).unwrap();
        matches!(req, ApiRequest::SessionInit { .. });
    }

    #[test]
    fn test_api_response_ok_serialises() {
        let resp = ApiResponse::ok();
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"ok\":true"));
        assert!(!json.contains("error"));
    }

    #[test]
    fn test_api_response_err_serialises() {
        let resp = ApiResponse::err("something broke");
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"ok\":false"));
        assert!(json.contains("something broke"));
    }
}
