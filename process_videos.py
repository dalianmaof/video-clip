#!/usr/bin/env python3
"""
Batch TS -> MP4 processor.

Features:
  - crop bottom subtitles
  - scale to output resolution
  - quality mode: libx264 + CRF 18 + slow
  - fast mode: h264_nvenc for quick validation
  - SQLite task tracking for resume / skip-done behavior
  - --status summary
  - --dry-run task registration only
  - --limit N for quick validation runs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from multiprocessing import Pool, freeze_support
from pathlib import Path


CONFIG = {
    "input_dir": "/path/to/input",
    "output_dir": "/path/to/output",
    "db_path": "",

    "input_ext": ".ts,.mp4,.mkv,.avi,.mov",

    # Subtitle strip confirmed at Y=983~1029, with safe margin at Y=980.
    # Final filter chain: crop=1920:980:0:0,scale=1920:1080
    "crop_bottom_px": 100,
    "source_width": 1920,
    "source_height": 1080,

    "output_width": 1920,
    "output_height": 1080,

    # quality = final production run, fast = NVENC validation
    "encode_mode": "quality",

    # quality mode
    "crf": 18,
    "preset": "slow",

    # fast mode
    "nvenc_cq": 18,
    "nvenc_preset": "p4",

    # audio copy for AAC input
    "audio_flags": ["-c:a", "copy"],

    # concurrency
    "workers": 4,

    # per-file timeout
    "timeout_seconds": 7200,

    # optional quick validation limit
    "limit": None,

    # optional sharding for multi-machine deployment
    "shard_index": None,
    "shard_count": None,
}


def load_external_config() -> dict:
    """Load config.json next to the script/exe if present."""
    candidates = [
        Path(sys.argv[0]).resolve().with_name("config.json"),
        Path(__file__).resolve().with_name("config.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                with candidate.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if "audio_flags" in data and not isinstance(data["audio_flags"], list):
                        raise SystemExit(
                            f"config.json: 'audio_flags' must be a JSON array, got {type(data['audio_flags']).__name__}"
                        )
                    return data
            except SystemExit:
                raise
            except Exception as exc:
                raise SystemExit(f"Failed to read config file {candidate}: {exc}") from exc
    return {}


def find_ffmpeg_exe() -> str:
    """Prefer PATH, then bundled local copies under ./tools or ./ffmpeg."""
    for name in ("ffmpeg", "ffmpeg.exe"):
        found = shutil.which(name)
        if found:
            return found

    target_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    search_roots = [
        Path.cwd() / "ffmpeg",
        Path.cwd() / "tools" / "ffmpeg",
        Path.cwd() / "tools",
    ]
    for root in search_roots:
        if root.exists():
            matches = list(root.rglob(target_name))
            if matches:
                return str(matches[0])

    raise FileNotFoundError(
        "ffmpeg not found. Put it on PATH or keep a local copy under ./tools/ffmpeg."
    )


def setup_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "process.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("batch")


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                input_path TEXT UNIQUE NOT NULL,
                output_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_msg TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_s REAL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def register_files(db_path: str, input_files: list[Path], output_dir: Path, input_dir: Path) -> int:
    new_count = 0
    with get_db(db_path) as conn:
        for f in input_files:
            rel = f.relative_to(input_dir)
            out = (output_dir / rel).with_suffix(".mp4")
            cur = conn.execute(
                "INSERT OR IGNORE INTO tasks (input_path, output_path, status) VALUES (?, ?, 'pending')",
                (str(f), str(out)),
            )
            new_count += cur.rowcount
    return new_count


def select_shard_files(input_files: list[Path], shard_index: int | None, shard_count: int | None) -> list[Path]:
    if shard_index is None and shard_count is None:
        return input_files
    if shard_index is None or shard_count is None:
        raise ValueError("Both shard_index and shard_count must be set together")
    if shard_count <= 0:
        raise ValueError("shard_count must be greater than 0")
    if not (0 <= shard_index < shard_count):
        raise ValueError("shard_index must be in range [0, shard_count)")

    selected: list[Path] = []
    for path in input_files:
        key = str(path).replace("\\", "/").lower().encode("utf-8")
        digest = hashlib.md5(key).hexdigest()
        bucket = int(digest[:8], 16) % shard_count
        if bucket == shard_index:
            selected.append(path)
    return selected


def fetch_pending(db_path: str) -> list[dict]:
    with get_db(db_path) as conn:
        # Only reset tasks that have been 'processing' for over 2 hours (stale from a crashed run).
        # This avoids clobbering tasks legitimately in-progress on another machine.
        conn.execute(
            "UPDATE tasks SET status='pending' WHERE status='processing'"
            " AND started_at < datetime('now', '-2 hours')"
        )
        rows = conn.execute(
            "SELECT id, input_path, output_path FROM tasks WHERE status='pending' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_status(
    db_path: str,
    task_id: int,
    status: str,
    error_msg: str | None = None,
    duration_s: float | None = None,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_db(db_path) as conn:
        if status == "processing":
            conn.execute(
                "UPDATE tasks SET status=?, started_at=? WHERE id=?",
                (status, now, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status=?, finished_at=?, error_msg=?, duration_s=? WHERE id=?",
                (status, now, error_msg, duration_s, task_id),
            )


def build_ffmpeg_cmd(input_path: str, output_path: str, cfg: dict) -> list[str]:
    sw = cfg["source_width"]
    sh = cfg["source_height"]
    crop_bottom_px = cfg["crop_bottom_px"]
    ow = cfg["output_width"]
    oh = cfg["output_height"]
    crop_h = sh - crop_bottom_px
    vf = f"crop={sw}:{crop_h}:0:0,scale={ow}:{oh}"

    if cfg["encode_mode"] == "fast":
        # 低画质：GPU 极速
        video_flags = [
            "-c:v", "h264_nvenc",
            "-cq", str(cfg["nvenc_cq"]),
            "-preset", cfg["nvenc_preset"],
        ]
    elif cfg["encode_mode"] == "gpu_quality":
        # 中画质：GPU 高质量
        video_flags = [
            "-c:v", "h264_nvenc",
            "-cq", "18",
            "-preset", "p7",
            "-rc", "vbr",
            "-bf", "4",
        ]
    else:
        # 高画质：CPU libx264
        video_flags = [
            "-c:v", "libx264",
            "-crf", str(cfg["crf"]),
            "-preset", cfg["preset"],
        ]

    return [
        cfg["ffmpeg_exe"],
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-vf",
        vf,
        *video_flags,
        *cfg["audio_flags"],
        "-movflags",
        "+faststart",
        "-y",
        output_path,
    ]


def process_one(task: dict) -> dict:
    task_id = task["id"]
    input_path = task["input_path"]
    output_path = task["output_path"]
    db_path = task["db_path"]
    cfg = task["cfg"]

    t0 = time.monotonic()
    mark_status(db_path, task_id, "processing")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_cmd(input_path, output_path, cfg)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=cfg["timeout_seconds"],
        )
        duration = time.monotonic() - t0
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[-500:]
            Path(output_path).unlink(missing_ok=True)
            mark_status(db_path, task_id, "failed", error_msg=err, duration_s=duration)
            return {"id": task_id, "input_path": input_path, "status": "failed", "error_msg": err, "duration_s": duration}

        out_file = Path(output_path)
        if not out_file.exists() or out_file.stat().st_size == 0:
            err = "output file missing or empty after ffmpeg exit 0"
            out_file.unlink(missing_ok=True)
            mark_status(db_path, task_id, "failed", error_msg=err, duration_s=duration)
            return {"id": task_id, "input_path": input_path, "status": "failed", "error_msg": err, "duration_s": duration}

        mark_status(db_path, task_id, "done", duration_s=duration)
        return {"id": task_id, "input_path": input_path, "status": "done", "duration_s": duration}

    except subprocess.TimeoutExpired:
        err = f"timeout > {cfg['timeout_seconds']}s"
        Path(output_path).unlink(missing_ok=True)
        mark_status(db_path, task_id, "failed", error_msg=err)
        return {"id": task_id, "input_path": input_path, "status": "failed", "error_msg": err, "duration_s": None}

    except Exception as exc:
        err = str(exc)
        mark_status(db_path, task_id, "failed", error_msg=err)
        return {"id": task_id, "input_path": input_path, "status": "failed", "error_msg": err, "duration_s": None}


def format_eta(elapsed_s: float, done: int, total: int) -> str:
    if done == 0:
        return "calculating"
    remaining = (elapsed_s / done) * (total - done)
    return str(timedelta(seconds=int(remaining)))


def print_summary(db_path: str, log: logging.Logger) -> None:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status ORDER BY status"
        ).fetchall()
        total_done_s = conn.execute(
            "SELECT COALESCE(SUM(duration_s), 0) FROM tasks WHERE status='done'"
        ).fetchone()[0]
        failed_rows = conn.execute(
            "SELECT input_path, error_msg FROM tasks WHERE status='failed' ORDER BY id"
        ).fetchall()

    log.info("=" * 60)
    log.info("Task Summary")
    for r in rows:
        log.info("  %-12s: %s", r["status"], r["cnt"])
    log.info("  total done time: %s", timedelta(seconds=int(total_done_s)))
    if failed_rows:
        log.warning("Failed files (%d):", len(failed_rows))
        for row in failed_rows:
            log.warning("  %s", row["input_path"])
            log.warning("    reason: %s", row["error_msg"])
    log.info("=" * 60)


def run_batch(cfg: dict) -> None:
    input_dir = Path(cfg["input_dir"])
    output_dir = Path(cfg["output_dir"])
    db_path = cfg["db_path"]

    output_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(output_dir)

    log.info("=" * 60)
    log.info("Batch start  mode=%s  workers=%s", cfg["encode_mode"], cfg["workers"])
    log.info("input=%s  output=%s", input_dir, output_dir)
    log.info(
        "crop bottom=%spx  output=%sx%s",
        cfg["crop_bottom_px"],
        cfg["output_width"],
        cfg["output_height"],
    )

    exts = [e.strip().lower() for e in cfg["input_ext"].split(",") if e.strip()]
    all_files: list[Path] = []
    for ext in exts:
        all_files.extend(input_dir.rglob(f"*{ext}"))
    all_files = sorted(set(all_files))
    if not all_files:
        log.error("No files with extensions %s found under %s", cfg["input_ext"], input_dir)
        raise SystemExit(1)
    log.info("scanned %d files", len(all_files))

    all_files = select_shard_files(all_files, cfg.get("shard_index"), cfg.get("shard_count"))
    if cfg.get("shard_count") is not None:
        log.info(
            "shard filter: index=%s count=%s -> %d files",
            cfg["shard_index"],
            cfg["shard_count"],
            len(all_files),
        )
        if not all_files:
            log.info("no files assigned to this shard")
            return

    init_db(db_path)
    new_count = register_files(db_path, all_files, output_dir, input_dir)
    log.info("new tasks registered: %d", new_count)

    pending = fetch_pending(db_path)
    if cfg.get("limit") is not None:
        pending = pending[: int(cfg["limit"])]
    total = len(pending)
    if total == 0:
        log.info("all files already processed")
        print_summary(db_path, log)
        return

    log.info("pending tasks: %d", total)
    for t in pending:
        t["db_path"] = db_path
        t["cfg"] = cfg

    import threading
    shutdown_flag = threading.Event()

    def handle_signal(sig, frame):
        log.warning("received signal %s, will stop after current task", sig)
        shutdown_flag.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    done_count = 0
    fail_count = 0
    start_time = time.monotonic()

    with Pool(processes=cfg["workers"]) as pool:
        for result in pool.imap_unordered(process_one, pending, chunksize=1):
            if shutdown_flag.is_set():
                pool.terminate()
                break

            processed = done_count + fail_count + 1
            elapsed = time.monotonic() - start_time

            if result["status"] == "done":
                done_count += 1
                log.info(
                    "[%d/%d] done  time=%.1fs  ETA=%s",
                    processed,
                    total,
                    result["duration_s"],
                    format_eta(elapsed, processed, total),
                )
            else:
                fail_count += 1
                log.error(
                    "[%d/%d] failed  %s  %s",
                    processed,
                    total,
                    Path(result.get("input_path", "?")).name,
                    (result.get("error_msg") or "")[:120],
                )

    elapsed_total = time.monotonic() - start_time
    log.info(
        "batch finished  elapsed=%s  done=%d  failed=%d",
        timedelta(seconds=int(elapsed_total)),
        done_count,
        fail_count,
    )
    print_summary(db_path, log)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch TS -> MP4 processor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage examples:
  # Production run: quality first
  python process_videos.py --input /data/ts --output /data/mp4

  # Fast validation: NVENC, process only a few files
  python process_videos.py --input /data/ts --output /data/mp4_test --mode fast --limit 10

  # 10-machine split, each machine takes one shard
  python process_videos.py --input /data/ts --output /data/mp4_0 --shard-index 0 --shard-count 10

  # Check progress
  python process_videos.py --input /data/ts --output /data/mp4 --status

  # Dry run only
  python process_videos.py --input /data/ts --output /data/mp4 --dry-run
        """,
    )
    parser.add_argument("--input", help="input directory (override CONFIG)")
    parser.add_argument("--output", help="output directory (override CONFIG)")
    parser.add_argument("--db", help="database path (default: output_dir/progress.db)")
    parser.add_argument("--workers", type=int, help="number of worker processes")
    parser.add_argument(
        "--mode",
        choices=["quality", "fast", "gpu_quality"],
        help="quality=libx264 CRF18 / fast=NVENC",
    )
    parser.add_argument("--limit", type=int, help="process only the first N pending files")
    parser.add_argument("--shard-index", type=int, help="0-based shard index for multi-machine splitting")
    parser.add_argument("--shard-count", type=int, help="total shard count for multi-machine splitting")
    parser.add_argument("--ext", help="comma-separated input extensions, e.g. .ts,.mp4 (default from config)")
    parser.add_argument("--dry-run", action="store_true", help="register tasks only")
    parser.add_argument("--status", action="store_true", help="print progress summary and exit")
    args = parser.parse_args()

    cfg = dict(CONFIG)
    cfg.update(load_external_config())
    if args.input:
        cfg["input_dir"] = args.input
    if args.output:
        cfg["output_dir"] = args.output
    if args.workers is not None:
        cfg["workers"] = args.workers
    if args.mode:
        cfg["encode_mode"] = args.mode
    if args.limit is not None:
        cfg["limit"] = max(0, args.limit)
    elif cfg["encode_mode"] == "fast" and cfg.get("limit") is None:
        cfg["limit"] = 10
    if args.shard_index is not None:
        cfg["shard_index"] = args.shard_index
    if args.shard_count is not None:
        cfg["shard_count"] = args.shard_count
    if (cfg["shard_index"] is None) != (cfg["shard_count"] is None):
        raise SystemExit("--shard-index and --shard-count must be used together")
    if hasattr(args, 'ext') and args.ext:
        cfg["input_ext"] = args.ext

    if args.db:
        cfg["db_path"] = args.db
    elif not cfg["db_path"]:
        cfg["db_path"] = str(Path(cfg["output_dir"]) / "progress.db")

    if args.status:
        init_db(cfg["db_path"])
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.StreamHandler()],
        )
        print_summary(cfg["db_path"], logging.getLogger("batch"))
        return

    if args.dry_run:
        input_dir = Path(cfg["input_dir"])
        output_dir = Path(cfg["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        init_db(cfg["db_path"])
        exts = [e.strip().lower() for e in cfg["input_ext"].split(",") if e.strip()]
        all_files: list[Path] = []
        for ext in exts:
            all_files.extend(input_dir.rglob(f"*{ext}"))
        all_files = sorted(set(all_files))
        all_files = select_shard_files(all_files, cfg.get("shard_index"), cfg.get("shard_count"))
        n = register_files(cfg["db_path"], all_files, output_dir, input_dir)
        print(f"Dry-run: scanned {len(all_files)} files, registered {n} new tasks")
        return

    cfg["ffmpeg_exe"] = find_ffmpeg_exe()
    run_batch(cfg)


if __name__ == "__main__":
    freeze_support()
    main()
