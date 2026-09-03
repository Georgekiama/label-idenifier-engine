"""
ExhibitPro - Deliverable 04
Stage 2: Feature Engineering v1

Transforms assembled units (Stage 1.5) into the Feature Matrix (X): one row per
unit, one column per approved engineered feature.

Feature definitions, formulas, expected ranges and audit rules are specified in
FEATURE_CATALOGUE_V2.md. Every constant they depend on lives in
contracts/features.yaml, versioned and hash-guarded. Nothing tunable is defined
in this file.

Two rules from the catalogue are load-bearing here:

  X carries the text. The catalogue v1 claimed the matrix was "the only input to
  Role Assignment", but the Composer needs the string. Each row carries `text`
  and `source_span_ids` alongside `features`. Scoring reads only `features`;
  the payload exists so a printed label can be walked back to the exact spans
  that produced it.

  Null is not zero. A feature that is undefined for a unit is emitted as None
  and excluded from scoring. Emitting 0.0 would assert a measurement that was
  never made - F-005 bold_flag on an OCR unit being the case that matters, since
  Tesseract cannot report weight and 19% of the legal subset has no bold at all.
"""

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import loader as _contracts  # noqa: E402

FEATURE_SET_VERSION = "2.0.0"

_FS = _contracts.load("features")
_F = _FS["features"]

FONT_RANK_CAP = _F["font_rank_cap"]
FONT_SIZE_BUCKET = _F["font_size_bucket"]
CENTER_TOLERANCE_RATIO = _F["center_tolerance_ratio"]
ISOLATION_DIVISOR = _F["isolation_divisor"]
ISOLATION_EDGE_DEFAULT = _F["isolation_edge_default"]
ISOLATION_UNDETERMINED = _F["isolation_undetermined"]
HEADER_ZONE_MAX = _F["header_zone_max"]
FOOTER_ZONE_MIN = _F["footer_zone_min"]
CONTRAST_MIN_SHARE = _F["contrast_min_share"]
CONTRAST_CAP = _F["contrast_cap"]
FLAT_TYPOGRAPHY_MAX = _F["flat_typography_max"]

# --- Pattern library (versioned with the contract) --------------------------

PATTERN_LIBRARY_VERSION = "PL-2.0.0"

IDENTIFIER_RE = re.compile(
    r"\b[A-Z]{2,}[-/][A-Z0-9][A-Z0-9-]{2,}\b"
    r"|\b(?:No|Case|Docket|Report|Publication)\.?\s*[:#]?\s*[A-Z0-9][A-Z0-9.-]{2,}\b",
    re.I,
)
DATE_RE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b",
    re.I,
)
# F-024: text that can never be identity, whatever it scores on layout.
NOISE_RES = [
    re.compile(r"^\W*$"),                                   # punctuation only
    re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*of\s*\d{1,4})?$", re.I),
    re.compile(r"^https?://|^www\.", re.I),
    re.compile(r"^\S+@\S+\.\S+$"),
    re.compile(r"^[\d\s().+-]{7,}$"),                       # phone / numeric run
    re.compile(r"intentionally\s+(?:left\s+)?blank", re.I),
]
DIGIT_RE = re.compile(r"\d")
ALPHA_RE = re.compile(r"[^\W\d_]", re.UNICODE)
DIGIT_MASK_RE = re.compile(r"\d")
WS_RE = re.compile(r"\s+")


def _norm_key(text):
    """Normalised identity of a unit's text, for F-012 first_occurrence.

    Digits are masked so a running header cannot defeat the feature simply by
    incrementing its own page number.
    """
    return DIGIT_MASK_RE.sub("#", WS_RE.sub(" ", text).strip().lower())


