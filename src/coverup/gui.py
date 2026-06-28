from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
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
    QSpinBox,
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
from .probe import extract_attached_cover_preview, probe_video
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
    original_cover_path: Path | None = None


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
    COL_ORIGINAL_COVER = 1
    COL_MODIFIED_COVER = 2
    COL_FILENAME = 3
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
        self.processing_inflight: set[int] = set()
        self.active_workers: set[CallableWorker] = set()
        self._refreshing_grid = False

        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        main_layout = QVBoxLayout(root)
        self.setStyleSheet("""
            /* ── Global ── */
            QMainWindow { background: #F5F5F7; }
            QLabel { color: #1D1D1F; font-size: 13px; }

            /* ── Table ── */
            QTableWidget {
                background: #FFFFFF;
                border: 1px solid #E5E5EA;
                border-radius: 10px;
                gridline-color: transparent;
                alternate-background-color: #FAFAFA;
                font-size: 13px;
            }
            QTableWidget::item {
                padding: 6px 10px;
                color: #1D1D1F;
            }
            QHeaderView::section {
                background: #FAFAFA;
                color: #6E6E73;
                border: none;
                border-bottom: 1px solid #E5E5EA;
                border-right: none;
                padding: 10px 10px;
                font-weight: 600;
                font-size: 12px;
            }
            QHeaderView::section:vertical {
                background: #FAFAFA;
                color: #8E8E93;
                border: none;
                border-bottom: 1px solid #F2F2F7;
                padding: 4px;
                font-size: 11px;
            }

            /* ── Buttons (secondary / default) ── */
            QPushButton {
                background: #F2F2F7;
                color: #1D1D1F;
                border: 1px solid #E5E5EA;
                border-radius: 8px;
                padding: 8px 16px;
                font-weight: 500;
                font-size: 13px;
            }
            QPushButton:hover { background: #E5E5EA; }
            QPushButton:pressed { background: #DCDCE0; }
            QPushButton:disabled {
                background: #F2F2F7;
                color: #C7C7CC;
            }

            /* ── Primary action buttons ── */
            QPushButton#btnRunSelected, QPushButton#btnRunAll {
                background: #007AFF;
                color: white;
                border: none;
                padding: 8px 20px;
                font-weight: 600;
            }
            QPushButton#btnRunSelected:hover, QPushButton#btnRunAll:hover { background: #0066D6; }
            QPushButton#btnRunSelected:pressed, QPushButton#btnRunAll:pressed { background: #0055B3; }

            /* ── Input fields ── */
            QLineEdit {
                border: 1px solid #E5E5EA;
                border-radius: 8px;
                padding: 7px 10px;
                background: #FFFFFF;
                color: #1D1D1F;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #007AFF; }

            /* ── Combo boxes ── */
            QComboBox {
                border: 1px solid #E5E5EA;
                border-radius: 8px;
                padding: 6px 10px;
                background: #FFFFFF;
                color: #1D1D1F;
                font-size: 13px;
                min-width: 100px;
            }
            QComboBox:hover { border-color: #C7C7CC; }
            QComboBox::drop-down { border: none; width: 22px; }
            QComboBox QAbstractItemView {
                background: #FFFFFF;
                border: 1px solid #E5E5EA;
                border-radius: 6px;
                selection-background-color: #007AFF;
                selection-color: white;
                padding: 4px;
            }

            /* ── Spin box ── */
            QSpinBox {
                border: 1px solid #E5E5EA;
                border-radius: 8px;
                padding: 6px 8px;
                background: #FFFFFF;
                color: #1D1D1F;
                font-size: 13px;
            }
            QSpinBox:focus { border-color: #007AFF; }

            /* ── Checkboxes ── */
            QCheckBox {
                color: #1D1D1F;
                font-size: 13px;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #C7C7CC;
                border-radius: 5px;
                background: #FFFFFF;
            }
            QCheckBox::indicator:checked {
                background: #007AFF;
                border-color: #007AFF;
            }

            /* ── Group boxes (cards) ── */
            QGroupBox {
                background: #FFFFFF;
                border: 1px solid #E5E5EA;
                border-radius: 12px;
                margin-top: 16px;
                padding: 20px 16px 16px 16px;
                font-weight: 600;
                font-size: 14px;
                color: #1D1D1F;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 16px;
                padding: 0 8px;
            }

            /* ── List widget (sample grid) ── */
            QListWidget {
                background: #FFFFFF;
                border: 1px solid #E5E5EA;
                border-radius: 12px;
                padding: 10px;
                outline: none;
            }
            QListWidget::item {
                border-radius: 8px;
                padding: 6px;
                margin: 4px;
                background: #FAFAFA;
                border: 2px solid transparent;
            }
            QListWidget::item:selected {
                background: #E8F2FF;
                border: 2px solid #007AFF;
            }
            QListWidget::item:hover:!selected {
                background: #F2F2F7;
                border: 2px solid #E5E5EA;
            }

            /* ── Scroll bars ── */
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #C7C7CC;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover { background: #AEAEB2; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal {
                background: transparent;
                height: 8px;
            }
            QScrollBar::handle:horizontal {
                background: #C7C7CC;
                border-radius: 4px;
                min-width: 30px;
            }
            QScrollBar::handle:horizontal:hover { background: #AEAEB2; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

            /* ── Toolbar card ── */
            QFrame#toolbarCard {
                background: #FFFFFF;
                border: 1px solid #E5E5EA;
                border-radius: 12px;
            }

            /* ── Section header labels ── */
            QLabel#sectionHeader {
                color: #6E6E73;
                font-weight: 600;
                font-size: 11px;
            }

            /* ── Tool tips ── */
            QToolTip {
                background: #1D1D1F;
                color: #FFFFFF;
                border: none;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 12px;
            }
        """)

        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(16)

        # ── Toolbar card ──
        toolbar = QFrame()
        toolbar.setObjectName("toolbarCard")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(16, 12, 16, 12)
        toolbar_layout.setSpacing(10)

        # Group 1: File operations
        self.btn_add_files = QPushButton("添加视频")
        self.btn_add_dir = QPushButton("打开目录")
        self.chk_recursive = QCheckBox("递归子目录")
        self.chk_recursive.setChecked(True)
        toolbar_layout.addWidget(self.btn_add_files)
        toolbar_layout.addWidget(self.btn_add_dir)
        toolbar_layout.addWidget(self.chk_recursive)

        # Separator 1
        sep1 = QWidget()
        sep1.setFixedWidth(1)
        sep1.setStyleSheet("background: #E5E5EA;")
        toolbar_layout.addWidget(sep1)

        # Group 2: Execution strategy
        self.chk_mode_metadata = QCheckBox("元数据封面")
        self.chk_mode_metadata.setChecked(True)
        self.chk_mode_first_frame = QCheckBox("替换首帧")
        self.cmb_metadata_failure_action = QComboBox()
        self.cmb_metadata_failure_action.addItem("失败后跳过", MetadataFailureAction.SKIP.value)
        self.cmb_metadata_failure_action.addItem("失败后首帧重编码", MetadataFailureAction.FIRST_FRAME.value)
        self.cmb_metadata_failure_action.addItem("直接首帧", MetadataFailureAction.DIRECT_FIRST_FRAME.value)
        self.cmb_metadata_failure_action.setCurrentIndex(0)
        lbl_strategy = QLabel("执行策略")
        lbl_strategy.setObjectName("sectionHeader")
        toolbar_layout.addWidget(lbl_strategy)
        toolbar_layout.addWidget(self.chk_mode_metadata)
        toolbar_layout.addWidget(self.chk_mode_first_frame)
        lbl_fallback = QLabel("元数据失败")
        lbl_fallback.setObjectName("sectionHeader")
        toolbar_layout.addWidget(lbl_fallback)
        toolbar_layout.addWidget(self.cmb_metadata_failure_action)

        # Separator 2
        sep2 = QWidget()
        sep2.setFixedWidth(1)
        sep2.setStyleSheet("background: #E5E5EA;")
        toolbar_layout.addWidget(sep2)

        # Group 3: Logging & concurrency
        self.cmb_log_verbosity = QComboBox()
        self.cmb_log_verbosity.addItem("紧凑日志", LogVerbosity.COMPACT.value)
        self.cmb_log_verbosity.addItem("中等日志", LogVerbosity.MEDIUM.value)
        self.cmb_log_verbosity.addItem("原始日志", LogVerbosity.RAW.value)
        self.cmb_log_verbosity.setCurrentIndex(1)
        self.cmb_log_verbosity.setToolTip("命令行日志详细程度")
        self.chk_cmd_logs = QCheckBox("显示命令行")
        self.chk_cmd_logs.setChecked(True)
        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 4)
        self.spin_concurrency.setValue(2)
        self.spin_concurrency.setToolTip("同时处理的文件数")
        self.spin_concurrency.setFixedWidth(56)
        lbl_log = QLabel("日志级别")
        lbl_log.setObjectName("sectionHeader")
        toolbar_layout.addWidget(lbl_log)
        toolbar_layout.addWidget(self.cmb_log_verbosity)
        toolbar_layout.addWidget(self.chk_cmd_logs)
        lbl_conc = QLabel("并发数")
        lbl_conc.setObjectName("sectionHeader")
        toolbar_layout.addWidget(lbl_conc)
        toolbar_layout.addWidget(self.spin_concurrency)

        toolbar_layout.addStretch(1)

        # Group 4: Primary action buttons
        self.btn_run_selected = QPushButton("执行选择")
        self.btn_run_selected.setObjectName("btnRunSelected")
        self.btn_run_all = QPushButton("执行全部")
        self.btn_run_all.setObjectName("btnRunAll")
        toolbar_layout.addWidget(self.btn_run_selected)
        toolbar_layout.addWidget(self.btn_run_all)

        main_layout.addWidget(toolbar)

        # ── Main splitter ──
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        # ── Left panel: video table ──
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["选择", "当前封面", "修改后封面", "文件名", "封面来源", "已有元数据", "状态", "策略结果", "错误信息", "分钟窗口"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(True)
        self.table.verticalHeader().setDefaultSectionSize(100)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(self.COL_SELECTED, QHeaderView.Fixed)
        self.table.setColumnWidth(self.COL_SELECTED, 62)
        self.table.horizontalHeader().setSectionResizeMode(self.COL_ERROR, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(self.COL_FILENAME, QHeaderView.Stretch)
        left_layout.addWidget(self.table)
        splitter.addWidget(left)

        # ── Right panel ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        # Video info card
        info_box = QGroupBox("当前视频")
        info_layout = QVBoxLayout(info_box)
        info_layout.setSpacing(8)
        self.lbl_video = QLabel("未选择视频")
        self.lbl_video.setStyleSheet("font-weight: 600; font-size: 14px;")
        self.lbl_probe = QLabel("探测信息：—")
        self.lbl_probe.setStyleSheet("color: #6E6E73; font-size: 12px;")
        self.lbl_window = QLabel("抽帧窗口：—")
        self.lbl_window.setStyleSheet("color: #6E6E73; font-size: 12px;")
        info_layout.addWidget(self.lbl_video)
        info_layout.addWidget(self.lbl_probe)
        info_layout.addWidget(self.lbl_window)
        right_layout.addWidget(info_box)

        # Action buttons row
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.btn_upload_cover = QPushButton("上传封面图")
        self.btn_next_minute = QPushButton("下一分钟")
        self.btn_delete_video = QPushButton("删除当前视频")
        action_row.addWidget(self.btn_upload_cover)
        action_row.addWidget(self.btn_next_minute)
        action_row.addWidget(self.btn_delete_video)
        right_layout.addLayout(action_row)

        # Sample thumbnail grid
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(220, 130))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setSpacing(10)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        right_layout.addWidget(self.grid, 1)

        # Help card
        help_box = QGroupBox("使用说明")
        help_layout = QVBoxLayout(help_box)
        help_layout.setSpacing(6)
        help_items = [
            "默认每分钟均匀抽取 12 张候选帧，首分钟从 0~60 秒区间开始",
            "末尾不足 1 分钟时在剩余时长内仍然抽取 12 张",
            "点击「下一分钟」循环切换抽取区间，选择后自动设为封面",
        ]
        for item_text in help_items:
            lbl = QLabel(f"• {item_text}")
            lbl.setStyleSheet("color: #6E6E73; font-size: 12px;")
            lbl.setWordWrap(True)
            help_layout.addWidget(lbl)
        right_layout.addWidget(help_box)

        splitter.addWidget(right)
        splitter.setSizes([900, 520])

        self.setCentralWidget(root)

        # ── Signal connections ──
        self.btn_add_files.clicked.connect(self._on_add_files)
        self.btn_add_dir.clicked.connect(self._on_add_dir)
        self.table.itemSelectionChanged.connect(self._on_selection_change)
        self.btn_upload_cover.clicked.connect(self._on_upload_cover)
        self.btn_next_minute.clicked.connect(self._on_next_minute)
        self.btn_delete_video.clicked.connect(self._on_delete_video)
        self.grid.itemClicked.connect(self._on_sample_selected)
        self.grid.currentItemChanged.connect(self._on_grid_current_changed)
        self.btn_run_all.clicked.connect(self._on_run_all)
        self.btn_run_selected.clicked.connect(self._on_run_selected)

    def _current_options(self) -> RunOptions:
        action_value = self.cmb_metadata_failure_action.currentData() or MetadataFailureAction.SKIP.value
        verbosity_value = self._current_log_verbosity()
        if action_value == MetadataFailureAction.DIRECT_FIRST_FRAME.value:
            return RunOptions(
                use_metadata=False,
                use_first_frame=True,
                metadata_failure_action=MetadataFailureAction(action_value),
                log_verbosity=LogVerbosity(verbosity_value),
            )
        return RunOptions(
            use_metadata=self.chk_mode_metadata.isChecked(),
            use_first_frame=self.chk_mode_first_frame.isChecked(),
            metadata_failure_action=MetadataFailureAction(action_value),
            log_verbosity=LogVerbosity(verbosity_value),
        )

    def _current_log_verbosity(self) -> str:
        return self.cmb_log_verbosity.currentData() or LogVerbosity.MEDIUM.value

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
            self._update_cover_previews(row)
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

        # Status color mapping (system semantic colors)
        _status_color = {
            JobStatus.SUCCESS.value: "#34C759",
            JobStatus.FAILED.value: "#FF3B30",
            JobStatus.SKIPPED.value: "#FF9500",
            JobStatus.RUNNING.value: "#007AFF",
            JobStatus.IDLE.value: "#8E8E93",
        }

        values = {
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
            item.setToolTip("")

        # Apply color coding to status column
        status_item = self.table.item(row, self.COL_STATUS)
        if status_item:
            color = _status_color.get(job.status.value, "#8E8E93")
            status_item.setForeground(QColor(color))
            if job.status == JobStatus.RUNNING:
                font = status_item.font()
                font.setBold(True)
                status_item.setFont(font)

        # Color code "has cover" column
        cover_item = self.table.item(row, self.COL_HAS_COVER)
        if cover_item and job.detected_has_cover is not None:
            cover_item.setForeground(QColor("#34C759" if job.detected_has_cover else "#8E8E93"))

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

    def _build_cover_label(self, empty_text: str) -> QLabel:
        label = QLabel(empty_text)
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumSize(QSize(160, 90))
        label.setStyleSheet(
            "border: 1px solid #E5E5EA; border-radius: 8px; color: #8E8E93; background: #FAFAFA; font-size: 12px;"
        )
        return label

    def _update_cover_cell(self, row: int, col: int, cover_path: Path | None, empty_text: str) -> None:
        if row < 0 or row >= len(self.jobs):
            return
        label = self._build_cover_label(empty_text)
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
        self.table.setCellWidget(row, col, label)

    def _update_original_cover_preview(self, row: int) -> None:
        if row < 0 or row >= len(self.jobs):
            return
        self._update_cover_cell(row, self.COL_ORIGINAL_COVER, self.jobs[row].original_cover_path, "未加载封面")

    def _update_modified_cover_preview(self, row: int) -> None:
        if row < 0 or row >= len(self.jobs):
            return
        self._update_cover_cell(row, self.COL_MODIFIED_COVER, self.jobs[row].cover_path, "未选封面")

    def _update_cover_previews(self, row: int) -> None:
        self._update_original_cover_preview(row)
        self._update_modified_cover_preview(row)

    def _schedule_probe(self, job_index: int, priority: int = 0) -> None:
        if job_index in self.probe_inflight:
            return
        self.probe_inflight.add(job_index)
        stream_logs = self.chk_cmd_logs.isChecked()
        verbosity = self._current_log_verbosity()

        def fn() -> ProbeTaskResult:
            result = probe_video(
                self.jobs[job_index].video_path,
                self.bins,
                stream_logs=stream_logs,
                log_verbosity=verbosity,
            )
            original_cover_path = extract_attached_cover_preview(
                self.jobs[job_index].video_path,
                result,
                self.bins,
                stream_logs=stream_logs,
                log_verbosity=verbosity,
            )
            return ProbeTaskResult(job_index=job_index, result=result, original_cover_path=original_cover_path)

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
        if payload.original_cover_path is not None:
            self.jobs[idx].original_cover_path = payload.original_cover_path
            self._update_original_cover_preview(idx)
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
        stream_logs = self.chk_cmd_logs.isChecked()
        verbosity = self._current_log_verbosity()

        def fn() -> SampleTaskResult:
            request = SampleRequest(video_path=self.jobs[job_index].video_path, minute_index=minute_index, sample_count=12)
            result = sample_minute(
                request,
                self.probes[job_index].duration,
                self.bins,
                stream_logs=stream_logs,
                log_verbosity=verbosity,
            )
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
        if job.original_cover_path is None and payload.result.thumbnail_paths:
            job.original_cover_path = payload.result.thumbnail_paths[0]
        self._update_cover_previews(payload.job_index)

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

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes >= 1_000_000_000:
            return f"{size_bytes / 1_000_000_000:.2f} GB"
        if size_bytes >= 1_000_000:
            return f"{size_bytes / 1_000_000:.2f} MB"
        if size_bytes >= 1_000:
            return f"{size_bytes / 1_000:.2f} KB"
        return f"{size_bytes} B"

    def _refresh_detail(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.jobs):
            return
        job = self.jobs[idx]
        self.lbl_video.setText(f"{job.video_path.name}   ({job.video_path.parent})")
        try:
            file_size = job.video_path.stat().st_size
            size_str = self._format_size(file_size)
        except OSError:
            size_str = "未知"
        probe = self.probes.get(idx)
        if probe:
            self.lbl_probe.setText(
                f"探测信息：{probe.format_name} | {probe.width}x{probe.height} | "
                f"时长 {probe.duration:.2f}s | 大小 {size_str} | "
                f"已有封面: {'是' if probe.has_attached_pic else '否'}"
            )
        else:
            self.lbl_probe.setText("探测信息：加载中...")

    def _refresh_samples(self, idx: int, minute_index: int) -> None:
        self._refreshing_grid = True
        try:
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
        finally:
            self._refreshing_grid = False

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
        self._update_modified_cover_preview(idx)
        self._render_row(idx)

    def _on_sample_selected(self, item: QListWidgetItem) -> None:
        if self._refreshing_grid:
            return
        self._apply_sample_selection(item)

    def _on_grid_current_changed(self, current: QListWidgetItem, previous: QListWidgetItem) -> None:
        if self._refreshing_grid:
            return
        if current is not None:
            self._apply_sample_selection(current)

    def _apply_sample_selection(self, item: QListWidgetItem) -> None:
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
        self._update_modified_cover_preview(idx)
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
        self._update_modified_cover_preview(idx)
        self._render_row(idx)
        self._schedule_sample(idx, job.minute_index, priority=20)
        self._refresh_samples(idx, job.minute_index)

    def _on_delete_video(self) -> None:
        idx = self._current_index()
        if idx < 0:
            QMessageBox.information(self, "未选择视频", "请先在左侧列表中选择一个视频。")
            return
        job = self.jobs[idx]
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除以下视频吗？\n\n{job.video_path.name}\n{job.video_path.parent}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Remove sample cache entries for this video
        video_key = str(job.video_path.resolve())
        stale_keys = [k for k in self.sample_cache if k[0] == video_key]
        for k in stale_keys:
            self.sample_cache.pop(k, None)
        self.sample_inflight = {k for k in self.sample_inflight if k[0] != video_key}

        # Remove probe data
        self.probes.pop(idx, None)
        # Re-key remaining probe entries (indices shift after removal)
        self.probes = {(k - 1 if k > idx else k): v for k, v in self.probes.items()}

        # Remove from jobs list and table
        del self.jobs[idx]
        self.table.removeRow(idx)

        # If table is now empty, reset detail panel
        if self.table.rowCount() == 0:
            self.lbl_video.setText("未选择视频")
            self.lbl_probe.setText("探测信息：—")
            self.lbl_window.setText("抽帧窗口：—")
            self.grid.clear()
            return

        # Select new row at same position (or last if we deleted the last row)
        new_idx = min(idx, self.table.rowCount() - 1)
        self.table.selectRow(new_idx)

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

        # 对当前正在预览的 job，从 grid 读取用户实际选择的封面
        # 这确保即使用户点击缩略图的信号没有正确触发，也能使用正确的选择
        if idx == self._current_index() and job.cover_source != CoverSource.UPLOAD:
            current_item = self.grid.currentItem()
            if current_item is not None:
                key = self._sample_key(idx, job.minute_index)
                result = self.sample_cache.get(key)
                if result:
                    grid_sample_id = int(current_item.data(Qt.UserRole))
                    if 0 <= grid_sample_id < len(result.thumbnail_paths):
                        grid_cover = result.thumbnail_paths[grid_sample_id]
                        if grid_cover != job.cover_path:
                            job.cover_source = CoverSource.SAMPLED
                            job.selected_sample_id = grid_sample_id
                            job.cover_path = grid_cover

        # Layer 2: reconstruct cover_path from selected_sample_id for ALL rows.
        # selected_sample_id is the authoritative record of user intent —
        # whenever it is set, the correct cover_path is always:
        #   sample_cache[key].thumbnail_paths[selected_sample_id]
        # Unlike Layer 1 (grid-sync), this layer covers non-current rows too,
        # so jobs the user is not currently looking at also get verified.
        if job.cover_source == CoverSource.SAMPLED and job.selected_sample_id is not None:
            key = self._sample_key(idx, job.minute_index)
            result = self.sample_cache.get(key)
            if result is not None and 0 <= job.selected_sample_id < len(result.thumbnail_paths):
                corrected = result.thumbnail_paths[job.selected_sample_id]
                if job.cover_path != corrected:
                    job.cover_path = corrected
            elif result is not None:
                # selected_sample_id out of range — reset to safe default
                job.cover_path = result.thumbnail_paths[0]
                job.selected_sample_id = 0

        if job.cover_path and Path(job.cover_path).exists():
            return True

        probe = self.probes.get(idx)
        if probe is None:
            try:
                probe = probe_video(
                    job.video_path,
                    self.bins,
                    stream_logs=self.chk_cmd_logs.isChecked(),
                    log_verbosity=self._current_log_verbosity(),
                )
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
                    stream_logs=self.chk_cmd_logs.isChecked(),
                    log_verbosity=self._current_log_verbosity(),
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
        if job.original_cover_path is None:
            job.original_cover_path = result.thumbnail_paths[0]
        self._update_cover_previews(idx)
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
        self.processing_inflight.clear()
        self.processing_now = True
        self._run_next()

    def _run_next(self) -> None:
        if not self.processing_queue and not self.processing_inflight:
            self.processing_now = False
            QMessageBox.information(self, "完成", "任务执行完成。")
            return

        concurrency = self.spin_concurrency.value()
        while self.processing_queue and len(self.processing_inflight) < concurrency:
            idx = self.processing_queue.pop(0)
            self.processing_inflight.add(idx)
            job = self.jobs[idx]
            job.status = JobStatus.RUNNING
            self._render_row(idx)
            stream_logs = self.chk_cmd_logs.isChecked()

            def fn(idx: int = idx, job: VideoJob = job) -> ProcessTaskResult:
                cover_path = Path(job.cover_path) if job.cover_path else Path()
                options = self.current_run_options
                verbosity = options.log_verbosity.value
                probe = self.probes.get(idx) or probe_video(
                    job.video_path,
                    self.bins,
                    stream_logs=stream_logs,
                    log_verbosity=verbosity,
                )

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
            worker.signals.failed.connect(lambda msg, i=idx: self._on_process_failed(i, msg))
            self._start_worker(worker, priority=20)

    def _on_process_done(self, payload: object) -> None:
        if not isinstance(payload, ProcessTaskResult):
            return
        idx = payload.job_index
        self.processing_inflight.discard(idx)
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
        self._update_cover_previews(idx)
        self._render_row(idx)
        if idx == self._current_index():
            self._refresh_detail(idx)
        self._run_next()

    def _on_process_failed(self, idx: int, msg: str) -> None:
        self.processing_inflight.discard(idx)
        if 0 <= idx < len(self.jobs):
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
