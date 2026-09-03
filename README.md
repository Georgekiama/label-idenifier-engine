# ExhibitPro — Document Identity Engine

No AI, no vision model — PyMuPDF only, OCR fallback only for text-less pages.

| Stage | Module | What it does |
|---|---|---|
| 0 | `page_census.py` | Cheap structural record for **every** page (~6.7 ms/page) |
| 0.5 | `segmenter.py` | Finds the seams inside a compound PDF; emits a segment map |
| 1 | `evidence_extractor.py` | Span-level extraction with geometry and typography |
| 2 | *spec only* | `FEATURE_CATALOGUE_V2.md` |

## Running the full pipeline

```
python page_census.py --input "<pdf folder>" --output census_json --validate
python segmenter.py --census census_json --output segments_json --pdf-dir "<pdf folder>" --report --validate
python evidence_extractor.py --input "<pdf folder>" --output extracted_json --validate
```

`segmenter.py` runs without `--pdf-dir`, just without Tier B refinement of
ambiguous seams.

**Benchmark (198 PDFs / 4,580 pages):** census 30s, segmentation 3.4s. Output of
both stages is byte-identical across repeat runs, verified by directory hash.

### Why Stage 0 exists

Stage 1 reads pages 1–2, which assumes one document per file. Production intake
is compound — a motion, its exhibits, a medical record, a scanned statement, all
in one PDF. Stage 0 measures every page cheaply (6.7 ms/page, so a 300-page file
takes ~2s), Stage 0.5 finds the boundaries from those measurements, and Stage 1
then extracts only each segment's head pages. Expensive span parsing costs
33.7 ms/page and is spent only where it can change a decision.

---

# Stage 1 — Document Input & Evidence Extractor v1

Run this in VS Code / a local terminal (no AI, no vision model — PyMuPDF only,
OCR fallback only for text-less pages).

## 1. Install dependencies

```
pip install -r requirements.txt
```

If any PDF pages are scanned images with no embedded text, the script falls back
to OCR via Tesseract. That needs the Tesseract engine installed separately (the
`pytesseract` package is just a Python wrapper around it):

- Windows: install from https://github.com/UB-Mannheim/tesseract/wiki, then make
  sure `tesseract.exe` is on your PATH (or set
  `pytesseract.pytesseract.tesseract_cmd` at the top of the script to its path).

If none of your PDFs are scanned/image-only, you can skip installing Tesseract —
the script only imports pytesseract/PIL when a page actually needs OCR.

## 2. Run it

```
python evidence_extractor.py --input "C:\Users\user\Downloads\corpus all pdfs" --output "C:\Users\user\Downloads\corpusEP\extracted_json" --validate
```

- `--input`: folder of source PDFs (198 files currently in `corpus all pdfs`)
- `--output`: folder to write one JSON per PDF into
- `--validate`: after extraction, checks every output JSON has the full schema
  (deliverable #3 — proving the schema is consistent)

## What it does

- Reads Page 1, then Page 2 only (Page 1 only if the PDF has just one page).
- One JSON file per PDF: `document_id`, `filename`, `total_pages`, `pages_read`, `blocks[]`.
- Each block: `id`, `page`, `text`, `x`, `y`, `width`, `height`, `font_size`,
  `font_name`, `bold`, `uppercase_ratio`, `centered`.
- No classification, no labels, deterministic output (content-hash `document_id`,
  stable PyMuPDF reading order, rounded geometry).

Full design notes and the "what counts as a text block" decision are documented
in the docstring at the top of `evidence_extractor.py`.

## After you run it

Let Claude know the output folder — it can read the resulting JSON files directly
(no sandbox needed for that) to sanity-check schema consistency across a sample.
