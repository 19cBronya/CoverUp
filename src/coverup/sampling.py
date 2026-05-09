from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg_tools import FfmpegBinaries, run_cmd
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


def sample_minute(
    request: SampleRequest,
    duration: float,
    bins: FfmpegBinaries,
) -> SampleResult:
    decision = decide_window(duration=duration, minute_index=request.minute_index)
    points = uniform_points(decision.window_start, decision.window_end, request.sample_count)
    thumb_dir = _sample_dir_key(request.video_path, request.minute_index)
    thumbs: list[Path] = []

    for idx, point in enumerate(points):
        out_path = thumb_dir / f"sample_{idx:02d}.jpg"
        args = [
            str(bins.ffmpeg),
            "-y",
            "-ss",
            f"{max(0.0, point):.3f}",
            "-i",
            str(request.video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_path),
        ]
        run_cmd(args, check=True)
        thumbs.append(out_path)

    return SampleResult(
        time_points=points,
        thumbnail_paths=thumbs,
        window_start=decision.window_start,
        window_end=decision.window_end,
        is_tail_window=decision.is_tail_window,
    )
