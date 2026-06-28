from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


def _repo_root() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class FfmpegBinaries:
    ffmpeg: Path
    ffprobe: Path
    mp4box: Path | None = None


class FfmpegError(RuntimeError):
    pass


def locate_binaries() -> FfmpegBinaries:
    bundled_dir = _repo_root() / "bin" / "windows"
    ffmpeg_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    ffprobe_name = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"

    bundled_ffmpeg = bundled_dir / ffmpeg_name
    bundled_ffprobe = bundled_dir / ffprobe_name
    mp4box = locate_mp4box()
    if bundled_ffmpeg.exists() and bundled_ffprobe.exists():
        return FfmpegBinaries(ffmpeg=bundled_ffmpeg, ffprobe=bundled_ffprobe, mp4box=mp4box)

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        return FfmpegBinaries(ffmpeg=Path(ffmpeg_path), ffprobe=Path(ffprobe_path), mp4box=mp4box)

    raise FfmpegError(
        "未找到 ffmpeg/ffprobe。请将 ffmpeg.exe 与 ffprobe.exe 放入 bin/windows/，"
        "或将它们加入系统 PATH。"
    )


def locate_mp4box() -> Path | None:
    """Try to locate MP4Box (GPAC) for fast metadata cover insertion.

    Returns the path if found, or None if unavailable.
    """
    mp4box_name = "MP4Box.exe" if sys.platform.startswith("win") else "MP4Box"
    bundled = _repo_root() / "bin" / "windows" / mp4box_name
    if bundled.exists():
        return bundled
    mp4box_path = shutil.which("MP4Box")
    if mp4box_path:
        return Path(mp4box_path)
    return None


_PROGRESS_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_PROGRESS_FPS_RE = re.compile(r"fps=\s*([0-9.]+)")
_PROGRESS_TIME_RE = re.compile(r"time=\s*([0-9:.]+)")
_PROGRESS_SPEED_RE = re.compile(r"speed=\s*([0-9.]+x)")


def _format_ffmpeg_progress(line: str) -> str | None:
    if not line.startswith("frame="):
        return None
    frame_match = _PROGRESS_FRAME_RE.search(line)
    fps_match = _PROGRESS_FPS_RE.search(line)
    time_match = _PROGRESS_TIME_RE.search(line)
    speed_match = _PROGRESS_SPEED_RE.search(line)
    if not frame_match:
        return None
    frame = frame_match.group(1)
    fps = fps_match.group(1) if fps_match else "?"
    timestamp = time_match.group(1) if time_match else "?"
    speed = speed_match.group(1) if speed_match else "?"
    return f"进度 frame={frame} time={timestamp} fps={fps} speed={speed}"


def _is_ffmpeg_key_line(line: str) -> bool:
    head = (
        "ffmpeg version",
        "Input #",
        "Output #",
        "Stream mapping:",
        "Press [q]",
        "video:",
        "audio:",
    )
    if line.startswith(head):
        return True
    lowered = line.lower()
    return "error" in lowered or "failed" in lowered or "invalid" in lowered


def _is_error_line(line: str) -> bool:
    lowered = line.lower()
    keywords = ("error", "failed", "invalid", "could not", "unable")
    return any(word in lowered for word in keywords)


def _normalize_log_verbosity(log_verbosity: str) -> str:
    value = (log_verbosity or "medium").strip().lower()
    if value not in {"compact", "medium", "raw"}:
        return "medium"
    return value


def _inject_progress_args(args: list[str], log_verbosity: str) -> tuple[list[str], bool]:
    if not args:
        return args, False
    cmd_name = Path(args[0]).name.lower()
    if "ffmpeg" not in cmd_name or log_verbosity == "raw":
        return args, False
    if "-progress" in args:
        return args, True
    return [args[0], "-progress", "pipe:2", "-nostats", *args[1:]], True


def _format_progress_fields(progress_fields: dict[str, str]) -> str:
    frame = progress_fields.get("frame", "?")
    timestamp = progress_fields.get("out_time", "?")
    fps = progress_fields.get("fps", "?")
    speed = progress_fields.get("speed", "?")
    return f"进度 frame={frame} time={timestamp} fps={fps} speed={speed}"


