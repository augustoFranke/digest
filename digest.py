#!/usr/bin/env python3
"""Digest: one CLI for text, recordings with audio, and slide materials."""

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import frames
import slide_materials
import transcript


def describe_crop(crop_boxes: dict) -> str:
    if not crop_boxes:
        return "Frames were not cropped."
    lines = []
    for index, box in sorted(crop_boxes.items()):
        if box is None:
            description = "whole frame retained; cropping was disabled or detection was inconclusive."
        else:
            left, top, right, bottom = box
            description = (
                f"cropped to `{left},{top},{right},{bottom}` in source pixels."
            )
        lines.append(f"- Segment {index + 1}: {description}")
    return "\n".join(lines) + (
        "\n\nFrames may also be scaled down. Crops can omit interface elements; "
        "do not infer that content outside a crop was absent from the original screen."
    )


def _describe_materials(has_slides: bool) -> tuple[str, str]:
    """Returns README fragments for an optional ingested slide deck."""
    if not has_slides:
        return "", ""
    source = (
        "- `materials/slides.pdf`, `materials/slides.md` and `materials/pages/`: professor's "
        "slide deck, indexed by page.\n"
        "- `materials/slide-links.md`: lexical page candidates for transcript sections; review "
        "before treating them as evidence.\n"
    )
    instructions = (
        "\nWhen `materials/slide-links.md` contains a candidate, open the corresponding page in "
        "`materials/slides.pdf` and confirm the claim against the page's visual layout. A "
        "candidate is not proof that the page was shown at that exact second.\n"
    )
    return source, instructions


def generate_readme(
    lecture_title: str,
    offsets_str: list[str],
    offsets_seconds: list[int],
    crop_boxes: dict,
    has_slides: bool = False,
    transcript_source: str = "Supplied text transcript",
) -> str:
    """
    Generates standard agent-optimized README.md.
    """
    alignment_lines = []
    if len(offsets_seconds) == 1:
        off_sec = offsets_seconds[0]
        off_fmt = frames.format_seconds_to_timestamp(off_sec)
        if off_sec > 0:
            alignment_lines.append(
                f"The screen recording started at transcript timestamp: `{off_fmt}`."
            )
            alignment_lines.append(
                f"Therefore there is no visual material for `00:00:00`–`{frames.format_seconds_to_timestamp(max(0, off_sec - 1))}`."
            )
        else:
            alignment_lines.append(
                "The screen recording is aligned with the transcript starting at `00:00:00`."
            )
    else:
        alignment_lines.append(
            "This lecture includes multiple recording segments aligned to transcript timestamps:"
        )
        for idx, (ostr, osec) in enumerate(zip(offsets_str, offsets_seconds)):
            alignment_lines.append(
                f"- Segment {idx + 1}: starts at `{frames.format_seconds_to_timestamp(osec)}` (offset: {ostr})"
            )

    alignment_block = "\n".join(alignment_lines)
    crop_block = describe_crop(crop_boxes)

    material_source, material_instructions = _describe_materials(has_slides)

    readme_content = f"""# Lecture Context: {lecture_title}

This directory contains structured context and materials from one lecture, compiled for AI agent consumption and study note generation.

## Sources

- `transcript.md`: {transcript_source}, normalized onto the lecture timeline.
- `frames/`: Keyframe screenshots extracted from the professor's shared screen.
- `frames/index.csv`: Fast lookup index of available frame timestamps.
{material_source}

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
{material_instructions}

## Where the output goes

This package is source material, not a destination. Nothing is written back into this
directory. The study note goes to the Obsidian vault:

`<vault>/raw/lectures/YYYY-MM-DD Disciplina — Tópico.md`

and the concepts the lecture taught are promoted into `<vault>/wiki/`.

The full protocol — filename, frontmatter, how to fold a question into the note, and the
promotion rules — is in `AGENTS.md` at the repo root. Read it before digesting.
"""
    return readme_content.strip() + "\n"


def probe_video(path: Path, needs_audio: bool) -> float:
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {path}")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"Could not read video {path}: {result.stderr.strip()}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    if not any(
        s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic")
        for s in streams
    ):
        raise ValueError(f"No video stream in {path}.")
    if needs_audio and not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError(
            f"No audio stream in {path}; supply --transcript for a silent recording."
        )
    duration = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Video has no usable duration: {path}")
    return duration


def resolve_offsets(videos: list[Path], offsets: list[str] | None) -> list[int]:
    if not videos:
        if offsets is not None:
            raise ValueError("--offsets requires video recordings.")
        return []
    if offsets is None:
        offsets = ["0"] if len(videos) == 1 else []
    if len(offsets) != len(videos):
        raise ValueError(
            "Expected one --offsets value per recording; provide all starts in chronological order."
        )
    result = []
    for value in offsets:
        parts = value.lstrip("+").split(":")
        if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
            raise ValueError(
                f"Invalid offset '{value}': use nonnegative seconds, MM:SS or HH:MM:SS."
            )
        if len(parts) > 1 and (
            int(parts[-1]) >= 60 or len(parts) == 3 and int(parts[-2]) >= 60
        ):
            raise ValueError(
                f"Invalid offset '{value}': minutes/seconds components must be below 60."
            )
        result.append(frames.parse_offset_string(value))
    if result != sorted(result):
        raise ValueError("--offsets must be in chronological order.")
    return result


