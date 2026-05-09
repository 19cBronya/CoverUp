from __future__ import annotations

import subprocess
from pathlib import Path

from coverup.models import CoverMode, ProbeResult
from coverup.processor import _reset_runtime_caches, process_in_place


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


def test_first_frame_prefers_qsv_when_available(monkeypatch, tmp_path: Path) -> None:
    _reset_runtime_caches()
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    seen: list[list[str]] = []

    monkeypatch.setattr("coverup.processor._available_h264_encoders", lambda _bins: {"h264_qsv", "h264_nvenc", "h264_amf"})

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        seen.append(args)
        Path(args[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.FIRST_FRAME,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code == 0
    assert seen
    assert "h264_qsv" in seen[0]


def test_first_frame_falls_back_to_nvenc_after_qsv_failure(monkeypatch, tmp_path: Path) -> None:
    _reset_runtime_caches()
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    seen: list[list[str]] = []
    calls = {"count": 0}

    monkeypatch.setattr("coverup.processor._available_h264_encoders", lambda _bins: {"h264_qsv", "h264_nvenc"})

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        seen.append(args)
        calls["count"] += 1
        if calls["count"] == 1:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="qsv failed")
        Path(args[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.FIRST_FRAME,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code == 0
    assert len(seen) == 2
    assert "h264_qsv" in seen[0]
    assert "h264_nvenc" in seen[1]


def test_first_frame_skips_failed_hardware_encoder_in_same_session(monkeypatch, tmp_path: Path) -> None:
    _reset_runtime_caches()
    video1 = tmp_path / "demo1.mp4"
    video2 = tmp_path / "demo2.mp4"
    cover = tmp_path / "cover.jpg"
    video1.write_bytes(b"video")
    video2.write_bytes(b"video")
    cover.write_bytes(b"cover")
    encoders_seen: list[str] = []

    monkeypatch.setattr("coverup.processor._available_h264_encoders", lambda _bins: {"h264_qsv", "h264_nvenc"})

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        encoder = args[args.index("-c:v") + 1]
        encoders_seen.append(encoder)
        if encoder == "h264_qsv":
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="qsv failed")
        Path(args[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)
    bins = type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})()

    first = process_in_place(
        video_path=video1,
        cover_path=cover,
        mode=CoverMode.FIRST_FRAME,
        bins=bins,
        probe=_probe(),
    )
    second = process_in_place(
        video_path=video2,
        cover_path=cover,
        mode=CoverMode.FIRST_FRAME,
        bins=bins,
        probe=_probe(),
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert encoders_seen.count("h264_qsv") == 1
    assert encoders_seen.count("h264_nvenc") == 2
