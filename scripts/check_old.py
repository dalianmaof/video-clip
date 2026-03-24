#!/usr/bin/env python3
"""
视频参数探测脚本
用法: python detect_params.py <样例视频路径>

输出:
  - 视频基本参数（分辨率、帧率、编码、时长）
  - 音频参数（编码、采样率、声道）
  - 底部字幕区域辅助截图（需要手动确认裁剪高度）
  - 推荐的 FFmpeg 处理参数
"""

import sys
import json
import subprocess
import os
from pathlib import Path


def run(cmd: list) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def probe(video_path: str) -> dict:
    """用 ffprobe 获取视频完整元数据"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        video_path,
    ]
    code, out, err = run(cmd)
    if code != 0:
        print(f"[错误] ffprobe 失败:\n{err}")
        sys.exit(1)
    return json.loads(out)


def check_nvenc() -> bool:
    """检测 NVIDIA GPU 编码器是否可用"""
    cmd = ["ffmpeg", "-hide_banner", "-encoders"]
    _, out, _ = run(cmd)
    return "h264_nvenc" in out


def extract_frames(video_path: str, output_dir: Path):
    """提取首帧和末尾帧，用于目视确认字幕位置"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 提取第 5 秒的帧（避开片头黑屏）
    cmd_first = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", "5", "-i", video_path,
        "-vframes", "1",
        str(output_dir / "frame_5s.png"),
        "-y",
    ]
    # 提取中间帧（字幕出现概率更高）
    run(cmd_first)

    cmd_mid = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", "60", "-i", video_path,
        "-vframes", "1",
        str(output_dir / "frame_60s.png"),
        "-y",
    ]
    run(cmd_mid)


def crop_bottom_strip(video_path: str, output_dir: Path, height: int, strip_px: int = 150):
    """
    截取视频底部 strip_px 像素的条带，放大后保存，便于目视量字幕高度。
    strip_px 默认 150px，足够覆盖常见字幕区域。
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 裁出底部条带并放大 3 倍
    crop_filter = f"crop=iw:{strip_px}:0:ih-{strip_px},scale=iw*3:-1"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", "60", "-i", video_path,
        "-vframes", "1",
        "-vf", crop_filter,
        str(output_dir / "subtitle_strip_3x.png"),
        "-y",
    ]
    run(cmd)


def audio_compatible_with_mp4(codec: str) -> tuple[bool, str]:
    """判断音频编解码器是否可直接封装进 MP4"""
    compatible = {"aac", "mp3", "ac3", "eac3", "dts", "opus", "flac", "alac", "pcm_s16le", "pcm_s24le"}
    codec_lower = codec.lower()
    if codec_lower in compatible:
        return True, "-c:a copy"
    # MP2 是广播 TS 常见格式，MP4 不支持
    if codec_lower in {"mp2", "mp2float"}:
        return False, "-c:a aac -b:a 192k  # MP2 不兼容 MP4，需转码"
    return False, f"-c:a aac -b:a 192k  # {codec} 未知兼容性，建议转码"


def main():
    if len(sys.argv) < 2:
        print("用法: python detect_params.py <视频文件路径>")
        sys.exit(1)

    video_path = sys.argv[1]
    if not os.path.exists(video_path):
        print(f"[错误] 文件不存在: {video_path}")
        sys.exit(1)

    print("=" * 60)
    print("  视频参数探测报告")
    print("=" * 60)

    # ── 1. ffprobe 元数据 ──────────────────────────────────────
    meta = probe(video_path)
    streams = meta.get("streams", [])
    fmt = meta.get("format", {})

    video_stream = next((s for s in streams if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in streams if s["codec_type"] == "audio"), None)

    if not video_stream:
        print("[错误] 未找到视频流")
        sys.exit(1)

    # 视频参数
    width = video_stream.get("width", "?")
    height = video_stream.get("height", "?")
    vcodec = video_stream.get("codec_name", "?")
    pix_fmt = video_stream.get("pix_fmt", "?")

    # 帧率（可能是分数形式如 "30000/1001"）
    fps_raw = video_stream.get("r_frame_rate", "?")
    try:
        num, den = fps_raw.split("/")
        fps = round(int(num) / int(den), 3)
    except Exception:
        fps = fps_raw

    duration = float(fmt.get("duration", 0))
    size_mb = int(fmt.get("size", 0)) / 1024 / 1024
    bitrate_kbps = int(fmt.get("bit_rate", 0)) // 1000

    print(f"\n【视频流】")
    print(f"  分辨率    : {width} × {height}")
    print(f"  编码      : {vcodec}")
    print(f"  像素格式  : {pix_fmt}")
    print(f"  帧率      : {fps} fps  (原始: {fps_raw})")
    print(f"  时长      : {duration:.1f} 秒 ({duration/3600:.2f} 小时)")
    print(f"  文件大小  : {size_mb:.1f} MB")
    print(f"  总码率    : {bitrate_kbps} kbps")

    # ── 2. 音频参数 ────────────────────────────────────────────
    if audio_stream:
        acodec = audio_stream.get("codec_name", "?")
        sample_rate = audio_stream.get("sample_rate", "?")
        channels = audio_stream.get("channels", "?")
        a_bitrate = int(audio_stream.get("bit_rate", 0)) // 1000

        compatible, audio_flag = audio_compatible_with_mp4(acodec)

        print(f"\n【音频流】")
        print(f"  编码      : {acodec}")
        print(f"  采样率    : {sample_rate} Hz")
        print(f"  声道数    : {channels}")
        print(f"  音频码率  : {a_bitrate if a_bitrate else '未知'} kbps")
        print(f"  MP4兼容性 : {'✓ 可直接 copy' if compatible else '✗ 需重新编码'}")
    else:
        print(f"\n【音频流】无音频")
        audio_flag = "-an"

    # ── 3. GPU 检测 ────────────────────────────────────────────
    has_nvenc = check_nvenc()
    encoder = "h264_nvenc" if has_nvenc else "libx264"
    print(f"\n【硬件加速】")
    print(f"  NVENC     : {'✓ 可用，将使用 GPU 编码' if has_nvenc else '✗ 不可用，将使用 CPU (libx264)'}")

    # ── 4. 字幕截图 ────────────────────────────────────────────
    script_dir = Path(video_path).parent
    frame_dir = script_dir / "detect_output"

    print(f"\n【字幕区域截图】")
    print(f"  正在提取帧... ", end="", flush=True)
    extract_frames(video_path, frame_dir)
    crop_bottom_strip(video_path, frame_dir, int(height) if isinstance(height, int) else 1080)
    print("完成")
    print(f"  截图目录  : {frame_dir}/")
    print(f"  - frame_5s.png          → 第5秒全帧，目视检查字幕")
    print(f"  - frame_60s.png         → 第60秒全帧，目视检查字幕")
    print(f"  - subtitle_strip_3x.png → 底部150px放大3倍，用于精确量取字幕高度")

    # ── 5. 推荐配置 ────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  推荐配置（请将以下内容填入主脚本的 CONFIG 部分）")
    print(f"{'=' * 60}")
    print(f"""
