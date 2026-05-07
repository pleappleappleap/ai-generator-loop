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
//!    requests to a Unix domain socket for budget and session operations.
//!    Every write operation is wrapped in an XA transaction against SQLite.
//!    Artemis RM enlistment (true 2PC across both RMs) is wired in step 9.
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

use serde::{Deserialize, Serialize};
use sqlx::SqlitePool;
use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
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
    /// Artemis AMQP broker — enlisted as best-effort in step 7; true XA in step 9.
    Artemis,
    /// SQLite WAL — enlisted for all write operations.
    Sqlite,
}

#[derive(Debug)]
struct XaTransaction {
    state: XaState,
    participants: Vec<String>,
}

/// In-memory XA transaction registry shared across all socket connections.
type XaRegistry = Arc<Mutex<HashMap<String, XaTransaction>>>;

/// Generate a unique XA transaction ID with a semantic prefix for debugging.
///
/// Uses nanosecond wall-clock time to ensure uniqueness across rapid repeated
/// calls (e.g. idempotent retries of the same SessionInit).
fn make_xid(prefix: &str, key: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{}:{}:{}", prefix, key, nanos)
}

/// Begin an XA transaction.
///
/// Inserts a `prepared` row into `xa_log` immediately so crash recovery can
/// roll it back if the process dies before `xa_commit`.
async fn xa_begin(
    pool: &SqlitePool,
    registry: &XaRegistry,
    xid: String,
) -> Result<(), Box<dyn std::error::Error>> {
    let now = db::now_unix();
    sqlx::query!(
        "INSERT INTO xa_log (xid, state, participants, created_at) VALUES (?1, 'prepared', '[]', ?2)",
        xid,
        now
    )
    .execute(pool)
    .await?;

    let tx = XaTransaction {
        state: XaState::Active,
        participants: vec![],
    };
    registry.lock().await.insert(xid, tx);
    Ok(())
}

/// Record a resource manager as a participant in the transaction.
async fn xa_enlist(
    registry: &XaRegistry,
    xid: &str,
    resource: Resource,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut guard = registry.lock().await;
    let tx = guard.get_mut(xid).ok_or("unknown xid")?;
    let name = format!("{:?}", resource);
    if !tx.participants.contains(&name) {
        tx.participants.push(name);
    }
    Ok(())
}

/// Phase 1: durably record the participant list, transition to Prepared.
async fn xa_prepare(
    pool: &SqlitePool,
    registry: &XaRegistry,
    xid: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let participants = {
        let mut guard = registry.lock().await;
        let tx = guard.get_mut(xid).ok_or("unknown xid")?;
        tx.state = XaState::Prepared;
        serde_json::to_string(&tx.participants)?
    };

    // fsync-backed write — SqliteSynchronous::Full in open_pool() ensures durability.
    sqlx::query!(
        "UPDATE xa_log SET state = 'prepared', participants = ?1 WHERE xid = ?2",
        participants,
        xid
    )
    .execute(pool)
    .await?;
    Ok(())
}

/// Phase 2 commit: mark Committed in xa_log, remove from in-memory registry.
async fn xa_commit(
    pool: &SqlitePool,
    registry: &XaRegistry,
    xid: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let now = db::now_unix();
    sqlx::query!(
        "UPDATE xa_log SET state = 'committed', resolved_at = ?1 WHERE xid = ?2",
        now,
        xid
    )
    .execute(pool)
    .await?;
    registry.lock().await.remove(xid);
    Ok(())
}

/// Roll back: mark Rolledback in xa_log, remove from in-memory registry.
async fn xa_rollback(
    pool: &SqlitePool,
    registry: &XaRegistry,
    xid: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let now = db::now_unix();
    sqlx::query!(
        "UPDATE xa_log SET state = 'rolledback', resolved_at = ?1 WHERE xid = ?2",
        now,
        xid
    )
    .execute(pool)
    .await?;
    registry.lock().await.remove(xid);
    Ok(())
}

/// Crash recovery: conservatively roll back every prepared-but-unresolved transaction.
///
/// Called on startup before accepting new connections. When Artemis XA support
/// is wired in step 9, this will query each RM for the outcome before deciding.
async fn recover_prepared_transactions(
    pool: &SqlitePool,
) -> Result<(), Box<dyn std::error::Error>> {
    let prepared = sqlx::query!("SELECT xid FROM xa_log WHERE state = 'prepared'")
        .fetch_all(pool)
        .await?;

    if prepared.is_empty() {
        info!("Crash recovery: no prepared transactions found");
        return Ok(());
    }

    warn!(
        "Crash recovery: {} prepared transaction(s) found — rolling back",
        prepared.len()
    );
    let now = db::now_unix();
    for row in prepared {
        sqlx::query!(
            "UPDATE xa_log SET state = 'rolledback', resolved_at = ?1 WHERE xid = ?2",
            now,
            row.xid
        )
        .execute(pool)
        .await?;
        warn!(
            "Crash recovery: rolled back xid={}",
            row.xid.as_deref().unwrap_or("?")
        );
    }
    Ok(())
}

