"""
Phrase Graph - reconstruct headings that are split across units.

Every unit is a node. An edge exists only where MEASURABLE evidence says two
units are parts of one heading:

    vertical adjacency   the next unit down, within a gap budget
    font similarity      same size (within a tolerance) and same weight
    alignment            left edges align, or both are centred
    same page            a heading does not span a page break
    hierarchy            both sit at the same typographic level

A connected chain of up to `max_chain` nodes yields a COMPOSED candidate:

    Motion to Compel
    Discovery Responses
        ->  Motion to Compel - Discovery Responses

This is composition, not generation. No word is created. The joiner is a single
contract-declared character, and every composed candidate records the exact unit
ids it was built from, so a reviewer can see the parts on the page.

Stage 1.5 already merges lines of identical style into one unit. This layer goes
further: it links units that assembly deliberately kept separate - a title and
its subtitle, set at different sizes or with a gap too wide to merge - which is
precisely the case assembly must not guess at but composition may propose.
"""

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402

_G = _contracts.load("identity")["phrase_graph"]
MAX_VGAP_RATIO = _G["max_vertical_gap_ratio"]
MAX_SIZE_DELTA = _G["max_size_delta"]
MAX_LEFT_DELTA = _G["max_left_delta"]
MAX_CENTER_DELTA = _G["max_center_delta"]
MAX_CHAIN = _G["max_chain"]
BLOCK_JOINER = _G["block_joiner"]
MAX_COMPOSED_WORDS = _G["max_composed_words"]


def _centre(u):
    return u["x"] + u["width"] / 2.0


def edge_evidence(a, b, median_gap):
    """Why (or why not) units a and b are parts of one heading.

    Returns (bool, [reasons]). Every reason is a measurement, never a guess.
    """
    reasons = []
    if a["page"] != b["page"]:
        return False, ["different_page"]

    gap = b["y"] - (a["y"] + a["height"])
    budget = max(median_gap, 1.0) * MAX_VGAP_RATIO
    if gap < -1.0 or gap > budget:
        return False, [f"vertical_gap {round(gap, 1)}pt > {round(budget, 1)}pt"]
    reasons.append(f"vertical_adjacency {round(gap, 1)}pt")

    size_delta = abs(a["font_size"] - b["font_size"])
    if size_delta > MAX_SIZE_DELTA:
        return False, [f"size_delta {round(size_delta, 2)}pt"]
    reasons.append("font_similarity")

    if bool(a.get("bold")) != bool(b.get("bold")):
        return False, ["weight_mismatch"]

    left_delta = abs(a["x"] - b["x"])
    centre_delta = abs(_centre(a) - _centre(b))
    if left_delta <= MAX_LEFT_DELTA:
        reasons.append(f"left_aligned {round(left_delta, 1)}pt")
    elif centre_delta <= MAX_CENTER_DELTA:
        reasons.append(f"centre_aligned {round(centre_delta, 1)}pt")
    else:
        return False, [f"alignment left {round(left_delta, 1)} centre {round(centre_delta, 1)}"]

    return True, reasons


def build(units):
    """Return {'edges': {unit_id: [(unit_id, reasons)]}, 'median_gap': float}."""
    ordered = sorted(units, key=lambda u: (u["page"], u["y"], u["x"]))
    gaps = []
    for a, b in zip(ordered, ordered[1:]):
        if a["page"] == b["page"]:
            g = b["y"] - (a["y"] + a["height"])
            if g > 0:
                gaps.append(g)
    median_gap = statistics.median(gaps) if gaps else 12.0

    edges = {}
    for i, a in enumerate(ordered):
        out = []
        for b in ordered[i + 1: i + 4]:      # only near-following units
            ok, reasons = edge_evidence(a, b, median_gap)
            if ok:
                out.append((b["unit_id"], reasons))
        edges[a["unit_id"]] = out
    return {"edges": edges, "median_gap": round(median_gap, 2), "order": ordered}


def compose(units, graph):
    """Walk chains of connected units into composed candidates."""
    by_id = {u["unit_id"]: u for u in units}
    edges = graph["edges"]
    out = []
    seen = set()

    def walk(chain):
        if len(chain) >= 2:
            parts = [by_id[i] for i in chain]
            text = BLOCK_JOINER.join(p["text"].strip() for p in parts)
            if len(text.split()) <= MAX_COMPOSED_WORDS:
                key = tuple(chain)
                if key not in seen:
                    seen.add(key)
                    out.append({
                        "text": text,
                        "unit_ids": list(chain),
                        "parts": [p["text"] for p in parts],
                        "reasons": [r for uid in chain[:-1]
                                    for tgt, rs in edges.get(uid, [])
                                    if tgt in chain for r in rs],
                    })
        if len(chain) >= MAX_CHAIN:
            return
        for nxt, _ in edges.get(chain[-1], []):
            walk(chain + [nxt])

    for u in sorted(units, key=lambda x: (x["page"], x["y"], x["x"])):
        walk([u["unit_id"]])
    return out
