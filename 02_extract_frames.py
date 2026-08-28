#!/usr/bin/env python3
"""
02_extract_frames.py

Extracts raw frames from one or more .mov screen recordings at regular intervals
(default: 1 frame every 5 seconds) using ffmpeg into a raw frames folder.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Union


def extract_frames_single(
    video_path: Path,
    output_dir: Path,
    interval_seconds: float = 5.0,
    quality: int = 2,
    prefix: str = "frame",
    clean: bool = True
) -> int:
    """
    Extracts frames from a single video using ffmpeg.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg not found in PATH. Please install ffmpeg.")

    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    if video_path.suffix.lower() != ".mov":
        raise ValueError(f"Recording must be a .mov file: {video_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    if clean:
        for f in output_dir.glob(f"{prefix}_*.jpg"):
            f.unlink()

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")

    fps_filter = f"fps=1/{interval_seconds}" if interval_seconds >= 1 else f"fps={1.0 / interval_seconds:.4f}"
    output_pattern = str(output_dir / f"{prefix}_%06d.jpg")

    print(f"Extracting 1 frame every {interval_seconds}s from '{video_path.name}'...")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(video_path),
        "-vf", fps_filter,
        "-q:v", str(quality),
        output_pattern
    ]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"FFmpeg error for {video_path}:\n{result.stderr}", file=sys.stderr)
        raise RuntimeError(f"FFmpeg failed with exit code {result.returncode}")

    extracted = list(output_dir.glob(f"{prefix}_*.jpg"))
    print(f"Extracted {len(extracted)} frames from '{video_path.name}'.")
    return len(extracted)


def extract_frames(
    video_paths: Union[Path, List[Path]],
    output_dir: Path,
    interval_seconds: float = 5.0,
    quality: int = 2,
    clean: bool = True
) -> int:
    """
    Extracts frames from one or more videos.
    """
    if isinstance(video_paths, (str, Path)):
        video_paths = [Path(video_paths)]

    output_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for f in output_dir.glob("*.jpg"):
            f.unlink()

    total = 0
    for idx, vpath in enumerate(video_paths):
        prefix = f"vid{idx+1}_frame" if len(video_paths) > 1 else "frame"
        count = extract_frames_single(
            video_path=Path(vpath),
            output_dir=output_dir,
            interval_seconds=interval_seconds,
            quality=quality,
            prefix=prefix,
            clean=False
        )
        total += count

    print(f"Total raw frames extracted across {len(video_paths)} video(s): {total}")
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Extract raw frames from video recording(s) every N seconds using ffmpeg."
    )
    parser.add_argument("videos", nargs="+", help="Path to screen recording .mov file(s)")
    parser.add_argument("-o", "--output-dir", default="frames/raw", help="Output directory for raw frames (default: frames/raw)")
    parser.add_argument("--interval", type=float, default=5.0, help="Interval in seconds between frames (default: 5.0)")
    parser.add_argument("-q", "--quality", type=int, default=2, help="JPEG quality 1-31 where 1 is best (default: 2)")
    parser.add_argument("--no-clean", action="store_true", help="Do not delete existing frame_*.jpg in output directory before extracting")

    args = parser.parse_args()

    video_paths = [Path(v) for v in args.videos]
    out_dir = Path(args.output_dir)

    try:
        extract_frames(
            video_paths=video_paths,
            output_dir=out_dir,
            interval_seconds=args.interval,
            quality=args.quality,
            clean=not args.no_clean
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
