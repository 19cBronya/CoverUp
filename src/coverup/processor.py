from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from .ffmpeg_tools import FfmpegBinaries, FfmpegError, run_cmd
from .models import CoverMode, JobResult, ProbeResult

MetadataStrategy = Literal["attached_pic", "mkv_attachment", "first_frame_only"]


def _tmp_output(video_path: Path) -> Path:
    ext = video_path.suffix or ".mp4"
    return video_path.with_name(f"{video_path.stem}.coverup.tmp{ext}")


_ENCODER_CACHE: dict[str, set[str]] = {}
_FAILED_ENCODER_CACHE: dict[str, set[str]] = {}
_PREFERRED_ENCODER_CACHE: dict[str, str] = {}


def _reset_runtime_caches() -> None:
    _ENCODER_CACHE.clear()
    _FAILED_ENCODER_CACHE.clear()
    _PREFERRED_ENCODER_CACHE.clear()


def _cache_key(bins: FfmpegBinaries) -> str:
    return str(bins.ffmpeg)


@dataclass(frozen=True, slots=True)
class CoverFormatPolicy:
    extension: str
    metadata_strategy: MetadataStrategy
    note: str


_COVER_POLICY_TABLE: dict[str, CoverFormatPolicy] = {
    ".mp4": CoverFormatPolicy(".mp4", "attached_pic", "MP4 使用 attached_pic 封面流"),
    ".m4v": CoverFormatPolicy(".m4v", "attached_pic", "M4V 使用 attached_pic 封面流"),
    ".3gp": CoverFormatPolicy(".3gp", "attached_pic", "3GP 使用 attached_pic 封面流"),
    ".mov": CoverFormatPolicy(".mov", "attached_pic", "MOV 使用 QuickTime/iTunes cover atom"),
    ".mkv": CoverFormatPolicy(".mkv", "mkv_attachment", "MKV 使用 attachment 封面"),
    ".avi": CoverFormatPolicy(".avi", "first_frame_only", "AVI 无通用封面元数据通道，使用首帧"),
    ".webm": CoverFormatPolicy(".webm", "first_frame_only", "WebM 无通用封面元数据通道，使用首帧"),
    ".flv": CoverFormatPolicy(".flv", "first_frame_only", "FLV 无通用封面元数据通道，使用首帧"),
    ".wmv": CoverFormatPolicy(".wmv", "first_frame_only", "WMV 无通用封面元数据通道，使用首帧"),
    ".mpeg": CoverFormatPolicy(".mpeg", "first_frame_only", "MPEG Program Stream 无通用封面元数据通道，使用首帧"),
    ".mpg": CoverFormatPolicy(".mpg", "first_frame_only", "MPG 无通用封面元数据通道，使用首帧"),
    ".ts": CoverFormatPolicy(".ts", "first_frame_only", "TS 无通用封面元数据通道，使用首帧"),
    ".m2ts": CoverFormatPolicy(".m2ts", "first_frame_only", "M2TS 无通用封面元数据通道，使用首帧"),
    ".rmvb": CoverFormatPolicy(".rmvb", "first_frame_only", "RMVB 无通用封面元数据通道，使用首帧"),
}

_DEFAULT_POLICY = CoverFormatPolicy(
    extension="*",
    metadata_strategy="first_frame_only",
    note="未知容器：默认使用首帧以保证可见性",
)


def cover_policy_for_path(video_path: Path) -> CoverFormatPolicy:
    return _COVER_POLICY_TABLE.get(video_path.suffix.lower(), _DEFAULT_POLICY)


def _emit(stream_logs: bool, prefix: str, tag: str, message: str) -> None:
    if not stream_logs:
        return
    print(f"{prefix} [{tag}] {message}", flush=True)


def _available_h264_encoders(bins: FfmpegBinaries) -> set[str]:
    key = _cache_key(bins)
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


