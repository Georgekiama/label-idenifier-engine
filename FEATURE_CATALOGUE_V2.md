# ExhibitPro — Feature Catalogue v2

**Status:** supersedes Feature Catalogue v1
**Feature set version:** `FS-2.0.0`
**Depends on:** Page Census v1.4.0 (Stage 0), Segmentation v1.3.0 (Stage 0.5), Document Input v1 (Stage 1)

---

## Purpose

This document defines every engineered feature the Document Identity Engine is
allowed to use. It is not code. It is the mathematical specification for
transforming Document Input into the Feature Matrix (X).

Every feature is deterministic, reproducible, and auditable.

---

## What changed from v1, and why

Each change below was forced by a measurement on the 198-document benchmark
corpus, not by preference.

| # | Change | Evidence |
|---|---|---|
| 1 | **Feature rows are UNITS, not spans.** New Stage 1.5 assembly. | 26.9% of page-1 visual lines are split across multiple spans; 7 of 26 legal titles are fragmented. A span-level row can win TITLE while holding half a title. |
| 2 | **`font_rank` normalises to body baseline, not page maximum.** | On the legal subset, page-max normalisation returned `"30050"`, `"CCASE:"`, and a body paragraph as the 1.00 winners. 31% of legal documents have ≤2 distinct font sizes; 42% are monospace. |
| 3 | **Pipeline is per-SEGMENT, not per-file.** | Production intake is compound. Stage 0.5 emits a segment map; every feature below is computed within one segment's head pages. |
| 4 | **Two identity modes share one matrix.** Legal roles are a subset, not the backbone. | Only 26 of 198 documents (13%) carry ≥2 legal indicators; 162 (82%) carry none. Documentary identity must be first-class. |
| 5 | **X carries the text.** | v1 said the matrix "becomes the only input to Role Assignment". The Composer needs the string. X is features **plus** `text` and `source_span_ids`. |
| 6 | **Roles are scored jointly, not independently.** | A NASA report number scored higher than the title on identical features. It is not a bad TITLE, it is a good IDENTIFIER. Independent per-role argmax cannot express that. |
| 7 | **Every ranking has a mandated total order.** | v1 left ties unspecified, which is a determinism hole. |

---

## Pipeline position

```
Stage 0    Page Census            per-page structural record (all pages)
Stage 0.5  Segmentation           segment map + head pages
Stage 1    Document Input         span extraction, head pages only
Stage 1.5  Unit Assembly          spans -> lines -> units          <-- NEW
Stage 2    Feature Engineering    THIS DOCUMENT -> Feature Matrix (X)
Stage 3    Role Assignment        constrained joint assignment
Stage 4    Evidence Scoring
Stage 5    Label Composer
Stage 6    Audit Ledger
```

---

# Stage 1.5 — Unit Assembly (normative)

Feature rows are **units**. A unit is the largest run of text that a reader would
call one heading or one paragraph line-group. Assembly is deterministic:

1. **Spans → lines.** Group spans on the same page whose baseline `y` values fall
   within `0.6 × median_line_height`. Order by `x` ascending. Join with a single
   space, collapsing runs of whitespace.
2. **Lines → units.** Merge vertically adjacent lines when **all** hold:
   - font size differs by `< 0.6 pt`
   - `bold` flag is equal
   - vertical gap `≤ 1.6 × median_line_gap` for the page
3. **Provenance.** Each unit carries `source_span_ids` — every contributing span
   id from Stage 1. Nothing is discarded; the audit ledger can always walk back
   from a printed label to the exact spans that produced it.

**Audit rule.** Concatenating every unit's text in reading order must reproduce
the page's full text content, modulo whitespace normalisation. Assembly may
regroup text; it may never invent, drop, or reorder it.

---

# Feature Matrix (X) — row schema

One row per unit per segment.

```json
{
  "document_id": "<sha256[:16]>",
  "segment_index": 2,
  "unit_id": "u0007",
  "text": "MOTION TO COMPEL DISCOVERY",
  "source_span_ids": ["b0012", "b0013", "b0014"],
  "features": { "F-001": 1.42, "F-002": 0.97, ... },
  "feature_set_version": "FS-2.0.0",
  "pattern_library_version": "PL-2.0.0"
}
```

