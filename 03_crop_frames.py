#!/usr/bin/env python3
"""
03_crop_frames.py

Crops raw lecture frames down to the region that actually carries the lecture,
and shrinks them.

A lecture recorded through a screen-sharing call is mostly furniture: browser
chrome, the call's own toolbar, participant tiles, letterbox. Almost none of it
is truly frozen — the clock ticks, the control bar fades in and out, webcams
move — so the question is not what moves but what *keeps rewriting itself*.
This step samples the frames of each recording, measures how much each pixel
changes from one sample to the next, and keeps the bounding box of the region
that changes as hard as the busiest cells in the frame. That region is the
shared screen: it redraws whole lines of text, while a webcam tile shifts a face
a few levels and the chrome only blinks.

The kept region is then resized so its long edge fits --max-edge (default 1568,
the resolution above which vision models downscale anyway) and re-encoded.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only on a broken install
    np = None
    Image = None

Box = Tuple[int, int, int, int]  # left, top, right, bottom, in full-resolution pixels

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


def group_frames_by_video(frame_files: List[Path]) -> Dict[int, List[Path]]:
    """
    Groups raw frames by their originating video, mirroring 02_extract_frames.py's
    `vid<N>_frame_<seq>` / `frame_<seq>` naming. Each video gets its own crop,
    because each recording can have a different window layout.
    """
    groups: Dict[int, List[Path]] = {}
    for f in frame_files:
        match_vid = re.match(r'vid(\d+)_frame_(\d+)', f.name)
        vid_idx = int(match_vid.group(1)) - 1 if match_vid else 0
        groups.setdefault(vid_idx, []).append(f)

    for vid_idx in groups:
        groups[vid_idx].sort(
            key=lambda p: int(re.search(r'frame_(\d+)', p.name).group(1)) if re.search(r'frame_(\d+)', p.name) else 0
        )
    return groups


def _change_map(frame_files: List[Path], sample: int, downscale: int):
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
    picks = sorted(set(np.linspace(0, len(frame_files) - 1, min(sample, len(frame_files))).astype(int).tolist()))

    stack = []
    full_size = None
    for i in picks:
        with Image.open(frame_files[i]) as img:
            gray = img.convert("L")
            full_size = gray.size
            small = gray.resize((max(1, gray.width // downscale), max(1, gray.height // downscale)), Image.BILINEAR)
            stack.append(np.asarray(small, dtype=np.float32))

    shapes = {a.shape for a in stack}
    if len(shapes) != 1:
        raise ValueError(f"Frames in this group have inconsistent sizes: {shapes}")

    return np.abs(np.diff(np.stack(stack), axis=0)).mean(axis=0), full_size[0], full_size[1]


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
        integral[size:size + h, size:size + w]
        - integral[0:h, size:size + w]
        - integral[size:size + h, 0:w]
        + integral[0:h, 0:w]
    )
    return total / (size * size)


def _trim_quiet_edges(change, box_cells, ratio: float = TRIM_RATIO, max_trim: float = MAX_TRIM_FRACTION):
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
    reference = float(np.median(change[top:bottom + 1, left:right + 1]))
    if reference <= 0:
        return box_cells

    floor = ratio * reference
    min_width = max(1, int(round((right - left + 1) * (1 - max_trim))))
    min_height = max(1, int(round((bottom - top + 1) * (1 - max_trim))))

    trimming = True
    while trimming:
        trimming = False
        if bottom - top + 1 > min_height:
            if change[top, left:right + 1].mean() < floor:
                top += 1
                trimming = True
            elif change[bottom, left:right + 1].mean() < floor:
                bottom -= 1
                trimming = True
        if right - left + 1 > min_width:
            if change[top:bottom + 1, left].mean() < floor:
                left += 1
                trimming = True
            elif change[top:bottom + 1, right].mean() < floor:
                right -= 1
                trimming = True

    return left, top, right, bottom


def _component_containing(mask, seed: Tuple[int, int]):
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
    seed = np.unravel_index(int(np.argmax(_box_blur(weighted_change, radius=3))), change.shape)
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


def _cell_box_to_frame_box(cells: Box, downscale: int, full_width: int, full_height: int) -> Box:
    """Scales inclusive analysis cells back to image coordinates."""
    left, top, right, bottom = cells
    return (
        left * downscale,
        top * downscale,
        min(full_width, (right + 1) * downscale),
        min(full_height, (bottom + 1) * downscale),
    )


def _report_detected_box(box: Box, full_width: int, full_height: int, verbose: bool) -> None:
    """Explains a successful detection and flags a potentially broad crop."""
    if not verbose:
        return
    left, top, right, bottom = box
    kept = ((right - left) * (bottom - top)) / float(full_width * full_height)
    print(f"  Detected content box {left},{top} -> {right},{bottom} "
          f"({right - left}x{bottom - top}, {kept:.0%} of the frame)")
    if kept <= SUSPICIOUS_AREA_FRACTION:
        return
    print("  That is most of the frame. Two things cause it, and they need opposite responses:")
    print("    - The window moved during the recording, or the call's layout changed (tiles "
          "from the side to the top, a chat panel opening). The box is then the union of every "
          "position the lecture occupied, and it is correct — no single crop does better.")
    print("    - The sample covers too short a stretch. Over a few minutes the shared screen "
          "sits still while the webcams move, so the detection separates nothing.")
    print("  Look at a frame from early and one from late before overriding with --box.")


def detect_content_box(
    frame_files: List[Path],
    sample: int = DEFAULT_SAMPLE,
    downscale: int = ANALYSIS_DOWNSCALE,
    verbose: bool = True,
) -> Optional[Box]:
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
            print(f"  Only {len(frame_files)} frame(s) — too few to detect a crop; keeping the full frame.")
        return None

    change, full_w, full_h = _change_map(frame_files, sample, downscale)
    mask = _activity_mask(change, verbose)
    if mask is None:
        return None

    cells = _content_cells(change, mask)
    left_c, top_c, right_c, bottom_c = cells
    share = float(change[top_c:bottom_c + 1, left_c:right_c + 1].sum() / change.sum())
    if share < MIN_ACTIVITY_SHARE:
        if verbose:
            print(f"  The detected region holds only {share:.0%} of the change in the frame, so the "
                  "lecture is probably not what was detected; keeping the full frame.")
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
            print(f"  Detected box {width}x{height} is too small to be the shared screen; keeping the full frame.")
        return False
    area = (width * height) / float(full_w * full_h)
    if area < MIN_AREA_FRACTION or area > MAX_AREA_FRACTION:
        if verbose:
            print(f"  Detected box covers {area:.0%} of the frame, which is implausible; keeping the full frame.")
        return False
    return True


def parse_box_string(text: str) -> Box:
    """Parses 'left,top,right,bottom'."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Invalid box '{text}'. Expected 'left,top,right,bottom' in pixels.")
    try:
        left, top, right, bottom = (int(p) for p in parts)
    except ValueError:
        raise ValueError(f"Invalid box '{text}'. All four values must be integers.")
    if right <= left or bottom <= top:
        raise ValueError(f"Invalid box '{text}'. Right must exceed left and bottom must exceed top.")
    return left, top, right, bottom


