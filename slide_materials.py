#!/usr/bin/env python3
"""Slide ingestion (PDF or .pptx) and conservative transcript-to-page linking."""

import csv
import re
import shutil
import subprocess
import tempfile
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
    ["a", "ao", "aos", "as", "de", "da", "das", "do", "dos", "e", "em", "na", "nas", "no", "nos", "o", "os", "para", "por", "que", "se", "um", "uma", "uns", "umas", "com", "como", "mais", "menos", "muito", "muita", "este", "esta", "esse", "essa", "isso", "aqui", "ali", "sobre", "entre", "pelo", "pela", "pelos", "pelas", "quando", "onde", "então", "também", "já", "não", "sim", "são", "ser", "foi", "tem", "temos", "vai", "vamos", "sua", "suas", "seu", "seus"]
)


PDF_SUFFIX = ".pdf"
PPTX_SUFFIX = ".pptx"
SUPPORTED_SLIDE_SUFFIXES = (PDF_SUFFIX, PPTX_SUFFIX)
MACOS_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
# A deck that stalls the converter never returns; the wait has to end somewhere.
CONVERSION_TIMEOUT_SECONDS = 300


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


def _find_soffice() -> str | None:
    """Finds LibreOffice, including the macOS bundle that installs no PATH entry."""
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return str(MACOS_SOFFICE) if MACOS_SOFFICE.is_file() else None


def convert_pptx_to_pdf(pptx_path: Path, staging_dir: Path) -> Path:
    """Converts a deck to PDF, because every later step reads pages, never shapes."""
    soffice = _find_soffice()
    if soffice is None:
        raise ValueError(
            f"LibreOffice is required to ingest '{pptx_path.name}'. "
            "Install it (`brew install --cask libreoffice`) or export the deck to PDF first."
        )

    try:
        subprocess.run(
            [
                soffice,
                # A private profile: the default one is locked while the desktop app runs.
                f"-env:UserInstallation=file://{staging_dir / 'libreoffice-profile'}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(staging_dir),
                str(pptx_path),
            ],
            check=True,
            capture_output=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"LibreOffice did not finish converting '{pptx_path.name}' within "
            f"{CONVERSION_TIMEOUT_SECONDS}s; convert the deck by hand and ingest the PDF."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip()
        raise ValueError(f"LibreOffice could not convert '{pptx_path}': {detail}") from exc

    converted = staging_dir / f"{pptx_path.stem}{PDF_SUFFIX}"
    if not converted.is_file():
        raise ValueError(f"LibreOffice reported success but produced no PDF for '{pptx_path}'.")
    return converted


def _write_slide_index(path: Path, pages: Iterable[SlidePage], rendered_pages: list[Path]) -> None:
    with path.open("w", newline="", encoding="utf-8") as index_file:
        writer = csv.writer(index_file)
        writer.writerow(["page", "characters", "extractable", "image"])
        writer.writerows(
            (page.number, len(page.text), bool(page.text), rendered_pages[page.number - 1].name if page.number <= len(rendered_pages) else "")
            for page in pages
        )


def _materials_readme(page_count: int, source_name: str, rendered_pages: int, converted: bool = False) -> str:
    origin = (
        f"Fonte preservada: `{source_name}` → `slides.pptx`, convertida pelo LibreOffice para `slides.pdf`."
        if converted
        else f"Fonte preservada: `{source_name}` → `slides.pdf`."
    )
    return f"""# Material de slides

{origin}
Páginas: {page_count}. Imagens renderizadas: {rendered_pages}.

`slides.md` contém o texto extraído por página, `pages/page-NNN.jpg` preserva a camada visual e `slides-index.csv` permite localizar rapidamente cada página.

O texto extraído serve para busca e associação com a transcrição. Ele não substitui a página visual: tabelas, diagramas e fórmulas devem ser conferidos no arquivo original.
"""


def _pending_package_readme(output_dir: Path) -> str:
    """Describes a package that currently contains materials but no transcript."""
    return f"""# Lecture Context: {output_dir.name}

This package currently contains the professor's slide material only. Add text later
with `digest.py --transcript transcript.txt -o <this-directory>`, optionally supplying
videos and their `--offsets`. For video with audio, use
`digest.py recording.mp4 --transcribe -o <this-directory>`.

Read `materials/README.md` and `materials/slides.md` to inspect the page-indexed deck.
"""


def ingest_slides(source_path: Path, output_dir: Path) -> int:
    """Preserves a slide deck (.pdf or .pptx) and creates a page-indexed bundle."""
    if not source_path.is_file():
        raise FileNotFoundError(f"Slide material not found: {source_path}")
    suffix = source_path.suffix.lower()
    if suffix not in SUPPORTED_SLIDE_SUFFIXES:
        raise ValueError(f"Slide material must be a .pdf or .pptx file: {source_path}")

    materials_dir = output_dir / "materials"
    destination = materials_dir / "slides.pdf"
    converted = suffix == PPTX_SUFFIX
    # Conversion and text extraction run before any output exists, so a deck that
    # cannot be read leaves no half-written package behind.
    with tempfile.TemporaryDirectory(prefix="digest_pptx_") as staging:
        pdf_path = convert_pptx_to_pdf(source_path, Path(staging)) if converted else source_path
        pages = extract_slide_pages(pdf_path)
        materials_dir.mkdir(parents=True, exist_ok=True)
        if converted:
            shutil.copy2(source_path, materials_dir / "slides.pptx")
        else:
            # A deck from an earlier ingestion would keep claiming to be this package's source.
            (materials_dir / "slides.pptx").unlink(missing_ok=True)
        if pdf_path.resolve() != destination.resolve():
            shutil.copy2(pdf_path, destination)

    rendered_pages = render_slide_pages(destination, materials_dir / "pages")
    (materials_dir / "slides.md").write_text(_render_slide_markdown(pages), encoding="utf-8")
    _write_slide_index(materials_dir / "slides-index.csv", pages, rendered_pages)
    (materials_dir / "README.md").write_text(
        _materials_readme(len(pages), source_path.name, len(rendered_pages), converted), encoding="utf-8"
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