CONFIG = {{
    # 输入输出
    "input_dir":  "/path/to/input",
    "output_dir": "/path/to/output",
    "input_ext":  ".ts",

    # 裁剪参数 ← 请根据 subtitle_strip_3x.png 确认此值
    "crop_bottom_px": 80,          # ← 待确认！

    # 输出分辨率
    "output_width":  1920,
    "output_height": 1080,

    # 编码器（已自动探测）
    "video_encoder": "{encoder}",
    {"# NVENC 参数" if has_nvenc else "# libx264 参数"}
    {"'nvenc_preset': 'p4',         # p1(快)~p7(慢)，p4 是速度/质量平衡点" if has_nvenc else "'crf': 23,                   # 18=高质量 / 23=默认 / 28=小文件"},
    {"'nvenc_cq': 23,               # 恒定质量模式，等效 CRF，越小越好" if has_nvenc else ""},

    # 音频
    "audio_flag": "{audio_flag}",

    # 并发
    "workers": {"4  # 建议: GPU数 × 2" if has_nvenc else "6  # 建议: CPU核数 × 0.75"},
}}
""")

    # ── 6. 存储预估 ────────────────────────────────────────────
    if bitrate_kbps and duration > 0:
        # 按 CRF23 / NVENC CQ23，输出码率通常比原始低 30-50%
        est_output_kbps = bitrate_kbps * 0.6
        total_hours = 5000
        est_total_gb = (est_output_kbps * 1000 / 8) * total_hours * 3600 / 1024**3
        print(f"\n【5000小时存储预估】")
        print(f"  当前样本码率   : {bitrate_kbps} kbps")
        print(f"  预估输出码率   : ~{int(est_output_kbps)} kbps  (约原始的60%)")
        print(f"  5000小时总大小 : ~{est_total_gb:.0f} GB  ({est_total_gb/1024:.1f} TB)")
        print(f"  ※ 最终码率取决于客户确认的 CRF/码率值")

    print(f"\n{'=' * 60}")
    print(f"  下一步：")
    print(f"  1. 打开 detect_output/subtitle_strip_3x.png，量取字幕高度（px）")
    print(f"  2. 将字幕高度填入上方 CONFIG 的 crop_bottom_px")
    print(f"  3. 等客户确认码率要求后，补充 crf / nvenc_cq 参数")
    print(f"  4. 将 CONFIG 发回，即可生成完整批处理脚本")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()