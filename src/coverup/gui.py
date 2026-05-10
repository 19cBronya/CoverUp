from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .ffmpeg_tools import FfmpegBinaries, FfmpegError, locate_binaries
from .models import (
    CoverMode,
    CoverSource,
    JobStatus,
    LogVerbosity,
    MetadataFailureAction,
    ProbeResult,
    RunOptions,
    SampleRequest,
    SampleResult,
    ScanOptions,
    VideoJob,
)
from .probe import probe_video
from .processor import process_in_place
from .sampling import decide_window, sample_minute
from .scanner import scan_videos


class WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    finished = Signal()


class CallableWorker(QRunnable):
    def __init__(self, fn: Callable[[], object]):
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as err:  # noqa: BLE001
            self._safe_emit(self.signals.failed.emit, str(err))
            self._safe_emit(self.signals.finished.emit)
            return
        self._safe_emit(self.signals.done.emit, result)
        self._safe_emit(self.signals.finished.emit)

    def _safe_emit(self, emitter: Callable[..., None], *args: object) -> None:
        try:
            emitter(*args)
        except RuntimeError:
            # App/window might be closing while background tasks are still unwinding.
            return


@dataclass(slots=True)
class SampleTaskResult:
    job_index: int
    minute_index: int
    result: SampleResult


@dataclass(slots=True)
class ProbeTaskResult:
    job_index: int
    result: ProbeResult


@dataclass(slots=True)
class ProcessTaskResult:
    job_index: int
    final_status: JobStatus
    strategy_result: str
    error: str
    old_path: Path
    renamed_path: Path | None = None


