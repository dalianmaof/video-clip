use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Instant;
use tokio::process::Command;
use walkdir::WalkDir;

/// Search for ffmpeg executable: first on PATH, then in local tool directories.
pub fn find_ffmpeg(resource_dir: Option<&Path>) -> Result<PathBuf, String> {
    let target = if cfg!(windows) { "ffmpeg.exe" } else { "ffmpeg" };

    // Check PATH
    if let Ok(p) = which_ffmpeg() {
        return Ok(p);
    }

    // Check bundled resource dir (Tauri resolves tools/ffmpeg/)
    if let Some(res) = resource_dir {
        if let Some(p) = rglob_find(res.join("tools").join("ffmpeg"), target) {
            return Ok(p);
        }
    }

    // Collect search roots: cwd, parent of cwd (project root in dev mode), exe directory
    let cwd = std::env::current_dir().unwrap_or_default();
    let mut roots = vec![cwd.clone()];
    if let Some(parent) = cwd.parent() {
        roots.push(parent.to_path_buf());
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            roots.push(exe_dir.to_path_buf());
        }
    }

    for root in &roots {
        for sub in &["ffmpeg", "tools/ffmpeg", "tools"] {
            let dir = root.join(sub);
            if dir.exists() {
                if let Some(p) = rglob_find(&dir, target) {
                    return Ok(p);
                }
            }
        }
    }

    Err("ffmpeg not found. Put it on PATH or keep a local copy under tools/ffmpeg/.".into())
}

/// Recursively search a directory for a file by name.
fn rglob_find(dir: impl AsRef<Path>, name: &str) -> Option<PathBuf> {
    WalkDir::new(dir)
        .into_iter()
        .filter_map(|e| e.ok())
        .find(|e| e.file_type().is_file() && e.file_name().to_string_lossy() == name)
        .map(|e| e.into_path())
}

fn which_ffmpeg() -> Result<PathBuf, ()> {
    let names = if cfg!(windows) {
        vec!["ffmpeg.exe", "ffmpeg"]
    } else {
        vec!["ffmpeg"]
    };
    for name in names {
        if let Some(path) = path_lookup(name) {
            return Ok(path);
        }
    }
    Err(())
}

