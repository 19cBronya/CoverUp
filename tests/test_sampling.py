import subprocess
from pathlib import Path

from coverup.models import SampleRequest
from coverup.sampling import decide_window, sample_minute, uniform_points


def test_uniform_points_count_and_range() -> None:
    points = uniform_points(0.0, 60.0, 12)
    assert len(points) == 12
    assert all(0.0 <= p <= 60.0 for p in points)
    assert points[0] < points[-1]


def test_decide_window_normal_then_tail_then_loop() -> None:
    first = decide_window(duration=130.0, minute_index=0)
    assert first.window_start == 0.0
    assert first.window_end == 60.0
    assert first.next_minute_index == 1

    second = decide_window(duration=130.0, minute_index=1)
    assert second.window_start == 60.0
    assert second.window_end == 120.0
    assert second.next_minute_index == 2

    tail = decide_window(duration=130.0, minute_index=2)
    assert tail.window_start == 120.0
    assert tail.window_end == 130.0
    assert tail.is_tail_window is True
    assert tail.next_minute_index == 0


def test_decide_window_short_video_loops() -> None:
    first = decide_window(duration=20.0, minute_index=0)
    assert first.window_start == 0.0
    assert first.window_end == 20.0
    assert first.next_minute_index == 0


def test_sample_minute_retries_with_accurate_seek(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mov"
    video.write_bytes(b"v")
    call_count = {"n": 0}

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        call_count["n"] += 1
        out = Path(args[-1])
        if call_count["n"] == 1:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="Invalid argument")
        out.write_bytes(b"jpeg")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.sampling.run_cmd", fake_run_cmd)

    result = sample_minute(
        SampleRequest(video_path=video, minute_index=0, sample_count=1),
        duration=20.0,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
    )

    assert len(result.thumbnail_paths) == 1
    assert result.thumbnail_paths[0].exists()
    assert call_count["n"] == 2


def test_sample_minute_falls_back_to_png_when_jpeg_fails(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mov"
    video.write_bytes(b"v")
    call_count = {"n": 0}

    def fake_run_cmd(args, **_kwargs):
        args = list(args)
        call_count["n"] += 1
        out = Path(args[-1])
        if call_count["n"] <= 2:
            return subprocess.CompletedProcess(
                args=args,
                returncode=-22,
                stdout="",
                stderr="ff_frame_thread_encoder_init failed",
            )
        out.write_bytes(b"png")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.sampling.run_cmd", fake_run_cmd)

    result = sample_minute(
        SampleRequest(video_path=video, minute_index=0, sample_count=1),
        duration=20.0,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
    )

    assert len(result.thumbnail_paths) == 1
    assert result.thumbnail_paths[0].exists()
    assert result.thumbnail_paths[0].suffix.lower() == ".png"
    assert call_count["n"] == 3
