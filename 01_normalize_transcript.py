#!/usr/bin/env python3
"""
01_normalize_transcript.py

Normalizes various transcript formats (plain text with timestamps, inline timestamps,
SRT, VTT, Whisper JSON, paused/resumed multi-part lectures) into a clean, standardized
Markdown file (transcript.md) with searchable timestamp headers.

Example output:
    # Transcript

    ## 00:00:00
    Welcome everyone to today's lecture.

    ## 00:04:37
    Let's begin looking at the architecture diagram.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Tuple, Optional, Union, Dict, Any


def parse_timestamp_to_seconds(ts_str: str) -> Optional[int]:
    """
    Parses timestamp strings like:
      - '04:37' -> 277
      - '00:04:37' -> 277
      - '1:04:37' -> 3877
      - '00:04:37,500' -> 277
      - '00:04:37.500' -> 277
    """
    ts_str = ts_str.strip().replace(',', '.')
    if '.' in ts_str:
        ts_str = ts_str.split('.')[0]

    parts = ts_str.split(':')
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
        return None
    return None


def format_seconds_to_timestamp(seconds: int, always_hours: bool = True) -> str:
    """
    Converts seconds to HH:MM:SS or MM:SS format.
    """
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if always_hours or h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_srt(content: str) -> List[Dict[str, Any]]:
    """
    Parses SubRip (.srt) subtitles into entries.
    """
    entries = []
    blocks = re.split(r'\n\s*\n', content.strip())
    srt_time_pat = re.compile(r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})')

    for block in blocks:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue
        time_match = None
        text_lines = []
        for line in lines:
            m = srt_time_pat.search(line)
            if m:
                time_match = m
            elif time_match:
                text_lines.append(line)

        if time_match and text_lines:
            start_ts = time_match.group(1)
            sec = parse_timestamp_to_seconds(start_ts)
            if sec is not None:
                entries.append({"type": "timestamp", "sec": sec, "text": " ".join(text_lines)})

    return entries


def parse_vtt(content: str) -> List[Dict[str, Any]]:
    """
    Parses WebVTT (.vtt) subtitles into entries.
    """
    entries = []
    vtt_time_pat = re.compile(r'((?:\d{1,2}:)?\d{2}:\d{2}[,\.]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[,\.]\d{3})')
    blocks = re.split(r'\n\s*\n', content.strip())

    for block in blocks:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines or lines[0].startswith('WEBVTT') or lines[0].startswith('NOTE'):
            continue
        time_match = None
        text_lines = []
        for line in lines:
            m = vtt_time_pat.search(line)
            if m:
                time_match = m
            elif time_match:
                text_lines.append(line)

        if time_match and text_lines:
            start_ts = time_match.group(1)
            sec = parse_timestamp_to_seconds(start_ts)
            if sec is not None:
                entries.append({"type": "timestamp", "sec": sec, "text": " ".join(text_lines)})

    return entries


def parse_whisper_json(content: str) -> List[Dict[str, Any]]:
    """
    Parses Whisper verbose JSON format containing 'segments'.
    """
    data = json.loads(content)
    entries = []
    segments = data.get('segments', [])
    for seg in segments:
        start_sec = int(round(seg.get('start', 0)))
        text = seg.get('text', '').strip()
        if text:
            entries.append({"type": "timestamp", "sec": start_sec, "text": text})
    return entries


def parse_inline_and_text(content: str) -> List[Dict[str, Any]]:
    """
    Parses timestamped text formats, including multi-line or inline timestamps
    (e.g., [04:37] Speaker: ..., 04:37 - Speaker, ## 04:37) and pause/resume markers.
    """
    # Pattern matching:
    # 1. Pause/Resume markers: [7:57 PM] -- Paused -- ...
    # 2. Bracketed timestamps: [04:37] or [00:04:37]
    # 3. Markdown/Line-start timestamps: ## 04:37 or ^04:37 or \n04:37
    pattern = re.compile(
        r'(\[\d{1,2}:\d{2}\s*(?:AM|PM)\]\s*--\s*(?:Paused|Resumed)\s*--(?:\s*\[\d{1,2}:\d{2}\s*(?:AM|PM)\]\s*--\s*(?:Paused|Resumed)\s*--)*|'
        r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]|'
        r'(?:^|[\n\r])\s*(?:##\s*)?(\d{1,2}:\d{2}(?::\d{2})?)\b[\s\-:]*)',
        re.MULTILINE
    )

    tokens: List[Dict[str, Any]] = []
    last_pos = 0

    # Extract initial summary/preamble if present before the first transcript timestamp
    first_match = pattern.search(content)
    if first_match and first_match.start() > 0:
        preamble = content[:first_match.start()].strip()
        if preamble:
            tokens.append({"type": "preamble", "text": preamble})
        last_pos = first_match.start()

    for match in pattern.finditer(content):
        start, end = match.span()
        matched_str = match.group(0).strip()
        ts_group = match.group(2) or match.group(3)

        # Preceding text belongs to previous token
        if tokens and tokens[-1].get("type") in ("timestamp", "marker"):
            preceding = content[last_pos:start].strip()
            if preceding:
                if tokens[-1].get("text"):
                    tokens[-1]["text"] += " " + preceding
                else:
                    tokens[-1]["text"] = preceding

        if "--" in matched_str and ("Paused" in matched_str or "Resumed" in matched_str):
            tokens.append({
                "type": "marker",
                "marker": matched_str,
                "text": ""
            })
        elif ts_group:
            sec = parse_timestamp_to_seconds(ts_group)
            if sec is not None:
                tokens.append({
                    "type": "timestamp",
                    "sec": sec,
                    "raw_ts": ts_group,
                    "text": ""
                })
        last_pos = end

    # Add remaining text after last timestamp
    if tokens and last_pos < len(content):
        remaining = content[last_pos:].strip()
        if remaining:
            if tokens[-1].get("text"):
                tokens[-1]["text"] += " " + remaining
            else:
                tokens[-1]["text"] = remaining

    if not tokens:
        tokens.append({"type": "timestamp", "sec": 0, "text": content.strip()})

    return tokens


def normalize_transcript(content: str, filename_hint: str = "") -> List[Dict[str, Any]]:
    """
    Detects format and parses content into a list of entry dictionaries.
    """
    stripped = content.strip()
    if not stripped:
        return []

    if (filename_hint.endswith('.json') or stripped.startswith('{')) and '"segments"' in stripped:
        try:
            return parse_whisper_json(stripped)
        except Exception:
            pass

    if filename_hint.endswith('.srt') or ('-->' in stripped and re.search(r'\d{2}:\d{2}:\d{2},\d{3}', stripped)):
        parsed = parse_srt(stripped)
        if parsed:
            return parsed

    if filename_hint.endswith('.vtt') or stripped.startswith('WEBVTT') or ('-->' in stripped and re.search(r'\d{2}:\d{2}\.\d{3}', stripped)):
        parsed = parse_vtt(stripped)
        if parsed:
            return parsed

    return parse_inline_and_text(stripped)


def generate_markdown(entries: List[Dict[str, Any]], merge_window_seconds: int = 0) -> str:
    """
    Formats parsed entries into standardized markdown.
    """
    if not entries:
        return "# Transcript\n\n*(Empty transcript)*\n"

    md_lines = []
    has_transcript_header = False

    for entry in entries:
        etype = entry.get("type")
        if etype == "preamble":
            md_lines.append(entry["text"])
            md_lines.append("")
        elif etype == "marker":
            md_lines.append(f"\n---\n### ⏸️ {entry['marker']}\n---\n")
        elif etype == "timestamp":
            if not has_transcript_header and not any("Transcript" in line for line in md_lines):
                md_lines.append("# Transcript")
                md_lines.append("")
                has_transcript_header = True

            sec = entry.get("sec", 0)
            ts = format_seconds_to_timestamp(sec, always_hours=True)
            text = entry.get("text", "").strip()
            if text:
                md_lines.append(f"## {ts}")
                md_lines.append(text)
                md_lines.append("")

    return "\n".join(md_lines).strip() + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Normalize raw lecture transcripts (SRT, VTT, JSON, TXT, MD) into a clean, searchable transcript.md"
    )
    parser.add_argument("input", help="Path to input transcript file (e.g. transcript.txt, audio.srt, whisper.json)")
    parser.add_argument("-o", "--output", help="Path to output transcript.md (defaults to stdout)")
    parser.add_argument("--merge-window", type=int, default=0, help="Optional window in seconds to merge nearby short subtitles (default: 0)")

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Error: Input file '{args.input}' does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        content = input_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = input_path.read_text(encoding="latin-1")

    entries = normalize_transcript(content, filename_hint=input_path.name.lower())
    markdown_output = generate_markdown(entries, merge_window_seconds=args.merge_window)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown_output, encoding="utf-8")
        print(f"Normalized transcript saved to: {out_path} ({len(entries)} sections)")
    else:
        print(markdown_output)


if __name__ == "__main__":
    main()
