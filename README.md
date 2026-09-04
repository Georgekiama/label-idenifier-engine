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

Current baselines (policy `balanced`):

| metric | naive | realistic |
|---|---|---|
| precision | 0.952 | 0.789 |
| recall | 0.444 | 0.549 |
| **assisted_recall** | **0.978** | **0.863** |

Recall by seam difficulty on the realistic set:

| difficulty | cut | reaching a human |
|---|---|---|
| easy (slip sheets) | 0.684 | 0.684 |
| medium (producer change) | 0.694 | 0.972 |
| hard (continuous Bates run) | 0.179 | **0.964** |

**assisted_recall is the number to read.** In a continuous Bates production the
real boundaries carry no structural change at all, so they cannot be cut on
evidence the engine can see - but they are never lost silently. Continuity
evidence decides whether we CUT; it never decides whether a seam is SEEN.

## Granularity policy

Whether "Section I / Section II / Appendices" of one report is four binder tabs
or one is a product decision, not a constant, so it is named rather than
hardcoded:

```
python segmenter.py --census census_json --output segments_json --policy conservative
```

| policy | threshold | realistic precision | assisted_recall | false cuts per single document |
|---|---|---|---|---|
| conservative | 1.80 | 0.932 | 0.990 | 0.00 |
| balanced (default) | 1.50 | 0.789 | 0.863 | 0.20 |
| aggressive | 1.00 | 0.738 | 0.843 | 0.50 |

assisted_recall stays between 0.84 and 0.99 across the whole range, so the dial
trades **automation against review workload**, not accuracy against inaccuracy.

## Contracts: where the tunables are owned

Every weight, threshold and policy lives in `contracts/segmentation.yaml`, not
in Python. A contract's `version` must move whenever its content moves, or two
runs of the same declared version can disagree and the audit ledger becomes a
liar. That is enforced mechanically:

```
python tools/verify_contracts.py            # exit 1 on drift
python tools/verify_contracts.py --update   # after a deliberate version bump
```

Every segment map stamps `contract_versions` and the active `policy`, so any
label can be traced to the exact configuration that produced it.

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

## Stage 5: Identity Composer

Given pages 1-2, produce the top presentable binder labels using deterministic
algorithms only. No AI, no vision models, no LLMs, no embeddings. The engine
never invents words - it extracts, ranks, joins, normalises and composes text
that already exists on the page. A test enforces exactly that.

Six independent evidence sources, fused by contract weights:

| Source | Contributes |
|---|---|
| `typography` | hierarchy level by character mass, not raw size |
| `presentability` | fitness for a binder tab, independent of topic |
| `bm25` | corpus-rarity relevance against a **frozen** IDF table |
| `rake` | connector-aware multi-word phrase extraction |
| `textrank` | classical PageRank, fixed iterations |
| `phrase graph` | reconstructs headings split across units |

```
python tools/build_idf.py --corpus "<pdfs>"            # one-off, frozen artefact
python tools/evaluate_labels.py --corpus "<pdfs>"
python tools/evaluate_labels.py --corpus "<pdfs>" --ablate
```

### Two deviations from the brief, both forced by measurement

**RAKE keeps interior connectors.** Classic RAKE splits on every stopword, so
`Motion to Compel` becomes `Motion` + `Compel`. 30% of phrase-like units on page
1 carry an interior stopword, including `TREATY WITH THE TRIBES OF MIDDLE
OREGON`. Splitting is now on punctuation and *hard* stopwords only.

**BM25 has no query.** BM25 ranks documents against a query; there is neither
here. The candidate phrase is treated as the query and the document as the
corpus entry, so the score asks how well a phrase represents its document given
corpus-wide term rarity. IDF is corpus state, so the table is frozen and
versioned like any other contract.

### Measured, with the ablation

77 harvested titles, match at token-F1 >= 0.70:

| metric | value |
|---|---|
| top1 accuracy | 0.286 |
| top3 accuracy | 0.338 |
| binder-shaped (2-12 words) | **0.935** |
| mean presentability | 0.863 |

Disabling one source at a time:

| disabled | top1 delta | verdict |
|---|---|---|
| typography | −0.065 | load-bearing |
| bm25 | −0.039 | load-bearing |
| rake | −0.026 | load-bearing |
| presentability | −0.013 | neutral on accuracy, decisive for shape |
| textrank | −0.013 | no measurable effect |
| position | +0.013 | no measurable effect |
| graph_bonus | +0.000 | no measurable effect |

**Three sources earn their weight; three do not, on this corpus.** They are kept
at low weight rather than deleted, for a stated reason: the phrase graph targets
headings split across lines and the benchmark is 198 unrelated single documents,
so it supplies only 3 of 77 winners here. The same caveat applies to IDF - built
over one firm's intake, with the same letterhead on every file, it would be far
stronger than it is over 198 unrelated issuers.

