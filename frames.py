"""Video frame extraction, shared-screen cropping and timeline deduplication."""

import csv
import re
import shutil
import subprocess
import sys
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

ANALYSIS_DOWNSCALE = 8
DEFAULT_SAMPLE = 24
DEFAULT_MAX_EDGE = 1568
DEFAULT_QUALITY = 82

# A cell counts as content when it changes at least this share of what the
# busiest cells change, never below the absolute floor. Measured on the real
# lectures at these settings: the shared screen averages ~106 levels of
# frame-to-frame change, the participant tiles ~29, the black letterbox ~22,
# against a peak of ~147. A third of the peak is the gap between the first and
# the rest; a lower cut lets the tiles in and the crop then swallows the call.
ACTIVITY_THRESHOLD_FRACTION = 0.30
ACTIVITY_FLOOR = 3.0

# The kept region has to account for most of the change in the frame. If it does
# not, the detector locked onto something that is moving beside the lecture —
# the tile grid, most likely — and the whole frame is the safer answer.
MIN_ACTIVITY_SHARE = 0.60

# A detected box has to look like a shared screen, or we do not trust it.
MIN_SIDE_FRACTION = 0.15
MIN_AREA_FRACTION = 0.05
MAX_AREA_FRACTION = 0.98

# An edge is "quiet" below this share of the box's own median activity, and no
# more than this share of a side may be trimmed away.
TRIM_RATIO = 0.25
MAX_TRIM_FRACTION = 0.25

# Keeping more than this share of the frame is not wrong, but it is the
# signature of a detection that separated nothing. Worth saying out loud.
SUSPICIOUS_AREA_FRACTION = 0.70


def _require_deps() -> None:
    if np is None or Image is None:
        raise ImportError("numpy and Pillow are required. Install with `uv sync`.")


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


