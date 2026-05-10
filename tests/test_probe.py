from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coverup.probe import extract_attached_cover_preview, probe_video


def test_probe_video_parses_attached_pic_stream_index(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"v")

    payload = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "30.0"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30000/1001",
                "disposition": {"attached_pic": 0},
            },
            {
                "index": 2,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 1},
            },
        ],
    }

    def fake_run_cmd(args, **_kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("coverup.probe.run_cmd", fake_run_cmd)

    result = probe_video(video, bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})())
    assert result.has_attached_pic is True
    assert result.attached_pic_stream_index == 2


def test_extract_attached_cover_preview_success(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"v")
    out = tmp_path / "cover.png"

    probe = type(
        "Probe",
        (),
        {
            "has_attached_pic": True,
            "attached_pic_stream_index": 1,
        },
    )()

    monkeypatch.setattr("coverup.probe._cached_cover_path", lambda _path: out)

    def fake_run_cmd(args, **_kwargs):
        Path(args[-1]).write_bytes(b"png")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("coverup.probe.run_cmd", fake_run_cmd)

    result = extract_attached_cover_preview(
        video,
        probe,
        bins=type("Bins", (), {"ffmpeg": Path("ffmpeg"), "ffprobe": Path("ffprobe")})(),
    )
    assert result == out
    assert out.exists()
