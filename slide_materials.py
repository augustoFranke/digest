#!/usr/bin/env python3
"""PDF slide ingestion and conservative transcript-to-page linking."""

import csv
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - exercised only on a broken install
    PdfReader = None


@dataclass(frozen=True)
class SlidePage:
    number: int
    text: str


WORD_PATTERN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
STOPWORDS = frozenset(
    "a ao aos as de da das do dos e em na nas no nos o os para por que se um uma "
    "uns umas com como mais menos muito muita este esta esse essa isso aqui ali "
    "sobre entre pelo pela pelos pelas quando onde então também já não sim são "
    "ser foi tem temos vai vamos sua suas seu seus".split()
)


def _require_pdf_reader() -> None:
    if PdfReader is None:
        raise ImportError("pypdf is required for slide ingestion. Run `uv sync`.")


def _normalise_page_text(text: str | None) -> str:
    """Makes extracted text readable while retaining page-local line breaks."""
    if not text:
        return ""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def extract_slide_pages(pdf_path: Path) -> list[SlidePage]:
    """Extracts one text record per PDF page; image-only pages remain explicit."""
    _require_pdf_reader()
    try:
        reader = PdfReader(str(pdf_path))
        return [SlidePage(index + 1, _normalise_page_text(page.extract_text())) for index, page in enumerate(reader.pages)]
    except Exception as exc:
        raise ValueError(f"Could not read slide PDF '{pdf_path}': {exc}") from exc


def _render_slide_markdown(pages: Iterable[SlidePage]) -> str:
    lines = ["# Slides", "", "Texto extraído por página do PDF original.", ""]
    for page in pages:
        lines.extend([f"## Página {page.number}", page.text or "*(Nenhum texto extraível; a página pode ser visual.)*", ""])
    return "\n".join(lines).strip() + "\n"


def render_slide_pages(pdf_path: Path, pages_dir: Path) -> list[Path]:
    """Renders PDF pages to JPEGs for agents that cannot inspect PDF layout directly."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    for old_page in pages_dir.glob("page-*.jpg"):
        old_page.unlink()
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        return []

    prefix = pages_dir / "slide"
    try:
        subprocess.run(
            [pdftoppm, "-jpeg", "-r", "144", str(pdf_path), str(prefix)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip()
        raise ValueError(f"Could not render slide PDF '{pdf_path}': {detail}") from exc

    rendered = []
    for page_path in sorted(pages_dir.glob("slide-*.jpg"), key=lambda path: int(re.search(r"-(\d+)\.jpg$", path.name).group(1))):
        page_number = int(re.search(r"-(\d+)\.jpg$", page_path.name).group(1))
        destination = pages_dir / f"page-{page_number:03d}.jpg"
        page_path.replace(destination)
        rendered.append(destination)
    return rendered


def _write_slide_index(path: Path, pages: Iterable[SlidePage], rendered_pages: list[Path]) -> None:
    with path.open("w", newline="", encoding="utf-8") as index_file:
        writer = csv.writer(index_file)
        writer.writerow(["page", "characters", "extractable", "image"])
        writer.writerows(
            (page.number, len(page.text), bool(page.text), rendered_pages[page.number - 1].name if page.number <= len(rendered_pages) else "")
            for page in pages
        )


def _materials_readme(page_count: int, source_name: str, rendered_pages: int) -> str:
    return f"""# Material de slides

Fonte preservada: `{source_name}` → `slides.pdf`.
Páginas: {page_count}. Imagens renderizadas: {rendered_pages}.

`slides.md` contém o texto extraído por página, `pages/page-NNN.jpg` preserva a camada visual e `slides-index.csv` permite localizar rapidamente cada página.

O texto extraído serve para busca e associação com a transcrição. Ele não substitui a página visual: tabelas, diagramas e fórmulas devem ser conferidos no PDF original.
"""


def _pending_package_readme(output_dir: Path) -> str:
    """Describes a package that currently contains materials but no transcript."""
    return f"""# Lecture Context: {output_dir.name}

This package currently contains the professor's slide material only. Add the Wispr `.txt`
transcript later by running `prepare_lecture.py` with the recording, or
`prepare_transcript.py` when the class has no screen recording, against this same directory.

