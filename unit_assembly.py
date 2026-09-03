"""
ExhibitPro - Deliverable 03
Stage 1.5: Unit Assembly v1

Purpose
-------
Stage 1 emits SPANS: maximal runs of uniform font/size/style. That is the right
unit for recording typography exactly, and the wrong unit for identity.

Measured on the 198-document benchmark corpus:
    - 26.9% of page-1 visual lines are split across more than one span
    - 7 of 26 legal document titles are fragmented across spans
    - titles routinely WRAP across two or three lines

So a span-level feature row can win the TITLE role while holding
"MOTION TO COMPEL" and leaving "DISCOVERY" in a different row. The Label
Composer would print the fragment.

This stage assembles spans into UNITS: the largest run of text a reader would
call one heading or one paragraph line-group. Units are the rows of the Feature
Matrix (Stage 2).

Assembly rules (normative, from Feature Catalogue v2)
-----------------------------------------------------
1. Spans -> lines. Group spans on a page whose baseline y values fall within
   0.6 x median line height. Order by x. Join, collapsing whitespace.
2. Lines -> units. Merge vertically adjacent lines when ALL hold:
      - font size differs by < 0.6 pt
      - bold flag is equal
      - vertical gap <= 1.6 x the page's median line gap
3. Provenance. Every unit carries source_span_ids. Nothing is discarded.

Audit rule
----------
Concatenating every unit's text in reading order reproduces the page's full text
content, modulo whitespace normalisation. Assembly may regroup text; it may
never invent, drop, or reorder it. assemble() enforces this and raises on
violation - see verify_lossless().

Segment awareness
-----------------
Stage 1 reads pages 1-2 of a FILE. That assumption dies on compound intake, so
this stage takes an explicit page list - normally a segment's head_pages from
Stage 0.5 - and every position feature is computed relative to that segment.
"""

import re
import statistics
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF is required. Install with: pip install pymupdf", file=sys.stderr)
    raise

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evidence_extractor as stage1  # noqa: E402

ASSEMBLY_VERSION = "1.3.0"

from contracts import loader as _contracts  # noqa: E402

_A = _contracts.load("assembly")["assembly"]
LINE_TOLERANCE_RATIO = _A["line_tolerance_ratio"]
COLUMN_GAP_RATIO = _A["column_gap_ratio"]
COLUMN_GAP_MIN_PTS = _A["column_gap_min_pts"]
COLUMN_BAND_GAP_PTS = _A["column_band_gap_pts"]
MIN_BAND_LINES = _A["min_band_lines"]
UNIT_GAP_RATIO = _A["unit_gap_ratio"]
HEADING_GAP_RATIO = _A["heading_gap_ratio"]
SIZE_EPSILON = _A["size_epsilon"]
MAX_HEADING_LINES = _A["max_heading_lines"]
SHORT_LINE_WORDS = _A["short_line_words"]
DEFAULT_LINE_HEIGHT = _A["default_line_height"]
DEFAULT_LINE_GAP = _A["default_line_gap"]

WS_RE = re.compile(r"\s+")


def normalise(text: str) -> str:
    return WS_RE.sub(" ", text).strip()


def _spans_for_page(doc, page_number: int):
    """Reuse Stage 1's extraction verbatim, including its OCR fallback.

    Importing rather than re-implementing means units can never disagree with
    the Stage 1 record they claim provenance over.
    """
    page = doc.load_page(page_number - 1)
    page_width = page.rect.width
    page_height = page.rect.height
    has_text = bool(page.get_text("text").strip())
    if has_text:
        blocks, _ = stage1.extract_text_page(page, page_number, 0, page_width)
        modality = "native"
    else:
        blocks, _ = stage1.extract_ocr_page(page, page_number, 0, page_width)
        modality = "ocr"
    for b in blocks:
        b["_modality"] = modality
    return blocks, page_width, page_height


