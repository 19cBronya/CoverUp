from __future__ import annotations

import subprocess
from pathlib import Path

from coverup.models import CoverMode, ProbeResult
from coverup.processor import cover_policy_for_path, process_in_place, resolve_metadata_mode


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
        mode=CoverMode.FIRST_FRAME,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code == 1
    assert captured["args"][-1].endswith(".coverup.tmp.mp4")


def test_metadata_retries_with_conservative_mapping_after_preserve_failure(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    seen: list[list[str]] = []

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        seen.append(args)
        if args[0].endswith("ffprobe"):
            # Simulate two existing video-type streams so dynamic cover index should be v:2.
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="0\n1\n", stderr="")
        if "0:V:0" in args:
            Path(args[-1]).write_bytes(b"ok")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="Could not write header (incorrect codec parameters ?): Invalid argument",
        )

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    ffmpeg_calls = [call for call in seen if call and call[0].endswith("ffmpeg")]
    assert result.exit_code == 0
    assert ffmpeg_calls
    assert "-c:v:2" in ffmpeg_calls[0]
    assert any("0:V:0" in call for call in ffmpeg_calls)
    assert result.attempt_trace == ["A:fail(exit=1)", "B:ok"]


def test_metadata_returns_both_failures_when_compat_retry_also_fails(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        if args[0].endswith("ffprobe"):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="0\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="Invalid argument")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code == 1
    assert result.attempt_trace == ["A:fail(exit=1)", "B:fail(exit=1)"]
    assert "兼容重试" in result.warning


def test_mov_uses_attached_pic_metadata_policy() -> None:
    mode, policy = resolve_metadata_mode(Path("demo.mov"), CoverMode.METADATA)
    assert mode == CoverMode.METADATA
    assert policy.metadata_strategy == "attached_pic"


def test_first_frame_only_formats_auto_switch_when_requesting_metadata() -> None:
    mode, policy = resolve_metadata_mode(Path("demo.ts"), CoverMode.METADATA)
    assert mode == CoverMode.FIRST_FRAME
    assert policy.metadata_strategy == "first_frame_only"


def test_supported_extensions_have_explicit_policy() -> None:
    for ext in (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".mpeg", ".mpg", ".ts", ".m2ts", ".3gp", ".rmvb"):
        policy = cover_policy_for_path(Path(f"demo{ext}"))
        assert policy.extension == ext


def test_mkv_metadata_uses_attachment_strategy(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mkv"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    seen: list[list[str]] = []

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        seen.append(args)
        Path(args[-1]).write_bytes(b"ok")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code == 0
    assert seen
    first_call = seen[0]
    assert "-attach" in first_call
    assert "-metadata:s:t:0" in first_call


def test_metadata_mode_on_first_frame_only_format_returns_failure_semantics(tmp_path: Path) -> None:
    video = tmp_path / "demo.ts"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
        probe=_probe(),
    )

    assert result.exit_code != 0
    assert "不支持通用元数据封面" in result.warning
    assert result.attempt_trace == ["UNSUPPORTED:metadata"]