// ── Unix socket API ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
#[serde(tag = "op")]
enum ApiRequest {
    // ── Budget ops (unchanged) ────────────────────────────────────────────────
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
    // ── Session init (extended with workflow/conversation IDs) ────────────────
    SessionInit {
        image_uuid: String,
        session_uuid: String,
        sequence_number: i64,
        prompt: String,
        workflow_path: String,
        workflow_params: String,
        score_timeout_secs: i64,
        workflow_id: Option<String>,
        conversation_id: Option<String>,
    },
    // ── Conversation / workflow hierarchy ─────────────────────────────────────
    ConversationCreate {
        name: Option<String>,
    },
    WorkflowCreate {
        conversation_id: String,
        name: Option<String>,
        workflow_path: String,
        description: Option<String>,
        /// "fresh" (reset budget on resume) | "accumulated" (continue counters)
        budget_policy: Option<String>,
        max_retries: i64,
        max_inpaints: i64,
    },
    /// Begin a new generation run for an existing workflow.
    /// Returns a fresh run_id (= new session_uuid for the operational layer).
    /// Also creates the tactical_budget row for this run.
    WorkflowResume {
        workflow_id: String,
        /// Override the workflow's stored budget_policy for this run.
        budget_policy: Option<String>,
        max_retries: Option<i64>,
        max_inpaints: Option<i64>,
    },
    // ── User feedback ─────────────────────────────────────────────────────────
    FeedbackAdd {
        image_uuid: String,
        workflow_id: String,
        run_id: String,
        rating: String, // "up" | "down"
        comment: Option<String>,
    },
    // ── Chat ──────────────────────────────────────────────────────────────────
    ChatAdd {
        conversation_id: String,
        workflow_id: Option<String>,
        role: String, // "user" | "assistant"
        content: String,
    },
    /// Return recent chat messages + user feedback for a workflow,
    /// used by tactical_llm to enrich its decision context.
    ContextGet {
        conversation_id: String,
        workflow_id: String,
        limit: Option<i64>,
    },
}

#[derive(Serialize)]
struct ApiResponse {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    // BudgetGet / BudgetInit fields
    #[serde(skip_serializing_if = "Option::is_none")]
    retries_used: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    inpaints_used: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_retries: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_inpaints: Option<i64>,
    // New hierarchy fields
    #[serde(skip_serializing_if = "Option::is_none")]
    conversation_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    workflow_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    run_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    workflow_path: Option<String>,
    /// JSON array of context items (chat messages + feedback) for ContextGet.
    #[serde(skip_serializing_if = "Option::is_none")]
    context: Option<serde_json::Value>,
    // ContextGet top-level arrays (mirrored from context for easy access)
    #[serde(skip_serializing_if = "Option::is_none")]
    chat: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    feedback: Option<serde_json::Value>,
}

impl ApiResponse {
    fn ok() -> Self {
        Self {
            ok: true,
            error: None,
            retries_used: None,
            inpaints_used: None,
            max_retries: None,
            max_inpaints: None,
            conversation_id: None,
            workflow_id: None,
            run_id: None,
            workflow_path: None,
            context: None,
            chat: None,
            feedback: None,
        }
    }
    fn err(msg: impl Into<String>) -> Self {
        Self {
            ok: false,
            error: Some(msg.into()),
            retries_used: None,
            inpaints_used: None,
            max_retries: None,
            max_inpaints: None,
            conversation_id: None,
            workflow_id: None,
            run_id: None,
            workflow_path: None,
            context: None,
            chat: None,
            feedback: None,
        }
    }

    fn with_id(mut self, field: &str, value: String) -> Self {
        match field {
            "conversation_id" => self.conversation_id = Some(value),
            "workflow_id"     => self.workflow_id     = Some(value),
            "run_id"          => self.run_id          = Some(value),
            "workflow_path"   => self.workflow_path   = Some(value),
            _ => {}
        }
        self
    }
}

