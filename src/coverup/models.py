from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class CoverMode(str, Enum):
    METADATA = "metadata"
    FIRST_FRAME = "first_frame"


class CoverSource(str, Enum):
    UPLOAD = "upload"
    SAMPLED = "sampled"


class JobStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class MetadataFailureAction(str, Enum):
    SKIP = "skip"
    FIRST_FRAME = "first_frame"


class LogVerbosity(str, Enum):
    COMPACT = "compact"
    MEDIUM = "medium"
    RAW = "raw"


@dataclass(slots=True)
class ProbeResult:
    format_name: str
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    has_attached_pic: bool
    metadata_cover_writable: bool


@dataclass(slots=True)
class SampleRequest:
    video_path: Path
    minute_index: int
    sample_count: int = 12


@dataclass(slots=True)
class SampleResult:
    time_points: list[float]
    thumbnail_paths: list[Path]
    window_start: float
    window_end: float
    is_tail_window: bool


@dataclass(slots=True)
class ScanOptions:
    directory_path: Path
    recursive: bool = True
    deduplicate: bool = True


@dataclass(slots=True)
class VideoJob:
    video_path: Path
    selected: bool = False
    cover_source: CoverSource = CoverSource.SAMPLED
    cover_path: Optional[Path] = None
    selected_sample_id: Optional[int] = None
    minute_index: int = 0
    detected_has_cover: Optional[bool] = None
    pending_filename: str = ""
    applied_filename: Optional[str] = None
    strategy_result: str = ""
    status: JobStatus = JobStatus.IDLE
    error_message: str = ""


@dataclass(slots=True)
class JobResult:
    exit_code: int
    elapsed_ms: int
    output_path: Path
    stdout_log: str = ""
    stderr_log: str = ""
    warning: str = ""
    attempt_trace: list[str] = field(default_factory=list)
    selected_encoder: str = ""


@dataclass(slots=True)
class AppState:
    jobs: list[VideoJob] = field(default_factory=list)
    selected_job_index: int = -1


@dataclass(slots=True)
class RunOptions:
    use_metadata: bool = True
    use_first_frame: bool = False
    metadata_failure_action: MetadataFailureAction = MetadataFailureAction.SKIP
    log_verbosity: LogVerbosity = LogVerbosity.MEDIUM
