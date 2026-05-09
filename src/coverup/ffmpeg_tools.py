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


class FfmpegError(RuntimeError):
    pass


def locate_binaries() -> FfmpegBinaries:
    bundled_dir = _repo_root() / "bin" / "windows"
    ffmpeg_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    ffprobe_name = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"

    bundled_ffmpeg = bundled_dir / ffmpeg_name
    bundled_ffprobe = bundled_dir / ffprobe_name
    if bundled_ffmpeg.exists() and bundled_ffprobe.exists():
        return FfmpegBinaries(ffmpeg=bundled_ffmpeg, ffprobe=bundled_ffprobe)

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        return FfmpegBinaries(ffmpeg=Path(ffmpeg_path), ffprobe=Path(ffprobe_path))

    raise FfmpegError(
        "未找到 ffmpeg/ffprobe。请将 ffmpeg.exe 与 ffprobe.exe 放入 bin/windows/，"
        "或将它们加入系统 PATH。"
    )


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


def run_cmd(
    args: Sequence[str],
    timeout: int | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
    stream_output: bool = False,
    log_prefix: str = "",
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    prefix = f"{log_prefix} " if log_prefix else ""
    if stream_output:
        print(f"{prefix}[command] {' '.join(args)}", file=sys.stderr, flush=True)
        started = time.perf_counter()
        proc = subprocess.Popen(
            list(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_env,
            bufsize=1,
        )

        out_lines: list[str] = []
        err_lines: list[str] = []
        last_progress_print = 0.0

        def _reader(stream, bucket: list[str], tag: str) -> None:
            nonlocal last_progress_print
            for line in iter(stream.readline, ""):
                bucket.append(line)
                line_text = line.rstrip("\n")
                if not line_text:
                    continue
                if tag == "stderr":
                    progress = _format_ffmpeg_progress(line_text)
                    if progress is not None:
                        now = time.perf_counter()
                        if now - last_progress_print >= 1.0:
                            last_progress_print = now
                            print(f"{prefix}[progress] {progress}", file=sys.stderr, flush=True)
                        continue
                    if _is_ffmpeg_key_line(line_text):
                        print(f"{prefix}[info] {line_text}", file=sys.stderr, flush=True)
                    continue
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
            args=list(args),
            returncode=proc.returncode,
            stdout="".join(out_lines),
            stderr="".join(err_lines),
        )
        elapsed = time.perf_counter() - started
        status = "ok" if completed.returncode == 0 else f"exit={completed.returncode}"
        print(f"{prefix}[done] {status} elapsed={elapsed:.1f}s", file=sys.stderr, flush=True)
    else:
        completed = subprocess.run(
            list(args),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=run_env,
        )

    if check and completed.returncode != 0:
        raise FfmpegError(
            f"命令失败(returncode={completed.returncode}): {' '.join(args)}\n"
            f"stderr:\n{completed.stderr.strip()}"
        )
    return completed