async fn handle_api_request(
    pool: &SqlitePool,
    registry: &XaRegistry,
    req: ApiRequest,
) -> ApiResponse {
    match req {
        ApiRequest::BudgetInit {
            session_uuid,
            max_retries,
            max_inpaints,
        } => {
            let xid = make_xid("bi", &session_uuid);
            if let Err(e) = xa_begin(pool, registry, xid.clone()).await {
                return ApiResponse::err(format!("xa_begin: {e}"));
            }
            xa_enlist(registry, &xid, Resource::Sqlite).await.ok();

            let now = db::now_unix();
            let expires_at = now + 86400; // 24 h session budget lifetime
            let result = sqlx::query!(
                "INSERT OR IGNORE INTO tactical_budget
                 (session_uuid, max_retries, max_inpaints, created_at, expires_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                session_uuid,
                max_retries,
                max_inpaints,
                now,
                expires_at
            )
            .execute(pool)
            .await;

            match result {
                Ok(_) => {
                    if let Err(msg) = xa_prepare(pool, registry, &xid)
                        .await
                        .map_err(|e| e.to_string())
                    {
                        xa_rollback(pool, registry, &xid).await.ok();
                        return ApiResponse::err(format!("xa_prepare: {msg}"));
                    }
                    xa_commit(pool, registry, &xid).await.ok();
                    ApiResponse::ok()
                }
                Err(e) => {
                    xa_rollback(pool, registry, &xid).await.ok();
                    ApiResponse::err(e.to_string())
                }
            }
        }

        ApiRequest::BudgetGet { session_uuid } => {
            // Read-only — no XA needed.
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
                    retries_used: Some(row.retries_used),
                    inpaints_used: Some(row.inpaints_used),
                    max_retries: Some(row.max_retries),
                    max_inpaints: Some(row.max_inpaints),
                    conversation_id: None,
                    workflow_id: None,
                    run_id: None,
                    workflow_path: None,
                    context: None,
                    chat: None,
                    feedback: None,
                },
                Ok(None) => ApiResponse::err("session not found"),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::BudgetUpdate {
            session_uuid,
            field,
        } => {
            let xid = make_xid("bu", &session_uuid);
            if let Err(e) = xa_begin(pool, registry, xid.clone()).await {
                return ApiResponse::err(format!("xa_begin: {e}"));
            }
            xa_enlist(registry, &xid, Resource::Sqlite).await.ok();

            let result = match field.as_str() {
                "retries_used" => sqlx::query!(
                    "UPDATE tactical_budget SET retries_used = retries_used + 1 WHERE session_uuid = ?1",
                    session_uuid
                )
                .execute(pool)
                .await,
                "inpaints_used" => sqlx::query!(
                    "UPDATE tactical_budget SET inpaints_used = inpaints_used + 1 WHERE session_uuid = ?1",
                    session_uuid
                )
                .execute(pool)
                .await,
                _ => {
                    xa_rollback(pool, registry, &xid).await.ok();
                    return ApiResponse::err(format!("unknown field: {}", field));
                }
            };

            match result {
                Ok(_) => {
                    if let Err(msg) = xa_prepare(pool, registry, &xid)
                        .await
                        .map_err(|e| e.to_string())
                    {
                        xa_rollback(pool, registry, &xid).await.ok();
                        return ApiResponse::err(format!("xa_prepare: {msg}"));
                    }
                    xa_commit(pool, registry, &xid).await.ok();
                    ApiResponse::ok()
                }
                Err(e) => {
                    xa_rollback(pool, registry, &xid).await.ok();
                    ApiResponse::err(e.to_string())
                }
            }
        }

        ApiRequest::SessionInit {
            image_uuid,
            session_uuid,
            sequence_number,
            prompt,
            workflow_path,
            workflow_params,
            score_timeout_secs,
            workflow_id,
            conversation_id,
        } => {
            let xid = make_xid("si", &image_uuid);
            if let Err(e) = xa_begin(pool, registry, xid.clone()).await {
                return ApiResponse::err(format!("xa_begin: {e}"));
            }
            xa_enlist(registry, &xid, Resource::Sqlite).await.ok();

            let now = db::now_unix();
            let expires_at = now + score_timeout_secs;
            let result = sqlx::query!(
                "INSERT OR IGNORE INTO scorer_session
                 (image_uuid, session_uuid, sequence_number, prompt, workflow_path,
                  workflow_params, workflow_id, conversation_id, created_at, expires_at)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
                image_uuid,
                session_uuid,
                sequence_number,
                prompt,
                workflow_path,
                workflow_params,
                workflow_id,
                conversation_id,
                now,
                expires_at
            )
            .execute(pool)
            .await;

            match result {
                Ok(_) => {
                    if let Err(msg) = xa_prepare(pool, registry, &xid)
                        .await
                        .map_err(|e| e.to_string())
                    {
                        xa_rollback(pool, registry, &xid).await.ok();
                        return ApiResponse::err(format!("xa_prepare: {msg}"));
                    }
                    xa_commit(pool, registry, &xid).await.ok();
                    ApiResponse::ok()
                }
                Err(e) => {
                    xa_rollback(pool, registry, &xid).await.ok();
                    ApiResponse::err(e.to_string())
                }
            }
        }

        // ── Conversation / workflow hierarchy ─────────────────────────────────

        ApiRequest::ConversationCreate { name } => {
            let conversation_id = uuid::Uuid::new_v4().to_string();
            let now = db::now_unix();
            let name = name.unwrap_or_else(|| "Untitled".into());
            match sqlx::query!(
                "INSERT INTO conversations (conversation_id, name, created_at) VALUES (?1,?2,?3)",
                conversation_id, name, now
            )
            .execute(pool)
            .await
            {
                Ok(_) => ApiResponse::ok().with_id("conversation_id", conversation_id),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::WorkflowCreate {
            conversation_id,
            name,
            workflow_path,
            description,
            budget_policy,
            max_retries,
            max_inpaints,
        } => {
            let workflow_id = uuid::Uuid::new_v4().to_string();
            let now = db::now_unix();
            let name = name.unwrap_or_else(|| "Untitled Workflow".into());
            let policy = budget_policy.unwrap_or_else(|| "fresh".into());
            match sqlx::query!(
                "INSERT INTO workflows
                 (workflow_id, conversation_id, name, workflow_path, description,
                  budget_policy, max_retries, max_inpaints, created_at)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                workflow_id, conversation_id, name, workflow_path, description,
                policy, max_retries, max_inpaints, now
            )
            .execute(pool)
            .await
            {
                Ok(_) => ApiResponse::ok()
                    .with_id("workflow_id", workflow_id)
                    .with_id("conversation_id", conversation_id)
                    .with_id("workflow_path", workflow_path),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::WorkflowResume {
            workflow_id,
            budget_policy,
            max_retries,
            max_inpaints,
        } => {
            // Load stored workflow defaults.
            let wf = match sqlx::query!(
                "SELECT budget_policy, max_retries, max_inpaints, conversation_id, workflow_path
                 FROM workflows WHERE workflow_id = ?1",
                workflow_id
            )
            .fetch_optional(pool)
            .await
            {
                Ok(Some(r)) => r,
                Ok(None) => return ApiResponse::err(format!("workflow not found: {workflow_id}")),
                Err(e) => return ApiResponse::err(e.to_string()),
            };

            let wf_conversation_id = wf.conversation_id.clone();
            let wf_workflow_path   = wf.workflow_path.clone();
            let policy = budget_policy.as_deref().unwrap_or(&wf.budget_policy);
            let retries  = max_retries.unwrap_or(wf.max_retries);
            let inpaints = max_inpaints.unwrap_or(wf.max_inpaints);
            let run_id   = uuid::Uuid::new_v4().to_string();
            let now      = db::now_unix();
            let expires  = now + 86400;

            // For "accumulated" policy, sum previous retries/inpaints used.
            let (prev_retries, prev_inpaints) = if policy == "accumulated" {
                let row = sqlx::query!(
                    "SELECT COALESCE(SUM(retries_used),0) AS r,
                            COALESCE(SUM(inpaints_used),0) AS i
                     FROM tactical_budget WHERE workflow_id = ?1",
                    workflow_id
                )
                .fetch_one(pool)
                .await;
                match row {
                    Ok(r) => (r.r, r.i),
                    Err(_) => (0, 0),
                }
            } else {
                (0, 0)
            };

            let xid = make_xid("wr", &run_id);
            if let Err(e) = xa_begin(pool, registry, xid.clone()).await {
                return ApiResponse::err(format!("xa_begin: {e}"));
            }
            xa_enlist(registry, &xid, Resource::Sqlite).await.ok();

            let result = sqlx::query!(
                "INSERT INTO tactical_budget
                 (session_uuid, workflow_id, retries_used, inpaints_used,
                  max_retries, max_inpaints, created_at, expires_at)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
                run_id, workflow_id,
                prev_retries, prev_inpaints,
                retries, inpaints, now, expires
            )
            .execute(pool)
            .await;

            match result {
                Ok(_) => {
                    if let Err(msg) = xa_prepare(pool, registry, &xid)
                        .await
                        .map_err(|e| e.to_string())
                    {
                        xa_rollback(pool, registry, &xid).await.ok();
                        return ApiResponse::err(format!("xa_prepare: {msg}"));
                    }
                    xa_commit(pool, registry, &xid).await.ok();
                    ApiResponse::ok()
                        .with_id("run_id", run_id)
                        .with_id("conversation_id", wf_conversation_id)
                        .with_id("workflow_path", wf_workflow_path)
                }
                Err(e) => {
                    xa_rollback(pool, registry, &xid).await.ok();
                    ApiResponse::err(e.to_string())
                }
            }
        }

        ApiRequest::FeedbackAdd {
            image_uuid,
            workflow_id,
            run_id,
            rating,
            comment,
        } => {
            let now = db::now_unix();
            match sqlx::query!(
                "INSERT INTO user_feedback
                 (image_uuid, workflow_id, run_id, rating, comment, created_at)
                 VALUES (?1,?2,?3,?4,?5,?6)",
                image_uuid, workflow_id, run_id, rating, comment, now
            )
            .execute(pool)
            .await
            {
                Ok(_) => ApiResponse::ok(),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::ChatAdd {
            conversation_id,
            workflow_id,
            role,
            content,
        } => {
            let now = db::now_unix();
            match sqlx::query!(
                "INSERT INTO chat_messages
                 (conversation_id, workflow_id, role, content, created_at)
                 VALUES (?1,?2,?3,?4,?5)",
                conversation_id, workflow_id, role, content, now
            )
            .execute(pool)
            .await
            {
                Ok(_) => ApiResponse::ok(),
                Err(e) => ApiResponse::err(e.to_string()),
            }
        }

        ApiRequest::ContextGet {
            conversation_id,
            workflow_id,
            limit,
        } => {
            let msg_limit = limit.unwrap_or(20);
            let fb_limit  = limit.unwrap_or(10);

            let messages = sqlx::query!(
                "SELECT role, content, created_at FROM chat_messages
                 WHERE conversation_id = ?1
                 ORDER BY created_at DESC, id DESC LIMIT ?2",
                conversation_id, msg_limit
            )
            .fetch_all(pool)
            .await;

            let feedback = sqlx::query!(
                "SELECT image_uuid, rating, comment, created_at FROM user_feedback
                 WHERE workflow_id = ?1
                 ORDER BY created_at DESC, id DESC LIMIT ?2",
                workflow_id, fb_limit
            )
            .fetch_all(pool)
            .await;

            match (messages, feedback) {
                (Ok(msgs), Ok(fbs)) => {
                    let mut chat: Vec<serde_json::Value> = msgs
                        .into_iter()
                        .map(|m| serde_json::json!({
                            "type": "chat",
                            "role": m.role,
                            "content": m.content,
                            "created_at": m.created_at,
                        }))
                        .collect();
                    chat.reverse(); // chronological order

                    let feedback_items: Vec<serde_json::Value> = fbs
                        .into_iter()
                        .map(|f| serde_json::json!({
                            "type": "feedback",
                            "image_uuid": f.image_uuid,
                            "rating": f.rating,
                            "comment": f.comment,
                            "created_at": f.created_at,
                        }))
                        .collect();

                    let chat_val     = serde_json::Value::Array(chat);
                    let feedback_val = serde_json::Value::Array(feedback_items);
                    let mut resp = ApiResponse::ok();
                    resp.context  = Some(serde_json::json!({
                        "chat":     chat_val.clone(),
                        "feedback": feedback_val.clone(),
                    }));
                    resp.chat     = Some(chat_val);
                    resp.feedback = Some(feedback_val);
                    resp
                }
                (Err(e), _) | (_, Err(e)) => ApiResponse::err(e.to_string()),
            }
        }
    }
}

async fn run_socket_server(
    pool: Arc<SqlitePool>,
    socket_path: &str,
    registry: XaRegistry,
) -> Result<(), Box<dyn std::error::Error>> {
    // Clean up stale socket from a previous run.
    let _ = std::fs::remove_file(socket_path);
    let listener = UnixListener::bind(socket_path)?;
    // Restrict socket to owner-only so only the pipeline user can connect.
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o600))?;
    info!("Coordinator Unix socket listening at {}", socket_path);

    let mut sigterm = tokio::signal::unix::signal(
        tokio::signal::unix::SignalKind::terminate(),
    )?;

    loop {
        let (stream, _) = tokio::select! {
            biased;
            _ = sigterm.recv() => {
                info!("SIGTERM received — coordinator shutting down");
                return Ok(());
            }
            _ = tokio::signal::ctrl_c() => {
                info!("SIGINT received — coordinator shutting down");
                return Ok(());
            }
            result = listener.accept() => result?,
        };
        let pool = pool.clone();
        let registry = registry.clone();
        tokio::spawn(async move {
            let (reader, mut writer) = stream.into_split();
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let response = match serde_json::from_str::<ApiRequest>(&line) {
                    Ok(req) => handle_api_request(&pool, &registry, req).await,
                    Err(e) => ApiResponse::err(format!("parse error: {}", e)),
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
    db::migrate(&pool).await?;
    db::spawn_cleanup_task(pool.clone(), cfg.database.cleanup_interval_secs);

    // Crash recovery before accepting new work.
    recover_prepared_transactions(&pool).await?;

    let socket_path = format!("{}.sock", cfg.database.path);
    let pool = Arc::new(pool);
    let registry: XaRegistry = Arc::new(Mutex::new(HashMap::new()));

    info!("Coordinator starting");
    run_socket_server(pool, &socket_path, registry).await?;
    Ok(())
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use sqlx::sqlite::SqlitePoolOptions;

    async fn make_pool() -> SqlitePool {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .expect("in-memory pool");
        db::init_schema(&pool).await.expect("init_schema");
        pool
    }

    fn make_registry() -> XaRegistry {
        Arc::new(Mutex::new(HashMap::new()))
    }

    // ── BudgetInit ────────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_budget_init_creates_row() {
        let pool = make_pool().await;
        let registry = make_registry();
        let resp = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetInit {
                session_uuid: "sess-1".into(),
                max_retries: 3,
                max_inpaints: 2,
            },
        )
        .await;
        assert!(resp.ok);

        let get = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetGet {
                session_uuid: "sess-1".into(),
            },
        )
        .await;
        assert!(get.ok);
        assert_eq!(get.max_retries, Some(3));
        assert_eq!(get.max_inpaints, Some(2));
        assert_eq!(get.retries_used, Some(0));
        assert_eq!(get.inpaints_used, Some(0));
    }

    #[tokio::test]
    async fn test_budget_init_idempotent() {
        let pool = make_pool().await;
        let registry = make_registry();
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetInit {
                session_uuid: "sess-1".into(),
                max_retries: 3,
                max_inpaints: 2,
            },
        )
        .await;
        let resp = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetInit {
                session_uuid: "sess-1".into(),
                max_retries: 99,
                max_inpaints: 99,
            },
        )
        .await;
        assert!(resp.ok);

        let get = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetGet {
                session_uuid: "sess-1".into(),
            },
        )
        .await;
        assert_eq!(get.max_retries, Some(3)); // original value preserved
    }

    #[tokio::test]
    async fn test_budget_init_commits_xa_log() {
        let pool = make_pool().await;
        let registry = make_registry();
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetInit {
                session_uuid: "sess-1".into(),
                max_retries: 3,
                max_inpaints: 2,
            },
        )
        .await;

        let states: Vec<String> =
            sqlx::query_scalar!("SELECT state FROM xa_log WHERE xid LIKE 'bi:sess-1:%'")
                .fetch_all(&pool)
                .await
                .unwrap();
        assert!(!states.is_empty());
        assert!(states.iter().all(|s| s == "committed"));
    }

    // ── BudgetGet ─────────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_budget_get_missing_session_returns_error() {
        let pool = make_pool().await;
        let registry = make_registry();
        let resp = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetGet {
                session_uuid: "nonexistent".into(),
            },
        )
        .await;
        assert!(!resp.ok);
        assert!(resp.error.is_some());
    }

    #[tokio::test]
    async fn test_budget_get_does_not_write_xa_log() {
        let pool = make_pool().await;
        let registry = make_registry();
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetGet {
                session_uuid: "sess-1".into(),
            },
        )
        .await;

        let count: i32 = sqlx::query_scalar!("SELECT COUNT(*) FROM xa_log")
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(count, 0);
    }

    // ── BudgetUpdate ──────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_budget_update_retries_increments() {
        let pool = make_pool().await;
        let registry = make_registry();
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetInit {
                session_uuid: "sess-1".into(),
                max_retries: 3,
                max_inpaints: 2,
            },
        )
        .await;

        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetUpdate {
                session_uuid: "sess-1".into(),
                field: "retries_used".into(),
            },
        )
        .await;
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetUpdate {
                session_uuid: "sess-1".into(),
                field: "retries_used".into(),
            },
        )
        .await;

        let get = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetGet {
                session_uuid: "sess-1".into(),
            },
        )
        .await;
        assert_eq!(get.retries_used, Some(2));
        assert_eq!(get.inpaints_used, Some(0));
    }

    #[tokio::test]
    async fn test_budget_update_inpaints_increments() {
        let pool = make_pool().await;
        let registry = make_registry();
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetInit {
                session_uuid: "sess-1".into(),
                max_retries: 3,
                max_inpaints: 2,
            },
        )
        .await;

        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetUpdate {
                session_uuid: "sess-1".into(),
                field: "inpaints_used".into(),
            },
        )
        .await;

        let get = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetGet {
                session_uuid: "sess-1".into(),
            },
        )
        .await;
        assert_eq!(get.inpaints_used, Some(1));
        assert_eq!(get.retries_used, Some(0));
    }

    #[tokio::test]
    async fn test_budget_update_unknown_field_returns_error() {
        let pool = make_pool().await;
        let registry = make_registry();
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetInit {
                session_uuid: "sess-1".into(),
                max_retries: 3,
                max_inpaints: 2,
            },
        )
        .await;

        let resp = handle_api_request(
            &pool,
            &registry,
            ApiRequest::BudgetUpdate {
                session_uuid: "sess-1".into(),
                field: "nonexistent_field".into(),
            },
        )
        .await;
        assert!(!resp.ok);
    }

    // ── SessionInit ───────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_session_init_inserts_row() {
        let pool = make_pool().await;
        let registry = make_registry();
        let resp = handle_api_request(
            &pool,
            &registry,
            ApiRequest::SessionInit {
                image_uuid: "img-1".into(),
                session_uuid: "sess-1".into(),
                sequence_number: 1,
                prompt: "a tiger".into(),
                workflow_path: "/workflows/default.json".into(),
                workflow_params: "{}".into(),
                score_timeout_secs: 60,
                workflow_id: None,
                conversation_id: None,
            },
        )
        .await;
        assert!(resp.ok);

        let row = sqlx::query!(
            "SELECT image_uuid, prompt FROM scorer_session WHERE image_uuid = 'img-1'"
        )
        .fetch_optional(&pool)
        .await
        .unwrap();
        assert!(row.is_some());
        assert_eq!(row.unwrap().prompt.unwrap_or_default(), "a tiger");
    }

    #[tokio::test]
    async fn test_session_init_idempotent() {
        let pool = make_pool().await;
        let registry = make_registry();
        for _ in 0..3 {
            let resp = handle_api_request(
                &pool,
                &registry,
                ApiRequest::SessionInit {
                    image_uuid: "img-1".into(),
                    session_uuid: "sess-1".into(),
                    sequence_number: 1,
                    prompt: "a tiger".into(),
                    workflow_path: "/workflows/default.json".into(),
                    workflow_params: "{}".into(),
                    score_timeout_secs: 60,
                    workflow_id: None,
                    conversation_id: None,
                },
            )
            .await;
            assert!(resp.ok);
        }
        let count: i32 =
            sqlx::query_scalar!("SELECT COUNT(*) FROM scorer_session WHERE image_uuid = 'img-1'")
                .fetch_one(&pool)
                .await
                .unwrap();
        assert_eq!(count, 1);
    }

    #[tokio::test]
    async fn test_session_init_commits_xa_log() {
        let pool = make_pool().await;
        let registry = make_registry();
        handle_api_request(
            &pool,
            &registry,
            ApiRequest::SessionInit {
                image_uuid: "img-1".into(),
                session_uuid: "sess-1".into(),
                sequence_number: 1,
                prompt: "a tiger".into(),
                workflow_path: "/workflows/default.json".into(),
                workflow_params: "{}".into(),
                score_timeout_secs: 60,
                workflow_id: None,
                conversation_id: None,
            },
        )
        .await;

        let states: Vec<String> =
            sqlx::query_scalar!("SELECT state FROM xa_log WHERE xid LIKE 'si:img-1:%'")
                .fetch_all(&pool)
                .await
                .unwrap();
        assert!(!states.is_empty());
        assert!(states.iter().all(|s| s == "committed"));
    }

    // ── recover_prepared_transactions ─────────────────────────────────────────

    #[tokio::test]
    async fn test_recover_no_prepared_is_no_op() {
        let pool = make_pool().await;
        sqlx::query!(
            "INSERT INTO xa_log (xid, state, participants, created_at)
             VALUES ('xid-1', 'committed', '[]', 1000)"
        )
        .execute(&pool)
        .await
        .unwrap();

        recover_prepared_transactions(&pool).await.unwrap();

        let state: String = sqlx::query_scalar!("SELECT state FROM xa_log WHERE xid = 'xid-1'")
            .fetch_one(&pool)
            .await
            .unwrap();
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
            .execute(&pool)
            .await
            .unwrap();
        }

        recover_prepared_transactions(&pool).await.unwrap();

        let states: Vec<String> = sqlx::query_scalar!(
            "SELECT state FROM xa_log WHERE xid IN ('xid-1', 'xid-2') ORDER BY xid"
        )
        .fetch_all(&pool)
        .await
        .unwrap();
        assert_eq!(states, vec!["rolledback", "rolledback"]);
    }

    // ── XA state machine ──────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_xa_begin_creates_log_entry() {
        let pool = make_pool().await;
        let registry = make_registry();
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();

        let row = sqlx::query!("SELECT xid, state FROM xa_log WHERE xid = 'xid-1'")
            .fetch_optional(&pool)
            .await
            .unwrap();
        assert!(row.is_some());

        let guard = registry.lock().await;
        assert!(guard.contains_key("xid-1"));
        assert_eq!(guard["xid-1"].state, XaState::Active);
    }

    #[tokio::test]
    async fn test_xa_enlist_adds_participant() {
        let pool = make_pool().await;
        let registry = make_registry();
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_enlist(&registry, "xid-1", Resource::Artemis)
            .await
            .unwrap();

        let guard = registry.lock().await;
        assert!(guard["xid-1"].participants.contains(&"Artemis".to_string()));
    }

    #[tokio::test]
    async fn test_xa_prepare_updates_state_to_prepared() {
        let pool = make_pool().await;
        let registry = make_registry();
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_prepare(&pool, &registry, "xid-1").await.unwrap();

        let guard = registry.lock().await;
        assert_eq!(guard["xid-1"].state, XaState::Prepared);
    }

    #[tokio::test]
    async fn test_xa_commit_removes_from_registry() {
        let pool = make_pool().await;
        let registry = make_registry();
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_prepare(&pool, &registry, "xid-1").await.unwrap();
        xa_commit(&pool, &registry, "xid-1").await.unwrap();

        assert!(!registry.lock().await.contains_key("xid-1"));

        let state: String = sqlx::query_scalar!("SELECT state FROM xa_log WHERE xid = 'xid-1'")
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(state, "committed");
    }

    #[tokio::test]
    async fn test_xa_rollback_removes_from_registry() {
        let pool = make_pool().await;
        let registry = make_registry();
        xa_begin(&pool, &registry, "xid-1".into()).await.unwrap();
        xa_rollback(&pool, &registry, "xid-1").await.unwrap();

        assert!(!registry.lock().await.contains_key("xid-1"));

        let state: String = sqlx::query_scalar!("SELECT state FROM xa_log WHERE xid = 'xid-1'")
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(state, "rolledback");
    }

    #[tokio::test]
    async fn test_xa_enlist_unknown_xid_returns_error() {
        let registry = make_registry();
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

    // ── ConversationCreate ────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_conversation_create_returns_id() {
        let pool = make_pool().await;
        let registry = make_registry();
        let resp = handle_api_request(
            &pool,
            &registry,
            ApiRequest::ConversationCreate { name: Some("Test session".into()) },
        )
        .await;
        assert!(resp.ok);
        assert!(resp.conversation_id.is_some());

        let id = resp.conversation_id.unwrap();
        let row = sqlx::query!("SELECT name FROM conversations WHERE conversation_id = ?1", id)
            .fetch_optional(&pool)
            .await
            .unwrap();
        assert_eq!(row.unwrap().name, "Test session");
    }

    #[tokio::test]
    async fn test_conversation_create_default_name() {
        let pool = make_pool().await;
        let registry = make_registry();
        let resp = handle_api_request(
            &pool,
            &registry,
            ApiRequest::ConversationCreate { name: None },
        )
        .await;
        assert!(resp.ok);
        let id = resp.conversation_id.unwrap();
        let row = sqlx::query!("SELECT name FROM conversations WHERE conversation_id = ?1", id)
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(row.name, "Untitled");
    }

    // ── WorkflowCreate ────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_workflow_create_returns_ids() {
        let pool = make_pool().await;
        let registry = make_registry();

        let conv = handle_api_request(
            &pool, &registry,
            ApiRequest::ConversationCreate { name: None },
        ).await;
        let conv_id = conv.conversation_id.unwrap();

        let resp = handle_api_request(
            &pool, &registry,
            ApiRequest::WorkflowCreate {
                conversation_id: conv_id.clone(),
                name: Some("Portrait workflow".into()),
                workflow_path: "/workflows/portrait.json".into(),
                description: Some("Dark fantasy".into()),
                budget_policy: Some("fresh".into()),
                max_retries: 3,
                max_inpaints: 2,
            },
        ).await;
        assert!(resp.ok);
        assert!(resp.workflow_id.is_some());
        assert_eq!(resp.conversation_id.as_deref(), Some(conv_id.as_str()));
        assert_eq!(resp.workflow_path.as_deref(), Some("/workflows/portrait.json"));
    }

    // ── WorkflowResume ────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_workflow_resume_creates_run_and_budget() {
        let pool = make_pool().await;
        let registry = make_registry();

        let conv = handle_api_request(
            &pool, &registry,
            ApiRequest::ConversationCreate { name: None },
        ).await;
        let conv_id = conv.conversation_id.unwrap();

        let wf = handle_api_request(
            &pool, &registry,
            ApiRequest::WorkflowCreate {
                conversation_id: conv_id,
                name: None,
                workflow_path: "/wf.json".into(),
                description: None,
                budget_policy: None,
                max_retries: 3,
                max_inpaints: 2,
            },
        ).await;
        let wf_id = wf.workflow_id.unwrap();

        let resp = handle_api_request(
            &pool, &registry,
            ApiRequest::WorkflowResume {
                workflow_id: wf_id.clone(),
                budget_policy: None,
                max_retries: None,
                max_inpaints: None,
            },
        ).await;
        assert!(resp.ok);
        assert!(resp.run_id.is_some());
        assert!(resp.conversation_id.is_some());
        assert_eq!(resp.workflow_path.as_deref(), Some("/wf.json"));

        // Budget row should exist for the run.
        let run_id = resp.run_id.unwrap();
        let budget = sqlx::query!(
            "SELECT max_retries, max_inpaints FROM tactical_budget WHERE session_uuid = ?1",
            run_id
        )
        .fetch_optional(&pool)
        .await
        .unwrap();
        assert!(budget.is_some());
        assert_eq!(budget.unwrap().max_retries, 3);
    }

    #[tokio::test]
    async fn test_workflow_resume_missing_workflow_returns_error() {
        let pool = make_pool().await;
        let registry = make_registry();
        let resp = handle_api_request(
            &pool, &registry,
            ApiRequest::WorkflowResume {
                workflow_id: "nonexistent".into(),
                budget_policy: None,
                max_retries: None,
                max_inpaints: None,
            },
        ).await;
        assert!(!resp.ok);
        assert!(resp.error.is_some());
    }

    #[tokio::test]
    async fn test_workflow_resume_accumulated_sums_previous_usage() {
        let pool = make_pool().await;
        let registry = make_registry();

        let conv = handle_api_request(
            &pool, &registry,
            ApiRequest::ConversationCreate { name: None },
        ).await;

        let wf = handle_api_request(
            &pool, &registry,
            ApiRequest::WorkflowCreate {
                conversation_id: conv.conversation_id.unwrap(),
                name: None,
                workflow_path: "/wf.json".into(),
                description: None,
                budget_policy: Some("accumulated".into()),
                max_retries: 10,
                max_inpaints: 5,
            },
        ).await;
        let wf_id = wf.workflow_id.unwrap();

        // First run — spend 2 retries.
        let run1 = handle_api_request(
            &pool, &registry,
            ApiRequest::WorkflowResume {
                workflow_id: wf_id.clone(),
                budget_policy: None,
                max_retries: None,
                max_inpaints: None,
            },
        ).await;
        let run1_id = run1.run_id.unwrap();
        for _ in 0..2 {
            handle_api_request(
                &pool, &registry,
                ApiRequest::BudgetUpdate { session_uuid: run1_id.clone(), field: "retries_used".into() },
            ).await;
        }

        // Second run with accumulated policy — retries_used should start at 2.
        let run2 = handle_api_request(
            &pool, &registry,
            ApiRequest::WorkflowResume {
                workflow_id: wf_id,
                budget_policy: Some("accumulated".into()),
                max_retries: None,
                max_inpaints: None,
            },
        ).await;
        assert!(run2.ok);
        let run2_id = run2.run_id.unwrap();

        let budget = handle_api_request(
            &pool, &registry,
            ApiRequest::BudgetGet { session_uuid: run2_id },
        ).await;
        assert_eq!(budget.retries_used, Some(2));
    }

    // ── FeedbackAdd ───────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_feedback_add_inserts_row() {
        let pool = make_pool().await;
        let registry = make_registry();
        let resp = handle_api_request(
            &pool, &registry,
            ApiRequest::FeedbackAdd {
                image_uuid: "img-1".into(),
                workflow_id: "wf-1".into(),
                run_id: "run-1".into(),
                rating: "up".into(),
                comment: Some("Great lighting".into()),
            },
        ).await;
        assert!(resp.ok);

        let count: i32 = sqlx::query_scalar!(
            "SELECT COUNT(*) FROM user_feedback WHERE image_uuid = 'img-1' AND rating = 'up'"
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        assert_eq!(count, 1);
    }

    // ── ChatAdd / ContextGet ──────────────────────────────────────────────────

    #[tokio::test]
    async fn test_chat_add_and_context_get_roundtrip() {
        let pool = make_pool().await;
        let registry = make_registry();

        handle_api_request(
            &pool, &registry,
            ApiRequest::ChatAdd {
                conversation_id: "conv-1".into(),
                workflow_id: Some("wf-1".into()),
                role: "user".into(),
                content: "Make it more dramatic".into(),
            },
        ).await;
        handle_api_request(
            &pool, &registry,
            ApiRequest::ChatAdd {
                conversation_id: "conv-1".into(),
                workflow_id: Some("wf-1".into()),
                role: "assistant".into(),
                content: "Understood — adding storm clouds".into(),
            },
        ).await;

        handle_api_request(
            &pool, &registry,
            ApiRequest::FeedbackAdd {
                image_uuid: "img-1".into(),
                workflow_id: "wf-1".into(),
                run_id: "run-1".into(),
                rating: "down".into(),
                comment: None,
            },
        ).await;

        let resp = handle_api_request(
            &pool, &registry,
            ApiRequest::ContextGet {
                conversation_id: "conv-1".into(),
                workflow_id: "wf-1".into(),
                limit: Some(10),
            },
        ).await;
        assert!(resp.ok);

        let chat = resp.chat.as_ref().and_then(|v| v.as_array()).unwrap();
        assert_eq!(chat.len(), 2);
        assert_eq!(chat[0]["role"], "user");
        assert_eq!(chat[1]["role"], "assistant");

        let feedback = resp.feedback.as_ref().and_then(|v| v.as_array()).unwrap();
        assert_eq!(feedback.len(), 1);
        assert_eq!(feedback[0]["rating"], "down");
    }
}
