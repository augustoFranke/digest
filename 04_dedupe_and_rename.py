#!/usr/bin/env python3
"""
04_dedupe_and_rename.py

Deduplicates lecture frames using perceptual hash (pHash) and renames the kept
frames using the lecture transcript timestamp:
    lecture_time = video_time + offset_seconds

Input is the cropped frame directory produced by 03_crop_frames.py, so the hash
compares lecture content instead of the call's furniture.

Output:
    frames/
    ├── index.csv
    ├── 00-04-37.jpg
    ├── 00-04-52.jpg
    └── ...
"""

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path
from typing import List, Tuple, Optional, Union, Dict

try:
    from PIL import Image
    import imagehash
except ImportError:
    Image = None
    imagehash = None


def parse_offset_string(offset_str: Union[str, int]) -> int:
    """
    Parses offset string like '+277', '277', '04:37', '00:04:37', '+00:04:37' into total seconds.
    """
    if isinstance(offset_str, (int, float)):
        return int(offset_str)

    offset_str = str(offset_str).strip().lstrip('+')
    if offset_str.isdigit():
        return int(offset_str)

    parts = offset_str.split(':')
    try:
        parts = [int(p) for p in parts]
        if len(parts) == 3:
            h, m, s = parts
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = parts
            return m * 60 + s
        elif len(parts) == 1:
            return parts[0]
    except ValueError:
        raise ValueError(f"Invalid offset format: '{offset_str}'. Expected seconds (e.g. 277) or timestamp (e.g. 04:37 or 00:04:37)")
    raise ValueError(f"Invalid offset format: '{offset_str}'")


def format_seconds_to_filename(seconds: int, suffix: str = "") -> str:
    """
    Converts seconds to 'HH-MM-SS.jpg' format.
    """
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if suffix:
        return f"{h:02d}-{m:02d}-{s:02d}_{suffix}.jpg"
    return f"{h:02d}-{m:02d}-{s:02d}.jpg"


def format_seconds_to_timestamp(seconds: int) -> str:
    """
    Converts seconds to 'HH:MM:SS' format.
    """
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_phash(image_path: Path):
    """
    Computes perceptual hash (pHash) for an image.
    """
    if Image is None or imagehash is None:
        raise ImportError("Pillow and imagehash are required. Install with `uv add pillow imagehash`.")
    with Image.open(image_path) as img:
        return imagehash.phash(img)


def _frame_groups(frame_files: List[Path]) -> Dict[int, List[Path]]:
    """Groups extraction filenames by their originating recording."""
    groups: Dict[int, List[Path]] = {}
    for frame_path in frame_files:
        match = re.match(r"vid(\d+)_frame_(\d+)", frame_path.name)
        video_index = int(match.group(1)) - 1 if match else 0
        groups.setdefault(video_index, []).append(frame_path)

    for files in groups.values():
        files.sort(key=_frame_number)
    return groups


def _frame_number(frame_path: Path) -> int:
    """Returns the extraction sequence number, or zero for an unknown name."""
    match = re.search(r"frame_(\d+)", frame_path.name)
    return int(match.group(1)) if match else 0


def _video_time(frame_path: Path, position: int, interval_seconds: float) -> float:
    """Derives a frame's recording-relative time from its extraction filename."""
    number = _frame_number(frame_path)
    return (number - 1 if number else position) * interval_seconds


def _should_keep_frame(current_hash, previous_hash, video_time: float, previous_time: Optional[float], phash_threshold: int, max_interval_seconds: float) -> bool:
    """Keeps the first frame, visible changes, and periodic continuity frames."""
    if previous_hash is None:
        return True
    return current_hash - previous_hash >= phash_threshold or video_time - previous_time >= max_interval_seconds


def _destination_filename(lecture_time: int, video_index: int, existing: set[str]) -> str:
    """Returns a non-conflicting timestamp filename for an overlapping recording."""
    filename = format_seconds_to_filename(lecture_time)
    collision = 1
    while filename in existing:
        filename = format_seconds_to_filename(lecture_time, suffix=f"p{video_index + 1}_{collision}")
        collision += 1
    return filename


def _write_index(output_dir: Path, records: List[Tuple[str, str, int, float]]) -> Path:
    """Writes the frame lookup index sorted by lecture timestamp."""
    index_path = output_dir / "index.csv"
    with open(index_path, mode="w", newline="", encoding="utf-8") as index_file:
        writer = csv.writer(index_file)
        writer.writerow(["timestamp", "file"])
        writer.writerows((timestamp, filename) for timestamp, filename, _, _ in records)
    return index_path


def _offset_list(offsets: Union[int, List[int]]) -> List[int]:
    """Normalizes the public single-offset and multi-offset forms."""
    return [offsets] if isinstance(offsets, int) else list(offsets)


