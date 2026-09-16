"""Video frame extraction, shared-screen cropping and timeline deduplication."""

import csv
import io
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image


def extract_frames_single(
    video_path: Path,
    output_dir: Path,
    interval_seconds: float = 5.0,
    quality: int = 2,
    prefix: str = "frame",
    clean: bool = True,
) -> int:
    """
    Extracts frames from a single video using ffmpeg.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg not found in PATH. Please install ffmpeg.")

    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    if clean:
        for f in output_dir.glob(f"{prefix}_*.jpg"):
            f.unlink()

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")

    fps_filter = f"fps=1/{interval_seconds}:start_time=0:round=up"
    output_pattern = str(output_dir / f"{prefix}_%06d.jpg")

    print(f"Extracting 1 frame every {interval_seconds}s from '{video_path.name}'...")

    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0:V:0",
        "-vf",
        fps_filter,
        "-q:v",
        str(quality),
        output_pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"FFmpeg error for {video_path}:\n{result.stderr}", file=sys.stderr)
        raise RuntimeError(f"FFmpeg failed with exit code {result.returncode}")

    extracted = list(output_dir.glob(f"{prefix}_*.jpg"))
    print(f"Extracted {len(extracted)} frames from '{video_path.name}'.")
    return len(extracted)


def extract_frames(
    video_paths: Path | list[Path],
    output_dir: Path,
    interval_seconds: float = 5.0,
    quality: int = 2,
    clean: bool = True,
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
        prefix = f"vid{idx + 1}_frame" if len(video_paths) > 1 else "frame"
        count = extract_frames_single(
            video_path=Path(vpath),
            output_dir=output_dir,
            interval_seconds=interval_seconds,
            quality=quality,
            prefix=prefix,
            clean=False,
        )
        total += count

    print(f"Total raw frames extracted across {len(video_paths)} video(s): {total}")
    return total


Box = tuple[int, int, int, int]  # left, top, right, bottom, in full-resolution pixels

DEFAULT_MAX_EDGE = 1568
DEFAULT_QUALITY = 82


@dataclass(frozen=True)
class CropSummary:
    total: int
    cropped: int
    fixed_box: Box | None = None


def group_frames_by_video(frame_files: list[Path]) -> dict[int, list[Path]]:
    """
    Groups raw frames by their originating video, using the extractor's
    `vid<N>_frame_<seq>` / `frame_<seq>` naming. Each video gets its own crop,
    because each recording can have a different window layout.
    """
    groups: dict[int, list[Path]] = {}
    for f in frame_files:
        match_vid = re.match(r"vid(\d+)_frame_(\d+)", f.name)
        vid_idx = int(match_vid.group(1)) - 1 if match_vid else 0
        groups.setdefault(vid_idx, []).append(f)

    for files in groups.values():
        files.sort(
            key=lambda p: (
                int(re.search(r"frame_(\d+)", p.name).group(1))
                if re.search(r"frame_(\d+)", p.name)
                else 0
            )
        )
    return groups


def _runs(mask):
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))


def _presentation_rectangle(image: Image.Image) -> Box | None:
    """Find a dominant landscape tile bounded by the dark Meet gutters."""
    small = image.convert("RGB")
    small.thumbnail((800, 800))
    pixels = np.asarray(small).astype(np.int16)
    height, width = pixels.shape[:2]
    if width < 200 or height < 150:
        return None
    # Exclude browser/presentation headers and call controls. A tile touching
    # these limits is ambiguous, so it will be retained as a full frame.
    top, bottom = round(height * 0.18), round(height * 0.88)
    region = pixels[top:bottom]
    neutral = (region.max(axis=2) <= 64) & (np.ptp(region, axis=2) <= 8)
    if neutral.mean() < 0.10:
        return None
    levels = region[neutral].mean(axis=1).astype(int)
    background = int(np.bincount(levels, minlength=256).argmax())
    foreground = np.max(np.abs(region - background), axis=2) > 12
    # Webcam focus glows can paint part of a gutter; require a majority of
    # the column to belong to a tile instead of treating any color as content.
    columns = foreground.mean(axis=0) > 0.55
    # Ignore single-pixel seams within a shared screen, but preserve gutters.
    for left, right in list(_runs(~columns)):
        if left > 0 and right < width and right - left < 3:
            columns[left:right] = True
    candidates = []
    for left, right in _runs(columns):
        if right - left < width * 0.40 or left < 2 or right > width - 2:
            continue
        rows = foreground[:, left + 2 : right - 2].mean(axis=1) > 0.10
        for upper, lower in _runs(rows):
            tile_width, tile_height = right - left, lower - upper
            if upper < 2 or lower > bottom - top - 2:
                continue
            if not 1.3 <= tile_width / tile_height <= 2.5:
                continue
            if tile_width * tile_height < width * height * 0.20:
                continue
            tile = foreground[upper:lower, left:right]
            if tile.mean() < 0.65:
                continue
            # Solid outer edges distinguish a complete tile from a moving
            # patch or a disconnected group of participant tiles.
            edges = (tile[2, :], tile[-3, :], tile[:, 2], tile[:, -3])
            if any(edge.mean() < 0.70 for edge in edges):
                continue
            candidates.append((left, upper + top, right, lower + top))
    if len(candidates) != 1:
        return None
    left, top, right, bottom = candidates[0]
    scale_x, scale_y = image.width / width, image.height / height
    # Round outwards with one analysis pixel of padding to preserve edge text.
    return (
        max(0, math.floor((left - 1) * scale_x)),
        max(0, math.floor((top - 1) * scale_y)),
        min(image.width, math.ceil((right + 1) * scale_x)),
        min(image.height, math.ceil((bottom + 1) * scale_y)),
    )


def _meet_presenting_header(image: Image.Image, tesseract: str) -> bool:
    header = image.crop((0, 0, image.width, round(image.height * 0.18)))
    header.thumbnail((2200, 2200))
    data = io.BytesIO()
    header.save(data, format="PNG")
    result = subprocess.run(
        [tesseract, "stdin", "stdout", "--psm", "11", "tsv"],
        input=data.getvalue(),
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Tesseract failed: {result.stderr.decode(errors='replace').strip()}"
        )
    address_bottom = None
    presenter_top = None
    for word in csv.DictReader(io.StringIO(result.stdout.decode()), delimiter="\t"):
        if float(word["conf"]) < 60:
            continue
        text = word["text"].lower()
        x, y = int(word["left"]), int(word["top"])
        if (
            re.search(r"^(?:https?://)?meet\.google\.com/[a-z]", text)
            and y < header.height * 0.5
        ):
            address_bottom = y + int(word["height"])
        if re.search(r"\b(presenting|apresentando)\b", text) and x > header.width * 0.5:
            presenter_top = y
    return (
        address_bottom is not None
        and presenter_top is not None
        and presenter_top > address_bottom
    )


def detect_meet_content_box(frame: Path, tesseract: str) -> Box | None:
    """Crop only a recognized Meet presentation; uncertainty keeps the frame."""
    with Image.open(frame) as image:
        box = _presentation_rectangle(image)
        if box is None:
            return None
        try:
            presenting = _meet_presenting_header(image, tesseract)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
            print(
                f"  {frame.name}: Meet detection unavailable ({error}); keeping full frame.",
                file=sys.stderr,
            )
            return None
        return box if presenting else None


def parse_box_string(text: str) -> Box:
    """Parses 'left,top,right,bottom'."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"Invalid box '{text}'. Expected 'left,top,right,bottom' in pixels."
        )
    try:
        left, top, right, bottom = (int(p) for p in parts)
    except ValueError:
        raise ValueError(f"Invalid box '{text}'. All four values must be integers.")
    if right <= left or bottom <= top:
        raise ValueError(
            f"Invalid box '{text}'. Right must exceed left and bottom must exceed top."
        )
    return left, top, right, bottom


