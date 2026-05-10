from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .ffmpeg_tools import FfmpegBinaries, run_cmd
from .models import ProbeResult


METADATA_FRIENDLY_FORMATS = {
    "mp4",
    "mov",
    "matroska",
    "3gp",
    "m4v",
}


def _safe_float(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _safe_int(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _parse_fps(rate: str | None) -> float:
    if not rate:
        return 0.0
    if "/" in rate:
        a, b = rate.split("/", maxsplit=1)
        try:
            aa = float(a)
            bb = float(b)
            if bb == 0:
                return 0.0
            return aa / bb
        except ValueError:
            return 0.0
    return _safe_float(rate)


def _cover_cache_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "coverup_original_cover_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached_cover_path(video_path: Path) -> Path:
    stat = video_path.stat()
    token = f"{video_path.resolve()}::{stat.st_mtime_ns}::{stat.st_size}".encode("utf-8", errors="ignore")
    key = hashlib.sha1(token).hexdigest()[:24]
    return _cover_cache_dir() / f"{key}.png"


def probe_video(
    video_path: Path,
    bins: FfmpegBinaries,
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> ProbeResult:
    args = [
        str(bins.ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,disposition",
        "-of",
        "json",
        str(video_path),
    ]
    proc = run_cmd(
        args,
        check=True,
        stream_output=stream_logs,
        log_prefix=f"[probe:{video_path.name}]",
        log_verbosity=log_verbosity,
    )
    payload = json.loads(proc.stdout or "{}")

    format_name = ((payload.get("format") or {}).get("format_name") or "").split(",")[0]
    duration = _safe_float((payload.get("format") or {}).get("duration"))
    streams = payload.get("streams") or []

    width = 0
    height = 0
    fps = 0.0
    video_codec = ""
    audio_codec = ""
    has_attached_pic = False
    attached_pic_stream_index: int | None = None

    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not has_attached_pic:
            disp = stream.get("disposition") or {}
            if bool(disp.get("attached_pic")):
                has_attached_pic = True
                attached_pic_stream_index = _safe_int(str(stream.get("index")))
        if codec_type == "video" and width == 0 and not (stream.get("disposition") or {}).get("attached_pic"):
            width = _safe_int(str(stream.get("width")))
            height = _safe_int(str(stream.get("height")))
            fps = _parse_fps(stream.get("r_frame_rate"))
            video_codec = stream.get("codec_name") or ""
        if codec_type == "audio" and not audio_codec:
            audio_codec = stream.get("codec_name") or ""

    metadata_cover_writable = format_name in METADATA_FRIENDLY_FORMATS
    return ProbeResult(
        format_name=format_name,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        video_codec=video_codec,
        audio_codec=audio_codec,
        has_attached_pic=has_attached_pic,
        metadata_cover_writable=metadata_cover_writable,
        attached_pic_stream_index=attached_pic_stream_index,
    )


def extract_attached_cover_preview(
    video_path: Path,
    probe: ProbeResult,
    bins: FfmpegBinaries,
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> Path | None:
    if not probe.has_attached_pic or probe.attached_pic_stream_index is None:
        return None

    out_path = _cached_cover_path(video_path)
    if out_path.exists():
        return out_path

    args = [
        str(bins.ffmpeg),
        "-y",
        "-i",
        str(video_path),
        "-map",
        f"0:{probe.attached_pic_stream_index}",
        "-frames:v",
        "1",
        "-threads:v",
        "1",
        "-update",
        "1",
        "-c:v",
        "png",
        str(out_path),
    ]
    proc = run_cmd(
        args,
        stream_output=stream_logs,
        log_prefix=f"[cover-preview:{video_path.name}]",
        log_verbosity=log_verbosity,
    )
    if proc.returncode == 0 and out_path.exists():
        return out_path
    if out_path.exists():
        out_path.unlink()
    return None