`text` and `source_span_ids` are **not** features. They are the payload the
Composer consumes and the ledger audits. No scoring may read them directly;
scoring reads only `features`.

---

# Category A — Layout

## F-001 — font_rank *(REVISED — breaking change from v1)*

**Feature ID:** F-001
**Name:** font_rank
**Category:** A — Layout
**Purpose:** Measure typographic prominence relative to the document's own body text.
**Input source:** `units[].font_size`; all unit font sizes across the segment's head pages.
**Formula:**
```
body_baseline = char-weighted modal font size across the segment's head pages,
                sizes rounded to 0.5 pt, weight = len(unit.text)
font_rank     = min(unit.font_size / body_baseline, 2.5) / 2.5
```
**Output type:** Float
**Expected range:** 0.0 – 1.0 (body text lands at `1/2.5 = 0.40`)
**Audit rule:** `body_baseline > 0` always; if the segment yields no text, the
feature is undefined and the unit is not emitted. Any unit at or below body size
must score `≤ 0.40`. The old v1 rule ("largest text must return 1.00") is
**withdrawn** — it was satisfiable by a docket stamp.

> **Why the change.** Dividing by the page maximum lets one oversized element —
> a Bates stamp, a decorative letterhead, a page number — define the scale and
> crush everything else. Dividing by the body baseline asks the question that
> actually matters: *is this bigger than what this document is set in?* It also
> degrades honestly: on a flat monospace pleading every unit scores ~0.40, which
> correctly reports "typography carries no signal here" rather than falsely
> nominating a winner.

---

## F-002 — center_score

**Feature ID:** F-002
**Name:** center_score
**Category:** A — Layout
**Purpose:** Measure horizontal centring, a strong title indicator in pleadings and cover pages.
**Input source:** `units[].x`, `units[].width`, page width from Page Census.
**Formula:**
```
unit_center = x + width / 2
page_center = page_width / 2
center_score = max(0, 1 - |unit_center - page_center| / (page_width * 0.25))
```
**Output type:** Float
**Expected range:** 0.0 – 1.0
**Audit rule:** A unit whose centre is exactly the page centre must return 1.00.
A unit whose centre is ≥ 25% of page width away must return 0.00. Page width is
taken from the census (rotation already resolved), never from raw MediaBox.

---

## F-003 — top_ratio

**Feature ID:** F-003
**Name:** top_ratio
**Category:** A — Layout
**Purpose:** Measure vertical position; identity concentrates at the top of a page.
**Input source:** `units[].y`, page height from Page Census.
**Formula:** `top_ratio = y / page_height`
**Output type:** Float
**Expected range:** 0.0 – 1.0 (0 = top of page)
**Audit rule:** Computed against the **rotation-resolved** page height from the
census. A page with `/Rotate 90` must use its presented height, not its MediaBox
height, or every unit on it is misplaced.

---

## F-004 — block_area

**Feature ID:** F-004
**Name:** block_area
**Category:** A — Layout
**Purpose:** Measure visual footprint, separating headings from incidental marks.
**Input source:** `units[].width`, `units[].height`, page dimensions.
**Formula:** `block_area = (width * height) / (page_width * page_height)`
**Output type:** Float
**Expected range:** 0.0 – 1.0
**Audit rule:** Normalised by page area so the value is comparable across page
sizes; a raw point-area is not comparable between a Letter page and an A3 exhibit.
Sum of all unit areas may exceed 1.0 (units may overlap); this is not an error.

---

## F-013 — whitespace_isolation *(NEW)*

**Feature ID:** F-013
**Name:** whitespace_isolation
**Category:** A — Layout
**Purpose:** Measure how much empty space surrounds a unit. Titles are set apart;
body text is not.
**Input source:** `units[].y`, neighbouring unit positions, median line gap.
**Formula:**
```
gap_above = (unit.y - previous_unit.y) / median_gap    (2.0 if first on page)
gap_below = (next_unit.y - unit.y) / median_gap        (2.0 if last on page)
whitespace_isolation = min((gap_above + gap_below) / 4.0, 1.0)
```
**Output type:** Float
**Expected range:** 0.0 – 1.0
**Audit rule:** `median_gap > 0`; where a page has fewer than 3 units the feature
returns 0.5 (undetermined) and must be recorded as imputed in the ledger.