def crop_and_resize(
    src: Path,
    dest: Path,
    box: Box | None,
    max_edge: int,
    quality: int,
) -> None:
    """Crops one frame to `box`, fits its long edge in `max_edge`, and writes JPEG."""
    with Image.open(src) as img:
        out = img.crop(box) if box else img.copy()
        if max_edge > 0 and max(out.size) > max_edge:
            scale = max_edge / float(max(out.size))
            out = out.resize(
                (max(1, round(out.width * scale)), max(1, round(out.height * scale))),
                Image.LANCZOS,
            )
        out.convert("RGB").save(
            dest, format="JPEG", quality=quality, optimize=True, progressive=True
        )


def crop_frames(
    raw_dir: Path,
    output_dir: Path,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_QUALITY,
    box: Box | None = None,
    detect: bool = True,
) -> dict[int, CropSummary]:
    """
    Crops and shrinks every raw frame in `raw_dir` into `output_dir`, keeping the
    filenames so the later steps can still read the sequence numbers.

    Meet presentation bounds are checked independently in every frame.
    An explicit `box` overrides detection. Returns counts per video group.
    """
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"Raw frames directory '{raw_dir}' does not exist.")

    frame_files = sorted(raw_dir.glob("*.jpg")) + sorted(raw_dir.glob("*.png"))
    if not frame_files:
        print(f"No image frames found in '{raw_dir}'.", file=sys.stderr)
        return {}

    output_dir.mkdir(parents=True, exist_ok=True)
    groups = group_frames_by_video(frame_files)

    bytes_before = sum(f.stat().st_size for f in frame_files)
    applied: dict[int, CropSummary] = {}
    tesseract = shutil.which("tesseract") if detect and box is None else None
    if detect and box is None and tesseract is None:
        print(
            "  Tesseract is unavailable; keeping full frames. Install with `brew install tesseract` for automatic Meet cropping."
        )

    for vid_idx in sorted(groups):
        files = groups[vid_idx]
        label = f"[Video Group {vid_idx + 1}]"
        print(f"\n{label} Cropping {len(files)} frame(s)...")

        if box is not None:
            print(f"  Using the box given on the command line: {box}")
        cropped = 0
        for f in files:
            frame_box = box
            if box is None and tesseract is not None:
                frame_box = detect_meet_content_box(f, tesseract)
            cropped += frame_box is not None
            crop_and_resize(f, output_dir / f.name, frame_box, max_edge, quality)
        applied[vid_idx] = CropSummary(len(files), cropped, box)
        print(f"  Cropped {cropped}/{len(files)} frames; kept the rest whole.")

    written = sorted(output_dir.glob("*.jpg"))
    bytes_after = sum(f.stat().st_size for f in written)
    saved = (1 - bytes_after / bytes_before) * 100 if bytes_before else 0.0

    with Image.open(written[0]) as sample_img:
        sample_size = sample_img.size

    print("\nDone cropping:")
    print(f"  Frames processed:  {len(written)}")
    print(f"  Frame size now:    {sample_size[0]}x{sample_size[1]}")
    print(
        f"  Size on disk:      {bytes_before / 1e6:.1f} MB -> {bytes_after / 1e6:.1f} MB ({saved:.1f}% smaller)"
    )
    print(f"  Saved to:          {output_dir}")
    return applied


