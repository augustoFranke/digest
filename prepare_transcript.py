#!/usr/bin/env python3
"""Builds a transcript-only lecture package, optionally using ingested slides."""

import argparse
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import slide_materials

root_dir = Path(__file__).parent.resolve()
norm_mod = SourceFileLoader("transcript_normalizer", str(root_dir / "01_normalize_transcript.py")).load_module()


def _read_transcript(path: Path) -> str:
    """Reads Wispr text while tolerating legacy Latin-1 exports."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _transcript_readme(title: str, has_slides: bool) -> str:
    """Describes the evidence available in a transcript-only package."""
    slides = (
        "\n- `materials/slides.pdf`, `materials/pages/` and `materials/slide-links.md`: professor's slide material and reviewable page candidates."
        if has_slides
        else ""
    )
    slide_guidance = (
        "\nWhen `materials/slide-links.md` contains a candidate, verify it in the PDF or the rendered page image. A candidate is not proof that the page was shown at that exact second."
        if has_slides
        else ""
    )
    return f"""# Lecture Context: {title}

This is a transcript-only package. No screen recording was supplied, so it contains no time-based visual evidence from the classroom.

## Sources

- `transcript.md`: Wispr transcript normalized onto the lecture timeline.{slides}

## Instructions for Agents

Use `transcript.md` as the verbal source. Do not infer what was on screen from phrases such as "isso aqui" or "como vocês podem ver".{slide_guidance}

The study note belongs in `<vault>/raw/lectures/`; this package remains read-only source material.
"""


def prepare_transcript(transcript_path: Path, output_dir: Path, title: str = "") -> int:
    """Writes a transcript-only package and links any previously ingested slides."""
    if not transcript_path.is_file():
        raise FileNotFoundError(f"Transcript file not found: {transcript_path}")
    if transcript_path.suffix.lower() != ".txt":
        raise ValueError(f"Transcript must be a Wispr Flow .txt export: {transcript_path}")

    entries = norm_mod.normalize_transcript(_read_transcript(transcript_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "transcript.md").write_text(norm_mod.generate_markdown(entries), encoding="utf-8")

    has_slides = (output_dir / "materials" / "slides.pdf").is_file()
    linked_sections = slide_materials.write_slide_links(output_dir, entries) if has_slides else 0
    package_title = title or output_dir.name
    (output_dir / "README.md").write_text(
        _transcript_readme(package_title, has_slides), encoding="utf-8"
    )
    return linked_sections


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a transcript-only lecture package, with optional ingested slides.")
    parser.add_argument("transcript", help="Path to the Wispr Flow .txt export")
    parser.add_argument("-o", "--output-dir", default="lectures/lecture", help="Lecture package directory")
    parser.add_argument("--title", default="", help="Lecture title (optional, defaults to output directory name)")
    args = parser.parse_args()

    try:
        count = prepare_transcript(Path(args.transcript), Path(args.output_dir), args.title)
        print(f"Prepared transcript-only package with {count} slide-linked section(s).")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
