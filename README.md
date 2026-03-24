# 视频批量处理器 (Video Batch Processor)

批量将视频文件转换为 MP4，自动裁剪底部字幕区域，支持 GPU 加速，多机并行部署。

## 功能特性

- 批量转换：自动扫描输入文件夹，转换为 MP4
- 字幕裁剪：裁剪底部 100px 字幕区域，输出 1920x1080
- 三档画质：高画质（CPU）/ 中画质（GPU）/ 低画质（GPU 极速）
- 断点续传：SQLite 任务追踪，中断后重启自动跳过已完成文件
- 多机部署：分片机制，多台机器并行处理互不干扰
- 桌面 GUI：简单易用，无需命令行

## 快速开始

### 安装依赖

```powershell
uv sync
```

### 运行 GUI

```powershell
uv run python gui.py
```

### 界面说明

| 选项 | 说明 |
|------|------|
| 输入文件夹 | 存放源视频文件的文件夹 |
| 输出文件夹 | MP4 输出目标文件夹 |
| 视频格式 | 要处理的格式，多个用逗号分隔，如 `.ts,.mp4,.mkv` |
| 并行数量 | 同时处理的文件数，建议设为物理核心数的一半 |
| 画质选择 | 见下方说明 |
| 多机分片 | 多台机器并行时启用，每台设置不同编号 |

### 画质选择

| 档位 | 编码器 | 速度 | 说明 |
|------|--------|------|------|
| 高画质 | libx264 CRF18 slow | 慢 | 最佳质量，依赖 CPU |
| 中画质 | NVENC p7 VBR CQ18 | 快 | GPU 高质量，需要 NVIDIA 显卡 |
| 低画质 | NVENC p4 CQ18 | 极快 | GPU 极速，需要 NVIDIA 显卡 |

无 NVIDIA GPU 时，中/低画质选项自动置灰不可选。

## 多机部署

适合多台 Windows 电脑并行处理同一批文件。

1. 将源视频放在共享路径（或每台机器本地各一份）
2. 每台机器设置各自的输出文件夹
3. 在 GUI 中勾选「启用分片」，设置本机编号（0 起）和总机器数
4. 各自点击开始，互不干扰，各机有独立的 `progress.db`

**示例（3 台机器）：**
- 机器 0：本机编号=0，总机器数=3
- 机器 1：本机编号=1，总机器数=3
- 机器 2：本机编号=2，总机器数=3

## 打包便携版

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
```

输出：
- `dist\VideoBatchProcessor\` — 便携目录
- `release\VideoBatchProcessor-win-portable.zip` — 分发包

解压后只需编辑 `config.json` 或直接通过 GUI 设置，无需安装 Python。

## 配置文件 config.json

首次运行后自动生成，GUI 设置会自动保存。主要字段：

```json
{
  "input_dir": "D:/path/to/input",
  "output_dir": "D:/path/to/output",
  "input_ext": ".ts,.mp4,.mkv,.avi,.mov",
  "workers": 4,
  "encode_mode": "fast",
  "shard_index": null,
  "shard_count": null
}
```

## 注意事项

- ffmpeg 优先使用系统 PATH，其次使用 `tools/ffmpeg/` 内置版本
- 失败的文件标记为 `failed`，不影响其他文件继续处理
- 点击「查看状态」可随时查看已完成/失败数量

3. 在 GUI 中勾选