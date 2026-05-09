from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    proc = subprocess.run(
        list(args),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=run_env,
    )
    if check and proc.returncode != 0:
        raise FfmpegError(
            f"命令失败(returncode={proc.returncode}): {' '.join(args)}\n"
            f"stderr:\n{proc.stderr.strip()}"
        )
    return proc