def crop_and_resize(
    src: Path,
    dest: Path,
    box: Optional[Box],
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
        out.convert("RGB").save(dest, format="JPEG", quality=quality, optimize=True, progressive=True)


def crop_frames(
    raw_dir: Path,
    output_dir: Path,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_QUALITY,
    sample: int = DEFAULT_SAMPLE,
    box: Optional[Box] = None,
    detect: bool = True,
) -> Dict[int, Optional[Box]]:
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
    applied: Dict[int, Optional[Box]] = {}

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
    print(f"  Size on disk:      {bytes_before / 1e6:.1f} MB -> {bytes_after / 1e6:.1f} MB ({saved:.1f}% smaller)")
    print(f"  Saved to:          {output_dir}")
    return applied


def main():
    parser = argparse.ArgumentParser(
        description="Crop lecture frames to the shared-screen region and shrink them."
    )
    parser.add_argument("raw_dir", nargs="?", default="frames/raw", help="Directory containing raw frames (default: frames/raw)")
    parser.add_argument("-o", "--output-dir", default="frames/cropped", help="Output directory (default: frames/cropped)")
    parser.add_argument("--max-edge", type=int, default=DEFAULT_MAX_EDGE, help=f"Fit the long edge in this many pixels; 0 disables resizing (default: {DEFAULT_MAX_EDGE})")
    parser.add_argument("-q", "--quality", type=int, default=DEFAULT_QUALITY, help=f"JPEG quality 1-95 (default: {DEFAULT_QUALITY})")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help=f"How many frames to sample when detecting the crop (default: {DEFAULT_SAMPLE})")
    parser.add_argument("--box", help="Skip detection and crop every frame to 'left,top,right,bottom' in pixels")
    parser.add_argument("--no-detect", action="store_true", help="Skip detection and keep the full frame; resize only")

    args = parser.parse_args()

    try:
        crop_frames(
            raw_dir=Path(args.raw_dir),
            output_dir=Path(args.output_dir),
            max_edge=args.max_edge,
            quality=args.quality,
            sample=args.sample,
            box=parse_box_string(args.box) if args.box else None,
            detect=not args.no_detect,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