def validate_options(args: argparse.Namespace) -> tuple[list[int], list[dict]]:
    if not (args.transcript or args.transcribe or args.slides):
        raise ValueError(
            "Supply --transcript, --transcribe, or --slides. See --help for modes."
        )
    if args.transcribe and not args.videos:
        raise ValueError("--transcribe requires at least one video with audio.")
    if args.videos and not (args.transcript or args.transcribe):
        raise ValueError("Video input requires --transcript or --transcribe.")
    if args.no_frames and not args.videos:
        raise ValueError("--no-frames requires a video.")
    if not args.transcribe and (args.model is not None or args.language is not None):
        raise ValueError("--model and --language require --transcribe.")
    for name in ("interval", "max_interval"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"--{name.replace('_', '-')} must be finite and greater than zero."
            )
    if not 1 <= args.quality <= 31 or not 1 <= args.frame_quality <= 95:
        raise ValueError("--quality must be 1–31 and --frame-quality must be 1–95.")
    if args.max_edge < 0 or not 0 <= args.phash_threshold <= 64:
        raise ValueError(
            "--max-edge must be nonnegative and --phash-threshold must be 0–64."
        )
    if args.crop_box and min(args.crop_box) < 0:
        raise ValueError("--crop-box coordinates must be nonnegative.")
    output = args.output_dir
    if output.exists() and not output.is_dir():
        raise ValueError(f"Output is not a directory: {output}")
    if (output / "transcript.md").exists() or (output / "frames").exists():
        raise ValueError(
            f"Package already compiled: {output}. Use a new output directory."
        )
    if args.slides:
        if not args.slides.is_file():
            raise FileNotFoundError(f"Slide material not found: {args.slides}")
        if args.slides.suffix.lower() not in slide_materials.SUPPORTED_SLIDE_SUFFIXES:
            raise ValueError("--slides accepts .pdf or .pptx.")
    offsets = resolve_offsets(args.videos, args.offsets)
    entries = transcript.read_entries(args.transcript) if args.transcript else []
    if args.videos:
        for executable in ("ffmpeg", "ffprobe"):
            if not shutil.which(executable):
                raise RuntimeError(
                    f"{executable} is required; install FFmpeg (`brew install ffmpeg`)."
                )
        durations = [probe_video(video, args.transcribe) for video in args.videos]
        if args.transcribe:
            for i in range(1, len(offsets)):
                if offsets[i] < offsets[i - 1] + durations[i - 1]:
                    raise ValueError(
                        "Audio recordings overlap on the timeline. Correct --offsets or supply one --transcript."
                    )
    return offsets, entries


def transcript_readme(title: str, source: str, has_slides: bool) -> str:
    material_source, material_instructions = _describe_materials(has_slides)
    return f"""# Lecture Context: {title}

This is a transcript-only package. No frames were requested or no video was supplied;
there is no time-based visual evidence from the classroom.

## Sources

- `transcript.md`: {source}, normalized onto the lecture timeline.
{material_source}
## Instructions for Agents

Use `transcript.md` as the verbal source. Do not infer what was on screen from
phrases such as "isso aqui" or "como vocês podem ver".
{material_instructions}
The study note belongs in `<vault>/raw/lectures/`, and concepts in `<vault>/wiki/`.
This package remains read-only source material. Read the repository's `AGENTS.md`.
"""