def _count_video_streams(video_path: Path, bins: FfmpegBinaries) -> int:
    args = [
        str(bins.ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    try:
        proc = run_cmd(args, timeout=15)
    except Exception:
        return 1
    if proc.returncode != 0:
        return 1
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return len(lines) if lines else 1


def _merge_logs(parts: Sequence[str]) -> str:
    return "\n".join(part for part in parts if part)


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


def _cover_mimetype(cover_path: Path) -> str:
    ext = cover_path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "application/octet-stream"


def _run_metadata_mkv_attachment_mode(
    video_path: Path,
    cover_path: Path,
    out_path: Path,
    bins: FfmpegBinaries,
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> tuple[int, str, str, list[str]]:
    prefix = f"[metadata-mkv:{video_path.name}]"
    _emit(stream_logs, prefix, "START", "strategy=attachment")
    args = [
        str(bins.ffmpeg),
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0",
        "-c",
        "copy",
        "-attach",
        str(cover_path),
        "-metadata:s:t:0",
        f"mimetype={_cover_mimetype(cover_path)}",
        "-metadata:s:t:0",
        f"filename={cover_path.name}",
        str(out_path),
    ]
    proc = run_cmd(args, stream_output=stream_logs, log_prefix=prefix, log_verbosity=log_verbosity)
    trace = ["MKV:ok(attachment)"] if proc.returncode == 0 else [f"MKV:fail(exit={proc.returncode})"]
    return proc.returncode, proc.stdout, proc.stderr, trace


def _run_metadata_mode(
    video_path: Path,
    cover_path: Path,
    out_path: Path,
    bins: FfmpegBinaries,
    metadata_strategy: MetadataStrategy = "attached_pic",
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> tuple[int, str, str, list[str]]:
    if metadata_strategy == "mkv_attachment":
        return _run_metadata_mkv_attachment_mode(
            video_path,
            cover_path,
            out_path,
            bins,
            stream_logs=stream_logs,
            log_verbosity=log_verbosity,
        )

    prefix = f"[metadata:{video_path.name}]"
    attempt_trace: list[str] = []
    _emit(stream_logs, prefix, "START", "strategy=metadata attempts=A(preserve),B(conservative)")

    video_stream_count = _count_video_streams(video_path, bins)
    cover_output_index = max(video_stream_count, 1)
    _emit(stream_logs, prefix, "STEP", f"metadata-A preserve-all cover-index=v:{cover_output_index}")
    args_a = [
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
        f"-c:v:{cover_output_index}",
        "mjpeg",
        f"-disposition:v:{cover_output_index}",
        "attached_pic",
    ]
    if video_path.suffix.lower() in {".mov", ".mp4", ".m4v", ".3gp"}:
        args_a.extend(["-movflags", "+use_metadata_tags"])
    args_a.append(str(out_path))
    proc_a = run_cmd(
        args_a,
        stream_output=stream_logs,
        log_prefix=f"{prefix}:A",
        log_verbosity=log_verbosity,
    )
    logs_out = [proc_a.stdout]
    logs_err = [proc_a.stderr]
    if proc_a.returncode == 0:
        attempt_trace.append(f"A:ok(v:{cover_output_index})")
        _emit(stream_logs, prefix, "RESULT", "ok via metadata-A")
        return 0, _merge_logs(logs_out), _merge_logs(logs_err), attempt_trace

    attempt_trace.append(f"A:fail(exit={proc_a.returncode})")
    if out_path.exists():
        out_path.unlink()

    _emit(stream_logs, prefix, "STEP", "metadata-B conservative map=0:V:0 + 0:a? + 0:s? + cover")
    args_b = [
        str(bins.ffmpeg),
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(cover_path),
        "-map",
        "0:V:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map",
        "1:v:0",
        "-c",
        "copy",
        "-c:v:1",
        "mjpeg",
        "-disposition:v:1",
        "attached_pic",
    ]
    if video_path.suffix.lower() in {".mov", ".mp4", ".m4v", ".3gp"}:
        args_b.extend(["-movflags", "+use_metadata_tags"])
    args_b.append(str(out_path))
    proc_b = run_cmd(
        args_b,
        stream_output=stream_logs,
        log_prefix=f"{prefix}:B",
        log_verbosity=log_verbosity,
    )
    logs_out.append(proc_b.stdout)
    logs_err.append(proc_b.stderr)
    if proc_b.returncode == 0:
        attempt_trace.append("B:ok")
        _emit(stream_logs, prefix, "RESULT", "ok via metadata-B")
    else:
        attempt_trace.append(f"B:fail(exit={proc_b.returncode})")
        _emit(stream_logs, prefix, "RESULT", f"fail exit={proc_b.returncode}")
    return proc_b.returncode, _merge_logs(logs_out), _merge_logs(logs_err), attempt_trace


def _run_first_frame_mode(
    video_path: Path,
    cover_path: Path,
    out_path: Path,
    bins: FfmpegBinaries,
    probe: ProbeResult,
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> tuple[int, str, str, str, list[str]]:
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
    encoder_profiles: list[tuple[str, str, list[str], bool]] = []
    available = _available_h264_encoders(bins)
    cache_key = _cache_key(bins)
    failed_encoders = _FAILED_ENCODER_CACHE.setdefault(cache_key, set())
    preferred_encoder = _PREFERRED_ENCODER_CACHE.get(cache_key)

    if preferred_encoder == "h264_qsv" and "h264_qsv" in available and "h264_qsv" not in failed_encoders:
        encoder_profiles.append(("intel-qsv", "h264_qsv", ["-preset", "faster", "-global_quality", "26"], True))
    if preferred_encoder == "h264_nvenc" and "h264_nvenc" in available and "h264_nvenc" not in failed_encoders:
        encoder_profiles.append(("nvidia-nvenc", "h264_nvenc", ["-preset", "p4", "-cq", "23", "-b:v", "0"], True))
    if preferred_encoder == "h264_amf" and "h264_amf" in available and "h264_amf" not in failed_encoders:
        encoder_profiles.append(
            ("amd-amf", "h264_amf", ["-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"], True)
        )

    # Fixed probe order: Intel -> NVIDIA -> AMD -> CPU
    if "h264_qsv" in available:
        encoder_profiles.append(("intel-qsv", "h264_qsv", ["-preset", "faster", "-global_quality", "26"], True))
    if "h264_nvenc" in available:
        encoder_profiles.append(("nvidia-nvenc", "h264_nvenc", ["-preset", "p4", "-cq", "23", "-b:v", "0"], True))
    if "h264_amf" in available:
        encoder_profiles.append(
            ("amd-amf", "h264_amf", ["-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"], True)
        )
    encoder_profiles.append(("cpu-x264", "libx264", ["-preset", "fast", "-crf", "20"], False))

    deduped_profiles: list[tuple[str, str, list[str], bool]] = []
    seen_encoders: set[str] = set()
    for profile in encoder_profiles:
        accel_name, encoder_name, _, is_hardware = profile
        if encoder_name in seen_encoders:
            continue
        if is_hardware and encoder_name in failed_encoders:
            _emit(
                stream_logs,
                f"[first-frame:{video_path.name}]",
                "SKIP",
                f"skip {accel_name} due to session cache",
            )
            continue
        seen_encoders.add(encoder_name)
        deduped_profiles.append(profile)

    combined_out = ""
    combined_err = ""
    last_code = 1
    attempt_trace: list[str] = []
    selected_encoder = ""
    _emit(stream_logs, f"[first-frame:{video_path.name}]", "START", "strategy=first-frame")

    for accel_name, encoder_name, encoder_opts, is_hardware in deduped_profiles:
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
        _emit(stream_logs, prefix, "STEP", f"try encoder={encoder_name}")
        proc = run_cmd(args, stream_output=stream_logs, log_prefix=prefix, log_verbosity=log_verbosity)
        last_code = proc.returncode
        combined_out += proc.stdout
        combined_err += proc.stderr
        if proc.returncode == 0:
            selected_encoder = encoder_name
            _PREFERRED_ENCODER_CACHE[cache_key] = encoder_name
            attempt_trace.append(f"{accel_name}:ok({encoder_name})")
            _emit(stream_logs, prefix, "RESULT", f"ok encoder={encoder_name}")
            return 0, combined_out, combined_err, selected_encoder, attempt_trace
        attempt_trace.append(f"{accel_name}:fail({encoder_name},exit={proc.returncode})")
        if out_path.exists():
            out_path.unlink()
        if is_hardware:
            failed_encoders.add(encoder_name)
            _emit(stream_logs, prefix, "CACHE", "mark failed for this session")
        _emit(stream_logs, prefix, "FALLBACK", "try next encoder")

    return last_code, combined_out, combined_err, selected_encoder, attempt_trace


def process_in_place(
    video_path: Path,
    cover_path: Path,
    mode: CoverMode,
    bins: FfmpegBinaries,
    probe: ProbeResult,
    stream_logs: bool = False,
    log_verbosity: str = "medium",
) -> JobResult:
    if not cover_path.exists():
        raise FfmpegError(f"封面文件不存在: {cover_path}")

    start = time.perf_counter()
    temp_out = _tmp_output(video_path)
    if temp_out.exists():
        temp_out.unlink()

    policy = cover_policy_for_path(video_path)
    if mode == CoverMode.METADATA and policy.metadata_strategy == "first_frame_only":
        elapsed = int((time.perf_counter() - start) * 1000)
        return JobResult(
            exit_code=2,
            elapsed_ms=elapsed,
            output_path=video_path,
            warning=f"该格式不支持通用元数据封面，请按失败策略选择跳过或首帧。({policy.note})",
            attempt_trace=["UNSUPPORTED:metadata"],
        )

    selected_encoder = ""
    attempt_trace: list[str] = []
    if mode == CoverMode.METADATA:
        code, out, err, attempt_trace = _run_metadata_mode(
            video_path,
            cover_path,
            temp_out,
            bins,
            metadata_strategy=policy.metadata_strategy,
            stream_logs=stream_logs,
            log_verbosity=log_verbosity,
        )
    else:
        code, out, err, selected_encoder, attempt_trace = _run_first_frame_mode(
            video_path,
            cover_path,
            temp_out,
            bins,
            probe,
            stream_logs=stream_logs,
            log_verbosity=log_verbosity,
        )

    if code != 0:
        if temp_out.exists():
            temp_out.unlink()
        elapsed = int((time.perf_counter() - start) * 1000)
        warning = ""
        if mode == CoverMode.METADATA:
            warning = "元数据封面写入失败，已完成兼容重试。"
        return JobResult(
            exit_code=code,
            elapsed_ms=elapsed,
            output_path=video_path,
            stdout_log=out,
            stderr_log=err,
            warning=warning,
            attempt_trace=attempt_trace,
            selected_encoder=selected_encoder,
        )

    out_path = _replace_in_place(temp_out, video_path)
    elapsed = int((time.perf_counter() - start) * 1000)
    return JobResult(
        exit_code=0,
        elapsed_ms=elapsed,
        output_path=out_path,
        stdout_log=out,
        stderr_log=err,
        warning=(f"元数据策略已按格式适配：{policy.note}" if mode == CoverMode.METADATA else ""),
        attempt_trace=attempt_trace,
        selected_encoder=selected_encoder,
    )
