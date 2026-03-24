# 视频批量处理器 (Video Batch Processor)

批量将视频文件转换为 MP4，自动裁剪底部字幕区域，支持 GPU 加速，多机并行部署。

基于 **Tauri v2 + Vue 3 + TypeScript** 构建桌面客户端，Rust 原生处理视频转码。

## 功能特性

- 批量转换：自动扫描输入文件夹，转换为 MP4
- 字幕裁剪：裁剪底部 100px 字幕区域，输出 1920x1080
- 三档画质：高画质（CPU）/ 中画质（GPU）/ 低画质（GPU 极速）
- NVENC 自动检测：启动时自动探测 GPU 可用性
- 断点续传：SQLite 任务追踪，中断后重启自动跳过已完成文件
- 多机部署：分片机制，多台机器并行处理互不干扰
- 桌面 GUI：Tauri 原生窗口，体积小、启动快

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 3 + TypeScript, Vite, Tailwind CSS, shadcn-vue |
| 桌面框架 | Tauri v2 (Rust) |
| 视频处理 | Rust 原生 (rusqlite + tokio) + ffmpeg |
| 打包 | Tauri bundler (NSIS/MSI) |

## 开发环境搭建

### 前置条件

- **Node.js** >= 18
- **Rust** >= 1.77（含 cargo）
- **Tauri CLI**：`cargo install tauri-cli --version "^2"`
- **ffmpeg**：放入 `tools/ffmpeg/` 或加入系统 PATH

### 安装依赖

```powershell
npm install
```

## 开发测试

### 启动开发服务器

```powershell
npm run tauri dev
```

这会同时启动：
- Vite dev server（localhost:1420，热更新）
- Tauri 原生窗口（WebView2）

### 测试步骤

#### 1. 界面加载

启动后应看到完整界面：文件夹设置、处理选项、分片配置、进度条、日志区域、底部按钮栏。

#### 2. NVENC 检测

- 启动时自动检测 GPU
- **有 NVIDIA GPU**：画质下拉可选全部三项
- **无 GPU**：中画质/低画质显示「— 无 GPU」且不可选，自动回退到高画质

#### 3. 文件夹选择

- 点击「浏览」按钮，应弹出系统文件夹选择对话框
- 选中后路径显示在输入框中

#### 4. 配置持久化

- 修改任意设置后点击「开始处理」
- 关闭程序重新打开，之前的设置应被恢复

#### 5. 处理流程（需要测试视频文件）

准备一个包含少量 .ts 或 .mp4 文件的文件夹：

1. 选择输入/输出文件夹
2. 并行数量设为 1，画质选「高画质」
3. 点击「开始处理」
4. 观察：日志实时滚动输出、进度条更新、完成/失败计数变化
5. 处理完毕后状态栏显示「处理完成」

#### 6. 停止功能

- 处理过程中点击「停止」，进程应优雅停止，状态栏提示已停止

#### 7. 查询状态

- 设置好输出文件夹后，点击「查询状态」
- 日志区域应输出 pending/done/failed 汇总信息

#### 8. 分片配置

- 勾选「多机分片」，应出现编号和数量输入框
- 取消勾选后输入框隐藏

### 仅测试前端（不启动 Tauri）

```powershell
npm run dev
```

在浏览器打开 http://localhost:1420 可查看 UI 布局和样式，但 Tauri API（文件夹选择、后端命令）不可用。

### TypeScript 类型检查

```powershell
npx vue-tsc --noEmit
```

## 界面说明

| 选项 | 说明 |
|------|------|
| 输入文件夹 | 存放源视频文件的文件夹 |
| 输出文件夹 | MP4 输出目标文件夹 |
| 文件扩展名 | 要处理的格式，多个用逗号分隔，如 `.ts,.mp4,.mkv` |
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
3. 在 GUI 中勾选「多机分片」，设置本机编号（0 起）和总机器数
4. 各自点击开始，互不干扰，各机有独立的 `progress.db`

**示例（3 台机器）：**
- 机器 0：本机编号=0，总机器数=3
- 机器 1：本机编号=1，总机器数=3
- 机器 2：本机编号=2，总机器数=3

## 打包发布

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
```

输出位于 `src-tauri/target/release/bundle/`：
- `nsis/` — exe 安装包

## 配置文件 config.json

首次运行后自动生成，GUI 设置会自动保存。主要字段：

```json
{
  "input_dir": "D:/path/to/input",
  "output_dir": "D:/path/to/output",
  "input_ext": ".ts,.mp4,.mkv,.avi,.mov",
  "workers": 4,
  "encode_mode": "quality",
  "shard_index": null,
  "shard_count": null
}
```

## 项目结构

```
video_clip/
├── src/                     # Vue 前端
│   ├── App.vue              # 主界面
│   ├── main.ts              # 入口
│   ├── assets/index.css     # Tailwind + 主题变量
│   ├── components/ui/       # shadcn-vue 组件
│   └── lib/utils.ts         # 工具函数
├── src-tauri/               # Tauri 后端 (Rust)
│   ├── src/
│   │   ├── lib.rs           # 插件注册 + 命令注册
│   │   ├── commands.rs      # Tauri IPC 命令
│   │   └── batch/           # 批处理核心模块
│   │       ├── mod.rs       # 批处理编排
│   │       ├── db.rs        # SQLite 任务追踪
│   │       ├── ffmpeg.rs    # ffmpeg 命令构建与执行
│   │       └── scanner.rs   # 文件扫描与分片
│   ├── tauri.conf.json      # 窗口/资源配置
│   └── capabilities/        # 权限声明
├── tools/ffmpeg/            # 内置 ffmpeg
├── build_portable.ps1       # 一键构建脚本
└── config.json              # 运行时配置
```

## 注意事项

- ffmpeg 优先使用系统 PATH，其次使用 `tools/ffmpeg/` 内置版本
- 失败的文件标记为 `failed`，不影响其他文件继续处理
- 点击「查询状态」可随时查看已完成/失败数量
- 开发时修改 Vue 代码可热更新，修改 Rust 代码需重新编译
