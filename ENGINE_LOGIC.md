# Document Identity Engine — Engine Logic

**Purpose of this document.** A precise description of how our engine decides
what a document is called, written for another engineer who has solved the same
problem a different way and wants to merge. It covers logic only — no UI, no
deployment.

It is organised around the three things a merge actually needs:

1. **Invariants** — the properties any merged system must preserve, and why.
2. **Stages and their data contracts** — the seams where two systems can join.
3. **What is proven and what is not** — measured, so nothing already known to be
   inert gets carried across.

Everything here is the state at: census `1.5.0`, segmenter `1.6.0`, assembly
`1.3.0`, feature set `2.0.0`, composer `1.0.0`.

---

## 1. The problem, as we've framed it

> Given a PDF — usually compound — produce a short string a paralegal will
> recognise at a glance, and be able to explain every character of it.

Two consequences that shape everything downstream:

**A wrong label is worse than no label.** The engine is allowed to decline. Any
merged design needs an abstain path, not just a best guess.

**The label is not the document's "topic".** It is the document's own
self-declared heading. This distinction matters more than it sounds: the most
topically representative string on a page is usually a whole sentence, and a
sentence is not a label. We score *relevance* and *presentability* separately
for this reason (§5.6).

---

## 2. Invariants

These are non-negotiable in our implementation. If your side differs on any of
them, that is the first thing to resolve, because they constrain the
architecture rather than the tuning.

| # | Invariant | Enforced by |
|---|---|---|
| I1 | **No AI, no vision model, no LLM, no embeddings.** Native PDF structure only; OCR only for pages with no extractable text. | design |
| I2 | **The engine never invents words.** Every word in an emitted label already exists on the inspected pages. It may extract, rank, join, normalise, compose — never generate. | automated test |
| I3 | **Same input → same output, byte for byte.** | directory-hash test over the full corpus |
| I4 | **Every output is traceable to source spans.** A printed label leads back to the exact extracted spans behind it. | automated test |
| I5 | **Null is not zero.** A feature that could not be measured is `null` and is excluded from scoring, never `0.0`. | automated test |
| I6 | **Nothing is silently dropped.** A rejected candidate stays in the audit record with the rule that rejected it. | automated test |
| I7 | **Every tunable lives in a versioned contract**, not in code. A contract's version must move when its content moves. | content-hash gate in CI |

### Notes on the ones that are easy to get wrong

**I2** is the one that most distinguishes this from an LLM approach, and it has a
sharp edge: our phrase graph *joins* two units with a declared separator
(`" - "`). We treat that as composition, not generation, because no word is
created and the audit records the exact units joined. If your side generates or
paraphrases anywhere, that is a genuine architectural difference to settle
before merging, not a detail.

**I3** has a subtle failure mode worth flagging: any corpus-derived statistic
(IDF, boilerplate frequency) makes today's output depend on yesterday's corpus.
We solve it by freezing such tables into versioned, hash-checked artifacts. If
your side uses corpus statistics live, that is a determinism break we'd need to
close.

**I5** matters because OCR cannot report font weight. Emitting `bold=false` for
an OCR unit asserts a measurement that was never taken, and 19% of the legal
documents in our corpus genuinely have no bold — so the two are indistinguishable
downstream unless one is `null`.

---

## 3. Pipeline

```
PDF
 │
 ├─ Stage 0    Page Census          cheap structural fact sheet, EVERY page
 │
 ├─ Stage 0.5  Segmentation         find sub-document seams → segment map
 │
 ├─ Stage 1    Span Extraction      spans with geometry + typography (head pages only)
 │
 ├─ Stage 1.5  Unit Assembly        spans → lines → units, provenance kept
 │
 ├─ Stage 2    Feature Matrix       one row per unit, 21 features
 │
 ├─ Stage 3/4  Role scoring         (TITLE implemented; see §6)
 │
 └─ Stage 5    Identity Composer    candidates → evidence fusion → top 3 labels
```

The load-bearing design decision is **Stage 0 before Stage 1**: measure every
page cheaply, decide where to look, *then* spend real extraction cost. See §4.1.

---

## 4. Stage by stage

