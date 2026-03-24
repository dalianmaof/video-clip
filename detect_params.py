#!/usr/bin/env python3
"""
Video parameter detection helper.

Usage:
  python detect_params.py /path/to/sample.ts

What it does:
  - Prints a concise parameter report:
    resolution, fps, video/audio codec, bitrate, file size, GPU availability
  - Generates 3 screenshots under detect_output/:
    two full-frame grabs + a 150px bottom strip enlarged 3x
  - Prints a CONFIG template you can paste into the main script
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Toolchain:
    ffmpeg: str
    ffprobe: str


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def which_or_none(name: str) -> str | None:
    return shutil.which(name)


def find_tool(name: str) -> str | None:
    exe_name = f"{name}.exe" if os.name == "nt" else name

    candidates = [
        which_or_none(name),
        which_or_none(exe_name),
        os.environ.get("FFMPEG_HOME"),
        Path.cwd() / "ffmpeg",
        Path.cwd() / "ffmpeg" / "bin",
        Path.cwd() / "tools" / "ffmpeg",
        Path.cwd() / "tools" / "ffmpeg" / "bin",
        Path.home() / "ffmpeg" / "bin",
        Path.home() / "tools" / "ffmpeg" / "bin",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ffmpeg" / "bin",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() and path.name.lower() == exe_name.lower():
            return str(path)
        if path.is_dir():
            exe_path = path / exe_name
            if exe_path.exists():
                return str(exe_path)
            matches = list(path.rglob(exe_name))
            if matches:
                return str(matches[0])
    return None


def resolve_toolchain() -> Toolchain:
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = []
        if not ffmpeg:
            missing.append("ffmpeg")
        if not ffprobe:
            missing.append("ffprobe")
        raise RuntimeError(
            "Missing required tool(s): "
            + ", ".join(missing)
            + ". Install FFmpeg or place the executables in PATH, "
            + "a local ./ffmpeg/bin directory, or set FFMPEG_HOME."
        )
    return Toolchain(ffmpeg=ffmpeg, ffprobe=ffprobe)


def probe(video_path: Path, toolchain: Toolchain) -> dict[str, Any]:
    cmd = [
        toolchain.ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(video_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def parse_fraction(value: str | None) -> float | str:
    if not value:
        return "unknown"
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return "unknown"
            return round(float(num) / den_f, 3)
        return round(float(value), 3)
    except Exception:
        return value


def int_or_none(value: Any) -> int | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return int(float(value))
    except Exception:
        return None


def check_nvidia_gpu() -> tuple[bool, str]:
    if os.name == "nt":
        nvidia_smi = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    else:
        nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return False, "nvidia-smi not found"
    result = run([nvidia_smi, "-L"])
    if result.returncode != 0:
        return False, result.stderr.strip() or "nvidia-smi failed"
    text = result.stdout.strip()
    return bool(text), text or "GPU detected"


def check_nvenc(toolchain: Toolchain) -> bool:
    result = run([toolchain.ffmpeg, "-hide_banner", "-encoders"])
    if result.returncode != 0:
        return False
    return "h264_nvenc" in result.stdout


def pick_capture_times(duration: float) -> tuple[float, float]:
    first = 5.0 if duration >= 5 else max(duration * 0.25, 0.0)
    if duration >= 60:
        second = 60.0
    else:
        second = max(first + 1.0, duration * 0.75)
    if duration > 0:
        second = min(second, max(duration - 0.5, first))
    return first, second


def ffmpeg_extract_frame(toolchain: Toolchain, video_path: Path, ts: float, out_path: Path) -> None:
    cmd = [
        toolchain.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{ts:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-y",
        str(out_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"failed to extract frame at {ts}s")


def ffmpeg_extract_bottom_strip(toolchain: Toolchain, video_path: Path, height: int, out_path: Path) -> None:
    strip_h = max(1, min(150, height))
    vf = f"crop=iw:{strip_h}:0:ih-{strip_h},scale=iw*3:-1"
    cmd = [
        toolchain.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "60",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-y",
        str(out_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "failed to extract strip")


def audio_mp4_compatibility(codec: str) -> tuple[bool, str]:
    compatible = {
        "aac",
        "mp3",
        "ac3",
        "eac3",
        "dts",
        "opus",
        "flac",
        "alac",
        "pcm_s16le",
        "pcm_s24le",
    }
    codec_lower = codec.lower()
    if codec_lower in compatible:
        return True, "-c:a copy"
    if codec_lower in {"mp2", "mp2float"}:
        return False, "-c:a aac -b:a 192k  # MP2 is not MP4-friendly"
    return False, f"-c:a aac -b:a 192k  # {codec} may need transcoding"


def print_section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect video parameters and dump screenshots.")
    parser.add_argument("video", help="Path to the sample video file")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Where to write screenshots. Defaults to <video_dir>/detect_output",
    )
    args = parser.parse_args()

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        print(f"[error] file not found: {video_path}", file=sys.stderr)
        return 1

    try:
        toolchain = resolve_toolchain()
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    meta = probe(video_path, toolchain)
    streams = meta.get("streams", [])
    fmt = meta.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream:
        print("[error] no video stream found", file=sys.stderr)
        return 1

    width = int_or_none(video_stream.get("width")) or 0
    height = int_or_none(video_stream.get("height")) or 0
    vcodec = video_stream.get("codec_name", "unknown")
    pix_fmt = video_stream.get("pix_fmt", "unknown")
    fps_raw = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "unknown"
    fps = parse_fraction(fps_raw) if isinstance(fps_raw, str) else fps_raw

    duration = float(fmt.get("duration") or 0.0)
    size_bytes = int_or_none(fmt.get("size")) or 0
    bitrate_bps = int_or_none(fmt.get("bit_rate")) or 0

    audio_codec = "none"
    sample_rate = "n/a"
    channels = "n/a"
    audio_bitrate = 0
    audio_flag = "-an"
    if audio_stream:
        audio_codec = audio_stream.get("codec_name", "unknown")
        sample_rate = audio_stream.get("sample_rate", "unknown")
        channels = audio_stream.get("channels", "unknown")
        audio_bitrate = int_or_none(audio_stream.get("bit_rate")) or 0
        _, audio_flag = audio_mp4_compatibility(audio_codec)

    nvidia_ok, nvidia_msg = check_nvidia_gpu()
    nvenc_ok = check_nvenc(toolchain)
    encoder = "h264_nvenc" if nvenc_ok else "libx264"

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else video_path.parent / "detect_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    ts1, ts2 = pick_capture_times(duration)
    frame1 = output_dir / "frame_5s.png"
    frame2 = output_dir / "frame_60s.png"
    strip = output_dir / "subtitle_strip_3x.png"

    ffmpeg_extract_frame(toolchain, video_path, ts1, frame1)
    ffmpeg_extract_frame(toolchain, video_path, ts2, frame2)
    ffmpeg_extract_bottom_strip(toolchain, video_path, height or 1080, strip)

    print("=" * 72)
    print("Video Parameter Detection Report")
    print("=" * 72)
    print_section("Video")
    print(f"  Resolution   : {width} x {height}")
    print(f"  Codec        : {vcodec}")
    print(f"  Pixel format : {pix_fmt}")
    print(f"  FPS          : {fps} (raw: {fps_raw})")
    print(f"  Duration     : {duration:.3f} s")
    print(f"  File size    : {size_bytes / 1024 / 1024:.2f} MB")
    print(f"  Bitrate      : {bitrate_bps / 1000:.0f} kbps")

    print_section("Audio")
    if audio_stream:
        print(f"  Codec        : {audio_codec}")
        print(f"  Sample rate  : {sample_rate} Hz")
        print(f"  Channels     : {channels}")
        print(f"  Bitrate      : {audio_bitrate / 1000:.0f} kbps" if audio_bitrate else "  Bitrate      : unknown")
        print(f"  MP4 flag     : {audio_flag}")
    else:
        print("  No audio stream")
        print("  MP4 flag     : -an")

    print_section("GPU")
    print(f"  nvidia-smi   : {'yes' if nvidia_ok else 'no'}")
    print(f"  detail       : {nvidia_msg}")
    print(f"  h264_nvenc   : {'yes' if nvenc_ok else 'no'}")

    print_section("Screenshots")
    print(f"  Output dir   : {output_dir}")
    print(f"  Frame 1      : {frame1.name}  ({ts1:.1f}s)")
    print(f"  Frame 2      : {frame2.name}  ({ts2:.1f}s)")
    print(f"  Strip        : {strip.name}  (bottom 150px x3)")

    print_section("CONFIG Template")
    print(
        f"""CONFIG = {{
    # input / output
    "input_dir": "/path/to/input",
    "output_dir": "/path/to/output",
    "input_ext": ".ts",

    # subtitle crop
    "subtitle_height_px": None,  # measure from detect_output/subtitle_strip_3x.png

    # output resolution
    "output_width": {width or 1920},
    "output_height": {height or 1080},

    # video encoder
    "video_encoder": "{encoder}",
    {"'nvenc_preset': 'p4'," if nvenc_ok else "'crf': 23,"}
    {"'nvenc_cq': 23," if nvenc_ok else "# 'crf': 23,"}

    # audio
    "audio_flag": "{audio_flag}",

    # concurrency
    "workers": {4 if nvenc_ok else 6},
}}"""
    )

    print_section("Next Steps")
    print("  1. Open detect_output/subtitle_strip_3x.png and measure subtitle height in px")
    print("  2. Fill subtitle_height_px in the CONFIG block")
    print("  3. Paste the CONFIG block into the main processing script")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