fn path_lookup(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Test NVENC availability by running a quick ffmpeg encode.
pub async fn detect_nvenc(ffmpeg: &Path) -> bool {
    let result = Command::new(ffmpeg)
        .args([
            "-hide_banner",
            "-f", "lavfi",
            "-i", "testsrc=size=256x256:rate=1",
            "-t", "1",
            "-c:v", "h264_nvenc",
            "-f", "null",
            "-",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    matches!(result, Ok(status) if status.success())
}

/// Encoding configuration.
#[derive(Debug, Clone)]
pub struct EncodeConfig {
    pub ffmpeg_exe: PathBuf,
    pub encode_mode: String,
    // source
    pub source_width: u32,
    pub source_height: u32,
    pub crop_bottom_px: u32,
    // output
    pub output_width: u32,
    pub output_height: u32,
    // quality mode
    pub crf: u32,
    pub preset: String,
    // fast mode
    pub nvenc_cq: u32,
    pub nvenc_preset: String,
    // audio
    pub audio_flags: Vec<String>,
    // timeout
    pub timeout_seconds: u64,
}

impl Default for EncodeConfig {
    fn default() -> Self {
        Self {
            ffmpeg_exe: PathBuf::from("ffmpeg"),
            encode_mode: "quality".into(),
            source_width: 1920,
            source_height: 1080,
            crop_bottom_px: 100,
            output_width: 1920,
            output_height: 1080,
            crf: 18,
            preset: "slow".into(),
            nvenc_cq: 18,
            nvenc_preset: "p4".into(),
            audio_flags: vec!["-c:a".into(), "copy".into()],
            timeout_seconds: 7200,
        }
    }
}

/// Build the ffmpeg command arguments for a single file.
pub fn build_ffmpeg_args(input: &str, output: &str, cfg: &EncodeConfig) -> Vec<String> {
    let crop_h = cfg.source_height - cfg.crop_bottom_px;
    let vf = format!(
        "crop={}:{}:0:0,scale={}:{}",
        cfg.source_width, crop_h, cfg.output_width, cfg.output_height
    );

    let video_flags: Vec<String> = match cfg.encode_mode.as_str() {
        "fast" => vec![
            "-c:v".into(), "h264_nvenc".into(),
            "-cq".into(), cfg.nvenc_cq.to_string(),
            "-preset".into(), cfg.nvenc_preset.clone(),
        ],
        "gpu_quality" => vec![
            "-c:v".into(), "h264_nvenc".into(),
            "-cq".into(), "18".into(),
            "-preset".into(), "p7".into(),
            "-rc".into(), "vbr".into(),
            "-bf".into(), "4".into(),
        ],
        _ => vec![
            "-c:v".into(), "libx264".into(),
            "-crf".into(), cfg.crf.to_string(),
            "-preset".into(), cfg.preset.clone(),
        ],
    };

    let mut args = vec![
        "-hide_banner".into(),
        "-loglevel".into(), "error".into(),
        "-i".into(), input.into(),
        "-vf".into(), vf,
    ];
    args.extend(video_flags);
    args.extend(cfg.audio_flags.clone());
    args.extend(["-movflags".into(), "+faststart".into(), "-y".into(), output.into()]);
    args
}

/// Result of processing a single file.
#[derive(Debug)]
pub struct ProcessResult {
    pub input_path: String,
    pub status: String,        // "done" or "failed"
    pub error_msg: Option<String>,
    pub duration_s: Option<f64>,
}

/// Process a single video file. Returns the result.
pub async fn process_one(
    input_path: &str,
    output_path: &str,
    cfg: &EncodeConfig,
) -> ProcessResult {
    // Ensure output directory exists
    if let Some(parent) = Path::new(output_path).parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    let args = build_ffmpeg_args(input_path, output_path, cfg);
    let start = Instant::now();

    let result: Result<std::io::Result<std::process::ExitStatus>, _> = tokio::time::timeout(
        std::time::Duration::from_secs(cfg.timeout_seconds),
        Command::new(&cfg.ffmpeg_exe)
            .args(&args)
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .status(),
    )
    .await;

    let duration = start.elapsed().as_secs_f64();

    match result {
        Ok(Ok(status)) if status.success() => {
            // Verify output file exists and is non-empty
            let out = Path::new(output_path);
            if !out.exists() || out.metadata().map(|m| m.len() == 0).unwrap_or(true) {
                let _ = std::fs::remove_file(output_path);
                ProcessResult {
                    input_path: input_path.into(),
                    status: "failed".into(),
                    error_msg: Some("output file missing or empty after ffmpeg exit 0".into()),
                    duration_s: Some(duration),
                }
            } else {
                ProcessResult {
                    input_path: input_path.into(),
                    status: "done".into(),
                    error_msg: None,
                    duration_s: Some(duration),
                }
            }
        }
        Ok(Ok(_status)) => {
            let _ = std::fs::remove_file(output_path);
            ProcessResult {
                input_path: input_path.into(),
                status: "failed".into(),
                error_msg: Some("ffmpeg exited with non-zero status".into()),
                duration_s: Some(duration),
            }
        }
        Ok(Err(e)) => {
            let _ = std::fs::remove_file(output_path);
            ProcessResult {
                input_path: input_path.into(),
                status: "failed".into(),
                error_msg: Some(format!("failed to run ffmpeg: {e}")),
                duration_s: Some(duration),
            }
        }
        Err(_) => {
            let _ = std::fs::remove_file(output_path);
            ProcessResult {
                input_path: input_path.into(),
                status: "failed".into(),
                error_msg: Some(format!("timeout > {}s", cfg.timeout_seconds)),
                duration_s: None,
            }
        }
    }
}