### 4.1 Stage 0 — Page Census

**Why it exists.** "Read pages 1–2" assumes one document per file. Production
intake is compound — a motion, its exhibits, a medical record, a scanned
statement, all in one PDF — so page 1 may be a fax cover and the real identity
may start at page 47.

**What made it affordable.** Measured cost per page on our corpus:

| operation | ms/page |
|---|---|
| geometry only | 0.29 |
| `get_fonts()` (page resource table) | 0.74 |
| `get_text("text")` | 6.39 |
| **`get_text("dict")`** (span parsing) | **33.73** |

Span parsing is 5× everything else, so Stage 0 deliberately excludes it. Font
information comes from the page *resource table*, not from parsing spans — a
usable typographic fingerprint for 1/45th of the cost. Full census runs at
**~6.4 ms/page**; a 300-page compound PDF is censused in about two seconds.

**Per-page output:** size class + orientation (named, not raw floats),
rotation, modality (`native`/`scanned`), char/word/line counts, image count,
font families, Bates candidate, page label, `is_blank`, `is_spacer`,
`is_slip_sheet`, header/footer signature, `ends_mid_sentence`,
`starts_lowercase`, content-token fingerprint, text hash.

**Two normalisations worth copying:**

- **Named size classes, not raw dimensions.** Scanned pages carry scanner
  jitter: one 194-page document produced 67 distinct page sizes that were all
  just Letter. Classification into named sizes with 5% tolerance makes the
  comparison robust *and* makes the audit readable ("Letter → Legal").
- **Digit-masked edge signatures.** The first and last line with digits masked
  (`"JSC-63743 Page 12"` → `"jsc-##### page ##"`), so a running header is
  recognised as the same *template* across pages.

### 4.2 Stage 0.5 — Segmentation

Finds the seams between sub-documents. Scores each page-to-page boundary from
**19 signals in two directions**.

**Change signals** (this is a new document):

| weight | signal |
|---|---|
| +1.60 | `slip_sheet_ahead` — next page is a near-empty divider |
| +1.60 | `bates_prefix_change` |
| +1.00 | `modality_flip`, `page_size_change`, `page_label_restart`, `font_family_change` |
| +0.80 | `rotation_change` |
| +0.70 | `bates_number_gap` |
| +0.60 | `font_size_change` (Tier B only) |
| +0.40 | `blank_page` |

**Continuity signals** (these pages belong together):

| weight | signal |
|---|---|
| −1.60 | `slip_sheet_behind` — a divider belongs to the document it opens |
| −1.20 | `running_header_match`, `running_header_alternates`, `page_label_continues`, `bates_consecutive` |
| −1.00 | `sentence_continuation` — prose runs across the break |
| −0.80 | `running_footer_match` |

**Review-only signals** (+0.70 `vocabulary_change`, +0.70
`running_header_change`) — see below.

#### The three rules that make this work

**Rule 1: a cut needs `change − continuity ≥ threshold` AND at least one
individually strong (≥1.00) change signal.** Soft evidence must not accumulate
into a boundary: a topic shift plus a section header plus a rotated figure is
three hints inside one report, not a document boundary.

**Rule 2: continuity decides whether we CUT; it never decides whether a seam is
SEEN.** Candidacy for human review is tested on *change evidence alone*. This
exists because in a continuous Bates production, `bates_consecutive` fires at
every single page pair *including the true boundaries* — netting it against
change evidence pushed 23 of 28 real seams below the review floor as well as the
cut threshold. They weren't just missed, they were missed **silently**, which is
the failure the whole design exists to prevent.

**Rule 3: review-only signals raise a seam for review but never contribute to a
cut.** `vocabulary_change` and `running_header_change` describe *content drift*,
and content drifts inside single documents constantly. Admitted into the cut
decision, they tipped a lone strong signal over the threshold and tripled false
cuts on single-document controls.

#### Granularity is a policy, not a constant

Whether "Section I / Section II / Appendices" of one report is four binder tabs
or one is a **product decision** that varies by firm and matter. It is a named,
selectable policy:

