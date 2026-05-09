from __future__ import annotations

import subprocess
from pathlib import Path

from coverup.models import CoverMode, ProbeResult
from coverup.processor import process_in_place


def _probe() -> ProbeResult:
    return ProbeResult(
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        duration=12.0,
        width=1280,
        height=720,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        has_attached_pic=False,
        metadata_cover_writable=True,
    )


def test_process_uses_temp_output_with_real_container_suffix(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")

    captured: dict[str, list[str]] = {}

    def fake_run_cmd(args, **_kwargs):
        captured["args"] = list(args)
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="mock error")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code == 1
    assert captured["args"][-1].endswith(".coverup.tmp.mp4")


def test_process_temp_output_defaults_to_mp4_for_extensionless_path(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")

    captured: dict[str, list[str]] = {}

    def fake_run_cmd(args, **_kwargs):
        captured["args"] = list(args)
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="mock error")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code == 1
    assert captured["args"][-1].endswith(".coverup.tmp.mp4")