### Accuracy and presentability are different questions

Ground-truth titles run to a median of 7 words but **39% exceed 8 words** and
30% exceed 72 characters, up to 26 words. So reproducing the metadata title and
producing a binder label are not the same objective, and the evaluator reports
both axes. An early presentability band of 3-8 words measured as actively
*harmful* (+0.052 top1 when disabled) because it fought that tail; widened to
3-12 against the observed distribution, top1 rose 0.221 to 0.286 while 93.5% of
labels stayed binder-shaped.

Weight configurations beyond that are statistically indistinguishable on 77
documents - one document is 0.013 - so no further tuning is justified until the
hand-labelled set exists.

## Audit dashboard

A local tool for examining and fine-tuning the engine. Upload PDFs, see the
label, and see **on the page itself** which block won and which features paid
for it.

```
pip install -r requirements.txt
python dashboard/server.py          # http://127.0.0.1:5000
```

- **Upload many PDFs at once** (button or drag-and-drop); each is run through
  the full pipeline: census, segmentation, assembly, features, TITLE.
- **Page preview with scored overlays.** Every assembled unit is drawn as a box
  on the rendered page. The winner is outlined in green with its score; click
  any other box to see why it lost. Tick *excluded* to show units that were
  ineligible, each with the rule that excluded it.
- **Why this label.** A per-feature table - value, weight, contribution, signed
  bar - that sums to the score. This is the ledger entry a reviewer argues with.
- **Candidates.** The full ranking. Click one to jump to its page and swap the
  breakdown, so "why that block and not this one" is two clicks.
- **Segmentation.** Every cut with the signals that made it, plus boundaries
  flagged for review where change evidence was present but continuity outweighed
  it.
- **Features.** The raw Feature Matrix row, including span provenance.
- **Granularity policy** is switchable in the header; *Re-run* re-analyses the
  selected document without re-uploading.
- **Remove what you have tested.** Hover a document for its **×**, or *Clear all*
  in the panel header. Deleting removes the stored PDF from the temp directory
  as well as the row, so the list on screen and the bytes on disk agree.

Contract and stage versions are shown in the header, so any finding can be
reproduced against the exact configuration that produced it. Nothing leaves the
machine; uploads go to a temp directory.

**Sharing it with someone else:** see [DEPLOY.md](DEPLOY.md). Short version — it
needs a container, not a serverless function, because an uploaded PDF is stored
and re-read by later requests for page images. `render.yaml` and `Dockerfile`
are included. Set `EP_USER`/`EP_PASSWORD` or the dashboard is open to anyone
with the URL.

## Stage 1.5 / 2 / 3: units, features, TITLE

```
| Stage | Module | Output |
|---|---|---|
| 1.5 | unit_assembly.py | spans -> lines -> units, with provenance |
| 2 | feature_matrix.py | one row per unit, 21 features |
| 3/4 | role_title.py | TITLE + confidence + full audit trail |
```

Assembly is column-aware: spans sharing a baseline in different columns are not
one line, and reading order runs band by band. Without that, a two-column page
produced `"HUCKLEBERRYfully dried them. In British Columb"` - a heading welded
to unrelated body text. Assembly is verified lossless on every run: it may
regroup text, never invent, drop, or reorder it.

```
python tools/evaluate_titles.py --corpus "<pdfs>" --check
```

Measured against 77 harvested titles (match at token-F1 >= 0.70):

| metric | value |
|---|---|
| coverage | 0.805 |
| accuracy | 0.247 |
| precision_emit | 0.306 |
| candidate_accuracy | 0.260 |
| escalation | 0.195 |

**This is an honest first measurement, and it is not good enough to ship.**
`candidate_accuracy` 0.260 says the scorer, not the calibration, is the ceiling.
Three things are known about that number:

- Roughly a third of the "misses" are ground-truth mismatches in KIND, not
  errors: a PDF's `/Title` is often the case caption or the parent publication
  where the engine correctly returns the document's own heading. `000901` truth
  is `Docket No. 98-0672, Patterson Drilling Company`; the engine returns
  `DECISION AND ORDER`.
- A further group are near-misses scored as zero: `SHORELINE COUNTERMEASURES
  MANUAL` against a truth of `Shoreline Countermeasures Manual: Tropical
  Coastal Environments`.
- The rest are real failures - author lists, court headers and section markers
  beating the title.

**`auto` is deliberately disabled.** Measured margin-vs-correctness is flat at
0.26-0.40 above a margin of 0.10, so no band earns unattended acceptance.
Every emitted title goes to review. Lowering the bar to manufacture an auto rate
would be inventing confidence the evidence does not support.

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