| policy | threshold | precision | assisted recall | false cuts / single doc |
|---|---|---|---|---|
| conservative | 1.80 | 0.932 | 0.990 | 0.00 |
| balanced *(default)* | 1.50 | 0.789 | 0.863 | 0.20 |
| aggressive | 1.00 | 0.738 | 0.843 | 0.50 |

Assisted recall stays 0.84–0.99 across the whole range, so **the dial trades
automation against review workload, not accuracy against inaccuracy.** That is
the useful thing to be able to tell a customer, and it is worth preserving in
any merge.

#### Adaptive cost

Span-level font analysis (33.7 ms/page) runs *only* on seams whose score lands
in an ambiguous band just under the threshold — where it could actually change
the outcome. Cost is bounded by the number of doubtful seams, not by document
length. On 4,580 pages it touched 138.

### 4.3 Stage 1.5 — Unit Assembly

Spans are the right unit for recording typography and the **wrong** unit for
identity. On our corpus, **26.9% of page-1 visual lines are split across more
than one span**, and titles routinely wrap across lines.

Rules:
1. **Spans → lines**: group by baseline within 0.6 × median line height, order
   by x.
2. **Split lines on column gutters**: a gap wider than 2.5 × local font size (min
   18pt) is a column, not word spacing.
3. **Establish column-band reading order**: cluster left edges into bands, read
   band by band.
4. **Lines → units**: merge when size differs <0.6pt, weight matches, same band,
   and the gap is within `max(1.6 × median line gap, 1.4 × font size)`.
5. **Provenance**: every unit carries its contributing span ids.

**Assembly is verified lossless on every call** — the multiset of non-space
characters in the units must equal that of the source spans. It may regroup
text; it may never invent, drop, or reorder it.

Three bugs here are worth naming because any implementation will hit them:

- **Columns share baselines.** Joining across a gutter produced
  `"HUCKLEBERRYfully dried them. In British Columb"` — a heading welded to
  unrelated body text.
- **Sorting lines by `y` interleaves columns**, so `BLACK` and `HUCKLEBERRY`
  were never adjacent and a two-line heading could not reassemble. Reading order
  must be band-aware.
- **Line spacing scales with type size.** A 20pt heading sat 23pt apart against
  a body median of 11pt and failed a purely median-relative merge test that a
  12pt paragraph would have passed.

### 4.4 Stage 2 — Feature Matrix

One row per unit, 21 features, grouped by category: layout (`font_rank`,
`center_score`, `top_ratio`, `block_area`, `whitespace_isolation`,
`left_indent_ratio`, `zone`), typography (`bold_flag`, `uppercase_ratio`,
`typographic_contrast`), lexical (`identifier_pattern`, `date_pattern`,
`noise_pattern`), structural (`page_in_segment`, `first_occurrence`,
`word_count`, `digit_ratio`, `unit_line_count`), provenance
(`source_modality`, `segment_index`, `segment_page_span`).

**One formula worth arguing about.** `font_rank` normalises against the
**char-weighted modal font size (the body baseline)**, never the page maximum.
Page-max normalisation lets one oversized element define the scale: on our legal
subset it returned 1.00 for `"30050"` and `"CCASE:"`. Body-baseline
normalisation asks the question that matters — *is this bigger than what this
document is set in?* — and degrades honestly on a flat monospace pleading, where
everything scores the same and the engine correctly reports that typography
carries no signal here.

**The row carries the text.** X is `features` **plus** `text` and
`source_span_ids`. Scoring reads only `features`; the payload exists because the
composer needs the string and the ledger needs provenance.

### 4.5 Stage 5 — Identity Composer

This is the layer most likely to be where the two systems merge, because it is
explicitly designed as a **plug-in point for independent evidence**.

#### Candidate generation

Three generators, all provenance-tagged:

| generator | what it yields | win rate (of 77) |
|---|---|---|
| `unit` | an assembled unit as it appears | 47 winners, 32% correct |
| `subphrase` | a phrase *within* a unit | 26 winners, 23% correct |
| `composed` | a chain of units joined by the phrase graph | 3 winners, 0% correct |

