#!/usr/bin/env python3
"""
prepare_lecture.py

Lecture Context Compiler

Compiles raw lecture video recording(s) and transcript into an agent-ready lecture context folder:
    <lecture_dir>/
    ├── README.md
    ├── transcript.md
    └── frames/
        ├── index.csv
        ├── 00-04-37.jpg
        └── ...

The package is read-only source material. Study notes go to the Obsidian vault; see AGENTS.md.

Input contract: one or more macOS screen recordings in .mov format, one timestamped
Wispr Flow .txt export, and exactly one transcript offset per recording.
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Union

from importlib.machinery import SourceFileLoader

curr_dir = Path(__file__).parent.resolve()
norm_mod = SourceFileLoader("01_norm", str(curr_dir / "01_normalize_transcript.py")).load_module()
extract_mod = SourceFileLoader("02_ext", str(curr_dir / "02_extract_frames.py")).load_module()
crop_mod = SourceFileLoader("03_crop", str(curr_dir / "03_crop_frames.py")).load_module()
dedupe_mod = SourceFileLoader("04_dedupe", str(curr_dir / "04_dedupe_and_rename.py")).load_module()

RECORDING_SUFFIX = ".mov"
TRANSCRIPT_SUFFIX = ".txt"


def validate_inputs(video_paths: List[Path], transcript_path: Path, offsets: List[str]) -> None:
    """Validates the real capture contract before creating any output."""
    if not video_paths:
        raise ValueError("At least one .mov screen recording is required.")
    for video_path in video_paths:
        if not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")
        if video_path.suffix.lower() != RECORDING_SUFFIX:
            raise ValueError(f"Screen recording must be a .mov file: {video_path}")

    if not transcript_path.is_file():
        raise FileNotFoundError(f"Transcript file not found: {transcript_path}")
    if transcript_path.suffix.lower() != TRANSCRIPT_SUFFIX:
        raise ValueError(f"Transcript must be a Wispr Flow .txt export: {transcript_path}")

    if len(offsets) != len(video_paths):
        raise ValueError(
            f"Expected one --offsets value per recording: got {len(offsets)} offset(s) "
            f"for {len(video_paths)} .mov file(s)."
        )


def describe_crop(crop_boxes: dict) -> str:
    """
    One paragraph telling the agent what was cut out of the frames, so a missing
    tab bar or window title is a documented choice rather than a mystery.
    """
    if not crop_boxes:
        return "Frames were not cropped."

    lines = []
    for vid_idx in sorted(crop_boxes):
        box = crop_boxes[vid_idx]
        prefix = f"- Segment {vid_idx + 1}: " if len(crop_boxes) > 1 else ""
        if box is None:
            lines.append(f"{prefix}kept whole; no shared-screen region could be detected.")
        else:
            left, top, right, bottom = box
            lines.append(
                f"{prefix}cropped to the shared screen at `{left},{top} -> {right},{bottom}` "
                f"({right - left}x{bottom - top} px in the original recording)."
            )

    return (
        "Frames show only the region of the screen that carried the lecture. The video call's "
        "own interface — window chrome, participant tiles, control bar, letterbox — fell outside "
        "the detected lecture region and was removed, and the result was scaled down.\n\n"
        + "\n".join(lines)
        + "\n\nSo a frame is not a screenshot of the whole screen: the shared window's tab bar and "
        "address bar are usually outside the crop. Read what is in the frame; do not infer that "
        "something was absent from the professor's screen because it is absent here."
    )


def generate_readme(
    lecture_title: str,
    offsets_str: List[str],
    offsets_seconds: List[int],
    crop_boxes: dict
) -> str:
    """
    Generates standard agent-optimized README.md.
    """
    alignment_lines = []
    if len(offsets_seconds) == 1:
        off_sec = offsets_seconds[0]
        off_fmt = dedupe_mod.format_seconds_to_timestamp(off_sec)
        if off_sec > 0:
            alignment_lines.append(f"The screen recording started at transcript timestamp: `{off_fmt}`.")
            alignment_lines.append(f"Therefore there is no visual material for `00:00:00`–`{dedupe_mod.format_seconds_to_timestamp(max(0, off_sec - 1))}`.")
        else:
            alignment_lines.append("The screen recording is aligned with the transcript starting at `00:00:00`.")
    else:
        alignment_lines.append("This lecture includes multiple recording segments aligned to transcript timestamps:")
        for idx, (ostr, osec) in enumerate(zip(offsets_str, offsets_seconds)):
            alignment_lines.append(f"- Segment {idx + 1}: starts at `{dedupe_mod.format_seconds_to_timestamp(osec)}` (offset: {ostr})")

    alignment_block = "\n".join(alignment_lines)
    crop_block = describe_crop(crop_boxes)

    readme_content = f"""# Lecture Context: {lecture_title}