def compile_package(args: argparse.Namespace) -> None:
    offsets, entries = validate_options(args)
    output = args.output_dir
    title = args.title or output.name
    source = (
        f"Supplied text transcript (`{args.transcript.name}`)"
        if args.transcript
        else ""
    )
    # Only publish generated files after every processing step succeeds.
    with tempfile.TemporaryDirectory(prefix="digest_") as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        if args.transcribe:
            model = args.model or "small"
            language = args.language or "pt"
            entries = transcript.transcribe_videos(
                args.videos, offsets, Path(temporary), model, language
            )
            source = f"Local faster-whisper ASR (model `{model}`, language `{language}`); recognition may contain errors"
        if args.slides:
            slide_materials.ingest_slides(args.slides, stage)
        slides_pdf = (stage if args.slides else output) / "materials" / "slides.pdf"
        has_slides = slides_pdf.is_file()
        if entries:
            (stage / "transcript.md").write_text(
                transcript.generate_markdown(entries), encoding="utf-8"
            )
            if has_slides:
                pages = slide_materials.extract_slide_pages(slides_pdf)
                links = slide_materials.link_transcript_to_slides(entries, pages)
                (stage / "materials").mkdir(exist_ok=True)
                (stage / "materials" / "slide-links.md").write_text(
                    slide_materials.render_slide_links(links), encoding="utf-8"
                )
            if args.videos and not args.no_frames:
                frames_dir = stage / "frames"
                raw = frames_dir / "raw"
                cropped = frames_dir / "cropped"
                count = frames.extract_frames(
                    args.videos, raw, args.interval, args.quality
                )
                if not count:
                    raise ValueError(
                        "No frames extracted; check the recording and --interval."
                    )
                boxes = frames.crop_frames(
                    raw,
                    cropped,
                    max_edge=args.max_edge,
                    quality=args.frame_quality,
                    box=args.crop_box,
                    detect=not args.no_crop,
                )
                frames.dedupe_and_rename_frames(
                    cropped,
                    frames_dir,
                    offsets,
                    args.interval,
                    args.phash_threshold,
                    args.max_interval,
                    clean_raw=not args.keep_raw,
                )
                if not args.keep_raw:
                    shutil.rmtree(raw)
                readme = generate_readme(
                    title, [str(o) for o in offsets], offsets, boxes, has_slides, source
                )
            else:
                readme = transcript_readme(title, source, has_slides)
            if args.videos:
                readme += (
                    "\n## Recording sources\n\n"
                    + "\n".join(
                        f"- `{video.name}` starts at `{frames.format_seconds_to_timestamp(offset)}`."
                        for video, offset in zip(args.videos, offsets, strict=True)
                    )
                    + "\n"
                )
            (stage / "README.md").write_text(readme, encoding="utf-8")
        else:
            (stage / "README.md").write_text(
                slide_materials._pending_package_readme(output).replace(
                    output.name, title, 1
                ),
                encoding="utf-8",
            )
        if args.slides and output.exists():
            # Replace only generated material files; preserve unrelated user files.
            old_materials = output / "materials"
            for pattern in ("slides.pptx", "slide-links.md", "pages/page-*.jpg"):
                for stale in old_materials.glob(pattern):
                    stale.unlink()
        shutil.copytree(stage, output, dirs_exist_ok=True)
    print(f"Digest prepared: {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Digest: compile transcripts, videos and slides into agent-ready context.",
        epilog="Modes: --transcript text.txt [with videos]; videos --transcribe; --slides deck.pdf alone or with either mode. Multiple videos require one --offsets value each.",
    )
    parser.add_argument(
        "videos",
        nargs="*",
        type=Path,
        help="Local videos in chronological order (any container readable by FFmpeg)",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-t",
        "--transcript",
        type=Path,
        help="Existing timestamped .txt; any video audio is ignored",
    )
    source.add_argument(
        "--transcribe",
        action="store_true",
        help="Generate a transcript locally from the first audio track of each video",
    )
    parser.add_argument(
        "--slides",
        type=Path,
        help="Optional PDF/PPTX, also usable alone before the recording",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        type=Path,
        help="New package directory, or a package containing slides only",
    )
    parser.add_argument(
        "--offsets",
        nargs="+",
        help="Start of each video on the transcript timeline; seconds/MM:SS/HH:MM:SS. Single video defaults to 0",
    )
    parser.add_argument(
        "--title", default="", help="Package title (default: output directory name)"
    )
    parser.add_argument(
        "--model",
        help="faster-whisper model name or local model directory (default: small, CPU int8)",
    )
    parser.add_argument(
        "--language",
        help="Speech language code (default: pt); auto detects per recording",
    )
    parser.add_argument(
        "--no-frames",
        action="store_true",
        help="Produce a transcript-only package from supplied videos",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between sampled frames (default: 5)",
    )
    parser.add_argument(
        "--phash-threshold",
        type=int,
        default=6,
        help="Keep a frame at this pHash distance (0–64; default: 6)",
    )
    parser.add_argument(
        "--max-interval",
        type=float,
        default=30.0,
        help="Force a frame after this many seconds (default: 30)",
    )
    parser.add_argument(
        "-q",
        "--quality",
        type=int,
        default=2,
        help="Raw FFmpeg JPEG quality (1–31; default: 2)",
    )
    parser.add_argument(
        "--max-edge",
        type=int,
        default=1568,
        help="Final image longest edge; 0 disables resizing (default: 1568)",
    )
    parser.add_argument(
        "--frame-quality",
        type=int,
        default=82,
        help="Final JPEG quality (1–95; default: 82)",
    )
    crop = parser.add_mutually_exclusive_group()
    crop.add_argument(
        "--crop-box",
        type=frames.parse_box_string,
        help="Crop left,top,right,bottom in source pixels",
    )
    crop.add_argument(
        "--no-crop",
        action="store_true",
        help="Keep whole frames; recommended for general videos",
    )
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help="Retain raw and cropped intermediate frames",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        compile_package(args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