def _change_map(frame_files: list[Path], sample: int, downscale: int):
    """
    Per-pixel mean absolute difference between consecutive sampled frames,
    computed at 1/downscale scale. Returns (change_map, full_width, full_height).

    Consecutive differences rather than a standard deviation over the whole
    sample: both are large for the shared screen, but only the deviation is
    large for something that sits in two states over an hour — a control bar
    that hides itself, a webcam tile whose owner walked away. Differencing
    first charges each cell for how often it changes, not for how far apart its
    extremes are, and that is what tells the lecture from the call around it.
    """
    picks = sorted(
        set(
            np.linspace(0, len(frame_files) - 1, min(sample, len(frame_files)))
            .astype(int)
            .tolist()
        )
    )

    stack = []
    full_size = None
    for i in picks:
        with Image.open(frame_files[i]) as img:
            gray = img.convert("L")
            full_size = gray.size
            small = gray.resize(
                (max(1, gray.width // downscale), max(1, gray.height // downscale)),
                Image.BILINEAR,
            )
            stack.append(np.asarray(small, dtype=np.float32))

    shapes = {a.shape for a in stack}
    if len(shapes) != 1:
        raise ValueError(f"Frames in this group have inconsistent sizes: {shapes}")

    return (
        np.abs(np.diff(np.stack(stack), axis=0)).mean(axis=0),
        full_size[0],
        full_size[1],
    )


def _erode(mask):
    """3x3 erosion. Removes structures thinner than the kernel — the call's text rows."""
    out = mask.copy()
    out[1:, :] &= mask[:-1, :]
    out[:-1, :] &= mask[1:, :]
    out[:, 1:] &= mask[:, :-1]
    out[:, :-1] &= mask[:, 1:]
    return out


def _box_blur(arr, radius: int):
    """Mean over a (2*radius+1) square, via a summed-area table."""
    pad = np.pad(arr, radius + 1, mode="edge")
    integral = pad.cumsum(axis=0).cumsum(axis=1)
    size = 2 * radius + 1
    h, w = arr.shape
    total = (
        integral[size : size + h, size : size + w]
        - integral[0:h, size : size + w]
        - integral[size : size + h, 0:w]
        + integral[0:h, 0:w]
    )
    return total / (size * size)


def _trim_quiet_edges(
    change, box_cells, ratio: float = TRIM_RATIO, max_trim: float = MAX_TRIM_FRACTION
):
    """
    Shaves rows and columns off the edges of `box_cells` while they are far
    quieter than the box's own interior.

    The blob search returns a bounding box, and a bounding box is greedy: one
    live webcam tile touching the shared screen, or a window that moved during
    the lecture, drags a whole edge outwards. Those edges are quiet compared to
    the lecture content, so they can be shaved off. The trim is bounded so a
    genuinely calm lecture cannot be whittled away.
    """
    left, top, right, bottom = box_cells
    reference = float(np.median(change[top : bottom + 1, left : right + 1]))
    if reference <= 0:
        return box_cells

    floor = ratio * reference
    min_width = max(1, round((right - left + 1) * (1 - max_trim)))
    min_height = max(1, round((bottom - top + 1) * (1 - max_trim)))

    trimming = True
    while trimming:
        trimming = False
        if bottom - top + 1 > min_height:
            if change[top, left : right + 1].mean() < floor:
                top += 1
                trimming = True
            elif change[bottom, left : right + 1].mean() < floor:
                bottom -= 1
                trimming = True
        if right - left + 1 > min_width:
            if change[top : bottom + 1, left].mean() < floor:
                left += 1
                trimming = True
            elif change[top : bottom + 1, right].mean() < floor:
                right -= 1
                trimming = True

    return left, top, right, bottom


def _component_containing(mask, seed: tuple[int, int]):
    """Flood fill of `mask` from `seed`, by repeated 4-neighbour dilation."""
    current = np.zeros_like(mask)
    current[seed] = True
    while True:
        grown = current.copy()
        grown[1:, :] |= current[:-1, :]
        grown[:-1, :] |= current[1:, :]
        grown[:, 1:] |= current[:, :-1]
        grown[:, :-1] |= current[:, 1:]
        grown &= mask
        if grown.sum() == current.sum():
            return current
        current = grown


def _activity_mask(change, verbose: bool):
    """Returns cells whose repeated change is strong enough to be lecture content."""
    peak = float(np.percentile(change, 99.5))
    if peak < 2.0:
        if verbose:
            print("  Frames barely change over time; keeping the full frame.")
        return None

    mask = _erode(change > max(ACTIVITY_FLOOR, ACTIVITY_THRESHOLD_FRACTION * peak))
    if not mask.any() and verbose:
        print("  No region survived the activity threshold; keeping the full frame.")
    return mask if mask.any() else None


def _content_cells(change, mask) -> Box:
    """Finds and trims the connected active region around the densest area."""
    weighted_change = np.where(mask, change, 0.0)
    seed = np.unravel_index(
        int(np.argmax(_box_blur(weighted_change, radius=3))), change.shape
    )
    if not mask[seed]:
        seed = np.unravel_index(int(np.argmax(weighted_change)), change.shape)

    component = _component_containing(mask, seed)
    rows = np.where(component.any(axis=1))[0]
    cols = np.where(component.any(axis=0))[0]
    cells = (
        max(0, int(cols.min()) - 1),
        max(0, int(rows.min()) - 1),
        min(change.shape[1] - 1, int(cols.max()) + 1),
        min(change.shape[0] - 1, int(rows.max()) + 1),
    )
    return _trim_quiet_edges(change, cells)


def _cell_box_to_frame_box(
    cells: Box, downscale: int, full_width: int, full_height: int
) -> Box:
    """Scales inclusive analysis cells back to image coordinates."""
    left, top, right, bottom = cells
    return (
        left * downscale,
        top * downscale,
        min(full_width, (right + 1) * downscale),
        min(full_height, (bottom + 1) * downscale),
    )


def _report_detected_box(
    box: Box, full_width: int, full_height: int, verbose: bool
) -> None:
    """Explains a successful detection and flags a potentially broad crop."""
    if not verbose:
        return
    left, top, right, bottom = box
    kept = ((right - left) * (bottom - top)) / float(full_width * full_height)
    print(
        f"  Detected content box {left},{top} -> {right},{bottom} "
        f"({right - left}x{bottom - top}, {kept:.0%} of the frame)"
    )
    if kept <= SUSPICIOUS_AREA_FRACTION:
        return
    print(
        "  That is most of the frame. Two things cause it, and they need opposite responses:"
    )
    print(
        "    - The window moved during the recording, or the call's layout changed (tiles "
        "from the side to the top, a chat panel opening). The box is then the union of every "
        "position the lecture occupied, and it is correct — no single crop does better."
    )
    print(
        "    - The sample covers too short a stretch. Over a few minutes the shared screen "
        "sits still while the webcams move, so the detection separates nothing."
    )
    print(
        "  Look at a frame from early and one from late before overriding with --crop-box."
    )


def detect_content_box(
    frame_files: list[Path],
    sample: int = DEFAULT_SAMPLE,
    downscale: int = ANALYSIS_DOWNSCALE,
    verbose: bool = True,
) -> Box | None:
    """
    Finds the region of the frame that carries the lecture, or None when the
    frames give no trustworthy answer.

    Everything the call itself draws — window chrome, participant tiles, the
    control bar, letterbox — either sits still, blinks a handful of times, or
    moves gently. The shared screen is the one large area that rewrites itself
    over and over, so: threshold the frame-to-frame change, erode away the thin
    strips, and take the bounding box of the blob containing the densest
    activity.
    """
    _require_deps()

    if len(frame_files) < 4:
        if verbose:
            print(
                f"  Only {len(frame_files)} frame(s) — too few to detect a crop; keeping the full frame."
            )
        return None

    change, full_w, full_h = _change_map(frame_files, sample, downscale)
    mask = _activity_mask(change, verbose)
    if mask is None:
        return None

    cells = _content_cells(change, mask)
    left_c, top_c, right_c, bottom_c = cells
    share = float(
        change[top_c : bottom_c + 1, left_c : right_c + 1].sum() / change.sum()
    )
    if share < MIN_ACTIVITY_SHARE:
        if verbose:
            print(
                f"  The detected region holds only {share:.0%} of the change in the frame, so the "
                "lecture is probably not what was detected; keeping the full frame."
            )
        return None

    box = _cell_box_to_frame_box(cells, downscale, full_w, full_h)
    if not _box_is_plausible(box, full_w, full_h, verbose=verbose):
        return None
    _report_detected_box(box, full_w, full_h, verbose)
    return box


def _box_is_plausible(box: Box, full_w: int, full_h: int, verbose: bool = True) -> bool:
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if width < MIN_SIDE_FRACTION * full_w or height < MIN_SIDE_FRACTION * full_h:
        if verbose:
            print(
                f"  Detected box {width}x{height} is too small to be the shared screen; keeping the full frame."
            )
        return False
    area = (width * height) / float(full_w * full_h)
    if area < MIN_AREA_FRACTION or area > MAX_AREA_FRACTION:
        if verbose:
            print(
                f"  Detected box covers {area:.0%} of the frame, which is implausible; keeping the full frame."
            )
        return False
    return True


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
    sample: int = DEFAULT_SAMPLE,
    box: Box | None = None,
    detect: bool = True,
) -> dict[int, Box | None]:
    """
    Crops and shrinks every raw frame in `raw_dir` into `output_dir`, keeping the
    filenames so the later steps can still read the sequence numbers.

    A crop is detected per video group. `box` overrides detection for every group.
    Returns the box applied to each group, None meaning the full frame was kept.
    """
    _require_deps()

    if not raw_dir.is_dir():
        raise FileNotFoundError(f"Raw frames directory '{raw_dir}' does not exist.")

    frame_files = sorted(raw_dir.glob("*.jpg")) + sorted(raw_dir.glob("*.png"))
    if not frame_files:
        print(f"No image frames found in '{raw_dir}'.", file=sys.stderr)
        return {}

    output_dir.mkdir(parents=True, exist_ok=True)
    groups = group_frames_by_video(frame_files)

    bytes_before = sum(f.stat().st_size for f in frame_files)
    applied: dict[int, Box | None] = {}

    for vid_idx in sorted(groups):
        files = groups[vid_idx]
        label = f"[Video Group {vid_idx + 1}]"
        print(f"\n{label} Cropping {len(files)} frame(s)...")

        if box is not None:
            group_box = box
            print(f"  Using the box given on the command line: {group_box}")
        elif detect:
            group_box = detect_content_box(files, sample=sample)
        else:
            group_box = None

        applied[vid_idx] = group_box
        for f in files:
            crop_and_resize(f, output_dir / f.name, group_box, max_edge, quality)

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