def parse_offset_string(offset_str: str | int) -> int:
    """
    Parses offset string like '+277', '277', '04:37', '00:04:37', '+00:04:37' into total seconds.
    """
    if isinstance(offset_str, (int, float)):
        return int(offset_str)

    offset_str = str(offset_str).strip().lstrip("+")
    if offset_str.isdigit():
        return int(offset_str)

    parts = offset_str.split(":")
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
        raise ValueError(
            f"Invalid offset format: '{offset_str}'. Expected seconds (e.g. 277) or timestamp (e.g. 04:37 or 00:04:37)"
        )
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
        raise ImportError(
            "Pillow and imagehash are required. Install with `uv add pillow imagehash`."
        )
    with Image.open(image_path) as img:
        return imagehash.phash(img)


def _frame_number(frame_path: Path) -> int:
    """Returns the extraction sequence number, or zero for an unknown name."""
    match = re.search(r"frame_(\d+)", frame_path.name)
    return int(match.group(1)) if match else 0


def _video_time(frame_path: Path, position: int, interval_seconds: float) -> float:
    """Derives a frame's recording-relative time from its extraction filename."""
    number = _frame_number(frame_path)
    return (number - 1 if number else position) * interval_seconds


def _should_keep_frame(
    current_hash,
    previous_hash,
    video_time: float,
    previous_time: float | None,
    phash_threshold: int,
    max_interval_seconds: float,
) -> bool:
    """Keeps the first frame, visible changes, and periodic continuity frames."""
    if previous_hash is None:
        return True
    return (
        current_hash - previous_hash >= phash_threshold
        or video_time - previous_time >= max_interval_seconds
    )