> **Why it earns its place.** This is the strongest remaining title signal on the
> 42% of legal documents that are monospace with no size or bold contrast. When
> Category A/B typography goes flat, isolation is what is left, and it is pure
> geometry.

---

## F-014 — left_indent_ratio *(NEW)*

**Feature ID:** F-014
**Name:** left_indent_ratio
**Category:** A — Layout
**Purpose:** Locate the left-hand caption column of pleading paper and distinguish
indented quotations from flush body text.
**Input source:** `units[].x`, page width.
**Formula:** `left_indent_ratio = x / page_width`
**Output type:** Float
**Expected range:** 0.0 – 1.0
**Audit rule:** Must be computed before any de-skew or normalisation of scanned
pages; the raw geometry is the measurement.

---

## F-015 — zone *(NEW)*

**Feature ID:** F-015
**Name:** zone
**Category:** A — Layout
**Purpose:** Band the page so running headers and footers can be discounted
without deleting them.
**Input source:** F-003 `top_ratio`.
**Formula:**
```
top_ratio < 0.10            -> "header"
0.10 <= top_ratio <= 0.90   -> "body"
top_ratio > 0.90            -> "footer"
```
**Output type:** Categorical — `header` | `body` | `footer`
**Expected range:** exactly those three values
**Audit rule:** Boundaries are fixed constants, versioned with the feature set.
Zone never removes a unit from X; it only informs scoring.

---

# Category B — Typography

## F-005 — bold_flag

**Feature ID:** F-005
**Name:** bold_flag
**Category:** B — Typography
**Purpose:** Indicate emphasis.
**Input source:** `units[].bold` (PyMuPDF span flag bit 4, OR `"bold"` in font name).
**Formula:** `1.0 if any contributing span is bold else 0.0`
**Output type:** Float (boolean-valued)
**Expected range:** {0.0, 1.0}
**Audit rule:** **Undefined for OCR-sourced units.** Tesseract does not report
weight, and Stage 1 hardcodes `false` there. Where F-029 `source_modality` is
`ocr`, this feature must be emitted as `null` and excluded from scoring — never
as `0.0`, which would falsely assert "not bold". 19% of the legal subset has no
bold anywhere, so a false zero is indistinguishable from a real one.

---

## F-006 — uppercase_ratio

**Feature ID:** F-006
**Name:** uppercase_ratio
**Category:** B — Typography
**Purpose:** Identify legal headings, which are conventionally set in capitals.
**Input source:** `units[].text`
**Formula:** `count(uppercase letters) / count(alphabetic characters)`; `0.0` if
no alphabetic characters.
**Output type:** Float
**Expected range:** 0.0 – 1.0, rounded to 4 dp
**Audit rule:** Non-alphabetic characters are excluded from both numerator and
denominator. A unit of pure digits returns 0.0, not undefined.

---

## F-007 — font_family_class

**Feature ID:** F-007
**Name:** font_family_class
**Category:** B — Typography
**Purpose:** Normalise vendor font names into deterministic classes.
**Input source:** `units[].font_name`, normalised by the Page Census rules
(subset prefix and style suffix stripped).
**Formula:** First matching rule wins, in this order: `mono`, `serif`, `sans`,
else `other`. Rule regexes live in the versioned pattern library.
**Output type:** Categorical — `mono` | `serif` | `sans` | `other`
**Expected range:** exactly those four values
**Audit rule:** Order is normative — `mono` is tested before `serif` so
"Courier" cannot fall through. Vendor-specific names must never reach X.

---

## F-016 — typographic_contrast *(NEW — segment-level)*

**Feature ID:** F-016
**Name:** typographic_contrast
**Category:** B — Typography
**Purpose:** Declare whether typography carries usable signal in this segment at
all, so Role Assignment can re-weight rather than trust a flat page.
**Input source:** All unit font sizes across the segment's head pages.
**Formula:**
```
distinct = count of distinct char-weighted font sizes (rounded 0.5 pt)
           holding >= 5% of the segment's characters
typographic_contrast = min(distinct, 4) / 4
```
**Output type:** Float — identical for every row in the segment
**Expected range:** 0.0 – 1.0
**Audit rule:** A segment scoring `≤ 0.25` (one dominant size) must cause Role
Assignment to down-weight Categories A and B and rely on C and D. That
re-weighting must be recorded in the ledger as an applied routing rule.