This directory contains structured context and materials from one lecture, compiled for AI agent consumption and study note generation.

## Sources

- `transcript.md`: Wispr transcript normalized onto the lecture timeline.
- `frames/`: Keyframe screenshots extracted from the professor's shared screen.
- `frames/index.csv`: Fast lookup index of available frame timestamps.

## Frame Naming Convention

Frame filenames correspond directly to lecture/transcript timestamps (`HH-MM-SS.jpg`).

Example:
`frames/00-18-42.jpg` represents approximately what was visible on screen at timestamp `00:18:42` in `transcript.md`.

## Recording alignment

{alignment_block}

## What the frames show

{crop_block}

## Instructions for Agents

When interpreting statements in `transcript.md` such as:
- "isso aqui" / "this right here"
- "esse valor" / "this value"
- "como vocês podem ver" / "as you can see"
- "essa linha" / "this line of code"
- "esse gráfico" / "this chart / diagram"

1. Check the timestamp in `transcript.md` where the statement occurs.
2. Inspect `frames/index.csv` or list `frames/` to find the closest matching or preceding frame.
3. Open and view the image in `frames/HH-MM-SS.jpg` to recover full visual context.
4. Use `transcript.md` as the primary verbal source and `frames/` as visual context.

## Where the output goes

This package is source material, not a destination. Nothing is written back into this
directory. The study note goes to the Obsidian vault:

`<vault>/raw/lectures/YYYY-MM-DD Disciplina — Tópico.md`

and the concepts the lecture taught are promoted into `<vault>/wiki/`.

