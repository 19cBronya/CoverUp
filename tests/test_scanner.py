from pathlib import Path

from coverup.models import ScanOptions
from coverup.scanner import scan_videos


def test_scan_videos_recursive_and_non_recursive(tmp_path: Path) -> None:
    root_video = tmp_path / "a.mp4"
    root_video.write_bytes(b"v")

    nested = tmp_path / "nested"
    nested.mkdir()
    nested_video = nested / "b.mkv"
    nested_video.write_bytes(b"v")

    non_recursive = scan_videos(ScanOptions(directory_path=tmp_path, recursive=False, deduplicate=True))
    assert root_video.resolve() in non_recursive
    assert nested_video.resolve() not in non_recursive

    recursive = scan_videos(ScanOptions(directory_path=tmp_path, recursive=True, deduplicate=True))
    assert root_video.resolve() in recursive
    assert nested_video.resolve() in recursive