> **Why.** 31% of the legal subset has ≤2 distinct sizes. On those documents,
> confident-looking layout features are noise. The engine should know that about
> itself and say so, rather than ranking noise.

---

# Category C — Lexical

All Category C features are deterministic pattern matches against a
version-controlled pattern library (`PL-2.0.0`). **No semantic interpretation.**
No feature in this category may be added, widened, or narrowed without bumping
the pattern library version, because doing so silently changes historical labels.

Every Category C feature shares this audit rule unless stated otherwise:

> **Shared audit rule.** The matched substring and its character offsets must be
> recorded in the ledger alongside the boolean. A pattern feature that cannot
> show what it matched is not auditable.

## F-008 — title_regex

**Feature ID:** F-008 · **Name:** title_regex · **Category:** C
**Purpose:** Detect approved legal document-title language.
**Input source:** `units[].text`
**Formula:** `1.0` if the text matches any pattern in `PL-2.0.0/title_patterns`
(Motion, Affidavit, Order, Complaint, Notice, Petition, Declaration, Subpoena,
Stipulation, Answer, Brief, Memorandum, Summons, Judgment, Opinion), else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule. Fires on ~33% of the benchmark corpus — it is a
**bonus signal, not the backbone**, and Role Assignment must not require it.

## F-009 — case_caption_pattern

**Feature ID:** F-009 · **Name:** case_caption_pattern · **Category:** C
**Purpose:** Detect an adversarial case caption.
**Input source:** `units[].text`
**Formula:** `1.0` if the text matches `<PARTY> v./vs./versus <PARTY>`, where a
party is a capitalised token sequence, else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** A bare `v` or `vs` with no capitalised party on **both** sides
must not fire. The naive v1 form matched table columns and inflated caption
coverage from 8% to 26% on the benchmark corpus.

## F-010 — court_pattern

**Feature ID:** F-010 · **Name:** court_pattern · **Category:** C
**Purpose:** Detect a named court institution.
**Input source:** `units[].text`
**Formula:** `1.0` on a match against `PL-2.0.0/court_patterns` (Superior,
District, Circuit, Supreme, Court of Appeals, Court of Claims, and the
"IN THE … COURT" header form), else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule.

## F-018 — date_pattern *(NEW)*

**Feature ID:** F-018 · **Name:** date_pattern · **Category:** C
**Purpose:** Supply the DATE role, which v1 defined in Stage 3 with no feature to
support it.
**Input source:** `units[].text`
**Formula:** `1.0` on a match against `PL-2.0.0/date_patterns`
(`M/D/YYYY`, `YYYY-MM-DD`, `Month D, YYYY`, `D Month YYYY`), else `0.0`.
The normalised ISO date is recorded as payload, not as a feature value.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule, plus: an ambiguous numeric date (`03/04/1907`) must
be recorded with its ambiguity flagged and must **never** be silently resolved to
one locale convention. Fires on 44% of the benchmark corpus.

## F-019 — exhibit_marker *(NEW)*

**Feature ID:** F-019 · **Name:** exhibit_marker · **Category:** C
**Purpose:** Detect an exhibit designation on the page.
**Input source:** `units[].text`
**Formula:** `1.0` on `EXHIBIT|EX\.|ATTACHMENT|APPENDIX` followed by a
designator of 1–4 alphanumerics, else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule. **This feature is a cross-check, not the source of
truth.** In production the exhibit number is injected by the application, which
already knows which tab it is building. Where the detected designator disagrees
with the injected one, the ledger records a `exhibit_mismatch` warning and the
injected value wins.

## F-020 — party_role_pattern *(NEW)*

**Feature ID:** F-020 · **Name:** party_role_pattern · **Category:** C
**Purpose:** Support the PARTY role and corroborate a caption.
**Input source:** `units[].text`
**Formula:** `1.0` on `PLAINTIFF|DEFENDANT|APPELLANT|APPELLEE|PETITIONER|
RESPONDENT|MOVANT|CLAIMANT` (singular or plural), else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule. Fires on 13% of the benchmark corpus.

## F-021 — org_source_pattern *(NEW)*

