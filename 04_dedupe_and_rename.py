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

    if isinstance(offsets, int):
        offsets_list = [offsets]
    else:
        offsets_list = list(offsets)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Group files by video index if prefixed with vid<N>_
    video_groups: Dict[int, List[Path]] = {}
    for f in all_frame_files:
        match_vid = re.match(r'vid(\d+)_frame_(\d+)', f.name)
        if match_vid:
            vid_idx = int(match_vid.group(1)) - 1
            video_groups.setdefault(vid_idx, []).append(f)
        else:
            video_groups.setdefault(0, []).append(f)

    # Sort each group numerically
    for vid_idx in video_groups:
        video_groups[vid_idx].sort(
            key=lambda p: int(re.search(r'frame_(\d+)', p.name).group(1)) if re.search(r'frame_(\d+)', p.name) else 0
        )

    saved_records = []
    seen_dest_filenames = set()

    for vid_idx in sorted(video_groups.keys()):
        frame_files = video_groups[vid_idx]
        offset_sec = offsets_list[vid_idx] if vid_idx < len(offsets_list) else offsets_list[-1]

        print(f"\n[Video Group {vid_idx + 1}] Processing {len(frame_files)} frames with offset = {offset_sec}s ({format_seconds_to_timestamp(offset_sec)})...")

        last_saved_hash = None
        last_saved_video_time = None

        for idx, frame_path in enumerate(frame_files):
            num_match = re.search(r'frame_(\d+)', frame_path.name)
            if num_match:
                frame_num = int(num_match.group(1))
                video_time = (frame_num - 1) * interval_seconds
            else:
                video_time = idx * interval_seconds

            lecture_time_sec = int(round(video_time + offset_sec))
            curr_hash = get_phash(frame_path)

            should_save = False
            if last_saved_hash is None:
                should_save = True
            else:
                hash_dist = curr_hash - last_saved_hash
                time_diff = video_time - last_saved_video_time
                if hash_dist >= phash_threshold or time_diff >= max_interval_seconds:
                    should_save = True

            if should_save:
                dest_filename = format_seconds_to_filename(lecture_time_sec)
                # Handle possible collision if two videos overlap in timestamp
                collision_count = 1
                while dest_filename in seen_dest_filenames:
                    dest_filename = format_seconds_to_filename(lecture_time_sec, suffix=f"p{vid_idx+1}_{collision_count}")
                    collision_count += 1

                seen_dest_filenames.add(dest_filename)
                dest_path = output_dir / dest_filename
                shutil.copy2(frame_path, dest_path)

                ts_str = format_seconds_to_timestamp(lecture_time_sec)
                saved_records.append((ts_str, dest_filename, lecture_time_sec, video_time))

                last_saved_hash = curr_hash
                last_saved_video_time = video_time

    # Sort saved records by lecture time
    saved_records.sort(key=lambda r: r[2])

    # Generate index.csv in output_dir
    index_csv_path = output_dir / "index.csv"
    with open(index_csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "file"])
        for ts_str, filename, _, _ in saved_records:
            writer.writerow([ts_str, filename])

    total_raw = len(all_frame_files)
    total_kept = len(saved_records)
    reduction = ((total_raw - total_kept) / total_raw * 100) if total_raw > 0 else 0

    print(f"\nDone deduplicating:")
    print(f"  Total raw frames:  {total_raw}")
    print(f"  Retained frames:   {total_kept} ({reduction:.1f}% reduction)")
    print(f"  Saved to:          {output_dir}")
    print(f"  Index generated:   {index_csv_path}")

    if clean_raw and raw_dir.resolve() != output_dir.resolve():
        shutil.rmtree(raw_dir)
        print(f"  Cleaned raw directory: {raw_dir}")

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
