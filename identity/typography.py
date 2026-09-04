"""
Typography Hierarchy Engine.

The objective is document HIERARCHY, not the largest text. Those differ
constantly: a Bates stamp, a decorative numeral or a page number can each be the
biggest glyphs on a page while carrying no identity at all.

Method: cluster font sizes by CHARACTER MASS. A size only founds a hierarchy
level if it carries a meaningful share of the segment's characters, so a single
oversized token cannot become H1. The largest substantial size is H1, the next
H2, and so on; the size carrying the bulk of the text is body, whatever its
absolute value - a document set entirely in 14pt has no 14pt heading.
"""

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402

_T = _contracts.load("identity")["typography"]
MIN_LEVEL_MASS = _T["min_level_mass"]
MIN_LEVEL_CHARS = _T["min_level_chars"]
SIZE_BUCKET = _T["size_bucket"]
MAX_LEVELS = _T["max_levels"]
LEVEL_SCORES = _T["level_scores"]
BODY_MASS_FLOOR = _T["body_mass_floor"]

LEVEL_NAMES = ["h1", "h2", "h3", "h4"]


def bucket(size):
    return round(float(size) / SIZE_BUCKET) * SIZE_BUCKET


def build_hierarchy(units):
    """Cluster the segment's font sizes into named hierarchy levels."""
    mass = collections.Counter()
    for u in units:
        mass[bucket(u["font_size"])] += max(len(u["text"]), 1)
    total = sum(mass.values())
    if not total:
        return {"levels": {}, "body_size": None, "mass": {}, "sizes": []}

    share = {s: n / total for s, n in mass.items()}

    # Body is the size carrying the most characters. Everything else is measured
    # relative to it, which is what makes the hierarchy scale-free.
    body_size = max(share.items(), key=lambda kv: (kv[1], -kv[0]))[0]

    # A size founds a heading level on EITHER enough mass or enough absolute
    # characters. Mass alone demotes real titles, which are short by nature;
    # the character floor is what actually distinguishes a heading from a
    # stray large glyph such as a Bates stamp or a page number.
    def qualifies(s):
        return s > body_size and (share[s] >= MIN_LEVEL_MASS
                                  or mass[s] >= MIN_LEVEL_CHARS)

    heading_sizes = sorted((s for s in share if qualifies(s)), reverse=True)[:MAX_LEVELS]

    levels = {s: LEVEL_NAMES[i] for i, s in enumerate(heading_sizes)}
    levels[body_size] = "body"
    for s in share:
        if s not in levels:
            # Larger than body but too little mass to found a level (a stamp, a
            # stray numeral), or simply smaller than body.
            levels[s] = "outlier" if s > body_size else "body"

    return {
        "levels": levels,
        "body_size": body_size,
        "body_mass": round(share.get(body_size, 0.0), 4),
        "mass": {s: round(v, 4) for s, v in share.items()},
        "chars": dict(mass),
        "sizes": sorted(share, reverse=True),
        "heading_sizes": heading_sizes,
        "heading_rich": share.get(body_size, 0.0) < BODY_MASS_FLOOR,
    }


def level_of(unit, hierarchy):
    return hierarchy["levels"].get(bucket(unit["font_size"]), "body")


def score(unit, hierarchy):
    """Return (0-1 prominence, level name)."""
    lvl = level_of(unit, hierarchy)
    base = LEVEL_SCORES.get(lvl, LEVEL_SCORES["body"])
    if unit.get("bold") and lvl in ("body", "h3", "h4"):
        # Bold promotes body-sized text toward heading status but must never
        # let it outrank a genuine size level.
        base = min(LEVEL_SCORES["h2"], base + 0.12)
    return round(base, 4), lvl
