from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
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
        print(f"{prefix}$ {' '.join(args)}", file=sys.stderr, flush=True)
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

        def _reader(stream, bucket: list[str], tag: str) -> None:
            for line in iter(stream.readline, ""):
                bucket.append(line)
                line_text = line.rstrip("\n")
                if line_text:
                    print(f"{prefix}[{tag}] {line_text}", file=sys.stderr, flush=True)
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
