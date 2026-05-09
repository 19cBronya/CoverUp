from __future__ import annotations

import shutil
import time
from pathlib import Path

from .ffmpeg_tools import FfmpegBinaries, FfmpegError, run_cmd
from .models import CoverMode, JobResult, ProbeResult


def _tmp_output(video_path: Path) -> Path:
    ext = video_path.suffix or ".mp4"
    return video_path.with_name(f"{video_path.stem}.coverup.tmp{ext}")


_ENCODER_CACHE: dict[str, set[str]] = {}


def _available_h264_encoders(bins: FfmpegBinaries) -> set[str]:
    key = str(bins.ffmpeg)
    cached = _ENCODER_CACHE.get(key)
    if cached is not None:
        return cached
    found: set[str] = set()
    try:
        proc = run_cmd([str(bins.ffmpeg), "-hide_banner", "-encoders"], timeout=15)
        text = f"{proc.stdout}\n{proc.stderr}"
        for name in ("h264_qsv", "h264_nvenc", "h264_amf"):
            if name in text:
                found.add(name)
    except Exception:
        # If probing fails, keep an empty set and fall back to CPU.
        pass
    _ENCODER_CACHE[key] = found
    return found


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


def _run_metadata_mode(
    video_path: Path,
    cover_path: Path,
    out_path: Path,
    bins: FfmpegBinaries,
    stream_logs: bool = False,
) -> tuple[int, str, str]:
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
    proc = run_cmd(args, stream_output=stream_logs, log_prefix=f"[metadata:{video_path.name}]")
    return proc.returncode, proc.stdout, proc.stderr


def _run_first_frame_mode(
    video_path: Path,
    cover_path: Path,
    out_path: Path,
    bins: FfmpegBinaries,
    probe: ProbeResult,
    stream_logs: bool = False,
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
    base_args = [
        str(bins.ffmpeg),
        "-y",
        "-hwaccel",
        "auto",
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
    ]
    encoder_profiles: list[tuple[str, str, list[str]]] = []
    available = _available_h264_encoders(bins)

    if "h264_qsv" in available:
        encoder_profiles.append(("intel-qsv", "h264_qsv", ["-preset", "faster", "-global_quality", "26"]))
    if "h264_nvenc" in available:
        encoder_profiles.append(("nvidia-nvenc", "h264_nvenc", ["-preset", "p4", "-cq", "23", "-b:v", "0"]))
    if "h264_amf" in available:
        encoder_profiles.append(("amd-amf", "h264_amf", ["-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]))
    encoder_profiles.append(("cpu-x264", "libx264", ["-preset", "fast", "-crf", "20"]))

    combined_out = ""
    combined_err = ""
    last_code = 1
    for accel_name, encoder_name, encoder_opts in encoder_profiles:
        args = [
            *base_args,
            "-c:v",
            encoder_name,
            *encoder_opts,
            "-c:a",
            "copy",
            str(out_path),
        ]
        prefix = f"[first-frame:{video_path.name}:{accel_name}]"
        if stream_logs:
            print(f"{prefix} [try] encoder={encoder_name}", flush=True)
        proc = run_cmd(args, stream_output=stream_logs, log_prefix=prefix)
        last_code = proc.returncode
        combined_out += proc.stdout
        combined_err += proc.stderr
        if proc.returncode == 0:
            if stream_logs:
                print(f"{prefix} [selected] encoder={encoder_name}", flush=True)
            return 0, combined_out, combined_err
        if out_path.exists():
            out_path.unlink()
        if stream_logs:
            print(f"{prefix} [fallback] encoder failed, try next", flush=True)

    return last_code, combined_out, combined_err


def process_in_place(
    video_path: Path,
    cover_path: Path,
    mode: CoverMode,
    bins: FfmpegBinaries,
    probe: ProbeResult,
    stream_logs: bool = False,
) -> JobResult:
    if not cover_path.exists():
        raise FfmpegError(f"封面文件不存在: {cover_path}")

    start = time.perf_counter()
    temp_out = _tmp_output(video_path)
    if temp_out.exists():
        temp_out.unlink()

    if mode == CoverMode.METADATA:
        code, out, err = _run_metadata_mode(video_path, cover_path, temp_out, bins, stream_logs=stream_logs)
    else:
        code, out, err = _run_first_frame_mode(video_path, cover_path, temp_out, bins, probe, stream_logs=stream_logs)

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
