use crate::batch::{self, BatchConfig, BatchState};
use crate::batch::db::StatusSummary;
use serde::Deserialize;
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tauri::{AppHandle, Manager, State};

#[tauri::command]
pub async fn detect_nvenc(app: AppHandle) -> Result<bool, String> {
    let resource_dir = app.path().resource_dir().ok();
    let ffmpeg = batch::ffmpeg::find_ffmpeg(resource_dir.as_deref())
        .map_err(|e| format!("ffmpeg not found: {e}"))?;
    Ok(batch::ffmpeg::detect_nvenc(&ffmpeg).await)
}

#[tauri::command]
pub async fn start_batch(
    app: AppHandle,
    state: State<'_, Arc<BatchState>>,
    config: BatchConfig,
) -> Result<(), String> {
    let state = state.inner().clone();
    let resource_dir = app.path().resource_dir().ok();

    tokio::spawn(async move {
        batch::run_batch(app, config, state, resource_dir).await;
    });

    Ok(())
}

#[tauri::command]
pub async fn stop_batch(state: State<'_, Arc<BatchState>>) -> Result<(), String> {
    state.shutdown.store(true, Ordering::SeqCst);
    Ok(())
}

#[derive(Deserialize)]
pub struct StatusQuery {
    pub output_dir: String,
}

#[tauri::command]
pub async fn query_status(query: StatusQuery) -> Result<StatusSummary, String> {
    let db_path = PathBuf::from(&query.output_dir).join("progress.db");
    let db_str = db_path.to_string_lossy().to_string();

    if !batch::db::db_exists(&db_str) {
        return Err("数据库文件不存在，请先运行一次处理任务".into());
    }

    batch::db::get_summary(&db_str).map_err(|e| format!("查询失败: {e}"))
}