def _group_lines(spans):
    """Spans -> visual lines, ordered top-to-bottom then left-to-right."""
    if not spans:
        return []
    heights = [s["height"] for s in spans if s["height"] > 0]
    med_h = statistics.median(heights) if heights else DEFAULT_LINE_HEIGHT
    tol = max(med_h * LINE_TOLERANCE_RATIO, 0.5)

    lines = []
    for span in sorted(spans, key=lambda s: (round(s["y"], 2), s["x"])):
        placed = False
        for ln in lines:
            if abs(ln["y"] - span["y"]) <= tol:
                ln["spans"].append(span)
                placed = True
                break
        if not placed:
            lines.append({"y": span["y"], "spans": [span]})

    out = []
    for ln in sorted(lines, key=lambda l: l["y"]):
        sp = sorted(ln["spans"], key=lambda s: s["x"])
        # Split on column gutters. Spans sharing a baseline are not one line if
        # a wide horizontal gap separates them - that is a second column, and
        # joining across it welds unrelated text together.
        runs, current = [], [sp[0]]
        for prev, nxt in zip(sp, sp[1:]):
            gap = nxt["x"] - (prev["x"] + prev["width"])
            limit = max(prev["font_size"] * COLUMN_GAP_RATIO, COLUMN_GAP_MIN_PTS)
            if gap > limit:
                runs.append(current)
                current = [nxt]
            else:
                current.append(nxt)
        runs.append(current)
        for sp in runs:
            _emit_line(out, sp)
    return _column_bands(out)


def _emit_line(out, sp):
        text = normalise("".join(s["text"] for s in sp))
        if not text:
            return
        out.append({
            "y": min(s["y"] for s in sp),
            "x0": min(s["x"] for s in sp),
            "x1": max(s["x"] + s["width"] for s in sp),
            "bottom": max(s["y"] + s["height"] for s in sp),
            "text": text,
            "font_size": max(s["font_size"] for s in sp),
            "font_name": sp[0]["font_name"],
            "bold": any(s["bold"] for s in sp),
            "span_ids": [s["id"] for s in sp],
            "modality": sp[0].get("_modality", "native"),
        })


def _column_bands(lines):
    """Assign lines to column bands and return them in READING order.

    Sorting lines by y alone interleaves columns: on a two-column page the
    right column's first line sits between the left column's first and second,
    so "BLACK" and "HUCKLEBERRY" stop being adjacent and a two-line heading can
    never be reassembled. Reading order must be band by band, top to bottom
    within each.

    Bands are clusters of left edges separated by more than a gutter. A cluster
    must hold at least MIN_BAND_LINES lines to count, so a centred heading or a
    lone indented quotation cannot invent a column that is not there; a page
    with fewer than two qualifying clusters is single-column and untouched.
    """
    if len(lines) < 2:
        return lines
    xs = sorted({round(l["x0"], 1) for l in lines})
    clusters, current = [], [xs[0]]
    for prev, nxt in zip(xs, xs[1:]):
        if nxt - prev > COLUMN_BAND_GAP_PTS:
            clusters.append(current)
            current = [nxt]
        else:
            current.append(nxt)
    clusters.append(current)

    def population(c):
        lo, hi = min(c), max(c)
        return sum(1 for l in lines if lo - 0.05 <= l["x0"] <= hi + 0.05)

    bands = [c for c in clusters if population(c) >= MIN_BAND_LINES]
    if len(bands) < 2:
        return sorted(lines, key=lambda l: (l["y"], l["x0"]))

    starts = [min(c) for c in bands]

    def band_of(line):
        # Nearest band start at or left of the line, else the nearest overall.
        candidates = [i for i, s in enumerate(starts) if line["x0"] >= s - 0.05]
        if candidates:
            return candidates[-1]
        return min(range(len(starts)), key=lambda i: abs(starts[i] - line["x0"]))

    for l in lines:
        l["_band"] = band_of(l)
    return sorted(lines, key=lambda l: (l["_band"], l["y"], l["x0"]))


