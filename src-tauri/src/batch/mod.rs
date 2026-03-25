pub mod db;
pub mod ffmpeg;
pub mod scanner;

use crate::batch::db::TaskRow;
use crate::batch::ffmpeg::{EncodeConfig, ProcessResult};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;
use tauri::{AppHandle, Emitter};
use tokio::sync::Semaphore;

/// Configuration received from the frontend.
#[derive(Debug, Clone, Deserialize)]
pub struct BatchConfig {
    pub input_dir: String,
    pub output_dir: String,
    pub input_ext: String,
    pub workers: u32,
    pub encode_mode: String,
    pub shard_index: Option<u32>,
    pub shard_count: Option<u32>,
}

/// Event payloads emitted to the frontend.
#[derive(Clone, Serialize)]
pub struct LogEvent {
    pub message: String,
}

#[derive(Clone, Serialize)]
pub struct ProgressEvent {
    pub processed: u64,
    pub total: u64,
    pub status: String,
    pub file_name: String,
    pub duration_s: f64,
    pub elapsed_s: f64,
}

#[derive(Clone, Serialize)]
pub struct FinishedEvent {
    pub success: bool,
    pub done: u64,
    pub failed: u64,
    pub elapsed_s: f64,
}

/// Shared state for the batch processor.
pub struct BatchState {
    pub running: AtomicBool,
    pub shutdown: AtomicBool,
}

impl BatchState {
    pub fn new() -> Self {
        Self {
            running: AtomicBool::new(false),
            shutdown: AtomicBool::new(false),
        }
    }
}

fn emit_log(app: &AppHandle, msg: impl Into<String>) {
    let _ = app.emit("batch:log", LogEvent { message: msg.into() });
}

/// Run the full batch processing pipeline.
pub async fn run_batch(
    app: AppHandle,
    config: BatchConfig,
    state: Arc<BatchState>,
    resource_dir: Option<PathBuf>,
) {
    if state.running.swap(true, Ordering::SeqCst) {
        emit_log(&app, "[ERROR] 已有批处理任务在运行中");
        return;
    }
    state.shutdown.store(false, Ordering::SeqCst);

    let batch_start = Instant::now();
    let result = run_batch_inner(&app, &config, &state, resource_dir.as_deref(), batch_start).await;

    state.running.store(false, Ordering::SeqCst);
    let elapsed = batch_start.elapsed().as_secs_f64();

    match result {
        Ok((done, failed)) => {
            let _ = app.emit("batch:finished", FinishedEvent {
                success: failed == 0,
                done,
                failed,
                elapsed_s: elapsed,
            });
        }
        Err(e) => {
            emit_log(&app, format!("[ERROR] {e}"));
            let _ = app.emit("batch:finished", FinishedEvent {
                success: false,
                done: 0,
                failed: 0,
                elapsed_s: elapsed,
            });
        }
    }
}

