from __future__ import annotations

import subprocess
from pathlib import Path

from coverup.models import CoverMode, ProbeResult
from coverup.processor import cover_policy_for_path, process_in_place


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
    policy = cover_policy_for_path(Path("demo.mov"))
    assert policy.metadata_strategy == "attached_pic"


def test_first_frame_only_formats_have_explicit_policy() -> None:
    policy = cover_policy_for_path(Path("demo.ts"))
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


# ---------------------------------------------------------------------------
# MP4Box fast-path tests
# ---------------------------------------------------------------------------


def test_mp4box_metadata_success(monkeypatch, tmp_path: Path) -> None:
    """MP4Box fast path succeeds on an .mp4 file."""
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video-data")
    cover.write_bytes(b"cover-data")

    captured: list[list[str]] = []

    def fake_run_cmd(args, **_kwargs):
        captured.append(list(args))
        # MP4Box writes nothing to the output file; the copy is already there.
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    bins = type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe"), "mp4box": Path("MP4Box")})()

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=bins,
        probe=_probe(),
    )

    assert result.exit_code == 0
    assert result.attempt_trace == ["MP4BOX:ok(itags)"]
    # Verify MP4Box was called with the expected arguments.
    assert captured
    mp4box_call = captured[0]
    assert mp4box_call[0].endswith("MP4Box")
    assert "-itags" in mp4box_call
    assert any(cover.name in arg for arg in mp4box_call)
    # Output file should be the temp file (ends with .coverup.tmp.mp4).
    assert mp4box_call[-1].endswith(".coverup.tmp.mp4")


def test_mp4box_metadata_falls_back_to_ffmpeg(monkeypatch, tmp_path: Path) -> None:
    """When MP4Box fails, fall through to FFmpeg metadata mode."""
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video-data")
    cover.write_bytes(b"cover-data")

    captured: list[list[str]] = []
    calls = {"count": 0}

    def fake_run_cmd(args, **_kwargs):
        args_list = list(args)
        captured.append(args_list)
        calls["count"] += 1
        # First call is MP4Box — simulate failure.
        if calls["count"] == 1:
            return subprocess.CompletedProcess(args=args_list, returncode=1, stdout="", stderr="mp4box error")
        # Second call is FFmpeg — simulate success.
        Path(args_list[-1]).write_bytes(b"ok")
        return subprocess.CompletedProcess(args=args_list, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    bins = type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe"), "mp4box": Path("MP4Box")})()

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=bins,
        probe=_probe(),
    )

    assert result.exit_code == 0
    # Should show FFmpeg's metadata success trace (the fallback path).
    assert any("ok" in t for t in result.attempt_trace)
    assert not any("MP4BOX" in t for t in result.attempt_trace)
    # First call should be MP4Box.
    assert captured[0][0].endswith("MP4Box")
    # A subsequent call should be FFmpeg (the metadata writer).
    assert any(call[0].endswith("ffmpeg") for call in captured)


def test_mp4box_skipped_when_unavailable(monkeypatch, tmp_path: Path) -> None:
    """When MP4Box is not available, use FFmpeg directly."""
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video-data")
    cover.write_bytes(b"cover-data")

    captured: list[list[str]] = []

    def fake_run_cmd(args, **_kwargs):
        args_list = list(args)
        captured.append(args_list)
        Path(args_list[-1]).write_bytes(b"ok")
        return subprocess.CompletedProcess(args=args_list, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    # No mp4box field at all — simulates older clients or missing binary.
    bins = type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})()

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=bins,
        probe=_probe(),
    )

    assert result.exit_code == 0
    # All calls should be ffmpeg — no MP4Box call.
    assert all(call[0].endswith("ffmpeg") or call[0].endswith("ffprobe") for call in captured)
    # The attempt_trace should show FFmpeg success, not MP4Box.
    assert any("ok" in t for t in result.attempt_trace)


def test_mp4box_skipped_for_non_iso_format(monkeypatch, tmp_path: Path) -> None:
    """MKV files use FFmpeg attachment mode even when MP4Box is available."""
    video = tmp_path / "demo.mkv"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video-data")
    cover.write_bytes(b"cover-data")

    captured: list[list[str]] = []

    def fake_run_cmd(args, **_kwargs):
        args_list = list(args)
        captured.append(args_list)
        Path(args_list[-1]).write_bytes(b"ok")
        return subprocess.CompletedProcess(args=args_list, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.processor.run_cmd", fake_run_cmd)

    bins = type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe"), "mp4box": Path("MP4Box")})()

    result = process_in_place(
        video_path=video,
        cover_path=cover,
        mode=CoverMode.METADATA,
        bins=bins,
        probe=_probe(),
    )

    assert result.exit_code == 0
    # MKV should use FFmpeg attachment strategy, not MP4Box.
    assert not any(call[0].endswith("MP4Box") for call in captured)
    assert any("-attach" in call for call in captured)
