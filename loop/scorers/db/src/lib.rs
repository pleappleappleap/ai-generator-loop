//! Shared SQLite connection setup, schema initialisation, and periodic cleanup.
//!
//! Used by the `coordinator` and `lancedb_manager` crates. Each process opens
//! its own pool with `max_connections(1)` — writers to distinct tables contend
//! only at the commit window (microseconds). `busy_timeout` gives block-not-fail
//! semantics under the rare case of same-table contention.
//!
//! # Usage
//!
//! ```no_run
//! use db::{open_pool, init_schema, spawn_cleanup_task};
//!
//! #[tokio::main]
//! async fn main() -> Result<(), Box<dyn std::error::Error>> {
//!     let pool = open_pool("/path/to/pipeline.db", 5000).await?;
//!     init_schema(&pool).await?;
//!     spawn_cleanup_task(pool.clone(), 300);
//!     Ok(())
//! }
//! ```

use sqlx::sqlite::{
    SqliteConnectOptions, SqliteJournalMode, SqlitePool, SqlitePoolOptions, SqliteSynchronous,
};
use std::time::Duration;
use tracing::info;

/// Open a WAL-mode SQLite pool with a single writer connection.
///
/// `busy_timeout_ms` controls how long a blocked write waits before returning
/// an error. 5000 ms is the recommended default.
pub async fn open_pool(path: &str, busy_timeout_ms: u64) -> Result<SqlitePool, sqlx::Error> {
    let options = SqliteConnectOptions::new()
        .filename(path)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Full)
        .busy_timeout(Duration::from_millis(busy_timeout_ms))
        .create_if_missing(true);

    SqlitePoolOptions::new()
        .max_connections(1)
        .connect_with(options)
        .await
}

/// Apply the schema to the database.
///
/// Reads `schema.sql` embedded at compile time and executes it. All
/// `CREATE TABLE IF NOT EXISTS` statements are idempotent — safe to call
/// on every startup.
pub async fn init_schema(pool: &SqlitePool) -> Result<(), sqlx::Error> {
    // schema.sql is embedded at compile time relative to the crate root.
    let schema = include_str!("../schema.sql");
    sqlx::raw_sql(schema).execute(pool).await?;
    info!("Database schema initialised");
    Ok(())
}

/// Spawn a background task that deletes expired rows on a fixed interval.
///
/// Deletes from `scorer_session` and `tactical_budget` where `expires_at`
/// is in the past. Runs every `interval_secs` seconds. The task is detached
/// and runs until the process exits.
pub fn spawn_cleanup_task(pool: SqlitePool, interval_secs: u64) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(interval_secs));
        loop {
            interval.tick().await;
            let now = now_unix();
            let r1 = sqlx::query!("DELETE FROM scorer_session  WHERE expires_at < ?1", now)
                .execute(&pool)
                .await;
            let r2 = sqlx::query!("DELETE FROM tactical_budget WHERE expires_at < ?1", now)
                .execute(&pool)
                .await;
            match (r1, r2) {
                (Ok(_), Ok(_)) => info!("Cleanup: expired rows deleted"),
                (Err(e), _) | (_, Err(e)) => tracing::warn!("Cleanup error: {}", e),
            }
        }
    });
}

/// Apply incremental schema migrations to databases that predate the current
/// schema. On a fresh install these are no-ops (columns already exist in
/// `schema.sql`); on an existing database they add missing columns.
///
/// SQLite has no `ADD COLUMN IF NOT EXISTS`, so each `ALTER TABLE` is
/// attempted and "duplicate column" errors are silently ignored. Any other
/// error is propagated.
///
/// Call once after `init_schema` on every startup.
pub async fn migrate(pool: &SqlitePool) -> Result<(), sqlx::Error> {
    let additions: &[&str] = &[
        // Added when conversation/workflow hierarchy was introduced.
        "ALTER TABLE scorer_session  ADD COLUMN workflow_id     TEXT",
        "ALTER TABLE scorer_session  ADD COLUMN conversation_id TEXT",
        "ALTER TABLE tactical_budget ADD COLUMN workflow_id     TEXT",
        "ALTER TABLE tactical_budget ADD COLUMN conversation_id TEXT",
    ];
    for stmt in additions {
        if let Err(e) = sqlx::raw_sql(stmt).execute(pool).await {
            if !e.to_string().contains("duplicate column") {
                return Err(e);
            }
        }
    }
    Ok(())
}

/// Current Unix timestamp in seconds.
pub fn now_unix() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}