**Feature ID:** F-021 · **Name:** org_source_pattern · **Category:** C
**Purpose:** Supply the SOURCE role for documentary identity — the issuing body of
a non-legal exhibit.
**Input source:** `units[].text`
**Formula:** `1.0` when the text matches an organisational form: a trailing
`Inc.|LLC|Ltd.|Corp.|Corporation|Company|Association|Foundation|University|
Hospital|Bank|Department|Bureau|Agency|Administration|Office of|Ministry`, or a
`U.S. <AGENCY>` construction. Else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule. This is the documentary-mode counterpart to F-010
and must carry comparable weight; the 82% of intake that is not a pleading
depends on it.

## F-022 — identifier_pattern *(NEW)*

**Feature ID:** F-022 · **Name:** identifier_pattern · **Category:** C
**Purpose:** Detect a document control number (report number, docket number,
policy number) so it is assigned the IDENTIFIER role instead of competing for TITLE.
**Input source:** `units[].text`
**Formula:** `1.0` when the text is predominantly a structured code — matching
`[A-Z]{2,}[-/][A-Z0-9-]{3,}` or `(No|Case|Docket|Report)\.?\s*[:#]?\s*[A-Z0-9-]{3,}` —
and F-027 `digit_ratio ≥ 0.2`. Else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule.

> **Why.** On `000739` the string `NASA/TM-2005-213991 AIAA-2005-5716` outscored
> the real title on every layout feature and won TITLE. It is not a bad title; it
> is a good identifier. Without this feature the two roles are indistinguishable
> and one must lose.

## F-023 — bates_pattern *(NEW)*

**Feature ID:** F-023 · **Name:** bates_pattern · **Category:** C
**Purpose:** Detect a production stamp, so it is never mistaken for identity.
**Input source:** `units[].text`; `pages[].bates_in_series` from Page Census.
**Formula:** `1.0` only when the unit contains a Bates match **and** the Page
Census promoted that page's stamp to a validated series. Else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** **A page-level match alone must never set this feature.** The
unvalidated form matched state+ZIP in mailing addresses (`IL 60439`) on 39 of 198
documents. Validation is document-level: same prefix across ≥3 pages, constant
digit width, ≥80% ascending.

## F-024 — noise_pattern *(NEW)*

**Feature ID:** F-024 · **Name:** noise_pattern · **Category:** C
**Purpose:** Positively identify units that can never be identity, so scoring can
exclude rather than merely out-rank them.
**Input source:** `units[].text`
**Formula:** `1.0` when the whole unit is a bare page number, a URL, an email
address, a phone number, a line of pure punctuation, or matches
`PL-2.0.0/spacer_patterns` ("this page intentionally left blank"). Else `0.0`.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Shared rule. A unit with `noise_pattern = 1.0` is retained in X
(never silently dropped) but is ineligible to win any role. The ledger records
the exclusion.

---

# Category D — Structural

## F-011 — page_in_segment *(REVISED — renamed from page_number)*

**Feature ID:** F-011
**Name:** page_in_segment
**Category:** D — Structural
**Purpose:** Preserve which head page a unit came from; identity concentrates on
the first page of a segment.
**Input source:** `units[].page`, segment `start_page`.
**Formula:** `unit.page - segment.start_page + 1`
**Output type:** Integer
**Expected range:** 1 – 3
**Audit rule:** Renamed because the value is now **relative to the segment**, not
absolute in the file. In a compound document, absolute page 47 may be the first
page of exhibit C, and it must behave exactly like page 1 of a standalone file.

## F-012 — first_occurrence *(REVISED)*

**Feature ID:** F-012
**Name:** first_occurrence
**Category:** D — Structural
**Purpose:** Suppress repeated running headers and footers.
**Input source:** normalised `units[].text` across the segment's head pages.
**Formula:** `1.0` if this normalised text has not appeared earlier in the
segment's head pages, else `0.0`. Normalisation lowercases, collapses whitespace,
and masks digits (so "Page 12" and "Page 13" are one text).
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Digit masking is normative — without it a running header defeats
the feature by incrementing its page number. Scope is the segment, never the file.

## F-025 — pleading_line_numbers *(NEW — page-level)*

**Feature ID:** F-025
**Name:** pleading_line_numbers
**Category:** D — Structural
**Purpose:** Detect California-style pleading paper, which is decisive evidence
that a page is a court filing rather than an attached exhibit.
**Input source:** All units on the page with F-014 `left_indent_ratio < 0.10`.
**Formula:** `1.0` when ≥ 15 such units are bare integers forming a
predominantly ascending run within 1–28, else `0.0`. Identical for every unit on
the page.
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** The detected line-number units must be marked so F-024
`noise_pattern` also excludes them from role competition.