Read `materials/README.md` and `materials/slides.md` to inspect the page-indexed deck.
"""


def ingest_slides(pdf_path: Path, output_dir: Path) -> int:
    """Copies a slide PDF and creates a page-indexed material bundle."""
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Slide PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Slide material must be a .pdf file: {pdf_path}")

    pages = extract_slide_pages(pdf_path)
    materials_dir = output_dir / "materials"
    materials_dir.mkdir(parents=True, exist_ok=True)
    destination = materials_dir / "slides.pdf"
    if pdf_path.resolve() != destination.resolve():
        shutil.copy2(pdf_path, destination)

    rendered_pages = render_slide_pages(destination, materials_dir / "pages")
    (materials_dir / "slides.md").write_text(_render_slide_markdown(pages), encoding="utf-8")
    _write_slide_index(materials_dir / "slides-index.csv", pages, rendered_pages)
    (materials_dir / "README.md").write_text(
        _materials_readme(len(pages), pdf_path.name, len(rendered_pages)), encoding="utf-8"
    )
    package_readme = output_dir / "README.md"
    if not package_readme.exists():
        package_readme.write_text(_pending_package_readme(output_dir), encoding="utf-8")
    return len(pages)


def _terms(text: str) -> set[str]:
    return {word.lower() for word in WORD_PATTERN.findall(text) if word.lower() not in STOPWORDS}


def _page_candidates(section_text: str, pages: Iterable[SlidePage]) -> list[dict[str, Any]]:
    section_terms = _terms(section_text)
    if len(section_terms) < 2:
        return []

    candidates = []
    for page in pages:
        page_terms = _terms(page.text)
        overlap = section_terms & page_terms
        if len(overlap) < 2:
            continue
        score = len(overlap) / max(1, (len(section_terms) * len(page_terms)) ** 0.5)
        if score < 0.08:
            continue
        confidence = "alta" if len(overlap) >= 4 and score >= 0.20 else "média"
        candidates.append({"page": page.number, "score": score, "confidence": confidence, "terms": sorted(overlap)})
    return sorted(candidates, key=lambda candidate: (-candidate["score"], candidate["page"]))[:3]


def link_transcript_to_slides(entries: Iterable[dict[str, Any]], pages: Iterable[SlidePage]) -> list[dict[str, Any]]:
    """Creates conservative page candidates for timestamped transcript sections."""
    pages = list(pages)
    links = []
    for entry in entries:
        if entry.get("type") != "timestamp" or not entry.get("text", "").strip():
            continue
        candidates = _page_candidates(entry["text"], pages)
        if candidates:
            links.append({"timestamp": entry.get("sec", 0), "text": entry["text"].strip(), "candidates": candidates})
    return links


def render_slide_links(links: Iterable[dict[str, Any]]) -> str:
    """Renders links as reviewable candidates, never as exact visual claims."""
    links = list(links)
    lines = [
        "# Candidatos de associação: transcript ↔ slides",
        "",
        "As associações abaixo usam sobreposição lexical entre cada trecho e o texto extraído das páginas.",
        "São candidatos para revisão, não prova de que o slide estava na tela naquele segundo.",
        "",
    ]
    if not links:
        lines.append("Nenhum candidato atingiu o limiar de confiança.")
        return "\n".join(lines) + "\n"

    for link in links:
        minutes, seconds = divmod(int(link["timestamp"]), 60)
        hours, minutes = divmod(minutes, 60)
        lines.extend([f"## {hours:02d}:{minutes:02d}:{seconds:02d}", f"> {link['text']}", ""])
        for candidate in link["candidates"]:
            terms = ", ".join(candidate["terms"])
            lines.append(f"- Página {candidate['page']} — confiança {candidate['confidence']} (termos: {terms})")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_slide_links(output_dir: Path, entries: Iterable[dict[str, Any]]) -> int:
    """Loads the preserved PDF and writes the association candidates."""
    pdf_path = output_dir / "materials" / "slides.pdf"
    pages = extract_slide_pages(pdf_path)
    links = link_transcript_to_slides(entries, pages)
    (output_dir / "materials" / "slide-links.md").write_text(render_slide_links(links), encoding="utf-8")
    return len(links)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Ingest a slide PDF before the lecture transcript is available.")
    parser.add_argument("pdf", help="Path to the slide PDF")
    parser.add_argument("-o", "--output-dir", default="lectures/lecture", help="Lecture package directory")
    args = parser.parse_args()

    try:
        pages = ingest_slides(Path(args.pdf), Path(args.output_dir))
        print(f"Ingested {pages} slide page(s) into {Path(args.output_dir) / 'materials'}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
