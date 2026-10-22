#!/usr/bin/env python3
"""Stage 1 — Ingest: render every source PDF of a job to page images and extract raw text.

Usage:
    python scripts/ingest.py <job_dir> [--dpi 150]

Reads <job_dir>/job.json:
    "sources": [{"file": "sources/01-notes.pdf", "name": "notes.pdf", "kind": "pdf"}, ...]
    (older jobs: "pdf": "source.pdf" — a single source)
Writes:
    <job_dir>/pages/page-001.png ...   page images, numbered continuously across sources
    <job_dir>/raw_text.md              text layer per page (reference only)
    <job_dir>/ingest.json              sources with their page ranges, page sizes, detected headings

Deps: pip install pymupdf
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:  # older installs expose it as fitz
    import fitz as pymupdf  # type: ignore

HEADING_RE = re.compile(
    r"^(Section|Subsection|Chapter|Kapitel|Abschnitt)\s+[\d.]+\s*$|"
    r"^\d+(\.\d+)*\s+[A-ZÄÖÜ][^\n]{2,80}$")


def sources_of(job: dict):
    if job.get("sources"):
        return job["sources"]
    return [{"file": job["pdf"], "name": job.get("original_pdf_name") or job["pdf"], "kind": "pdf"}]


def ingest(job_dir: Path, dpi: int) -> dict:
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    sources = sources_of(job)
    pages_dir = job_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    for old in pages_dir.glob("page-*.png"):
        old.unlink()
    zoom = dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)

    raw_lines = ["# Raw text layer\n",
                 "_Reference only. Formulas, code indentation and figures are "
                 "not reliable here — Claude transcribes from the page images._\n"]
    headings, page_info, src_info = [], [], []
    n = 0
    for k, src in enumerate(sources, start=1):
        pdf_path = job_dir / src["file"]
        if not pdf_path.exists():
            raise SystemExit(f"source not found: {pdf_path}")
        doc = pymupdf.open(pdf_path)
        first = n + 1
        raw_lines.append(f"\n\n# Source {k}: {src.get('name', src['file'])}\n")
        for page in doc:
            n += 1
            png = pages_dir / f"page-{n:03d}.png"
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(png)
            text = page.get_text("text")
            raw_lines.append(f"\n\n---\n\n## Page {n}  (source {k}, page {page.number + 1})\n\n{text.strip()}\n")
            for line in text.splitlines():
                if HEADING_RE.match(line.strip()):
                    headings.append({"page": n, "source": k, "text": line.strip()})
            page_info.append({"page": n, "source": k, "source_page": page.number + 1, "image": png.name,
                              "width": pix.width, "height": pix.height, "chars": len(text)})
            print(f"page {n} (source {k} p.{page.number + 1}) -> {png.name} ({len(text)} chars)")
        src_info.append({"index": k, "name": src.get("name", src["file"]), "file": src["file"],
                         "kind": src.get("kind", "pdf"), "first_page": first, "last_page": n,
                         "page_count": n - first + 1})

    (job_dir / "raw_text.md").write_text("".join(raw_lines), encoding="utf-8")
    scanned = sum(1 for p in page_info if p["chars"] < 40)
    info = {
        "sources": src_info,
        "pdf": sources[0]["file"],
        "page_count": n,
        "dpi": dpi,
        "pages": page_info,
        "headings": headings,
        "likely_scanned_pages": scanned,
        "note": ("Some pages have almost no text layer — transcribe them "
                 "fully from the images." if scanned else ""),
    }
    (job_dir / "ingest.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"\n{len(sources)} source(s), {n} pages, {len(headings)} headings detected, "
          f"{scanned} pages with no usable text layer")
    return info


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("--dpi", type=int, default=150,
                    help="render resolution (150 is readable for Claude; "
                         "use 200 for very small print)")
    args = ap.parse_args()
    sys.exit(0 if ingest(args.job_dir, args.dpi) else 1)
