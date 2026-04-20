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
