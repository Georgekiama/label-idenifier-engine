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

## Goldens: how we know it works

Segmentation quality cannot be measured on a corpus of single documents - every
cut is a false positive by construction, so precision looks perfect and **recall
is invisible**. The fixtures solve that by concatenating known PDFs into
compound files whose boundaries are known by construction. No hand-labelling.

```
python tools/make_compound_fixtures.py --corpus "<pdfs>" --output goldens/fixtures
python tools/evaluate_segmentation.py --fixtures goldens/fixtures --check
python -m pytest tests/ -q
```

Two fixture sets, both gated. The naive set concatenates unrelated documents -
every seam is easy. The realistic set adds the cases that actually occur:

| scenario | what it tests |
|---|---|
| `exhibit_binder` | cover page, then slip-sheet-introduced exhibits |
| `bates_production` | one continuous Bates run stamped across several same-producer documents - the seams are **invisible** to every change signal |
| `mixed_intake` | producer changes, scanned exhibits, landscape plates |
| `single_document` | one real corpus document, unmodified: **zero** true seams, so every cut is a false positive against real internal structure |

```
python tools/make_realistic_fixtures.py --corpus "<pdfs>" --output goldens/realistic
python tools/evaluate_segmentation.py --fixtures goldens/realistic --baseline goldens/baseline_realistic.json --check
```

Current baselines:

| metric | naive | realistic |
|---|---|---|
| precision | 0.950 | 0.792 |
| recall | 0.563 | 0.559 |
| f1 | 0.707 | 0.655 |
| assisted_recall | 0.889 | 0.726 |

Recall by seam difficulty on the realistic set is the number that matters:

| difficulty | cut | reaching a human |
|---|---|---|
| easy (slip sheets) | 0.684 | 0.684 |
| medium (producer change) | 0.722 | **0.944** |
| hard (continuous Bates run) | 0.179 | **0.500** |

**Hard seams are the open problem.** In a continuous Bates production the real
boundaries carry no structural change at all, so they cannot be cut on evidence
the engine can see. What is enforced instead is that they are never lost
*silently*: continuity evidence decides whether we CUT, never whether a seam is
SEEN. Half of them now reach the review queue; before that rule they reached
nobody.

Negative controls hold at 0.20 false cuts per single document.

`--check` exits 1 on regression, so this gates a build. Metrics are gated
individually: a recall improvement cannot pay for itself with false boundaries,
and a precision improvement cannot pay for itself by refusing to cut.

## Title ground truth

```
python tools/harvest_titles.py --corpus "<pdfs>" --census census_json --output goldens/labels.csv
```

79 of 198 titles (40%) harvest automatically from PDF metadata and outlines.
These are **test data, never training data** - the engine never reads them.

Coverage is biased toward easy documents: 23% of the identified legal documents
and 0% of documents with scanned first pages carry usable metadata. Hand-label
the gap, not the corpus.

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
