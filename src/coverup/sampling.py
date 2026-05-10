from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg_tools import FfmpegBinaries, FfmpegError, run_cmd
from .models import SampleRequest, SampleResult


@dataclass(slots=True)
class WindowDecision:
    window_start: float
    window_end: float
    is_tail_window: bool
    next_minute_index: int


def decide_window(duration: float, minute_index: int) -> WindowDecision:
    if duration <= 0:
        return WindowDecision(0.0, 0.0, True, 0)

    start = max(0.0, minute_index * 60.0)
    if start >= duration:
        return WindowDecision(0.0, min(60.0, duration), duration <= 60.0, 1 if duration > 60.0 else 0)

    end = min(start + 60.0, duration)
    is_tail = (duration - end) < 60.0 and end == duration
    if end >= duration:
        next_index = 0
    else:
        next_index = minute_index + 1
    return WindowDecision(start, end, is_tail, next_index)


def uniform_points(start: float, end: float, count: int) -> list[float]:
    if count <= 0:
        return []
    if end <= start:
        return [start for _ in range(count)]
    span = end - start
    step = span / count
    return [start + (idx + 0.5) * step for idx in range(count)]


def _cache_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "coverup_sample_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sample_dir_key(video_path: Path, minute_index: int) -> Path:
    token = f"{video_path.resolve()}::{minute_index}".encode("utf-8", errors="ignore")
    key = hashlib.sha1(token).hexdigest()[:16]
    path = _cache_dir() / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_extract_args(
    video_path: Path,
    point: float,
    out_path: Path,
    accurate_seek: bool,
    image_ext: str,
) -> list[str]:
    if image_ext.lower() == "png":
        codec_args = ["-c:v", "png", "-compression_level", "2"]
    else:
        codec_args = ["-c:v", "mjpeg", "-q:v", "2"]

    extract_args = [
        "-frames:v",
        "1",
        "-threads:v",
        "1",
        "-update",
        "1",
        *codec_args,
        str(out_path),
    ]

    if accurate_seek:
        # Some MOV/network files fail when seeking before input; retry with output-seek.
        return [
            "-y",
            "-i",
            str(video_path),
            "-ss",
            f"{max(0.0, point):.3f}",
            *extract_args,
        ]
    return [
        "-y",
        "-ss",
        f"{max(0.0, point):.3f}",
        "-i",
        str(video_path),
        *extract_args,
    ]


def _extract_frame_with_fallback(
    video_path: Path,
    point: float,
    out_base_path: Path,
    bins: FfmpegBinaries,
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> Path:
    attempts = (
        ("fast-seek-jpg", "jpg", False),
        ("accurate-seek-jpg", "jpg", True),
        ("fast-seek-png", "png", False),
        ("accurate-seek-png", "png", True),
    )
    errors: list[str] = []
    for name, image_ext, accurate_seek in attempts:
        out_path = out_base_path.with_suffix(f".{image_ext}")
        sub_args = _build_extract_args(video_path, point, out_path, accurate_seek=accurate_seek, image_ext=image_ext)
        args = [str(bins.ffmpeg), *sub_args]
        proc = run_cmd(
            args,
            stream_output=stream_logs,
            log_prefix=f"[sample:{video_path.name}:{name}@{point:.3f}s]",
            log_verbosity=log_verbosity,
        )
        if proc.returncode == 0 and out_path.exists():
            return out_path
        if out_path.exists():
            out_path.unlink()
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or "无 stderr/stdout"
        errors.append(f"{name} returncode={proc.returncode}: {detail[-220:]}")
    raise FfmpegError("抽帧失败（已重试 seek+编码格式 4 种组合）: " + " | ".join(errors))


def sample_minute(
    request: SampleRequest,
    duration: float,
    bins: FfmpegBinaries,
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> SampleResult:
    decision = decide_window(duration=duration, minute_index=request.minute_index)
    points = uniform_points(decision.window_start, decision.window_end, request.sample_count)
    thumb_dir = _sample_dir_key(request.video_path, request.minute_index)
    thumbs: list[Path] = []

    for idx, point in enumerate(points):
        out_base = thumb_dir / f"sample_{idx:02d}"
        out_path = _extract_frame_with_fallback(
            request.video_path,
            point,
            out_base,
            bins,
            stream_logs=stream_logs,
            log_verbosity=log_verbosity,
        )
        thumbs.append(out_path)

    return SampleResult(
        time_points=points,
        thumbnail_paths=thumbs,
        window_start=decision.window_start,
        window_end=decision.window_end,
        is_tail_window=decision.is_tail_window,
    )