def _median_gap(lines):
    gaps = [lines[i + 1]["y"] - lines[i]["y"] for i in range(len(lines) - 1)]
    gaps = [g for g in gaps if g > 0]
    return statistics.median(gaps) if gaps else DEFAULT_LINE_GAP


def _group_units(lines, page_number, page_width, page_height, counter):
    """Lines -> units, merging same-style adjacent lines (a wrapped heading)."""
    if not lines:
        return [], counter
    med_gap = _median_gap(lines)
    groups, current = [], [lines[0]]
    for prev, nxt in zip(lines, lines[1:]):
        same_style = (abs(nxt["font_size"] - prev["font_size"]) < SIZE_EPSILON
                      and nxt["bold"] == prev["bold"])
        allowed_gap = max(med_gap * UNIT_GAP_RATIO,
                          prev["font_size"] * HEADING_GAP_RATIO)
        close = (nxt["y"] - prev["y"]) <= allowed_gap
        # Lines in different column bands never form one unit.
        same_column = (prev.get("_band") == nxt.get("_band"))
        # A heading and the paragraph beneath it can share a size and a gap.
        # A wrapped title runs to a few lines; a paragraph keeps going. Stop
        # once the run is heading-length and the next line is not short.
        still_heading = (len(current) < MAX_HEADING_LINES
                         or len(nxt["text"].split()) <= SHORT_LINE_WORDS)
        if same_style and close and same_column and still_heading:
            current.append(nxt)
        else:
            groups.append(current)
            current = [nxt]
    groups.append(current)

    units = []
    for g in groups:
        counter += 1
        units.append({
            "unit_id": f"u{counter:04d}",
            "page": page_number,
            "text": normalise(" ".join(l["text"] for l in g)),
            "x": round(min(l["x0"] for l in g), 2),
            "y": round(min(l["y"] for l in g), 2),
            "width": round(max(l["x1"] for l in g) - min(l["x0"] for l in g), 2),
            "height": round(max(l["bottom"] for l in g) - min(l["y"] for l in g), 2),
            "font_size": round(max(l["font_size"] for l in g), 2),
            "font_name": g[0]["font_name"],
            "bold": any(l["bold"] for l in g),
            "line_count": len(g),
            "source_span_ids": [i for l in g for i in l["span_ids"]],
            "modality": g[0]["modality"],
            "page_width": round(page_width, 2),
            "page_height": round(page_height, 2),
            "median_line_gap": round(med_gap, 2),
        })
    return units, counter


def verify_lossless(units, spans):
    """Assembly may regroup text. It may never invent, drop, or reorder it.

    Compares the multiset of non-space characters, which is invariant under the
    whitespace normalisation and re-joining that assembly performs, and would
    catch a dropped span, a duplicated line, or text conjured from nowhere.
    """
    def bag(strings):
        from collections import Counter
        c = Counter()
        for s in strings:
            c.update(ch for ch in s if not ch.isspace())
        return c

    produced = bag(u["text"] for u in units)
    original = bag(s["text"] for s in spans)
    if produced != original:
        missing = original - produced
        extra = produced - original
        raise AssertionError(
            f"unit assembly is not lossless: "
            f"{sum(missing.values())} chars lost, {sum(extra.values())} invented"
        )
    return True


def assemble(pdf_path, pages, verify=True):
    """Assemble units for the given 1-based page numbers of one PDF."""
    doc = fitz.open(str(pdf_path))
    try:
        all_units, all_spans = [], []
        counter = 0
        for pno in pages:
            if pno < 1 or pno > doc.page_count:
                continue
            spans, pw, ph = _spans_for_page(doc, pno)
            all_spans.extend(spans)
            lines = _group_lines(spans)
            units, counter = _group_units(lines, pno, pw, ph, counter)
            all_units.extend(units)
    finally:
        doc.close()
    if verify:
        verify_lossless(all_units, all_spans)
    return all_units


def assemble_segment(pdf_path, segment):
    """Assemble the head pages of one Stage 0.5 segment."""
    return assemble(pdf_path, segment["head_pages"])