def _destination_filename(
    lecture_time: int, video_index: int, existing: set[str]
) -> str:
    """Returns a non-conflicting timestamp filename for an overlapping recording."""
    filename = format_seconds_to_filename(lecture_time)
    collision = 1
    while filename in existing:
        filename = format_seconds_to_filename(
            lecture_time, suffix=f"p{video_index + 1}_{collision}"
        )
        collision += 1
    return filename


def _write_index(output_dir: Path, records: list[tuple[str, str, int, float]]) -> Path:
    """Writes the frame lookup index sorted by lecture timestamp."""
    index_path = output_dir / "index.csv"
    with open(index_path, mode="w", newline="", encoding="utf-8") as index_file:
        writer = csv.writer(index_file)
        writer.writerow(["timestamp", "file"])
        writer.writerows((timestamp, filename) for timestamp, filename, _, _ in records)
    return index_path


def _offset_list(offsets: int | list[int]) -> list[int]:
    """Normalizes the public single-offset and multi-offset forms."""
    return [offsets] if isinstance(offsets, int) else list(offsets)


def _dedupe_group(
    frame_files: list[Path],
    video_index: int,
    offset_seconds: int,
    interval_seconds: float,
    phash_threshold: int,
    max_interval_seconds: float,
    output_dir: Path,
    existing_filenames: set[str],
) -> list[tuple[str, str, int, float]]:
    """Deduplicates one recording group and returns its saved frame records."""
    print(
        f"\n[Video Group {video_index + 1}] Processing {len(frame_files)} frames with offset = {offset_seconds}s ({format_seconds_to_timestamp(offset_seconds)})..."
    )
    records: list[tuple[str, str, int, float]] = []
    last_hash = None
    last_time = None

    for position, frame_path in enumerate(frame_files):
        video_time = _video_time(frame_path, position, interval_seconds)
        current_hash = get_phash(frame_path)
        if not _should_keep_frame(
            current_hash,
            last_hash,
            video_time,
            last_time,
            phash_threshold,
            max_interval_seconds,
        ):
            continue

        lecture_time = round(video_time + offset_seconds)
        filename = _destination_filename(lecture_time, video_index, existing_filenames)
        existing_filenames.add(filename)
        shutil.copy2(frame_path, output_dir / filename)
        records.append(
            (
                format_seconds_to_timestamp(lecture_time),
                filename,
                lecture_time,
                video_time,
            )
        )
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
    offsets: int | list[int] = 0,
    interval_seconds: float = 5.0,
    phash_threshold: int = 6,
    max_interval_seconds: float = 30.0,
    clean_raw: bool = False,
) -> list[tuple[str, str, int, float]]:
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

    video_groups = group_frames_by_video(all_frame_files)

    saved_records = []
    seen_dest_filenames = set()

    for vid_idx in sorted(video_groups):
        offset_sec = (
            offsets_list[vid_idx] if vid_idx < len(offsets_list) else offsets_list[-1]
        )
        saved_records.extend(
            _dedupe_group(
                video_groups[vid_idx],
                vid_idx,
                offset_sec,
                interval_seconds,
                phash_threshold,
                max_interval_seconds,
                output_dir,
                seen_dest_filenames,
            )
        )

    # Sort saved records by lecture time
    saved_records.sort(key=lambda r: r[2])

    index_csv_path = _write_index(output_dir, saved_records)

    total_raw = len(all_frame_files)
    total_kept = len(saved_records)
    reduction = ((total_raw - total_kept) / total_raw * 100) if total_raw > 0 else 0

    print("\nDone deduplicating:")
    print(f"  Total raw frames:  {total_raw}")
    print(f"  Retained frames:   {total_kept} ({reduction:.1f}% reduction)")
    print(f"  Saved to:          {output_dir}")
    print(f"  Index generated:   {index_csv_path}")

    _clean_raw_directory(raw_dir, output_dir, clean_raw)

    return saved_records