async fn run_batch_inner(
    app: &AppHandle,
    config: &BatchConfig,
    state: &BatchState,
    resource_dir: Option<&Path>,
    batch_start: Instant,
) -> Result<(u64, u64), String> {
    let input_dir = PathBuf::from(&config.input_dir);
    let output_dir = PathBuf::from(&config.output_dir);

    std::fs::create_dir_all(&output_dir).map_err(|e| format!("创建输出目录失败: {e}"))?;

    let db_path = output_dir.join("progress.db");
    let db_str = db_path.to_string_lossy().to_string();

    emit_log(app, format!(
        "批处理开始  mode={}  workers={}",
        config.encode_mode, config.workers
    ));
    emit_log(app, format!("input={}  output={}", config.input_dir, config.output_dir));

    // Find ffmpeg
    let ffmpeg = ffmpeg::find_ffmpeg(resource_dir)
        .map_err(|e| format!("ffmpeg 未找到: {e}"))?;
    emit_log(app, format!("ffmpeg: {}", ffmpeg.display()));

    // Scan files
    let exts = scanner::parse_extensions(&config.input_ext);
    let mut all_files = scanner::scan_files(&input_dir, &exts);
    if all_files.is_empty() {
        return Err(format!("未找到扩展名为 {} 的文件", config.input_ext));
    }
    emit_log(app, format!("扫描到 {} 个文件", all_files.len()));

    // Shard selection
    if let (Some(idx), Some(cnt)) = (config.shard_index, config.shard_count) {
        all_files = scanner::select_shard(&all_files, idx, cnt);
        emit_log(app, format!(
            "分片过滤: index={} count={} -> {} 个文件",
            idx, cnt, all_files.len()
        ));
        if all_files.is_empty() {
            emit_log(app, "当前分片无文件分配");
            return Ok((0, 0));
        }
    }

    // Init DB and register
    db::init_db(&db_str).map_err(|e| format!("初始化数据库失败: {e}"))?;
    let pairs = scanner::build_file_pairs(&all_files, &input_dir, &output_dir);
    let new_count = db::register_files(&db_str, &pairs).map_err(|e| format!("注册任务失败: {e}"))?;
    emit_log(app, format!("新注册任务: {new_count}"));

    // Fetch pending
    let pending = db::fetch_pending(&db_str).map_err(|e| format!("查询待处理任务失败: {e}"))?;
    let total = pending.len() as u64;
    if total == 0 {
        emit_log(app, "所有文件已处理完毕");
        return Ok((0, 0));
    }
    emit_log(app, format!("待处理任务: {total}"));

    // Emit initial progress so frontend knows total count immediately
    let _ = app.emit("batch:progress", ProgressEvent {
        processed: 0,
        total,
        status: "pending".into(),
        file_name: String::new(),
        duration_s: 0.0,
        elapsed_s: 0.0,
    });

    // Build encode config
    let enc_cfg = EncodeConfig {
        ffmpeg_exe: ffmpeg,
        encode_mode: config.encode_mode.clone(),
        ..Default::default()
    };

    // Process with concurrency control + mpsc channel for real-time progress
    let semaphore = Arc::new(Semaphore::new(config.workers as usize));
    let enc_cfg = Arc::new(enc_cfg);
    let db_str_arc = Arc::new(db_str.clone());
    let shutdown = Arc::new(AtomicBool::new(false));

    // Channel for real-time results (no head-of-line blocking)
    let (tx, mut rx) = tokio::sync::mpsc::channel::<(i64, ProcessResult)>(64);

    for task in pending {
        if state.shutdown.load(Ordering::SeqCst) || shutdown.load(Ordering::SeqCst) {
            emit_log(app, "收到停止信号，等待当前任务完成...");
            break;
        }

        let permit = semaphore.clone().acquire_owned().await.unwrap();

        if state.shutdown.load(Ordering::SeqCst) {
            drop(permit);
            emit_log(app, "收到停止信号，等待当前任务完成...");
            break;
        }

        let TaskRow { id, input_path, output_path } = task;
        let enc = enc_cfg.clone();
        let db = db_str_arc.clone();
        let tx = tx.clone();

        let _ = db::mark_processing(&db, id);

        tokio::spawn(async move {
            let result = ffmpeg::process_one(&input_path, &output_path, &enc).await;
            let _ = db::mark_finished(
                &db,
                id,
                &result.status,
                result.error_msg.as_deref(),
                result.duration_s,
            );
            drop(permit);
            let _ = tx.send((id, result)).await;
        });
    }
    drop(tx); // Close sender so rx.recv() returns None when all tasks complete

    // Receive results in real-time (completion order, not spawn order)
    let mut done_count = 0u64;
    let mut fail_count = 0u64;

    while let Some((_id, result)) = rx.recv().await {
        let processed = done_count + fail_count + 1;
        let elapsed = batch_start.elapsed().as_secs_f64();
        let file_name = Path::new(&result.input_path)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();

        if result.status == "done" {
            done_count += 1;
            emit_log(app, format!(
                "[{}/{}] done  {}  time={:.1}s",
                processed, total, file_name,
                result.duration_s.unwrap_or(0.0),
            ));
        } else {
            fail_count += 1;
            emit_log(app, format!(
                "[{}/{}] failed  {}  {}",
                processed, total, file_name,
                result.error_msg.as_deref().unwrap_or("").chars().take(120).collect::<String>(),
            ));
        }

        let _ = app.emit("batch:progress", ProgressEvent {
            processed,
            total,
            status: result.status,
            file_name,
            duration_s: result.duration_s.unwrap_or(0.0),
            elapsed_s: elapsed,
        });
    }

    // Summary
    if let Ok(summary) = db::get_summary(&db_str) {
        let elapsed = batch_start.elapsed().as_secs_f64();
        emit_log(app, "=".repeat(60));
        emit_log(app, format!(
            "任务摘要  耗时 {:.0}s  已处理 {}",
            elapsed, done_count + fail_count
        ));
        emit_log(app, format!("  pending:    {}", summary.pending));
        emit_log(app, format!("  processing: {}", summary.processing));
        emit_log(app, format!("  done:       {}", summary.done));
        emit_log(app, format!("  failed:     {}", summary.failed));
        if !summary.failed_files.is_empty() {
            emit_log(app, format!("失败文件 ({}):", summary.failed_files.len()));
            for f in &summary.failed_files {
                emit_log(app, format!("  {}", f.input_path));
                emit_log(app, format!("    原因: {}", f.error_msg));
            }
        }
        emit_log(app, "=".repeat(60));
    }

    Ok((done_count, fail_count))
}
