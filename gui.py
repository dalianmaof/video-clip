#!/usr/bin/env python3
"""
Video Batch Processor - GUI front-end
Requires: PyQt6
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path


def _detect_nvenc() -> bool:
    """Return True if ffmpeg can open h264_nvenc on this machine."""
    import shutil
    _here = Path(sys.argv[0]).resolve().parent
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        candidates = list((_here / "tools" / "ffmpeg").rglob("ffmpeg.exe"))
        if candidates:
            ffmpeg = str(candidates[0])
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "testsrc=size=256x256:rate=1",
             "-t", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


_HAS_NVENC = _detect_nvenc()

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QCheckBox,
)

# ---------------------------------------------------------------------------
# Locate process_videos.py (same dir as this file / exe)
# ---------------------------------------------------------------------------
_HERE = Path(sys.argv[0]).resolve().parent
_PROCESSOR = _HERE / "process_videos.py"
if not _PROCESSOR.exists():
    _PROCESSOR = Path(__file__).resolve().parent / "process_videos.py"

_CONFIG_FILE = _HERE / "config.json"


# ---------------------------------------------------------------------------
# Worker thread — runs the subprocess and forwards its output
# ---------------------------------------------------------------------------
class ProcessWorker(QThread):
    line_ready = pyqtSignal(str)          # one line of stdout/stderr
    stats_ready = pyqtSignal(int, int, int)  # pending, done, failed
    finished = pyqtSignal(int)             # returncode

    def __init__(self, cmd: list[str], parent=None):
        super().__init__(parent)
        self._cmd = cmd
        self._proc: subprocess.Popen | None = None
        self._stop = False

    def run(self):
        self._bracket_done = 0
        self._bracket_failed = 0
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in self._proc.stdout:
                line = line.rstrip()
                if line:
                    self.line_ready.emit(line)
                    self._parse_stats(line)
                if self._stop:
                    self._proc.terminate()
                    break
            self._proc.wait()
            self.finished.emit(self._proc.returncode)
        except Exception as exc:
            self.line_ready.emit(f"[ERROR] {exc}")
            self.finished.emit(-1)

    def stop(self):
        self._stop = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    # Match "[done/total] done" or "[done/total] failed" from process_videos.py log
    _BRACKET_RE = re.compile(r"\[(\d+)/(\d+)\]\s+(done|failed)", re.IGNORECASE)
    # Match "pending=12 done=34 failed=0" from --status / batch-finished summary
    _STATS_RE = re.compile(
        r"pending[=:\s]*(\d+).*done[=:\s]*(\d+).*failed[=:\s]*(\d+)",
        re.IGNORECASE,
    )

    def _parse_stats(self, line: str):
        # Try bracket format first: "[3/20] done"
        m = self._BRACKET_RE.search(line)
        if m:
            processed = int(m.group(1))
            total = int(m.group(2))
            status = m.group(3).lower()
            # We only know processed count, derive done/failed from running totals
            self._bracket_done = getattr(self, '_bracket_done', 0)
            self._bracket_failed = getattr(self, '_bracket_failed', 0)
            if status == 'done':
                self._bracket_done += 1
            else:
                self._bracket_failed += 1
            pending = total - processed
            self.stats_ready.emit(pending, self._bracket_done, self._bracket_failed)
            return
        # Fall back to summary format
        m = self._STATS_RE.search(line)
        if m:
            self.stats_ready.emit(int(m.group(1)), int(m.group(2)), int(m.group(3)))


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("视频批量处理器")
        self.setMinimumWidth(720)
        self._worker: ProcessWorker | None = None
        self._total = 0

        self._build_ui()
        self._load_config()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # --- folder group ---
        folder_group = QGroupBox("文件夹设置")
        form = QFormLayout(folder_group)

        self._input_edit = QLineEdit()
        self._input_edit.setPlaceholderText("选择视频源文件夹...")
        input_row = self._folder_row(self._input_edit, self._browse_input)
        form.addRow("输入文件夹:", input_row)

        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("选择 MP4 输出文件夹...")
        output_row = self._folder_row(self._output_edit, self._browse_output)
        form.addRow("输出文件夹:", output_row)

        self._ext_edit = QLineEdit()
        self._ext_edit.setPlaceholderText(".ts,.mp4,.mkv,.avi,.mov")
        self._ext_edit.setToolTip("要处理的视频格式，多个格式用逗号分隔")
        form.addRow("视频格式:", self._ext_edit)

        root.addWidget(folder_group)

        # --- options group ---
        opt_group = QGroupBox("处理选项")
        opt_form = QFormLayout(opt_group)

        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 32)
        self._workers_spin.setValue(4)
        self._workers_spin.setToolTip("并行处理的线程数，建议设为物理核心数的一半")
        opt_form.addRow("并行数量:", self._workers_spin)

        self._mode_combo = QComboBox()
        # Index 0: CPU high quality (always available)
        self._mode_combo.addItem("高画质（CPU，最佳质量）")
        if _HAS_NVENC:
            self._mode_combo.addItem("中画质（GPU，高质量）")
            self._mode_combo.addItem("低画质（GPU，极速）")
            self._mode_combo.setToolTip("高画质：libx264 CPU\n中画质：NVENC GPU 高质量\n低画质：NVENC GPU 极速")
        else:
            model = self._mode_combo.model()
            from PyQt6.QtGui import QStandardItem
            for label in ["中画质（GPU，高质量）— 无可用 GPU", "低画质（GPU，极速）— 无可用 GPU"]:
                item = QStandardItem(label)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                model.appendRow(item)
            self._mode_combo.setToolTip("未检测到 NVIDIA GPU，仅高画质（CPU）可用")
        opt_form.addRow("画质选择:", self._mode_combo)

        root.addWidget(opt_group)

        # --- shard group ---
        shard_group = QGroupBox("多机分片（可选）")
        shard_form = QFormLayout(shard_group)

        self._shard_check = QCheckBox("启用分片")
        self._shard_check.setToolTip("多台机器并行时，每台处理不同的分片")
        self._shard_check.stateChanged.connect(self._on_shard_toggle)
        shard_form.addRow("", self._shard_check)

        self._shard_index_spin = QSpinBox()
        self._shard_index_spin.setRange(0, 99)
        self._shard_index_spin.setEnabled(False)
        self._shard_index_spin.setToolTip("本机编号，从 0 开始")
        shard_form.addRow("本机编号 (0 起):", self._shard_index_spin)

        self._shard_count_spin = QSpinBox()
        self._shard_count_spin.setRange(2, 100)
        self._shard_count_spin.setValue(10)
        self._shard_count_spin.setEnabled(False)
        self._shard_count_spin.setToolTip("总机器数量")
        shard_form.addRow("总机器数量:", self._shard_count_spin)

        root.addWidget(shard_group)

        # --- progress ---
        progress_group = QGroupBox("进度")
        pg_layout = QVBoxLayout(progress_group)

        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%v / %m 个文件")
        self._progress_bar.setValue(0)
        self._progress_bar.setMaximum(0)
        pg_layout.addWidget(self._progress_bar)

        stats_row = QHBoxLayout()
        self._lbl_pending = QLabel("等待: 0")
        self._lbl_done = QLabel("完成: 0")
        self._lbl_failed = QLabel("失败: 0")
        for lbl in (self._lbl_pending, self._lbl_done, self._lbl_failed):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stats_row.addWidget(lbl)
        pg_layout.addLayout(stats_row)

        root.addWidget(progress_group)

        # --- log ---
        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        mono = QFont("Consolas", 9)
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._log.setFont(mono)
        self._log.setMinimumHeight(180)
        log_layout.addWidget(self._log)
        root.addWidget(log_group, stretch=1)

        # --- buttons ---
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("开始处理")
        self._start_btn.setFixedHeight(36)
        self._start_btn.clicked.connect(self._on_start)

        self._stop_btn = QPushButton("停止")
        self._stop_btn.setFixedHeight(36)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)

        self._status_btn = QPushButton("查看状态")
        self._status_btn.setFixedHeight(36)
        self._status_btn.clicked.connect(self._on_status)

        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._stop_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._status_btn)
        root.addLayout(btn_row)

        # status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("就绪")

    def _folder_row(self, edit: QLineEdit, slot) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("浏览...")
        btn.setFixedWidth(70)
        btn.clicked.connect(slot)
        h.addWidget(edit)
        h.addWidget(btn)
        return w

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------
    def _load_config(self):
        if not _CONFIG_FILE.exists():
            return
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            self._input_edit.setText(str(data.get("input_dir", "")))
            self._output_edit.setText(str(data.get("output_dir", "")))
            self._ext_edit.setText(str(data.get("input_ext", ".ts,.mp4,.mkv,.avi,.mov")))
            workers = data.get("workers", 4)
            self._workers_spin.setValue(int(workers))
            mode = data.get("encode_mode", "quality")
            idx = {"quality": 0, "gpu_quality": 1, "fast": 2}.get(mode, 0)
            if not _HAS_NVENC:
                idx = 0
            self._mode_combo.setCurrentIndex(idx)
            si = data.get("shard_index")
            sc = data.get("shard_count")
            if si is not None and sc is not None:
                self._shard_check.setChecked(True)
                self._shard_index_spin.setValue(int(si))
                self._shard_count_spin.setValue(int(sc))
        except Exception:
            pass

    def _save_config(self):
        """Persist current settings back to config.json so next launch remembers them."""
        try:
            data: dict = {}
            if _CONFIG_FILE.exists():
                data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data["input_dir"] = self._input_edit.text().strip()
        data["output_dir"] = self._output_edit.text().strip()
        data["workers"] = self._workers_spin.value()
        _MODE_MAP = ["quality", "gpu_quality", "fast"]
        idx = self._mode_combo.currentIndex()
        data["encode_mode"] = _MODE_MAP[idx] if _HAS_NVENC else "quality"
        ext = self._ext_edit.text().strip() or ".ts,.mp4,.mkv,.avi,.mov"
        data["input_ext"] = ext
        if self._shard_check.isChecked():
            data["shard_index"] = self._shard_index_spin.value()
            data["shard_count"] = self._shard_count_spin.value()
        else:
            data["shard_index"] = None
            data["shard_count"] = None
        try:
            _CONFIG_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _browse_input(self):
        d = QFileDialog.getExistingDirectory(self, "选择输入文件夹",
                                              self._input_edit.text() or str(Path.home()))
        if d:
            self._input_edit.setText(d)

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹",
                                              self._output_edit.text() or str(Path.home()))
        if d:
            self._output_edit.setText(d)

    def _on_shard_toggle(self, state):
        enabled = state == Qt.CheckState.Checked.value
        self._shard_index_spin.setEnabled(enabled)
        self._shard_count_spin.setEnabled(enabled)

    def _on_start(self):
        inp = self._input_edit.text().strip()
        out = self._output_edit.text().strip()
        if not inp or not out:
            QMessageBox.warning(self, "缺少信息", "请先选择输入和输出文件夹。")
            return
        if not Path(inp).is_dir():
            QMessageBox.warning(self, "路径无效", f"输入文件夹不存在：\n{inp}")
            return

        self._save_config()

        ext = self._ext_edit.text().strip() or ".ts,.mp4,.mkv,.avi,.mov"
        _MODE_MAP = ["quality", "gpu_quality", "fast"]
        idx = self._mode_combo.currentIndex()
        mode = _MODE_MAP[idx] if _HAS_NVENC else "quality"
        cmd = [
            sys.executable,
            str(_PROCESSOR),
            "--input", inp,
            "--output", out,
            "--workers", str(self._workers_spin.value()),
            "--mode", mode,
            "--ext", ext,
        ]
        if self._shard_check.isChecked():
            cmd += [
                "--shard-index", str(self._shard_index_spin.value()),
                "--shard-count", str(self._shard_count_spin.value()),
            ]

        self._log.clear()
        self._progress_bar.setValue(0)
        self._progress_bar.setMaximum(0)
        self._lbl_pending.setText("等待: -")
        self._lbl_done.setText("完成: 0")
        self._lbl_failed.setText("失败: 0")
        self._total = 0

        self._worker = ProcessWorker(cmd)
        self._worker.line_ready.connect(self._append_log)
        self._worker.stats_ready.connect(self._update_stats)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_bar.showMessage("正在处理...")

    def _on_stop(self):
        if self._worker:
            self._worker.stop()
        self._stop_btn.setEnabled(False)
        self._status_bar.showMessage("正在停止...")

    def _on_status(self):
        inp = self._input_edit.text().strip()
        out = self._output_edit.text().strip()
        if not out:
            QMessageBox.information(self, "提示", "请先设置输出文件夹。")
            return
        cmd = [
            sys.executable,
            str(_PROCESSOR),
            "--output", out,
            "--status",
        ]
        if inp:
            cmd += ["--input", inp]

        self._log.appendPlainText("--- 状态查询 ---")
        worker = ProcessWorker(cmd)
        worker.line_ready.connect(self._append_log)
        worker.finished.connect(lambda _: self._append_log("--- 查询完毕 ---"))
        worker.start()
        # keep reference so GC doesn't kill it
        self._status_worker = worker

    def _append_log(self, line: str):
        self._log.appendPlainText(line)
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    def _update_stats(self, pending: int, done: int, failed: int):
        total = pending + done + failed
        if total > self._total:
            self._total = total
        if self._total > 0:
            self._progress_bar.setMaximum(self._total)
            self._progress_bar.setValue(done)
        self._lbl_pending.setText(f"等待: {pending}")
        self._lbl_done.setText(f"完成: {done}")
        self._lbl_failed.setText(f"失败: {failed}")
        if failed > 0:
            self._lbl_failed.setStyleSheet("color: red; font-weight: bold;")
        else:
            self._lbl_failed.setStyleSheet("")

    def _on_finished(self, returncode: int):
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if returncode == 0:
            self._status_bar.showMessage("处理完成")
            self._append_log("\n[完成] 所有任务已处理完毕。")
        elif returncode == -1:
            self._status_bar.showMessage("处理出错")
        else:
            self._status_bar.showMessage(f"已停止（返回码 {returncode}）")
            self._append_log(f"\n[停止] 进程已退出，返回码: {returncode}")

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "确认退出",
                "处理正在进行中，确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self._worker.stop()
            self._worker.wait(3000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("视频批量处理器")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