The full protocol — filename, frontmatter, how to fold a question into the note, and the
promotion rules — is in `AGENTS.md` at the repo root. Read it before digesting.
"""
    return readme_content.strip() + "\n"


def prepare_lecture(
    video_paths: Union[Path, List[Path]],
    transcript_path: Path,
    output_dir: Path,
    offsets: Union[str, List[str]] = "00:00:00",
    interval_seconds: float = 5.0,
    phash_threshold: int = 6,
    max_interval_seconds: float = 30.0,
    quality: int = 2,
    max_edge: int = crop_mod.DEFAULT_MAX_EDGE,
    frame_quality: int = crop_mod.DEFAULT_QUALITY,
    crop_box: Optional[crop_mod.Box] = None,
    detect_crop: bool = True,
    keep_raw: bool = False,
    title: str = ""
):
    """
    Executes the full pipeline to prepare a lecture context package.
    """
    if isinstance(video_paths, (str, Path)):
        video_paths = [Path(video_paths)]
    else:
        video_paths = [Path(p) for p in video_paths]

    if isinstance(offsets, str):
        offsets_str_list = [offsets]
    else:
        offsets_str_list = list(offsets)

    validate_inputs(video_paths, transcript_path, offsets_str_list)

    offsets_seconds = [dedupe_mod.parse_offset_string(o) for o in offsets_str_list]
    lecture_title = title or output_dir.name

    try:
        transcript_content = transcript_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        transcript_content = transcript_path.read_text(encoding="latin-1")
    entries = norm_mod.normalize_transcript(transcript_content)
    transcript_md = norm_mod.generate_markdown(entries)

    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    raw_frames_dir = frames_dir / "raw"
    cropped_frames_dir = frames_dir / "cropped"

    print("=" * 60)
    print(f"🎬 Preparing Lecture Context: {lecture_title}")
    print(f"   Videos:      {len(video_paths)} file(s)")
    for idx, (v, o_str, o_sec) in enumerate(zip(video_paths, offsets_str_list, offsets_seconds)):
        print(f"     [{idx+1}] {v.name} (Offset: {o_str} -> {o_sec}s)")
    print(f"   Transcript:  {transcript_path}")
    print(f"   Output:      {output_dir}")
    print("=" * 60)

    # Step 1: Normalize Transcript
    print("\n[Step 1/5] Normalizing transcript...")
    transcript_out_path = output_dir / "transcript.md"
    transcript_out_path.write_text(transcript_md, encoding="utf-8")
    print(f"✓ Saved {len(entries)} transcript sections to {transcript_out_path}")

    # Step 2: Extract Frames
    print("\n[Step 2/5] Extracting video frames...")
    raw_frames_dir.mkdir(parents=True, exist_ok=True)
    total_raw = extract_mod.extract_frames(
        video_paths=video_paths,
        output_dir=raw_frames_dir,
        interval_seconds=interval_seconds,
        quality=quality,
        clean=True
    )

    # Step 3: Crop away the call's interface and shrink what is left
    print("\n[Step 3/5] Cropping frames to the shared screen and resizing...")
    crop_boxes = crop_mod.crop_frames(
        raw_dir=raw_frames_dir,
        output_dir=cropped_frames_dir,
        max_edge=max_edge,
        quality=frame_quality,
        box=crop_box,
        detect=detect_crop
    )
    if not keep_raw:
        shutil.rmtree(raw_frames_dir)
        print(f"  Removed full-resolution frames: {raw_frames_dir}")

    # Step 4: Deduplicate and Rename Frames
    print("\n[Step 4/5] Deduplicating and renaming frames by transcript timestamp...")
    saved_records = dedupe_mod.dedupe_and_rename_frames(
        raw_dir=cropped_frames_dir,
        output_dir=frames_dir,
        offsets=offsets_seconds,
        interval_seconds=interval_seconds,
        phash_threshold=phash_threshold,
        max_interval_seconds=max_interval_seconds,
        clean_raw=not keep_raw
    )

    # Step 5: Generate README.md
    print("\n[Step 5/5] Generating README.md...")
    readme_content = generate_readme(
        lecture_title=lecture_title,
        offsets_str=offsets_str_list,
        offsets_seconds=offsets_seconds,
        crop_boxes=crop_boxes
    )
    readme_path = output_dir / "README.md"
    readme_path.write_text(readme_content, encoding="utf-8")
    print(f"✓ Created agent README at {readme_path}")

    print("\n" + "=" * 60)
    print("✨ Lecture Context successfully prepared!")
    print(f"📁 Directory: {output_dir}")
    print(f"   ├── README.md")
    print(f"   ├── transcript.md")
    print(f"   └── frames/ ({len(saved_records)} deduplicated frames + index.csv)")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Compile .mov screen recording(s) and a Wispr Flow .txt transcript into agent-ready lecture context."
    )
    parser.add_argument("videos", nargs="+", help="Path to screen recording .mov file(s), in chronological order")
    parser.add_argument("-t", "--transcript", required=True, help="Path to the timestamped Wispr Flow .txt export")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="lectures/lecture",
        help="Output directory for lecture context (default: lectures/lecture)",
    )
    parser.add_argument("--offsets", nargs="+", default=["00:00:00"], help="One transcript start offset per .mov recording (e.g. '00:04:37')")
    parser.add_argument("--title", default="", help="Lecture title (optional, defaults to output directory name)")
    parser.add_argument("--interval", type=float, default=5.0, help="Interval in seconds between raw frame extractions (default: 5.0)")
    parser.add_argument("--phash-threshold", type=int, default=6, help="pHash hamming distance threshold (default: 6)")
    parser.add_argument("--max-interval", type=float, default=30.0, help="Max interval in seconds before forcing a frame save (default: 30.0)")
    parser.add_argument("-q", "--quality", type=int, default=2, help="ffmpeg JPEG quality 1-31 for the raw extraction (default: 2)")
    parser.add_argument("--max-edge", type=int, default=crop_mod.DEFAULT_MAX_EDGE, help=f"Fit each final frame's long edge in this many pixels; 0 disables resizing (default: {crop_mod.DEFAULT_MAX_EDGE})")
    parser.add_argument("--frame-quality", type=int, default=crop_mod.DEFAULT_QUALITY, help=f"JPEG quality 1-95 of the final frames (default: {crop_mod.DEFAULT_QUALITY})")
    parser.add_argument("--crop-box", help="Skip crop detection and use 'left,top,right,bottom' in pixels")
    parser.add_argument("--no-crop", action="store_true", help="Keep the whole frame; resize only")
    parser.add_argument("--keep-raw", action="store_true", help="Keep the full-resolution frames directory")

    args = parser.parse_args()

    video_paths = [Path(v) for v in args.videos]
    transcript_path = Path(args.transcript)
    output_dir = Path(args.output_dir)

    for vp in video_paths:
        if not vp.is_file():
            print(f"Error: Video file '{vp}' not found.", file=sys.stderr)
            sys.exit(1)

    if not transcript_path.is_file():
        print(f"Error: Transcript file '{args.transcript}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        prepare_lecture(
            video_paths=video_paths,
            transcript_path=transcript_path,
            output_dir=output_dir,
            offsets=args.offsets,
            interval_seconds=args.interval,
            phash_threshold=args.phash_threshold,
            max_interval_seconds=args.max_interval,
            quality=args.quality,
            max_edge=args.max_edge,
            frame_quality=args.frame_quality,
            crop_box=crop_mod.parse_box_string(args.crop_box) if args.crop_box else None,
            detect_crop=not args.no_crop,
            keep_raw=args.keep_raw,
            title=args.title
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