**Sub-phrases are taken only from heading-plausible units** (a typographic
heading level, or ≤16 words). Extracted from body paragraphs, they produce
well-shaped five-word fragments that score a perfect 1.00 on presentability —
`"including the Columbia Plateau Indians"` was winning documents. That is the
line between *extracting* identity and *inventing* it.

#### The phrase graph

Nodes are units; edges exist only from measurable evidence — vertical adjacency
within a gap budget, font size within 0.6pt, matching weight, left- or
centre-alignment, same page. A chain of ≤3 connected nodes yields a composed
candidate. Every composed label records the exact units and the measured reasons
they were joined.

#### Evidence fusion

Six sources, each scoring every candidate, then a weighted sum:

| source | weight | what it contributes |
|---|---|---|
| `typography` | 2.40 | hierarchy level by character mass |
| `presentability` | 1.60 | fitness for a binder tab |
| `position` | 1.20 | first page, upper region |
| `bm25` | 0.90 | corpus-rarity relevance |
| `textrank` | 0.70 | classical graph ranking |
| `rake` | 0.60 | multi-word phrase extraction |
| `graph_bonus` | 0.50 | composed from a coherent chain |

Penalties: `furniture` −1.20, `noise` −2.00, `not_first_page` −1.00.

**Normalisation rule — this one bit us.** Sources already on a meaningful
absolute 0–1 scale (`typography`, `presentability`, `position`, `graph_bonus`)
are **not** min-max rescaled across the candidate set. Rescaling maps the worst
candidate to 0 and the best to 1, so a document containing nothing but body text
reported its body text as *maximally prominent*. Only the unbounded IR scores
(BM25, RAKE, TextRank) are normalised.

Then: near-duplicate suppression by token overlap ≥0.70 (otherwise the top 3 is
one phrase three times), a **total ordering** that cannot tie
(`score, page, y, x, key`), and a margin-based confidence state.

---

## 5. The parts most worth comparing against your design

### 5.1 Typography hierarchy, not "largest text"

Font sizes are clustered into levels (`h1`/`h2`/`h3`/`body`/`outlier`) by
**character mass**, so a single oversized Bates stamp cannot become H1. Body is
the size carrying the most characters, whatever its absolute value — a document
set entirely in 14pt has no 14pt heading.

**The correction that matters:** mass alone is *backwards for titles*, because a
title is short by nature. `BLACK HUCKLEBERRY` at 20pt holds 0.2% of its page's
characters; a NASA report title at 24pt holds 2.6%. Both fell under a 3% mass
floor, were classified `outlier`, and were **penalised as noise** — while a
173-character author block became H1. The fix is a size qualifying on **either**
enough mass **or** enough absolute characters (14). A 9-character Bates stamp
still fails both.

### 5.2 Presentability — the "binder label" score

Deliberately separate from relevance. 0–1, with every rule that fired returned
by name.

Penalises: length outside the acceptable band, over-length in characters,
leading/trailing *prepositions* (but not articles — "A Design of…" is a normal
title opening), internal sentence punctuation, high stopword ratio, digit-heavy,
furniture words, unbalanced brackets, ALL-CAPS verbosity.
Rewards: ideal length, consistent title case, ALL CAPS when short (conventional
for legal headings), compactness.

**Word count and character count are not summed** — they measure the same thing,
and stacking them drove a legitimate 13-word document title to a score of 0.000.
The worse of the two applies.

### 5.3 BM25 with no query

BM25 ranks documents against a query; here there is neither. We invert it: the
**candidate phrase is the query**, the document is the corpus entry. The score
answers *"how well does this phrase represent this document, given corpus-wide
term rarity"*. Standard Okapi with k1=1.5, b=0.75, divided by `len^0.5` because
BM25 sums and would otherwise always prefer a longer phrase.

IDF is a frozen, versioned artifact (see I3). On our corpus it discounts
`states` (33% of documents), `national` and `department` (32%), `united` (30%) —
exactly the institutional boilerplate that otherwise outranks a document's own
heading.

### 5.4 RAKE, modified

Classic RAKE splits at every stopword, which shatters exactly the phrases a
binder label is made of: `Motion to Compel` → `Motion` + `Compel`. **30% of
phrase-like units on page 1 carry an interior stopword**, including `TREATY WITH
THE TRIBES OF MIDDLE OREGON`.

