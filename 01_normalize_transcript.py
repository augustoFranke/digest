#!/usr/bin/env python3
"""
01_normalize_transcript.py

Normalizes the timestamped plain-text export produced by Wispr Flow into a clean,
standardized Markdown file (transcript.md) with searchable timestamp headers.

Example output:
    # Transcript

    ## 00:00:00
    Welcome everyone to today's lecture.

    ## 00:04:37
    Let's begin looking at the architecture diagram.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


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


TIMESTAMP_TOKEN = re.compile(
    r'(\[\d{1,2}:\d{2}\s*(?:AM|PM)\]\s*--\s*(?:Paused|Resumed)\s*--(?:\s*\[\d{1,2}:\d{2}\s*(?:AM|PM)\]\s*--\s*(?:Paused|Resumed)\s*--)*|'
    r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]|'
    r'(?:^|[\n\r])\s*(?:##\s*)?(\d{1,2}:\d{2}(?::\d{2})?)\b[\s\-:]*)',
    re.MULTILINE,
)


def _append_text(token: Dict[str, Any], text: str) -> None:
    """Adds non-empty transcript text to a timestamp or pause marker."""
    text = text.strip()
    if not text:
        return
    token["text"] = f"{token.get('text', '')} {text}".strip()


def _token_from_match(match: re.Match[str]) -> Dict[str, Any]:
    """Converts one Wispr timestamp or pause marker into a transcript token."""
    matched_text = match.group(0).strip()
    timestamp = match.group(2) or match.group(3)
    if "--" in matched_text and ("Paused" in matched_text or "Resumed" in matched_text):
        return {"type": "marker", "marker": matched_text, "text": ""}

    seconds = parse_timestamp_to_seconds(timestamp)
    if seconds is None:
        raise ValueError(f"Invalid Wispr timestamp: '{timestamp}'")
    return {"type": "timestamp", "sec": seconds, "raw_ts": timestamp, "text": ""}


def _preamble_and_start(content: str) -> tuple[List[Dict[str, Any]], int]:
    """Returns an optional preamble and the point where timestamp parsing begins."""
    first_match = TIMESTAMP_TOKEN.search(content)
    if first_match is None:
        return [], 0
    preamble = content[:first_match.start()].strip()
    tokens = [{"type": "preamble", "text": preamble}] if preamble else []
    return tokens, first_match.start()


def _append_preceding_text(tokens: List[Dict[str, Any]], content: str, start: int, end: int) -> None:
    """Assigns text between timestamp tokens to the preceding transcript token."""
    if tokens and tokens[-1].get("type") in ("timestamp", "marker"):
        _append_text(tokens[-1], content[start:end])


def parse_inline_and_text(content: str) -> List[Dict[str, Any]]:
    """
    Parses timestamped text formats, including multi-line or inline timestamps
    (e.g., [04:37] Speaker: ..., 04:37 - Speaker, ## 04:37) and pause/resume markers.
    """
    tokens, last_pos = _preamble_and_start(content)

    for match in TIMESTAMP_TOKEN.finditer(content):
        start, end = match.span()
        _append_preceding_text(tokens, content, last_pos, start)
        tokens.append(_token_from_match(match))
        last_pos = end

    _append_preceding_text(tokens, content, last_pos, len(content))

    if tokens:
        return tokens
    return [{"type": "timestamp", "sec": 0, "text": content.strip()}]


def normalize_transcript(content: str) -> List[Dict[str, Any]]:
    """Parses a Wispr plain-text export into timestamped entries."""
    stripped = content.strip()
    if not stripped:
        return []
    entries = parse_inline_and_text(stripped)

    previous = None
    for entry in entries:
        if entry.get("type") != "timestamp":
            continue
        current = entry["sec"]
        if previous is not None and current < previous:
            raise ValueError(
                "Wispr transcript timestamps go backwards. Export one session or "
                "correct the combined source timeline before compiling."
            )
        previous = current

    return entries


def generate_markdown(entries: List[Dict[str, Any]]) -> str:
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
        description="Normalize a Wispr Flow .txt export into a searchable transcript.md"
    )
    parser.add_argument("input", help="Path to the Wispr Flow .txt export")
    parser.add_argument("-o", "--output", help="Path to output transcript.md (defaults to stdout)")

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Error: Input file '{args.input}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if input_path.suffix.lower() != ".txt":
        print(f"Error: Transcript must be a Wispr Flow .txt export: '{args.input}'.", file=sys.stderr)
        sys.exit(1)

    try:
        content = input_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = input_path.read_text(encoding="latin-1")

    try:
        entries = normalize_transcript(content)
        markdown_output = generate_markdown(entries)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown_output, encoding="utf-8")
        print(f"Normalized transcript saved to: {out_path} ({len(entries)} sections)")
    else:
        print(markdown_output)


if __name__ == "__main__":
    main()