def _render_returncode(returncode: int) -> str:
    # On Windows, negative exit codes may appear as unsigned 32-bit integers.
    if returncode > 0x7FFFFFFF:
        signed = returncode - 0x100000000
        return f"{returncode} (signed {signed})"
    return str(returncode)


def run_cmd(
    args: Sequence[str],
    timeout: int | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
    stream_output: bool = False,
    log_prefix: str = "",
    log_verbosity: str = "medium",
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    verbosity = _normalize_log_verbosity(log_verbosity)
    run_args = list(args)

    prefix = f"{log_prefix} " if log_prefix else ""
    if stream_output:
        run_args, progress_mode = _inject_progress_args(run_args, verbosity)
        if verbosity == "raw":
            print(f"{prefix}[command] {' '.join(run_args)}", file=sys.stderr, flush=True)
        started = time.perf_counter()
        proc = subprocess.Popen(
            run_args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_env,
            bufsize=1,
        )

        out_lines: list[str] = []
        err_lines: list[str] = []
        last_progress_print = 0.0

        progress_fields: dict[str, str] = {
            "frame": "?",
            "out_time": "?",
            "fps": "?",
            "speed": "?",
        }

        def _reader(stream, bucket: list[str], tag: str) -> None:
            nonlocal last_progress_print
            for line in iter(stream.readline, ""):
                bucket.append(line)
                line_text = line.rstrip("\n")
                if not line_text:
                    continue
                if verbosity == "raw":
                    print(f"{prefix}[{tag}] {line_text}", file=sys.stderr, flush=True)
                    continue
                if tag == "stderr":
                    if progress_mode and "=" in line_text:
                        key, value = line_text.split("=", maxsplit=1)
                        key = key.strip()
                        value = value.strip()
                        if key in {"frame", "out_time", "fps", "speed"}:
                            progress_fields[key] = value
                            continue
                        if key == "progress":
                            now = time.perf_counter()
                            if value == "end" or now - last_progress_print >= 1.0:
                                last_progress_print = now
                                if verbosity != "compact":
                                    progress = _format_progress_fields(progress_fields)
                                    print(f"{prefix}[progress] {progress}", file=sys.stderr, flush=True)
                            continue
                    progress = _format_ffmpeg_progress(line_text)
                    if progress is not None:
                        now = time.perf_counter()
                        if now - last_progress_print >= 1.0:
                            last_progress_print = now
                            if verbosity != "compact":
                                print(f"{prefix}[progress] {progress}", file=sys.stderr, flush=True)
                        continue
                    if verbosity == "medium" and _is_ffmpeg_key_line(line_text):
                        print(f"{prefix}[info] {line_text}", file=sys.stderr, flush=True)
                    elif verbosity == "compact" and _is_error_line(line_text):
                        print(f"{prefix}[error] {line_text}", file=sys.stderr, flush=True)
                    continue
                if verbosity != "compact":
                    print(f"{prefix}[stdout] {line_text}", file=sys.stderr, flush=True)
            stream.close()

        out_thread = threading.Thread(target=_reader, args=(proc.stdout, out_lines, "stdout"), daemon=True)
        err_thread = threading.Thread(target=_reader, args=(proc.stderr, err_lines, "stderr"), daemon=True)
        out_thread.start()
        err_thread.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as err:
            proc.kill()
            out_thread.join(timeout=1)
            err_thread.join(timeout=1)
            raise subprocess.TimeoutExpired(
                cmd=err.cmd,
                timeout=err.timeout,
                output="".join(out_lines),
                stderr="".join(err_lines),
            ) from err
        out_thread.join()
        err_thread.join()

        completed = subprocess.CompletedProcess(
            args=run_args,
            returncode=proc.returncode,
            stdout="".join(out_lines),
            stderr="".join(err_lines),
        )
        elapsed = time.perf_counter() - started
        status = "ok" if completed.returncode == 0 else f"exit={completed.returncode}"
        if verbosity != "compact":
            print(f"{prefix}[done] {status} elapsed={elapsed:.1f}s", file=sys.stderr, flush=True)
    else:
        completed = subprocess.run(
            run_args,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=run_env,
        )

    if check and completed.returncode != 0:
        raise FfmpegError(
            f"命令失败(returncode={_render_returncode(completed.returncode)}): {' '.join(args)}\n"
            f"stderr:\n{completed.stderr.strip()}"
        )
    return completed