One modification: split on punctuation and *hard* stopwords only. A connector
set (`of to for and the a an in on at by with from v vs versus re per &`) is
permitted inside a candidate and trimmed from its edges. Scoring is unchanged
from the paper — `deg(w)/freq(w)`, summed, length-normalised.

### 5.5 TextRank

Classical algorithm only. Word co-occurrence graph, window 3, damping 0.85.
Determinism requires two things: a **fixed iteration count** (40) rather than a
convergence tolerance — a tolerance test can stop an iteration early or late
depending on floating-point accumulation order — and **sorted node iteration**,
so accumulation order is identical on every machine.

### 5.6 Confidence and abstention

Three states, never two: `auto` / `review` / `escalate`. Below the review floor
the engine emits **no label**.

`auto` is currently **disabled deliberately**. Measured margin-versus-correctness
is flat at 0.26–0.40 above a margin of 0.10, so no band earns unattended
acceptance. Lowering the bar to manufacture an automation rate would be
inventing confidence the evidence does not support.

---

## 6. What is proven, and what is not

This is the section to read before merging anything, so nothing already known to
be inert gets carried across.

### Segmentation (against synthetic fixtures with boundaries known by construction)

| | naive fixtures | realistic fixtures |
|---|---|---|
| precision | 0.952 | 0.789 |
| recall | 0.444 | 0.549 |
| **assisted recall** | **0.978** | **0.863** |

By seam difficulty (realistic set):

| difficulty | cut | reaching a human |
|---|---|---|
| easy (slip sheets) | 0.684 | 0.684 |
| medium (producer change) | 0.694 | 0.972 |
| **hard (continuous Bates run)** | **0.179** | **0.964** |

**Known open problem:** in a same-producer continuous Bates production, real
boundaries carry *no structural change at all* — fonts, page size, modality and
the stamp run are all continuous. They cannot be cut on evidence the engine can
see. What is enforced instead is that they are never lost silently.

### Label composition (77 harvested titles, match at token-F1 ≥ 0.70)

| metric | value |
|---|---|
| top1 accuracy | 0.286 |
| top3 accuracy | 0.338 |
| binder-shaped (2–12 words) | 0.935 |
| mean presentability | 0.863 |

### Ablation — which evidence sources actually earn their weight

| disabled source | Δ top1 | verdict |
|---|---|---|
| `typography` | −0.065 | **load-bearing** |
| `bm25` | −0.039 | **load-bearing** |
| `rake` | −0.026 | **load-bearing** |
| `presentability` | −0.013 | neutral on accuracy, decisive for shape |
| `textrank` | −0.013 | no measurable effect |
| `position` | +0.013 | no measurable effect |
| `graph_bonus` | +0.000 | no measurable effect |

**Three of six sources are load-bearing on this corpus. Three are not.** They
are kept at low weight rather than deleted, with the reason recorded: the phrase
graph targets headings split across lines, and our benchmark is 198 *unrelated
single* documents, so it has almost nothing to work on here. The same caveat
applies to IDF — built over one firm's intake, with the same letterhead on every
file, it would be substantially stronger.

If your side has an equivalent signal, **run it through the ablation before
weighting it.** Fusing plausible-sounding signals is exactly the setup where a
component gets a weight it has not earned.

### Two honest caveats about the numbers above

**The ground truth is noisy.** Labels are harvested from PDF `/Title` metadata
(77 of 198 documents). Some of it is junk that survives filtering — one
document's "truth" is `New AFO Poster-2-06-07`, a filename, while the engine
correctly returns `REWARD` from what is visibly a wanted poster. Some is a
mismatch in *kind*: `/Title` is often the case caption or parent publication
where the engine correctly returns the document's own heading.

**Accuracy and presentability are different objectives.** Ground-truth titles
run to a median of 7 words but **39% exceed 8 words**, up to 26. A presentability
band of 3–8 words measured as actively *harmful* (+0.052 top1 when disabled)
because it fought that tail. Widened to 3–12 against the observed distribution,
top1 rose 0.221 → 0.286 while 93.5% of labels stayed binder-shaped. Optimising
accuracy alone would have made the labels worse for a paralegal.