def _dedupe_group(
    frame_files: List[Path],
    video_index: int,
    offset_seconds: int,
    interval_seconds: float,
    phash_threshold: int,
    max_interval_seconds: float,
    output_dir: Path,
    existing_filenames: set[str],
) -> List[Tuple[str, str, int, float]]:
    """Deduplicates one recording group and returns its saved frame records."""
    print(f"\n[Video Group {video_index + 1}] Processing {len(frame_files)} frames with offset = {offset_seconds}s ({format_seconds_to_timestamp(offset_seconds)})...")
    records: List[Tuple[str, str, int, float]] = []
    last_hash = None
    last_time = None

    for position, frame_path in enumerate(frame_files):
        video_time = _video_time(frame_path, position, interval_seconds)
        current_hash = get_phash(frame_path)
        if not _should_keep_frame(current_hash, last_hash, video_time, last_time, phash_threshold, max_interval_seconds):
            continue

        lecture_time = int(round(video_time + offset_seconds))
        filename = _destination_filename(lecture_time, video_index, existing_filenames)
        existing_filenames.add(filename)
        shutil.copy2(frame_path, output_dir / filename)
        records.append((format_seconds_to_timestamp(lecture_time), filename, lecture_time, video_time))
        last_hash = current_hash
        last_time = video_time
    return records


def _clean_raw_directory(raw_dir: Path, output_dir: Path, clean_raw: bool) -> None:
    """Removes intermediates only when requested and distinct from the output."""
    if clean_raw and raw_dir.resolve() != output_dir.resolve():
        shutil.rmtree(raw_dir)
        print(f"  Cleaned raw directory: {raw_dir}")


def dedupe_and_rename_frames(
    raw_dir: Path,
    output_dir: Path,
    offsets: Union[int, List[int]] = 0,
    interval_seconds: float = 5.0,
    phash_threshold: int = 6,
    max_interval_seconds: float = 30.0,
    clean_raw: bool = False
) -> List[Tuple[str, str, int, float]]:
    """
    Processes raw frames in raw_dir:
      - Groups frames by video prefix (vid1_, vid2_, or standard frame_)
      - Calculates pHash for each frame
      - Retains frames when pHash_distance >= phash_threshold OR elapsed time >= max_interval_seconds
      - Renames to HH-MM-SS.jpg based on lecture_time = video_time + offset_seconds
      - Writes index.csv

    Returns list of saved frames: (timestamp_str, filename, lecture_sec, video_sec)
    """
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"Raw frames directory '{raw_dir}' does not exist.")

    all_frame_files = sorted(raw_dir.glob("*.jpg")) + sorted(raw_dir.glob("*.png"))
    if not all_frame_files:
        print(f"No image frames found in '{raw_dir}'.", file=sys.stderr)
        return []

    offsets_list = _offset_list(offsets)
    output_dir.mkdir(parents=True, exist_ok=True)

    video_groups = _frame_groups(all_frame_files)

    saved_records = []
    seen_dest_filenames = set()

    for vid_idx in sorted(video_groups):
        offset_sec = offsets_list[vid_idx] if vid_idx < len(offsets_list) else offsets_list[-1]
        saved_records.extend(_dedupe_group(
            video_groups[vid_idx], vid_idx, offset_sec, interval_seconds,
            phash_threshold, max_interval_seconds, output_dir, seen_dest_filenames,
        ))

    # Sort saved records by lecture time
    saved_records.sort(key=lambda r: r[2])

    index_csv_path = _write_index(output_dir, saved_records)

    total_raw = len(all_frame_files)
    total_kept = len(saved_records)
    reduction = ((total_raw - total_kept) / total_raw * 100) if total_raw > 0 else 0

    print(f"\nDone deduplicating:")
    print(f"  Total raw frames:  {total_raw}")
    print(f"  Retained frames:   {total_kept} ({reduction:.1f}% reduction)")
    print(f"  Saved to:          {output_dir}")
    print(f"  Index generated:   {index_csv_path}")

    _clean_raw_directory(raw_dir, output_dir, clean_raw)

    return saved_records


def main():
    parser = argparse.ArgumentParser(
        description="Deduplicate lecture frames using perceptual hash and rename by lecture timestamp."
    )
    parser.add_argument("raw_dir", nargs="?", default="frames/cropped", help="Directory containing cropped frames (default: frames/cropped)")
    parser.add_argument("-o", "--output-dir", default="frames", help="Output directory for deduplicated frames (default: frames)")
    parser.add_argument("--offsets", nargs="+", default=["00:00:00"], help="Transcript offset(s) for video(s) (e.g. 04:37 10:43)")
    parser.add_argument("--interval", type=float, default=5.0, help="Interval in seconds used when frames were extracted (default: 5.0)")
    parser.add_argument("--phash-threshold", type=int, default=6, help="pHash hamming distance threshold (default: 6)")
    parser.add_argument("--max-interval", type=float, default=30.0, help="Max seconds before forcing a frame save (default: 30.0)")
    parser.add_argument("--clean-raw", action="store_true", help="Delete the input directory after processing")

    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.output_dir)
    offsets_sec = [parse_offset_string(off) for off in args.offsets]

    try:
        dedupe_and_rename_frames(
            raw_dir=raw_dir,
            output_dir=out_dir,
            offsets=offsets_sec,
            interval_seconds=args.interval,
            phash_threshold=args.phash_threshold,
            max_interval_seconds=args.max_interval,
            clean_raw=args.clean_raw
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
