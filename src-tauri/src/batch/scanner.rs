use md5::{Digest, Md5};
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

/// Recursively scan `input_dir` for files matching any of the given extensions.
/// Extensions should include the dot, e.g. `[".ts", ".mp4"]`.
pub fn scan_files(input_dir: &Path, extensions: &[String]) -> Vec<PathBuf> {
    let exts_lower: Vec<String> = extensions.iter().map(|e| e.to_lowercase()).collect();

    let mut files: Vec<PathBuf> = WalkDir::new(input_dir)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| {
            if let Some(ext) = e.path().extension() {
                let dot_ext = format!(".{}", ext.to_string_lossy().to_lowercase());
                exts_lower.iter().any(|x| x == &dot_ext)
            } else {
                false
            }
        })
        .map(|e| e.into_path())
        .collect();

    files.sort();
    files.dedup();
    files
}

/// Select a shard of files using MD5 hash-based bucketing.
/// Returns only files where `md5(normalized_path) % shard_count == shard_index`.
pub fn select_shard(files: &[PathBuf], shard_index: u32, shard_count: u32) -> Vec<PathBuf> {
    files
        .iter()
        .filter(|path| {
            let key = path
                .to_string_lossy()
                .replace('\\', "/")
                .to_lowercase();
            let mut hasher = Md5::new();
            hasher.update(key.as_bytes());
            let digest = hasher.finalize();
            // Take first 4 bytes as u32
            let bucket = u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]])
                % shard_count;
            bucket == shard_index
        })
        .cloned()
        .collect()
}

/// Parse a comma-separated extension string like ".ts,.mp4,.mkv" into a Vec.
pub fn parse_extensions(ext_str: &str) -> Vec<String> {
    ext_str
        .split(',')
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Build (input_path, output_path) pairs for registration.
pub fn build_file_pairs(
    files: &[PathBuf],
    input_dir: &Path,
    output_dir: &Path,
) -> Vec<(String, String)> {
    files
        .iter()
        .filter_map(|f| {
            let rel = f.strip_prefix(input_dir).ok()?;
            let out = output_dir.join(rel).with_extension("mp4");
            Some((f.to_string_lossy().into_owned(), out.to_string_lossy().into_owned()))
        })
        .collect()
}
