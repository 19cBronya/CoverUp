from __future__ import annotations

import shutil
import time
from pathlib import Path

from .ffmpeg_tools import FfmpegBinaries, FfmpegError, run_cmd
from .models import CoverMode, JobResult, ProbeResult


def _tmp_output(video_path: Path, suffix: str = ".tmp.mp4") -> Path:
    return video_path.with_name(f"{video_path.stem}.coverup{suffix}")


def _replace_in_place(temp_out: Path, original: Path) -> Path:
    backup = original.with_name(f"{original.name}.coverup.bak")
    if backup.exists():
        backup.unlink()
    shutil.move(str(original), str(backup))
    try:
        shutil.move(str(temp_out), str(original))
    except Exception:
        if backup.exists():
            shutil.move(str(backup), str(original))
        raise
    if backup.exists():
        backup.unlink()
    return original


def _run_metadata_mode(video_path: Path, cover_path: Path, out_path: Path, bins: FfmpegBinaries) -> tuple[int, str, str]:
    args = [
        str(bins.ffmpeg),
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(cover_path),
        "-map",
        "0",
        "-map",
        "1:v:0",
        "-c",
        "copy",
        "-c:v:1",
        "mjpeg",
        "-disposition:v:1",
        "attached_pic",
        str(out_path),
    ]
    proc = run_cmd(args)
    return proc.returncode, proc.stdout, proc.stderr


def _run_first_frame_mode(
    video_path: Path,
    cover_path: Path,
    out_path: Path,
    bins: FfmpegBinaries,
    probe: ProbeResult,
) -> tuple[int, str, str]:
    width = max(2, probe.width if probe.width > 0 else 1280)
    height = max(2, probe.height if probe.height > 0 else 720)
    fps = probe.fps if probe.fps > 0 else 25.0
    scale_pad = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    )
    filter_complex = (
        f"[0:v]{scale_pad},format=yuv420p,fps={fps}[cover];"
        f"[1:v]{scale_pad},format=yuv420p,fps={fps}[main];"
        f"[cover][main]concat=n=2:v=1:a=0[v]"
    )
    args = [
        str(bins.ffmpeg),
        "-y",
        "-loop",
        "1",
        "-t",
        "1",
        "-i",
        str(cover_path),
        "-i",
        str(video_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "copy",
        str(out_path),
    ]
    proc = run_cmd(args)
    return proc.returncode, proc.stdout, proc.stderr


def process_in_place(
    video_path: Path,
    cover_path: Path,
    mode: CoverMode,
    bins: FfmpegBinaries,
    probe: ProbeResult,
) -> JobResult:
    if not cover_path.exists():
        raise FfmpegError(f"封面文件不存在: {cover_path}")

    start = time.perf_counter()
    temp_out = _tmp_output(video_path, suffix=f".{video_path.suffix.lstrip('.')}.tmp")
    if temp_out.exists():
        temp_out.unlink()

    if mode == CoverMode.METADATA:
        code, out, err = _run_metadata_mode(video_path, cover_path, temp_out, bins)
    else:
        code, out, err = _run_first_frame_mode(video_path, cover_path, temp_out, bins, probe)

    if code != 0:
        if temp_out.exists():
            temp_out.unlink()
        elapsed = int((time.perf_counter() - start) * 1000)
        warning = ""
        if mode == CoverMode.METADATA:
            warning = "元数据封面写入失败，建议切换为“替换首帧”模式。"
        return JobResult(
            exit_code=code,
            elapsed_ms=elapsed,
            output_path=video_path,
            stdout_log=out,
            stderr_log=err,
            warning=warning,
        )

    out_path = _replace_in_place(temp_out, video_path)
    elapsed = int((time.perf_counter() - start) * 1000)
    return JobResult(
        exit_code=0,
        elapsed_ms=elapsed,
        output_path=out_path,
        stdout_log=out,
        stderr_log=err,
    )