def body_baseline(units):
    """Char-weighted modal font size across the segment - the BODY size.

    Weighting by characters rather than by unit means one large heading cannot
    outvote the body text. This is the denominator for F-001.
    """
    hist = collections.Counter()
    for u in units:
        size = round(u["font_size"] / FONT_SIZE_BUCKET) * FONT_SIZE_BUCKET
        hist[size] += len(u["text"])
    if not hist:
        return None
    # Ties break toward the smaller size: body text, not display text.
    return min(hist.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def typographic_contrast(units):
    """F-016. Does typography carry usable signal in this segment at all?

    31% of the legal subset has two or fewer distinct sizes and 42% is
    monospace. On those documents layout features are noise, and the engine
    should know that about itself rather than ranking noise confidently.
    """
    hist = collections.Counter()
    for u in units:
        size = round(u["font_size"] / FONT_SIZE_BUCKET) * FONT_SIZE_BUCKET
        hist[size] += len(u["text"])
    total = sum(hist.values())
    if not total:
        return 0.0
    significant = sum(1 for n in hist.values() if n / total >= CONTRAST_MIN_SHARE)
    return round(min(significant, CONTRAST_CAP) / CONTRAST_CAP, 4)


def _isolation(units_on_page, index, median_gap):
    if len(units_on_page) < 3 or not median_gap:
        return ISOLATION_UNDETERMINED, True
    u = units_on_page[index]
    above = ((u["y"] - units_on_page[index - 1]["y"]) / median_gap
             if index > 0 else ISOLATION_EDGE_DEFAULT)
    below = ((units_on_page[index + 1]["y"] - u["y"]) / median_gap
             if index < len(units_on_page) - 1 else ISOLATION_EDGE_DEFAULT)
    return round(min((above + below) / ISOLATION_DIVISOR, 1.0), 4), False


def build(units, segment_index=1, segment_start_page=1, segment_page_span=1):
    """Build the Feature Matrix for one segment's assembled units."""
    if not units:
        return {"rows": [], "segment": {}, "feature_set_version": FEATURE_SET_VERSION}

    baseline = body_baseline(units) or 1.0
    contrast = typographic_contrast(units)
    flat = contrast <= FLAT_TYPOGRAPHY_MAX

    by_page = collections.defaultdict(list)
    for u in units:
        by_page[u["page"]].append(u)
    for page_units in by_page.values():
        page_units.sort(key=lambda x: (x["y"], x["x"]))

    seen = set()
    rows = []
    for u in units:
        page_units = by_page[u["page"]]
        idx = page_units.index(u)
        isolation, imputed = _isolation(page_units, idx, u.get("median_line_gap"))

        text = u["text"]
        letters = ALPHA_RE.findall(text)
        digits = DIGIT_RE.findall(text)
        non_space = [c for c in text if not c.isspace()]
        pw = u["page_width"] or 612.0
        ph = u["page_height"] or 792.0

        top_ratio = round(min(max(u["y"] / ph, 0.0), 1.0), 4)
        centre = u["x"] + u["width"] / 2.0
        center_score = round(max(0.0, 1.0 - abs(centre - pw / 2.0)
                                 / (pw * CENTER_TOLERANCE_RATIO)), 4)
        zone = ("header" if top_ratio < HEADER_ZONE_MAX
                else "footer" if top_ratio > FOOTER_ZONE_MIN else "body")

        key = _norm_key(text)
        first_occurrence = 0.0 if key in seen else 1.0
        seen.add(key)

        is_ocr = u.get("modality") == "ocr"

        features = {
            # Category A - Layout
            "font_rank": round(min(u["font_size"] / baseline, FONT_RANK_CAP) / FONT_RANK_CAP, 4),
            "center_score": center_score,
            "top_ratio": top_ratio,
            "block_area": round(min((u["width"] * u["height"]) / (pw * ph), 1.0), 4),
            "whitespace_isolation": isolation,
            "left_indent_ratio": round(min(max(u["x"] / pw, 0.0), 1.0), 4),
            "zone": zone,
            # Category B - Typography
            # Null, not zero: Tesseract cannot report weight, so on an OCR unit
            # "not bold" was never measured.
            "bold_flag": None if is_ocr else (1.0 if u["bold"] else 0.0),
            "uppercase_ratio": (round(sum(1 for c in letters if c.isupper()) / len(letters), 4)
                                if letters else 0.0),
            "typographic_contrast": contrast,
            # Category C - Lexical
            "identifier_pattern": 1.0 if IDENTIFIER_RE.search(text) else 0.0,
            "date_pattern": 1.0 if DATE_RE.search(text) else 0.0,
            "noise_pattern": 1.0 if any(rx.search(text) for rx in NOISE_RES) else 0.0,
            # Category D - Structural
            "page_in_segment": u["page"] - segment_start_page + 1,
            "first_occurrence": first_occurrence,
            "word_count": len(text.split()),
            "digit_ratio": round(len(digits) / len(non_space), 4) if non_space else 0.0,
            "unit_line_count": u["line_count"],
            # Category E - Provenance
            "source_modality": "ocr" if is_ocr else "native",
            "segment_index": segment_index,
            "segment_page_span": segment_page_span,
        }

        rows.append({
            "unit_id": u["unit_id"],
            "text": text,
            "source_span_ids": u["source_span_ids"],
            "features": features,
            "imputed": ["whitespace_isolation"] if imputed else [],
            "feature_set_version": FEATURE_SET_VERSION,
            "pattern_library_version": PATTERN_LIBRARY_VERSION,
        })

    return {
        "rows": rows,
        "segment": {
            "segment_index": segment_index,
            "body_baseline": baseline,
            "typographic_contrast": contrast,
            "flat_typography": flat,
            "unit_count": len(rows),
        },
        "feature_set_version": FEATURE_SET_VERSION,
        "pattern_library_version": PATTERN_LIBRARY_VERSION,
        "contract_versions": _contracts.versions(),
    }
