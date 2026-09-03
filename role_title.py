"""
ExhibitPro - Deliverable 05
Stage 3/4 (vertical slice): the TITLE role

Scores every Feature Matrix row for the TITLE role and returns one winner, a
confidence state, and a full audit record.

Deliberately a linear model with published weights, loaded from
contracts/features.yaml. Every contribution can be read line by line in the
ledger, which is the whole point - a fitted model would score better on a
benchmark and would not be defensible to a court.

Three states, never two
-----------------------
    auto      margin >= auto_margin      accept
    review    margin >= review_margin    label, but queue for a human
    escalate  below that                 NO title is emitted

The margin between the top candidate and the runner-up predicts correctness far
better than the raw score. Measured on an untuned prototype over the benchmark
corpus: margin >= 1.0 was consistently correct, margin <= 0.1 was ambiguous or
wrong. Per the project charter, a wrong label is worse than no label, so below
the review floor the engine declines rather than invents.

Determinism
-----------
Ranking uses a total order that cannot itself tie:
    score DESC, page ASC, y ASC, x ASC, unit_id ASC
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import loader as _contracts  # noqa: E402

ROLE_VERSION = "1.0.0"

_FS = _contracts.load("features")
_T = _FS["title_role"]
WEIGHTS = _T["weights"]
PENALTIES = _T["penalties"]
ELIGIBILITY = _T["eligibility"]
FLAT_DAMPING = _T["flat_typography_damping"]
CONFIDENCE = _FS["confidence"]

# Features damped when the segment has no usable typographic contrast.
LAYOUT_FEATURES = {"font_rank", "center_score", "bold_flag", "uppercase_ratio"}

ALL_CAPS_LONG_WORDS = 8
DIGIT_HEAVY_RATIO = 0.30


def eligibility_failures(f):
    """Why this unit cannot win TITLE. Empty list means eligible.

    Ineligible units stay in the matrix and the reason is recorded. Nothing is
    silently dropped.
    """
    out = []
    wc = f["word_count"]
    if wc < ELIGIBILITY["min_words"]:
        out.append("too_few_words")
    if wc > ELIGIBILITY["max_words"]:
        out.append("too_many_words")
    if ELIGIBILITY["exclude_noise"] and f["noise_pattern"]:
        out.append("noise_pattern")
    if ELIGIBILITY["exclude_footer_zone"] and f["zone"] == "footer":
        out.append("footer_zone")
    if f["digit_ratio"] > ELIGIBILITY["digit_ratio_max"]:
        out.append("digit_heavy")
    return out


def score_row(row, flat_typography):
    """Weighted contributions for one unit. Returns (score, contributions)."""
    f = row["features"]
    if len(row["text"]) < ELIGIBILITY["min_chars"]:
        return None, [], ["too_short"]

    fails = eligibility_failures(f)
    if fails:
        return None, [], fails

    values = {
        "font_rank": f["font_rank"],
        "top_ratio_inverted": 1.0 - f["top_ratio"],
        "whitespace_isolation": f["whitespace_isolation"],
        "uppercase_ratio": f["uppercase_ratio"],
        "center_score": f["center_score"],
        # Null is not zero. An OCR unit's weight was never measured, so the
        # feature contributes nothing rather than asserting "not bold".
        "bold_flag": f["bold_flag"],
        "first_occurrence": f["first_occurrence"],
    }

    contributions, score = [], 0.0
    for name, weight in WEIGHTS.items():
        v = values.get(name)
        if v is None:
            contributions.append({"feature": name, "value": None,
                                  "weight": weight, "contribution": 0.0,
                                  "note": "undefined for this unit"})
            continue
        w = weight
        if flat_typography and name in LAYOUT_FEATURES:
            w = weight * FLAT_DAMPING
        c = round(v * w, 4)
        score += c
        contributions.append({"feature": name, "value": v,
                              "weight": round(w, 4), "contribution": c})

    applied = []
    if f["page_in_segment"] > 1:
        applied.append("not_first_page")
    if f["identifier_pattern"]:
        applied.append("identifier_pattern")
    if f["date_pattern"] and f["word_count"] <= 6:
        applied.append("date_pattern")
    if f["digit_ratio"] > DIGIT_HEAVY_RATIO:
        applied.append("digit_heavy")
    if f["uppercase_ratio"] > 0.9 and f["word_count"] > ALL_CAPS_LONG_WORDS:
        applied.append("all_caps_long")
    for name in applied:
        p = PENALTIES[name]
        score += p
        contributions.append({"feature": name, "value": 1.0,
                              "weight": p, "contribution": p})

    return round(score, 4), contributions, []


def assign(matrix):
    """Pick the TITLE for one segment's Feature Matrix.

    Returns a full audit record: winner, margin, confidence state, the scored
    ranking, and every excluded unit with its reason.
    """
    flat = matrix["segment"].get("flat_typography", False)

    scored, excluded = [], []
    for row in matrix["rows"]:
        score, contributions, fails = score_row(row, flat)
        if score is None:
            excluded.append({"unit_id": row["unit_id"],
                             "text": row["text"][:80], "reasons": fails})
            continue
        f = row["features"]
        scored.append({
            "unit_id": row["unit_id"],
            "text": row["text"],
            "score": score,
            "contributions": contributions,
            "source_span_ids": row["source_span_ids"],
            # Total ordering: score, then reading order, then id. Cannot tie.
            "_order": (-score, f["page_in_segment"], f["top_ratio"],
                       f["left_indent_ratio"], row["unit_id"]),
        })

    scored.sort(key=lambda r: r["_order"])
    for r in scored:
        del r["_order"]

    if not scored:
        return {
            "role": "TITLE", "value": None, "confidence": "escalate",
            "reason": "no eligible candidate", "margin": None,
            "ranking": [], "excluded": excluded,
            "role_version": ROLE_VERSION,
            "contract_versions": _contracts.versions(),
            "flat_typography": flat,
        }

    top = scored[0]
    margin = round(top["score"] - scored[1]["score"], 4) if len(scored) > 1 else top["score"]

    if margin >= CONFIDENCE["auto_margin"]:
        state, value = "auto", top["text"]
    elif margin >= CONFIDENCE["review_margin"]:
        state, value = "review", top["text"]
    else:
        # Below the floor the engine declines. A wrong label is worse than none.
        state, value = "escalate", None

    return {
        "role": "TITLE",
        "value": value,
        "candidate": top["text"],
        "unit_id": top["unit_id"],
        "source_span_ids": top["source_span_ids"],
        "score": top["score"],
        "margin": margin,
        "confidence": state,
        "contributions": top["contributions"],
        "ranking": [{"unit_id": r["unit_id"], "score": r["score"],
                     "text": r["text"][:90]} for r in scored[:5]],
        "excluded": excluded,
        "flat_typography": flat,
        "role_version": ROLE_VERSION,
        "contract_versions": _contracts.versions(),
    }
