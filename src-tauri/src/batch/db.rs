use rusqlite::{params, Connection, Result as SqlResult};
use serde::Serialize;
use std::path::Path;

/// Open (or create) the SQLite database at `db_path` and ensure the tasks table exists.
pub fn init_db(db_path: &str) -> SqlResult<()> {
    let conn = Connection::open(db_path)?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            input_path  TEXT UNIQUE NOT NULL,
            output_path TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            error_msg   TEXT,
            started_at  TEXT,
            finished_at TEXT,
            duration_s  REAL
        )",
    )?;
    Ok(())
}

/// Register input files into the database. Returns the number of newly inserted rows.
pub fn register_files(
    db_path: &str,
    input_files: &[(String, String)], // (input_path, output_path)
) -> SqlResult<usize> {
    let conn = Connection::open(db_path)?;
    let mut count = 0usize;
    let mut stmt = conn.prepare(
        "INSERT OR IGNORE INTO tasks (input_path, output_path, status) VALUES (?1, ?2, 'pending')",
    )?;
    for (inp, out) in input_files {
        count += stmt.execute(params![inp, out])?;
    }
    Ok(count)
}

#[derive(Debug, Clone)]
pub struct TaskRow {
    pub id: i64,
    pub input_path: String,
    pub output_path: String,
}

/// Reset stale 'processing' tasks (>2 hours) and return all pending tasks.
pub fn fetch_pending(db_path: &str) -> SqlResult<Vec<TaskRow>> {
    let conn = Connection::open(db_path)?;
    conn.execute(
        "UPDATE tasks SET status='pending' WHERE status='processing' AND started_at < datetime('now', '-2 hours')",
        [],
    )?;
    let mut stmt = conn.prepare(
        "SELECT id, input_path, output_path FROM tasks WHERE status='pending' ORDER BY id",
    )?;
    let rows = stmt
        .query_map([], |row| {
            Ok(TaskRow {
                id: row.get(0)?,
                input_path: row.get(1)?,
                output_path: row.get(2)?,
            })
        })?
        .collect::<SqlResult<Vec<_>>>()?;
    Ok(rows)
}

/// Mark a task as 'processing' (sets started_at).
pub fn mark_processing(db_path: &str, task_id: i64) -> SqlResult<()> {
    let conn = Connection::open(db_path)?;
    conn.execute(
        "UPDATE tasks SET status='processing', started_at=datetime('now','localtime') WHERE id=?1",
        params![task_id],
    )?;
    Ok(())
}

/// Mark a task as 'done' or 'failed' (sets finished_at, error_msg, duration_s).
pub fn mark_finished(
    db_path: &str,
    task_id: i64,
    status: &str,
    error_msg: Option<&str>,
    duration_s: Option<f64>,
) -> SqlResult<()> {
    let conn = Connection::open(db_path)?;
    conn.execute(
        "UPDATE tasks SET status=?1, finished_at=datetime('now','localtime'), error_msg=?2, duration_s=?3 WHERE id=?4",
        params![status, error_msg, duration_s, task_id],
    )?;
    Ok(())
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusSummary {
    pub pending: u64,
    pub processing: u64,
    pub done: u64,
    pub failed: u64,
    pub total_done_seconds: f64,
    pub failed_files: Vec<FailedFile>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FailedFile {
    pub input_path: String,
    pub error_msg: String,
}

/// Query a summary of the current database state.
pub fn get_summary(db_path: &str) -> SqlResult<StatusSummary> {
    let conn = Connection::open(db_path)?;

    let mut pending = 0u64;
    let mut processing = 0u64;
    let mut done = 0u64;
    let mut failed = 0u64;

    let mut stmt = conn.prepare("SELECT status, COUNT(*) FROM tasks GROUP BY status")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, u64>(1)?))
    })?;
    for row in rows {
        let (status, cnt) = row?;
        match status.as_str() {
            "pending" => pending = cnt,
            "processing" => processing = cnt,
            "done" => done = cnt,
            "failed" => failed = cnt,
            _ => {}
        }
    }

    let total_done_seconds: f64 = conn.query_row(
        "SELECT COALESCE(SUM(duration_s), 0) FROM tasks WHERE status='done'",
        [],
        |row| row.get(0),
    )?;

    let mut stmt2 =
        conn.prepare("SELECT input_path, COALESCE(error_msg,'') FROM tasks WHERE status='failed' ORDER BY id")?;
    let failed_files = stmt2
        .query_map([], |row| {
            Ok(FailedFile {
                input_path: row.get(0)?,
                error_msg: row.get(1)?,
            })
        })?
        .collect::<SqlResult<Vec<_>>>()?;

    Ok(StatusSummary {
        pending,
        processing,
        done,
        failed,
        total_done_seconds,
        failed_files,
    })
}

/// Check if the database file exists.
pub fn db_exists(db_path: &str) -> bool {
    Path::new(db_path).exists()
}