class MainWindow(QMainWindow):
    COL_SELECTED = 0
    COL_COVER = 1
    COL_FILENAME = 2
    COL_DIRECTORY = 3
    COL_SOURCE = 4
    COL_HAS_COVER = 5
    COL_STATUS = 6
    COL_STRATEGY = 7
    COL_ERROR = 8
    COL_MINUTE = 9

    def __init__(self, bins: FfmpegBinaries):
        super().__init__()
        self.bins = bins
        self.setWindowTitle("CoverUp - 视频封面可视化替换")
        self.resize(1420, 900)

        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)

        self.jobs: list[VideoJob] = []
        self.probes: dict[int, ProbeResult] = {}
        self.sample_cache: dict[tuple[str, int], SampleResult] = {}
        self.sample_inflight: set[tuple[str, int]] = set()
        self.probe_inflight: set[int] = set()
        self.processing_queue: list[int] = []
        self.current_run_options = RunOptions()
        self.processing_now = False
        self.current_processing_index: int | None = None
        self.active_workers: set[CallableWorker] = set()

        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        main_layout = QVBoxLayout(root)
        self.setStyleSheet(
            """
            QMainWindow { background: #f4f7fb; }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #d9e0ea;
                gridline-color: #e7edf5;
                alternate-background-color: #f8fbff;
            }
            QHeaderView::section {
                background: #edf3fa;
                color: #2a3342;
                border: none;
                border-right: 1px solid #d9e0ea;
                padding: 8px;
                font-weight: 600;
            }
            QPushButton {
                background: #1f6feb;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;
            }
            QPushButton:hover { background: #2c7bf4; }
            QLineEdit {
                border: 1px solid #c8d3e1;
                border-radius: 5px;
                padding: 5px 8px;
                background: #ffffff;
            }
            """
        )

        top = QHBoxLayout()
        self.btn_add_files = QPushButton("添加视频")
        self.btn_add_dir = QPushButton("打开目录")
        self.chk_recursive = QCheckBox("递归子目录")
        self.chk_recursive.setChecked(True)
        self.chk_mode_metadata = QCheckBox("元数据封面")
        self.chk_mode_metadata.setChecked(True)
        self.chk_mode_first_frame = QCheckBox("替换首帧")
        self.chk_cmd_logs = QCheckBox("命令行日志")
        self.chk_cmd_logs.setChecked(True)
        self.cmb_metadata_failure_action = QComboBox()
        self.cmb_metadata_failure_action.addItem("失败后跳过", MetadataFailureAction.SKIP.value)
        self.cmb_metadata_failure_action.addItem("失败后首帧重编码", MetadataFailureAction.FIRST_FRAME.value)
        self.cmb_metadata_failure_action.setCurrentIndex(0)
        self.cmb_log_verbosity = QComboBox()
        self.cmb_log_verbosity.addItem("紧凑", LogVerbosity.COMPACT.value)
        self.cmb_log_verbosity.addItem("中等", LogVerbosity.MEDIUM.value)
        self.cmb_log_verbosity.addItem("原始", LogVerbosity.RAW.value)
        self.cmb_log_verbosity.setCurrentIndex(1)
        self.btn_run_all = QPushButton("执行全部")
        self.btn_run_selected = QPushButton("执行选择")
        top.addWidget(self.btn_add_files)
        top.addWidget(self.btn_add_dir)
        top.addWidget(self.chk_recursive)
        top.addSpacing(18)
        top.addWidget(QLabel("执行策略："))
        top.addWidget(self.chk_mode_metadata)
        top.addWidget(self.chk_mode_first_frame)
        top.addWidget(self.chk_cmd_logs)
        top.addWidget(QLabel("元数据失败："))
        top.addWidget(self.cmb_metadata_failure_action)
        top.addWidget(QLabel("日志："))
        top.addWidget(self.cmb_log_verbosity)
        top.addStretch(1)
        top.addWidget(self.btn_run_selected)
        top.addWidget(self.btn_run_all)
        main_layout.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["选择", "当前封面", "文件名", "目录", "封面来源", "已有元数据", "状态", "策略结果", "错误信息", "分钟窗口"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(True)
        self.table.verticalHeader().setDefaultSectionSize(96)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(self.COL_SELECTED, QHeaderView.Fixed)
        self.table.setColumnWidth(self.COL_SELECTED, 62)
        self.table.horizontalHeader().setSectionResizeMode(self.COL_ERROR, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(self.COL_FILENAME, QHeaderView.Stretch)
        left_layout.addWidget(self.table)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)

        info_box = QGroupBox("当前视频")
        info_layout = QVBoxLayout(info_box)
        self.lbl_video = QLabel("未选择")
        self.lbl_probe = QLabel("探测信息：-")
        self.lbl_window = QLabel("抽帧窗口：-")
        info_layout.addWidget(self.lbl_video)
        info_layout.addWidget(self.lbl_probe)
        info_layout.addWidget(self.lbl_window)
        right_layout.addWidget(info_box)

        action_row = QHBoxLayout()
        self.btn_upload_cover = QPushButton("上传封面图")
        self.btn_next_minute = QPushButton("更换（下一分钟）")
        action_row.addWidget(self.btn_upload_cover)
        action_row.addWidget(self.btn_next_minute)
        right_layout.addLayout(action_row)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(220, 130))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setSpacing(10)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        right_layout.addWidget(self.grid, 1)

        help_box = QGroupBox("说明")
        help_layout = QGridLayout(help_box)
        help_layout.addWidget(QLabel("默认每分钟均匀抽 12 张，先从 0~60 秒。"), 0, 0)
        help_layout.addWidget(QLabel("末尾不足 1 分钟时在剩余时长内抽 12 张。"), 1, 0)
        help_layout.addWidget(QLabel("继续更换会回到第一分钟并循环。"), 2, 0)
        right_layout.addWidget(help_box)

        splitter.addWidget(right)
        splitter.setSizes([900, 520])

        self.setCentralWidget(root)

        self.btn_add_files.clicked.connect(self._on_add_files)
        self.btn_add_dir.clicked.connect(self._on_add_dir)
        self.table.itemSelectionChanged.connect(self._on_selection_change)
        self.btn_upload_cover.clicked.connect(self._on_upload_cover)
        self.btn_next_minute.clicked.connect(self._on_next_minute)
        self.grid.itemClicked.connect(self._on_sample_selected)
        self.btn_run_all.clicked.connect(self._on_run_all)
        self.btn_run_selected.clicked.connect(self._on_run_selected)

    def _current_options(self) -> RunOptions:
        action_value = self.cmb_metadata_failure_action.currentData() or MetadataFailureAction.SKIP.value
        verbosity_value = self.cmb_log_verbosity.currentData() or LogVerbosity.MEDIUM.value
        return RunOptions(
            use_metadata=self.chk_mode_metadata.isChecked(),
            use_first_frame=self.chk_mode_first_frame.isChecked(),
            metadata_failure_action=MetadataFailureAction(action_value),
            log_verbosity=LogVerbosity(verbosity_value),
        )

    def _current_index(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return -1
        return rows[0].row()

    def _on_add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频文件")
        if not files:
            return
        self._add_paths([Path(v) for v in files])

    def _on_add_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择目录")
        if not directory:
            return
        recursive = self.chk_recursive.isChecked()
        paths = scan_videos(options=ScanOptions(directory_path=Path(directory), recursive=recursive, deduplicate=True))
        self._add_paths(paths)

    def _add_paths(self, paths: list[Path]) -> None:
        existing = {j.video_path.resolve() for j in self.jobs}
        inserted = 0
        for path in paths:
            resolved = path.expanduser().resolve()
            if resolved in existing:
                continue
            if not resolved.is_file():
                continue
            existing.add(resolved)
            job = VideoJob(video_path=resolved, pending_filename=resolved.stem)
            self.jobs.append(job)
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._attach_selected_checkbox(row)
            self._attach_filename_editor(row)
            self._update_cover_preview(row)
            self._render_row(row)
            self._schedule_probe(row, priority=0)
            inserted += 1
        if inserted == 0:
            return
        if self._current_index() < 0 and self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _attach_filename_editor(self, row: int) -> None:
        editor = QLineEdit()
        editor.setPlaceholderText("输入新文件名（不含扩展名）")
        editor.editingFinished.connect(lambda r=row, e=editor: self._on_filename_edited(r, e.text()))
        self.table.setCellWidget(row, self.COL_FILENAME, editor)

    def _attach_selected_checkbox(self, row: int) -> None:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        checkbox = QCheckBox()
        checkbox.setChecked(False)
        checkbox.stateChanged.connect(lambda _state, r=row: self._on_selected_changed(r))
        layout.addWidget(checkbox)
        self.table.setCellWidget(row, self.COL_SELECTED, box)

    def _row_checkbox(self, row: int) -> QCheckBox | None:
        widget = self.table.cellWidget(row, self.COL_SELECTED)
        if widget is None:
            return None
        return widget.findChild(QCheckBox)

    def _on_selected_changed(self, row: int) -> None:
        if row < 0 or row >= len(self.jobs):
            return
        checkbox = self._row_checkbox(row)
        self.jobs[row].selected = bool(checkbox and checkbox.isChecked())

    def _on_filename_edited(self, row: int, text: str) -> None:
        if row < 0 or row >= len(self.jobs):
            return
        base = text.strip()
        if not base:
            base = self.jobs[row].video_path.stem
        self.jobs[row].pending_filename = base
        self._render_row(row)

    def _render_row(self, row: int) -> None:
        if row < 0 or row >= len(self.jobs):
            return
        job = self.jobs[row]
        checkbox = self._row_checkbox(row)
        if checkbox is not None and checkbox.isChecked() != job.selected:
            checkbox.blockSignals(True)
            checkbox.setChecked(job.selected)
            checkbox.blockSignals(False)

        editor = self.table.cellWidget(row, self.COL_FILENAME)
        if isinstance(editor, QLineEdit):
            if not editor.text().strip():
                editor.setText(job.pending_filename or job.video_path.stem)
            editor.setToolTip(str(job.video_path))

        directory_short = job.video_path.parent.name or str(job.video_path.parent)
        values = {
            self.COL_DIRECTORY: directory_short,
            self.COL_SOURCE: "上传" if job.cover_source == CoverSource.UPLOAD else "自动候选",
            self.COL_HAS_COVER: "-" if job.detected_has_cover is None else ("是" if job.detected_has_cover else "否"),
            self.COL_STATUS: job.status.value,
            self.COL_STRATEGY: job.strategy_result,
            self.COL_ERROR: job.error_message,
            self.COL_MINUTE: f"{job.minute_index} 分钟段",
        }
        for col, text in values.items():
            item = self.table.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, col, item)
            item.setText(text)
            if col == self.COL_DIRECTORY:
                item.setToolTip(str(job.video_path.parent))
            else:
                item.setToolTip("")

    def _start_worker(self, worker: CallableWorker, priority: int = 0) -> None:
        self.active_workers.add(worker)
        worker.signals.finished.connect(lambda w=worker: self.active_workers.discard(w))
        self.pool.start(worker, priority=priority)

    def _load_image_preview(self, image_path: Path, width: int, height: int) -> tuple[QPixmap | None, str]:
        if not image_path.exists():
            return None, "封面文件不存在"
        reader = QImageReader(str(image_path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            reason = reader.errorString() or "Qt 图片解码失败"
            return None, reason
        pix = QPixmap.fromImage(image)
        if pix.isNull():
            return None, "Qt 像素图构建失败"
        return pix.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation), ""

    def _update_cover_preview(self, row: int) -> None:
        if row < 0 or row >= len(self.jobs):
            return
        label = QLabel("未选封面")
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumSize(QSize(150, 84))
        label.setStyleSheet("border: 1px solid #d9e0ea; border-radius: 6px; color: #4c5d73; background: #f8fbff;")
        cover_path = self.jobs[row].cover_path
        if cover_path and Path(cover_path).exists():
            pix, reason = self._load_image_preview(Path(cover_path), width=150, height=84)
            if pix is not None:
                label.setPixmap(pix)
            else:
                label.setText("预览失败")
                label.setToolTip(f"{cover_path}\n{reason}")
                job = self.jobs[row]
                if not job.error_message:
                    job.error_message = f"封面已生成，但本机预览失败：{reason}"
                    self._render_row(row)
        self.table.setCellWidget(row, self.COL_COVER, label)

    def _schedule_probe(self, job_index: int, priority: int = 0) -> None:
        if job_index in self.probe_inflight:
            return
        self.probe_inflight.add(job_index)

        def fn() -> ProbeTaskResult:
            result = probe_video(self.jobs[job_index].video_path, self.bins)
            return ProbeTaskResult(job_index=job_index, result=result)

        worker = CallableWorker(fn)
        worker.signals.done.connect(self._on_probe_done)
        worker.signals.failed.connect(lambda msg, idx=job_index: self._on_probe_failed(idx, msg))
        self._start_worker(worker, priority=priority)

    def _on_probe_done(self, payload: object) -> None:
        if not isinstance(payload, ProbeTaskResult):
            return
        idx = payload.job_index
        self.probe_inflight.discard(idx)
        if idx < 0 or idx >= len(self.jobs):
            return
        self.probes[idx] = payload.result
        self.jobs[idx].detected_has_cover = payload.result.has_attached_pic
        self._render_row(idx)
        self._schedule_sample(idx, self.jobs[idx].minute_index, priority=0)
        if idx == self._current_index():
            self._refresh_detail(idx)

    def _on_probe_failed(self, idx: int, msg: str) -> None:
        self.probe_inflight.discard(idx)
        if idx < 0 or idx >= len(self.jobs):
            return
        self.jobs[idx].status = JobStatus.FAILED
        self.jobs[idx].error_message = f"探测失败: {msg}"
        self._render_row(idx)

    def _sample_key(self, job_index: int, minute_index: int) -> tuple[str, int]:
        video = str(self.jobs[job_index].video_path.resolve())
        return (video, minute_index)

    def _schedule_sample(self, job_index: int, minute_index: int, priority: int = 0) -> None:
        if job_index not in self.probes:
            self._schedule_probe(job_index, priority=priority)
            return
        key = self._sample_key(job_index, minute_index)
        if key in self.sample_cache or key in self.sample_inflight:
            return
        self.sample_inflight.add(key)

        def fn() -> SampleTaskResult:
            request = SampleRequest(video_path=self.jobs[job_index].video_path, minute_index=minute_index, sample_count=12)
            result = sample_minute(request, self.probes[job_index].duration, self.bins)
            return SampleTaskResult(job_index=job_index, minute_index=minute_index, result=result)

        worker = CallableWorker(fn)
        worker.signals.done.connect(self._on_sample_done)
        worker.signals.failed.connect(lambda msg, k=key: self._on_sample_failed(k, msg))
        self._start_worker(worker, priority=priority)

    def _on_sample_done(self, payload: object) -> None:
        if not isinstance(payload, SampleTaskResult):
            return
        key = self._sample_key(payload.job_index, payload.minute_index)
        self.sample_inflight.discard(key)
        self.sample_cache[key] = payload.result

        job = self.jobs[payload.job_index]
        if (
            job.cover_source == CoverSource.SAMPLED
            and job.minute_index == payload.minute_index
            and job.selected_sample_id is None
            and payload.result.thumbnail_paths
        ):
            job.selected_sample_id = 0
            job.cover_path = payload.result.thumbnail_paths[0]
            self._update_cover_preview(payload.job_index)

        if payload.job_index == self._current_index() and job.minute_index == payload.minute_index:
            self._refresh_samples(payload.job_index, payload.minute_index)

    def _on_sample_failed(self, key: tuple[str, int], msg: str) -> None:
        self.sample_inflight.discard(key)
        target_video = key[0]
        for idx, job in enumerate(self.jobs):
            if str(job.video_path.resolve()) != target_video:
                continue
            job.error_message = f"抽帧失败: {msg}"
            self._render_row(idx)
            break

    def _on_selection_change(self) -> None:
        idx = self._current_index()
        if idx < 0:
            return
        self._refresh_detail(idx)
        self._schedule_sample(idx, self.jobs[idx].minute_index, priority=10)
        self._refresh_samples(idx, self.jobs[idx].minute_index)

    def _refresh_detail(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.jobs):
            return
        job = self.jobs[idx]
        self.lbl_video.setText(f"{job.video_path.name}   ({job.video_path.parent})")
        probe = self.probes.get(idx)
        if probe:
            self.lbl_probe.setText(
                f"探测信息：{probe.format_name} | {probe.width}x{probe.height} | "
                f"时长 {probe.duration:.2f}s | 已有封面: {'是' if probe.has_attached_pic else '否'}"
            )
        else:
            self.lbl_probe.setText("探测信息：加载中...")

    def _refresh_samples(self, idx: int, minute_index: int) -> None:
        self.grid.clear()
        key = self._sample_key(idx, minute_index)
        result = self.sample_cache.get(key)
        if not result:
            self.lbl_window.setText(f"抽帧窗口：{minute_index} 分钟段生成中...")
            return
        window_text = (
            f"抽帧窗口：{result.window_start:.1f}s - {result.window_end:.1f}s "
            f"{'(末尾窗口)' if result.is_tail_window else ''}"
        )
        self.lbl_window.setText(window_text)
        preview_failed = 0
        for i, image_path in enumerate(result.thumbnail_paths):
            item = QListWidgetItem(f"{result.time_points[i]:.2f}s")
            pix, _reason = self._load_image_preview(Path(image_path), width=220, height=130)
            if pix is not None:
                item.setIcon(QIcon(pix))
            else:
                preview_failed += 1
            item.setData(Qt.UserRole, i)
            self.grid.addItem(item)
        if preview_failed > 0:
            self.lbl_window.setText(f"{window_text} | 预览异常 {preview_failed}/{len(result.thumbnail_paths)}")
            job = self.jobs[idx]
            if not job.error_message:
                job.error_message = "抽帧文件已生成，但本机预览失败（可能是系统/Qt 图片解码插件问题）"
                self._render_row(idx)
        selected = self.jobs[idx].selected_sample_id
        if selected is not None and 0 <= selected < self.grid.count():
            self.grid.setCurrentRow(selected)

    def _on_upload_cover(self) -> None:
        idx = self._current_index()
        if idx < 0:
            return
        file_path, _ = QFileDialog.getOpenFileName(self, "选择封面图", filter="Image Files (*.jpg *.jpeg *.png *.webp)")
        if not file_path:
            return
        job = self.jobs[idx]
        job.cover_source = CoverSource.UPLOAD
        job.cover_path = Path(file_path)
        job.selected_sample_id = None
        job.error_message = ""
        self._update_cover_preview(idx)
        self._render_row(idx)

    def _on_sample_selected(self, item: QListWidgetItem) -> None:
        idx = self._current_index()
        if idx < 0:
            return
        sample_id = int(item.data(Qt.UserRole))
        key = self._sample_key(idx, self.jobs[idx].minute_index)
        result = self.sample_cache.get(key)
        if not result:
            return
        if sample_id < 0 or sample_id >= len(result.thumbnail_paths):
            return
        job = self.jobs[idx]
        job.cover_source = CoverSource.SAMPLED
        job.selected_sample_id = sample_id
        job.cover_path = result.thumbnail_paths[sample_id]
        job.error_message = ""
        self._update_cover_preview(idx)
        self._render_row(idx)

    def _on_next_minute(self) -> None:
        idx = self._current_index()
        if idx < 0:
            return
        probe = self.probes.get(idx)
        if probe is None:
            self._schedule_probe(idx, priority=10)
            return
        job = self.jobs[idx]
        decision = decide_window(probe.duration, job.minute_index)
        job.minute_index = decision.next_minute_index
        job.cover_source = CoverSource.SAMPLED
        job.selected_sample_id = None
        job.cover_path = None
        self._update_cover_preview(idx)
        self._render_row(idx)
        self._schedule_sample(idx, job.minute_index, priority=20)
        self._refresh_samples(idx, job.minute_index)

    def _run_candidates(self, only_selected: bool) -> list[int]:
        if only_selected:
            return [idx for idx, job in enumerate(self.jobs) if job.selected]
        return list(range(len(self.jobs)))

    def _on_run_selected(self) -> None:
        indices = self._run_candidates(only_selected=True)
        if not indices:
            QMessageBox.information(self, "未选择对象", "请先勾选至少一个条目。")
            return
        self._start_processing(indices)

    def _on_run_all(self) -> None:
        indices = self._run_candidates(only_selected=False)
        self._start_processing(indices)

    def _resolve_final_name(self, job: VideoJob) -> tuple[Path, str]:
        base = (job.pending_filename or job.video_path.stem).strip()
        if not base:
            base = job.video_path.stem
        ext = job.video_path.suffix
        parent = job.video_path.parent
        candidate = parent / f"{base}{ext}"
        if candidate.resolve() == job.video_path.resolve():
            return candidate, base
        seq = 1
        while candidate.exists():
            candidate = parent / f"{base}({seq}){ext}"
            seq += 1
        final_base = candidate.stem
        return candidate, final_base

    def _apply_rename_if_needed(self, idx: int, old_path: Path) -> Path | None:
        if idx < 0 or idx >= len(self.jobs):
            return None
        job = self.jobs[idx]
        target, final_base = self._resolve_final_name(job)
        if target.resolve() == old_path.resolve():
            job.applied_filename = old_path.name
            job.pending_filename = final_base
            return None
        old_key = str(old_path.resolve())
        old_path.rename(target)
        new_key = str(target.resolve())
        moved: dict[tuple[str, int], SampleResult] = {}
        for (video_key, minute_idx), value in list(self.sample_cache.items()):
            if video_key != old_key:
                continue
            moved[(new_key, minute_idx)] = value
            del self.sample_cache[(video_key, minute_idx)]
        self.sample_cache.update(moved)
        job.video_path = target
        job.applied_filename = target.name
        job.pending_filename = final_base
        return target

    def _ensure_cover_selected(self, idx: int) -> bool:
        if idx < 0 or idx >= len(self.jobs):
            return False
        job = self.jobs[idx]
        if job.cover_path and Path(job.cover_path).exists():
            return True

        probe = self.probes.get(idx)
        if probe is None:
            try:
                probe = probe_video(job.video_path, self.bins)
                self.probes[idx] = probe
            except Exception as err:  # noqa: BLE001
                job.error_message = f"探测失败: {err}"
                return False
        key = self._sample_key(idx, job.minute_index)
        result = self.sample_cache.get(key)
        if result is None:
            try:
                result = sample_minute(
                    SampleRequest(video_path=job.video_path, minute_index=job.minute_index, sample_count=12),
                    probe.duration,
                    self.bins,
                )
            except Exception as err:  # noqa: BLE001
                job.error_message = f"抽帧失败: {err}"
                return False
            self.sample_cache[key] = result

        if not result.thumbnail_paths:
            job.error_message = "未生成可用候选封面"
            return False
        job.cover_source = CoverSource.SAMPLED
        job.selected_sample_id = 0
        job.cover_path = result.thumbnail_paths[0]
        self._update_cover_preview(idx)
        if idx == self._current_index():
            self._refresh_samples(idx, job.minute_index)
        return True

    def _summarize_error(self, text: str, limit: int = 240) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "执行失败"
        keys = ("error", "failed", "invalid", "could not", "unable")
        focused = [line for line in lines if any(key in line.lower() for key in keys)]
        target = focused[-1] if focused else lines[-1]
        return target[:limit]

    def _build_attempt_note(self, attempt_trace: list[str]) -> str:
        if not attempt_trace:
            return ""
        return " -> ".join(attempt_trace)

    def _start_processing(self, indices: list[int]) -> None:
        options = self._current_options()
        if not options.use_metadata and not options.use_first_frame:
            QMessageBox.warning(self, "执行策略为空", "请至少勾选一种执行策略。")
            return
        filtered: list[int] = []
        for idx in indices:
            if idx < 0 or idx >= len(self.jobs):
                continue
            job = self.jobs[idx]
            if not self._ensure_cover_selected(idx):
                job.status = JobStatus.FAILED
                if not job.error_message:
                    job.error_message = "未选择封面图"
                self._render_row(idx)
                continue
            job.pending_filename = job.pending_filename.strip() or job.video_path.stem
            editor = self.table.cellWidget(idx, self.COL_FILENAME)
            if isinstance(editor, QLineEdit):
                editor.setText(job.pending_filename)
            job.error_message = ""
            job.strategy_result = ""
            job.status = JobStatus.IDLE
            self._render_row(idx)
            filtered.append(idx)
        if not filtered:
            return
        if self.processing_now:
            QMessageBox.information(self, "处理中", "当前已有任务在执行。")
            return
        self.current_run_options = options
        self.processing_queue = filtered
        self.processing_now = True
        self._run_next()

    def _run_next(self) -> None:
        if not self.processing_queue:
            self.processing_now = False
            self.current_processing_index = None
            QMessageBox.information(self, "完成", "任务执行完成。")
            return
        idx = self.processing_queue.pop(0)
        self.current_processing_index = idx
        job = self.jobs[idx]
        job.status = JobStatus.RUNNING
        self._render_row(idx)
        stream_logs = self.chk_cmd_logs.isChecked()

        def fn() -> ProcessTaskResult:
            probe = self.probes.get(idx) or probe_video(job.video_path, self.bins)
            cover_path = Path(job.cover_path) if job.cover_path else Path()
            options = self.current_run_options
            verbosity = options.log_verbosity.value

            if options.use_metadata:
                meta_result = process_in_place(
                    job.video_path,
                    cover_path,
                    CoverMode.METADATA,
                    self.bins,
                    probe,
                    stream_logs=stream_logs,
                    log_verbosity=verbosity,
                )
                meta_attempt_note = self._build_attempt_note(meta_result.attempt_trace)
                if meta_result.exit_code == 0:
                    strategy = "元数据成功"
                    if meta_attempt_note:
                        strategy = f"{strategy} [{meta_attempt_note}]"
                    return ProcessTaskResult(
                        job_index=idx,
                        final_status=JobStatus.SUCCESS,
                        strategy_result=strategy,
                        error="",
                        old_path=job.video_path,
                    )

                meta_error = self._summarize_error(meta_result.stderr_log or meta_result.warning or "元数据写入失败")
                if options.metadata_failure_action == MetadataFailureAction.FIRST_FRAME:
                    first_result = process_in_place(
                        job.video_path,
                        cover_path,
                        CoverMode.FIRST_FRAME,
                        self.bins,
                        probe,
                        stream_logs=stream_logs,
                        log_verbosity=verbosity,
                    )
                    if first_result.exit_code == 0:
                        encoder_name = first_result.selected_encoder or "libx264"
                        first_attempt_note = self._build_attempt_note(first_result.attempt_trace)
                        strategy = f"元数据失败，首帧成功({encoder_name})"
                        if meta_attempt_note:
                            strategy = f"{strategy} [meta:{meta_attempt_note}]"
                        if first_attempt_note:
                            strategy = f"{strategy} [first:{first_attempt_note}]"
                        return ProcessTaskResult(
                            job_index=idx,
                            final_status=JobStatus.SUCCESS,
                            strategy_result=strategy,
                            error="",
                            old_path=job.video_path,
                        )
                    first_error = self._summarize_error(first_result.stderr_log or "首帧重编码失败")
                    strategy = "元数据失败，首帧重编码失败"
                    if meta_attempt_note:
                        strategy = f"{strategy} [meta:{meta_attempt_note}]"
                    return ProcessTaskResult(
                        job_index=idx,
                        final_status=JobStatus.FAILED,
                        strategy_result=strategy,
                        error=f"{meta_error} | {first_error}"[:240],
                        old_path=job.video_path,
                    )

                strategy = "元数据失败，按策略跳过"
                if meta_attempt_note:
                    strategy = f"{strategy} [{meta_attempt_note}]"
                return ProcessTaskResult(
                    job_index=idx,
                    final_status=JobStatus.SKIPPED,
                    strategy_result=strategy,
                    error=meta_error,
                    old_path=job.video_path,
                )

            if options.use_first_frame:
                first_result = process_in_place(
                    job.video_path,
                    cover_path,
                    CoverMode.FIRST_FRAME,
                    self.bins,
                    probe,
                    stream_logs=stream_logs,
                    log_verbosity=verbosity,
                )
                first_attempt_note = self._build_attempt_note(first_result.attempt_trace)
                if first_result.exit_code == 0:
                    encoder_name = first_result.selected_encoder or "libx264"
                    strategy = f"首帧成功({encoder_name})"
                    if first_attempt_note:
                        strategy = f"{strategy} [{first_attempt_note}]"
                    return ProcessTaskResult(
                        job_index=idx,
                        final_status=JobStatus.SUCCESS,
                        strategy_result=strategy,
                        error="",
                        old_path=job.video_path,
                    )
                return ProcessTaskResult(
                    job_index=idx,
                    final_status=JobStatus.FAILED,
                    strategy_result="首帧失败",
                    error=self._summarize_error(first_result.stderr_log or "首帧执行失败"),
                    old_path=job.video_path,
                )

            return ProcessTaskResult(
                job_index=idx,
                final_status=JobStatus.FAILED,
                strategy_result="执行失败",
                error="未选择执行策略",
                old_path=job.video_path,
            )

        worker = CallableWorker(fn)
        worker.signals.done.connect(self._on_process_done)
        worker.signals.failed.connect(self._on_process_failed)
        self._start_worker(worker, priority=20)

    def _on_process_done(self, payload: object) -> None:
        if not isinstance(payload, ProcessTaskResult):
            return
        idx = payload.job_index
        if idx < 0 or idx >= len(self.jobs):
            self._run_next()
            return
        job = self.jobs[idx]
        job.status = payload.final_status
        job.strategy_result = payload.strategy_result
        job.error_message = payload.error
        if payload.final_status == JobStatus.SUCCESS:
            try:
                renamed = self._apply_rename_if_needed(idx, payload.old_path)
                if renamed is not None:
                    payload.renamed_path = renamed
            except Exception as err:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error_message = f"改名失败: {err}"
        self._update_cover_preview(idx)
        self._render_row(idx)
        if idx == self._current_index():
            self._refresh_detail(idx)
        self._run_next()

    def _on_process_failed(self, msg: str) -> None:
        idx = self.current_processing_index
        if idx is not None and 0 <= idx < len(self.jobs):
            self.jobs[idx].status = JobStatus.FAILED
            self.jobs[idx].strategy_result = "执行异常"
            self.jobs[idx].error_message = msg[:240]
            self._render_row(idx)
        self._run_next()


def launch() -> int:
    try:
        bins = locate_binaries()
    except FfmpegError as err:
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "依赖缺失", str(err))
        return 2

    app = QApplication.instance() or QApplication([])
    win = MainWindow(bins)
    win.show()
    return app.exec()
