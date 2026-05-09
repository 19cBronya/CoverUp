from __future__ import annotations

from pathlib import Path

from .models import ScanOptions


COMMON_VIDEO_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".flv",
    ".wmv",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".ts",
    ".m2ts",
    ".3gp",
    ".rmvb",
}


def scan_videos(options: ScanOptions) -> list[Path]:
    base = options.directory_path.expanduser().resolve()
    iterator = base.rglob("*") if options.recursive else base.glob("*")
    out: list[Path] = []
    seen: set[Path] = set()
    for item in iterator:
        if not item.is_file():
            continue
        if item.suffix.lower() not in COMMON_VIDEO_SUFFIXES:
            continue
        path = item.resolve()
        if options.deduplicate:
            if path in seen:
                continue
            seen.add(path)
        out.append(path)
    out.sort()
    return out
