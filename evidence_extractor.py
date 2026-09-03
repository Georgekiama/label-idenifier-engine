"""
ExhibitPro - Deliverable 01
Document Input & Evidence Extractor v1

Scope (intentionally narrow):
    - Reads Page 1, then Page 2 only (Page 1 only if the PDF has a single page).
    - Extracts text blocks with exact text and PDF geometry (no AI, no vision model,
      no document classification, no label generation).
    - Primary extraction engine: PyMuPDF (fitz).
    - OCR (Tesseract, via pytesseract) is used ONLY as a fallback, and only for a page
      that has zero embedded/extractable text. It is a traditional OCR engine, not an
      AI/vision model, and it does not classify or label the document.
    - Output: one JSON file per PDF, schema below.

Definition of "text block" used here:
    PyMuPDF's page.get_text("dict") groups content into blocks -> lines -> spans.
    A single PDF "block" (paragraph-like region) can legitimately mix multiple fonts,
    sizes, or bold/non-bold runs across its lines. Since this schema requires exactly
    one font_size / font_name / bold value per record, this script treats each SPAN
    (a maximal run of text with uniform font/size/style) as one output "block" record.
    This is the most granular unit for which font_size/font_name/bold is unambiguous
    and geometrically exact. This decision is documented here for transparency and is
    easy to change (e.g. to line-level or native-block-level) if the next stage needs
    different granularity.

Output JSON schema (per PDF):
{
  "document_id": "<sha256[:16] of file bytes>",
  "filename": "<original filename>",
  "total_pages": <int>,
  "pages_read": [1] or [1, 2],
  "blocks": [
    {
      "id": "b0001",
      "page": 1,
      "text": "<exact original text>",
      "x": <float, PDF points>,
      "y": <float, PDF points>,
      "width": <float>,
      "height": <float>,
      "font_size": <float>,
      "font_name": "<string, or 'OCR' if this page fell back to OCR>",
      "bold": <bool>,
      "uppercase_ratio": <float 0.0-1.0>,
      "centered": <bool>
    },
    ...
  ]
}

Determinism:
    - Block order follows PyMuPDF's natural reading order from get_text("dict"),
      which is stable for a given file/library version.
    - All numeric geometry is rounded to 2 decimal places; uppercase_ratio to 4.
    - document_id is a content hash (sha256 of the raw PDF bytes), so re-running on
      an unchanged file always yields the same id and the same JSON.

Usage:
    pip install -r requirements.txt
    python evidence_extractor.py --input "C:\\path\\to\\corpus all pdfs" --output "C:\\path\\to\\extracted_json"

    Optional:
      --validate   After extraction, run a schema-consistency check across all
                    produced JSON files and print a short report.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF is required. Install with: pip install pymupdf", file=sys.stderr)
    raise

# OCR deps are optional at import time - only required if a page has no embedded text.
_OCR_AVAILABLE = True
try:
    import pytesseract
    from PIL import Image
except ImportError:
    _OCR_AVAILABLE = False

BOLD_FLAG = 1 << 4  # PyMuPDF span flag bit for bold
OCR_DPI = 300
CENTER_TOLERANCE_RATIO = 0.03  # fraction of page width; block is "centered" if its
                                # horizontal center falls within this tolerance of
                                # the page's horizontal center

REQUIRED_BLOCK_FIELDS = [
    "id", "page", "text", "x", "y", "width", "height",
    "font_size", "font_name", "bold", "uppercase_ratio", "centered",
]
REQUIRED_DOC_FIELDS = ["document_id", "filename", "total_pages", "pages_read", "blocks"]


def compute_document_id(pdf_path: Path) -> str:
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def compute_uppercase_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return round(upper / len(letters), 4)


def is_centered(x0: float, x1: float, page_width: float) -> bool:
    if page_width <= 0:
        return False
    block_center = (x0 + x1) / 2.0
    page_center = page_width / 2.0
    tolerance = page_width * CENTER_TOLERANCE_RATIO
    return abs(block_center - page_center) <= tolerance


def extract_text_page(page, page_number: int, counter_start: int, page_width: float):
    """Extract spans from a page that has embedded text, via PyMuPDF."""
    blocks = []
    counter = counter_start
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:  # 0 = text block; skip images (type 1)
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text == "":
                    continue
                x0, y0, x1, y1 = span.get("bbox", (0, 0, 0, 0))
                font_name = span.get("font", "")
                size = span.get("size", 0.0)
                flags = span.get("flags", 0)
                bold = bool(flags & BOLD_FLAG) or ("bold" in font_name.lower())
                counter += 1
                blocks.append({
                    "id": f"b{counter:04d}",
                    "page": page_number,
                    "text": text,
                    "x": round(x0, 2),
                    "y": round(y0, 2),
                    "width": round(x1 - x0, 2),
                    "height": round(y1 - y0, 2),
                    "font_size": round(size, 2),
                    "font_name": font_name,
                    "bold": bold,
                    "uppercase_ratio": compute_uppercase_ratio(text),
                    "centered": is_centered(x0, x1, page_width),
                })
    return blocks, counter


def extract_ocr_page(page, page_number: int, counter_start: int, page_width_pts: float):
    """OCR fallback for a page with no embedded text. Uses Tesseract (not AI/vision-LLM)."""
    if not _OCR_AVAILABLE:
        raise RuntimeError(
            "Page has no embedded text and OCR fallback deps are missing. "
            "Install with: pip install pytesseract pillow, and install the Tesseract "
            "OCR engine (see README)."
        )
    counter = counter_start
    blocks = []

    zoom = OCR_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    # Group OCR words into lines using (block_num, par_num, line_num)
    lines = {}
    n = len(data["text"])
    for i in range(n):
        word = data["text"][i]
        if word is None or word.strip() == "":
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    # px -> pdf points conversion (matches the zoom used for rendering)
    def to_pts(v):
        return v / zoom

    for key in sorted(lines.keys()):
        idxs = lines[key]
        words = [data["text"][i] for i in idxs]
        text = " ".join(words)
        lefts = [data["left"][i] for i in idxs]
        tops = [data["top"][i] for i in idxs]
        rights = [data["left"][i] + data["width"][i] for i in idxs]
        bottoms = [data["top"][i] + data["height"][i] for i in idxs]
        x0, y0 = to_pts(min(lefts)), to_pts(min(tops))
        x1, y1 = to_pts(max(rights)), to_pts(max(bottoms))
        counter += 1
        blocks.append({
            "id": f"b{counter:04d}",
            "page": page_number,
            "text": text,
            "x": round(x0, 2),
            "y": round(y0, 2),
            "width": round(x1 - x0, 2),
            "height": round(y1 - y0, 2),
            "font_size": round(y1 - y0, 2),  # best available estimate: line box height
            "font_name": "OCR",
            "bold": False,  # not reliably determinable from OCR without a model
            "uppercase_ratio": compute_uppercase_ratio(text),
            "centered": is_centered(x0, x1, page_width_pts),
        })
    return blocks, counter


def process_pdf(pdf_path: Path):
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    pages_to_read = [1] if total_pages <= 1 else [1, 2]

    all_blocks = []
    counter = 0
    for page_number in pages_to_read:
        page = doc.load_page(page_number - 1)  # 0-indexed
        page_width = page.rect.width
        has_text = bool(page.get_text("text").strip())
        if has_text:
            blocks, counter = extract_text_page(page, page_number, counter, page_width)
        else:
            blocks, counter = extract_ocr_page(page, page_number, counter, page_width)
        all_blocks.extend(blocks)

    doc.close()

    return {
        "document_id": compute_document_id(pdf_path),
        "filename": pdf_path.name,
        "total_pages": total_pages,
        "pages_read": pages_to_read,
        "blocks": all_blocks,
    }


def validate_schema(output_dir: Path):
    json_files = sorted(output_dir.glob("*.json"))
    if not json_files:
        print("VALIDATE: no JSON files found in output dir.")
        return
    print(f"VALIDATE: checking schema consistency across {len(json_files)} JSON files...")
    problems = 0
    for jf in json_files:
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)
        missing_doc = [k for k in REQUIRED_DOC_FIELDS if k not in data]
        if missing_doc:
            problems += 1
            print(f"  [FAIL] {jf.name}: missing document fields {missing_doc}")
            continue
        for b in data["blocks"]:
            missing_block = [k for k in REQUIRED_BLOCK_FIELDS if k not in b]
            if missing_block:
                problems += 1
                print(f"  [FAIL] {jf.name} block {b.get('id')}: missing fields {missing_block}")
                break
    if problems == 0:
        print(f"VALIDATE: OK - all {len(json_files)} files match the schema.")
    else:
        print(f"VALIDATE: {problems} file(s) had schema problems.")


def main():
    parser = argparse.ArgumentParser(description="ExhibitPro Document Input & Evidence Extractor v1")
    parser.add_argument("--input", required=True, help="Folder containing PDFs")
    parser.add_argument("--output", required=True, help="Folder to write one JSON per PDF into")
    parser.add_argument("--validate", action="store_true", help="Run schema-consistency check after extraction")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdf_paths = sorted(input_dir.glob("*.pdf"))
    if not pdf_paths:
        print(f"No PDFs found in {input_dir}")
        return

    print(f"Found {len(pdf_paths)} PDFs in {input_dir}")
    ok, failed = 0, []
    for pdf_path in pdf_paths:
        try:
            result = process_pdf(pdf_path)
            out_path = output_dir / f"{pdf_path.stem}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            ok += 1
            print(f"  [OK] {pdf_path.name} -> {out_path.name} ({len(result['blocks'])} blocks)")
        except Exception as e:
            failed.append((pdf_path.name, str(e)))
            print(f"  [ERROR] {pdf_path.name}: {e}")

    print(f"\nDone. {ok} succeeded, {len(failed)} failed.")
    if failed:
        print("Failures:")
        for name, err in failed:
            print(f"  - {name}: {err}")

    if args.validate:
        validate_schema(output_dir)


if __name__ == "__main__":
    main()