## F-026 — word_count *(NEW)*

**Feature ID:** F-026 · **Name:** word_count · **Category:** D
**Purpose:** Constrain plausible identity length. A title is not one word and not
a paragraph.
**Input source:** `units[].text`
**Formula:** `len(text.split())`
**Output type:** Integer · **Expected range:** 1 – unbounded (typical 1–40)
**Audit rule:** Raw count, never clipped in X. Any length preference belongs to
Stage 4 scoring, where it is visible and tunable, not baked into the feature.

## F-027 — digit_ratio *(NEW)*

**Feature ID:** F-027 · **Name:** digit_ratio · **Category:** D
**Purpose:** Separate prose from tabular data, stamps, dates, and control numbers.
**Input source:** `units[].text`
**Formula:** `count(digits) / count(non-space characters)`; `0.0` if the unit has
no non-space characters.
**Output type:** Float · **Expected range:** 0.0 – 1.0, rounded to 4 dp
**Audit rule:** Denominator excludes whitespace so that column spacing in tables
does not deflate the value.

## F-028 — unit_line_count *(NEW)*

**Feature ID:** F-028 · **Name:** unit_line_count · **Category:** D
**Purpose:** Record how many assembled lines a unit spans; multi-line units are
wrapped titles, and single-line units in a body block are usually prose.
**Input source:** Stage 1.5 assembly.
**Formula:** number of lines merged into the unit
**Output type:** Integer · **Expected range:** 1 – unbounded (typical 1–4)
**Audit rule:** Must equal the number of distinct baseline groups in
`source_span_ids`. A mismatch means assembly and provenance have diverged.

---

# Category E — Provenance and Context *(NEW CATEGORY)*

These features describe **where a unit came from**. They exist so that scoring
never compares two measurements that were made on different scales.

## F-029 — source_modality *(NEW)*

**Feature ID:** F-029
**Name:** source_modality
**Category:** E — Provenance
**Purpose:** Declare whether a unit's geometry and typography came from the PDF's
own text objects or from OCR.
**Input source:** Stage 1 `font_name == "OCR"`; Page Census `pages[].modality`.
**Formula:** `native` if the unit came from embedded text, `ocr` otherwise.
**Output type:** Categorical — `native` | `ocr`
**Expected range:** exactly those two values
**Audit rule:** **Normative consequence.** Where `source_modality = ocr`:
F-005 `bold_flag` is `null`; F-001 `font_rank` is computed against an OCR-only
body baseline and may never be compared against native units in the same
competition; F-007 `font_family_class` is `other`. Stage 1 currently sets OCR
`font_size` to the line-box height in pixels-converted-points, which is **not**
a font size — treating the two as one scale is a silent scoring error.

## F-030 — segment_index *(NEW)*

**Feature ID:** F-030 · **Name:** segment_index · **Category:** E
**Purpose:** Identify which sub-document within the file a unit belongs to.
**Input source:** Stage 0.5 segment map.
**Formula:** the segment's 1-based index
**Output type:** Integer · **Expected range:** 1 – segment count
**Audit rule:** Must match a segment present in the map for this `document_id`.
Segment maps tile the file exactly, so every unit has exactly one.

## F-031 — segment_page_span *(NEW)*

**Feature ID:** F-031 · **Name:** segment_page_span · **Category:** E
**Purpose:** Record how large the segment is; a 200-page segment and a 2-page
segment warrant different confidence in a single label.
**Input source:** Stage 0.5 segment map.
**Formula:** `end_page - start_page + 1`
**Output type:** Integer · **Expected range:** 1 – total pages
**Audit rule:** Sum of all segment spans equals the file's page count.

## F-032 — opened_by_slip_sheet *(NEW)*

**Feature ID:** F-032 · **Name:** opened_by_slip_sheet · **Category:** E
**Purpose:** Note that this segment was introduced by a divider page, which
usually carries the exhibit designation.
**Input source:** Page Census `is_slip_sheet` on the segment's first page.
**Formula:** `1.0` if the segment's first page is a slip sheet, else `0.0`
**Output type:** Float (boolean-valued) · **Expected range:** {0.0, 1.0}
**Audit rule:** Must be `0.0` where the first page is a spacer
("intentionally left blank") or a scanned page — neither announces anything. A
scanned page yields no text and therefore cannot be assessed for shortness at all.

