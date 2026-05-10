from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .ffmpeg_tools import FfmpegError, locate_binaries
from .models import CoverMode, ProbeResult, SampleRequest, ScanOptions
from .probe import probe_video
from .processor import process_in_place
from .sampling import sample_minute
from .scanner import scan_videos


def _probe_to_dict(result: ProbeResult) -> dict:
    return asdict(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coverup-cli")
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--scan-dir", type=Path)
    parser.add_argument("--recursive", action="store_true", default=False)
    parser.add_argument("--sample-minute", type=Path)
    parser.add_argument("--minute-index", type=int, default=0)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--run-job", type=Path)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--cover", type=Path)
    parser.add_argument("--mode", choices=[m.value for m in CoverMode], default=CoverMode.METADATA.value)
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    action_selected = any(
        [
            args.scan_dir is not None,
            args.probe is not None,
            args.sample_minute is not None,
            args.run_job is not None,
            args.video is not None and args.cover is not None,
        ]
    )
    if not action_selected:
        parser.print_help()
        return 0

    try:
        bins = locate_binaries()
    except FfmpegError as err:
        print(str(err), file=sys.stderr)
        return 2

    if args.scan_dir:
        options = ScanOptions(directory_path=args.scan_dir, recursive=args.recursive, deduplicate=True)
        files = scan_videos(options)
        print(json.dumps([str(p) for p in files], ensure_ascii=False, indent=2))
        return 0

    if args.probe:
        result = probe_video(args.probe, bins)
        print(json.dumps(_probe_to_dict(result), ensure_ascii=False, indent=2))
        return 0

    if args.sample_minute:
        probe = probe_video(args.sample_minute, bins)
        result = sample_minute(
            SampleRequest(video_path=args.sample_minute, minute_index=args.minute_index, sample_count=args.count),
            duration=probe.duration,
            bins=bins,
        )
        payload = {
            "time_points": result.time_points,
            "thumbnail_paths": [str(p) for p in result.thumbnail_paths],
            "window_start": result.window_start,
            "window_end": result.window_end,
            "is_tail_window": result.is_tail_window,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.run_job:
        payload = json.loads(args.run_job.read_text(encoding="utf-8"))
        video = Path(payload["video_path"])
        cover = Path(payload["cover_path"])
        mode = CoverMode(payload.get("mode", CoverMode.METADATA.value))
        probe = probe_video(video, bins)
        result = process_in_place(video, cover, mode, bins, probe)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        return 0 if result.exit_code == 0 else result.exit_code

    if args.video and args.cover:
        mode = CoverMode(args.mode)
        probe = probe_video(args.video, bins)
        result = process_in_place(args.video, args.cover, mode, bins, probe)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        return 0 if result.exit_code == 0 else result.exit_code

    parser.print_help()
    return 0