---

## 7. Data contracts — the actual seams

These are the interfaces to align on. Any stage can be swapped if it honours the
shape.

**Segment map** (Stage 0.5 → Stage 1)
```json
{ "document_id": "sha256[:16]", "total_pages": 88, "policy": "balanced",
  "segments": [ { "index": 1, "start_page": 1, "end_page": 14,
                  "head_pages": [1, 2], "opened_by": "boundary",
                  "boundary_score": 1.8,
                  "signals": [ {"name": "...", "weight": 1.0, "detail": {...}} ] } ],
  "candidate_boundaries": [ {"before_page": 42, "score": 1.0,
                             "change_score": 1.7, "signals": [...]} ] }
```

**Unit** (Stage 1.5 → Stage 2)
```json
{ "unit_id": "u0007", "page": 1, "text": "...",
  "x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0,
  "font_size": 20.0, "font_name": "...", "bold": false,
  "line_count": 2, "modality": "native|ocr",
  "page_width": 612.0, "page_height": 792.0, "median_line_gap": 11.4,
  "source_span_ids": ["b0012", "b0013"] }
```

**Feature Matrix row** (Stage 2 → scoring)
```json
{ "unit_id": "u0007", "text": "...", "source_span_ids": [...],
  "features": { "...21 keys..." },
  "imputed": ["whitespace_isolation"],
  "feature_set_version": "2.0.0", "pattern_library_version": "PL-2.0.0" }
```

**Composer output** (the deliverable)
```json
{ "labels": [ { "rank": 1, "text": "...", "score": 5.82,
                "hierarchy_level": "h1", "presentability": 1.0,
                "sources": ["unit"], "unit_ids": [...], "source_span_ids": [...],
                "composition": {"parts": [...], "reasons": [...]} | null,
                "contributions": [ {"source": "typography", "raw": 1.0,
                                    "normalised": 1.0, "weight": 2.4,
                                    "contribution": 2.4} ],
                "presentability_reasons": [ {"rule": "...", "delta": 0.3} ] } ],
  "confidence": "auto|review|escalate", "margin": 2.71, "candidates": 37,
  "typography": {...}, "idf": {"version": "..."}, "graph": {...},
  "composer_version": "1.0.0", "contract_versions": {...} }
```

---

## 8. Where a second approach plugs in

In rough order of how cleanly it merges:

1. **As a new evidence source in the composer.** Cleanest by far. Implement
   `score_phrase(text, model) -> float`, declare a weight in
   `contracts/identity.yaml`, run the ablation. Your signal competes on measured
   contribution and nothing else needs to change.

2. **As a new candidate generator.** If your approach finds label strings ours
   misses, emit them as candidates with provenance and let fusion arbitrate.
   Note the measurement in §4.5: the generator matters as much as the scorer.

3. **As additional segmentation signals.** New change or continuity signals slot
   into the existing weighted model. The one rule to respect is Rule 2 — a
   signal may argue against cutting, never against *seeing*.

4. **As a replacement stage.** Possible for any stage that honours the contract
   in §7. Stage 1.5 assembly and Stage 2 features are the most self-contained.

### Questions to settle first

- **Does your approach generate or paraphrase text anywhere?** If so, I2 is the
  conflict to resolve before anything else.
- **Do you use live corpus statistics?** If so, I3 needs a freezing strategy.
- **Do you have an abstain path**, or does your engine always answer?
- **What does your evaluation measure?** If it optimises accuracy against
  metadata titles alone, expect it to disagree with presentability, for the
  reason in §6.
- **How do you break ties?** Ours is a mandated total order. Unspecified
  tie-breaking is a determinism hole that surfaces as flaky output much later.

### The most valuable thing to merge first

Not a signal — the **harness**. Our synthetic compound fixtures generate
boundaries known by construction, so segmentation recall is measurable with zero
hand-labelling, and the ablation makes every weight defend itself. When we built
it, it immediately showed the shipped engine was missing more than half of all
real boundaries while its precision-only numbers looked excellent.

Whatever architecture we converge on, both sides should be measured by the same
harness before any weight is agreed.