---

# Category F — Corpus *(NEW CATEGORY)*

## F-033 — corpus_document_frequency *(NEW)*

**Feature ID:** F-033
**Name:** corpus_document_frequency
**Category:** F — Corpus
**Purpose:** Identify firm-wide boilerplate — letterhead, form language, standard
footers — which is never identity however prominent it looks.
**Input source:** normalised unit text; the **frozen boilerplate lexicon**
(`BL-<version>`), a versioned, content-hashed artefact built once from a corpus
snapshot and shipped as an input.
**Formula:** `document_frequency = documents containing this normalised text /
documents in the lexicon snapshot`
**Output type:** Float · **Expected range:** 0.0 – 1.0
**Audit rule:** **The lexicon must be frozen and versioned, never computed live.**
A live corpus statistic would make the same PDF produce different labels on
different days, breaking reproducibility. Every label records the lexicon
version and hash it was scored against. A unit with `document_frequency > 0.20`
is ineligible to win TITLE.

---

# Determinism requirements (normative)

1. **Versioning.** Every row carries `feature_set_version` and
   `pattern_library_version`. Every label carries those plus `census_version`,
   `segmenter_version`, and the boilerplate lexicon version and hash. Changing
   any weight, threshold, regex, or formula requires a version bump. Without
   this, a re-run silently invalidates the ledger.

2. **Total ordering.** Wherever units are ranked, ties break in this exact order,
   which is total and cannot itself tie:
   ```
   score DESC, segment_index ASC, page ASC, y ASC, x ASC, unit_id ASC
   ```

3. **Rounding.** Floats in X are rounded to 4 dp; geometry to 2 dp. No comparison
   may depend on precision beyond that.

4. **No live corpus state.** Every corpus-derived input is a frozen, hashed
   artefact. See F-033.

5. **Null is not zero.** A feature that is undefined for a unit (F-005 on OCR) is
   emitted as `null` and excluded from scoring. Emitting `0.0` asserts a
   measurement that was never made.

---

# Consequences for Stage 3 — Role Assignment

This catalogue is not code and does not define roles, but two constraints follow
directly from it and must hold:

1. **Roles are optional slots, filled by two modes.** Legal mode
   (CASE, COURT, TITLE, EXHIBIT, DATE) and documentary mode
   (SOURCE, TITLE, IDENTIFIER, DATE) share this matrix. No role is mandatory.
   Missing roles fall through a composition ladder rather than forcing a guess.

2. **Assignment is joint and constrained, not per-role argmax.** Score every unit
   for every role, then solve once under mutual exclusion — at most one unit per
   role, at most one role per unit — with the spatial priors already implied by
   F-003 and F-011. Independent argmax cannot express that
   `NASA/TM-2005-213991` is a strong IDENTIFIER *and* a weak TITLE simultaneously,
   so it will keep spending the title slot on control numbers.

---

# Confidence and escalation

The engine's own separation predicts its own correctness. On an untuned
prototype over the benchmark corpus, the margin between the top-scoring and
runner-up candidate tracked accuracy closely:

| Top-to-runner-up margin | Outcome |
|---|---|
| ≥ 1.0 | consistently correct |
| ≤ 0.1 | ambiguous or wrong |

Stage 4 must therefore emit **three states**, not a label and a number:

- **auto** — margin above the confident threshold
- **review** — labelled, but queued for a human glance
- **escalate** — no candidate cleared the floor; no label is invented

Thresholds are calibrated against the labelled ground-truth set, recorded in the
ledger, and versioned like everything else. Per the project's own principle, a
wrong label is worse than no label — so the escalation rate is a tuning dial the
firm owns, not a defect to minimise silently.

---

# Feature specification template

Every future feature must complete every section. No exceptions.

```
Feature ID:
Name:
Category:
Purpose:
Input source:
Formula:
Output type:
Expected range:
Audit rule:
```

---

# Out of scope

This document does not define: roles, evidence, scores, labels, regex
implementations, or composition templates. Those belong to later specifications.
