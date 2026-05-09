from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "bin" / "windows"


def main() -> int:
    ffmpeg = BIN_DIR / "ffmpeg.exe"
    ffprobe = BIN_DIR / "ffprobe.exe"
    if not ffmpeg.exists() or not ffprobe.exists():
        print("缺少 ffmpeg.exe 或 ffprobe.exe，请先放到 bin/windows/ 下。", file=sys.stderr)
        return 2

    if not shutil.which("pyinstaller"):
        print("未找到 pyinstaller，请先安装：pip install pyinstaller", file=sys.stderr)
        return 2

    sep = ";" if sys.platform.startswith("win") else ":"
    cmd = [
        "pyinstaller",
        "--noconfirm",
        "--name",
        "coverup",
        "--windowed",
        "--paths",
        str(ROOT / "src"),
        "--add-data",
        f"{ffmpeg}{sep}bin/windows",
        "--add-data",
        f"{ffprobe}{sep}bin/windows",
        str(ROOT / "src" / "coverup" / "main.py"),
    ]
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
