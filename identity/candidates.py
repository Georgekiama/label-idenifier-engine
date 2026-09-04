"""
Candidate Phrase Engine.

Everything the composer may elect must originate here, and every candidate must
carry provenance back to the spans Stage 1 extracted. Three sources:

    unit          an assembled unit, exactly as it appears on the page
    subphrase     a RAKE-style chunk WITHIN a unit, for the common case of a
                  heading followed by a dash and a subtitle on one line
    composed      a chain of units joined by the phrase graph

No candidate text is ever constructed from words that were not adjacent on the
page. The only character the engine adds is the graph's declared joiner.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from identity import graph as phrase_graph  # noqa: E402
from identity import lexicon as lex  # noqa: E402
from identity import rake  # noqa: E402
from identity import typography  # noqa: E402

MIN_CHARS = 6
MIN_WORDS = 1
# A unit longer than this is a paragraph. Splitting it produces well-shaped
# fragments that are not headings, so sub-phrases are taken only from units
# that could plausibly BE a heading: a typographic heading level, or a short
# line. This is the difference between extracting identity and inventing it.
SUBPHRASE_MAX_WORDS = 16
HEADING_LEVELS = ("h1", "h2", "h3", "h4")


def _clean(text):
    return " ".join((text or "").split()).strip(" \t-–—·|")


def _key(text):
    """Normalised identity for de-duplication."""
    return " ".join(w.lower() for w in lex.tokens(text))


def generate(units, hierarchy=None):
    """Return candidate dicts, de-duplicated, each with provenance."""
    out, seen = [], {}
    hierarchy = hierarchy or typography.build_hierarchy(units)

    def add(text, source, unit_ids, extra=None):
        t = _clean(text)
        if len(t) < MIN_CHARS or len(t.split()) < MIN_WORDS:
            return
        k = _key(t)
        if not k:
            return
        if k in seen:
            # Keep the richer provenance but do not duplicate the candidate.
            seen[k]["sources"].add(source)
            return
        anchor = unit_ids[0]
        c = {
            "text": t,
            "sources": {source},
            "unit_ids": list(unit_ids),
            "anchor_unit": anchor,
            "key": k,
        }
        if extra:
            c.update(extra)
        seen[k] = c
        out.append(c)

    by_id = {u["unit_id"]: u for u in units}

    # 1. Whole units, as they appear.
    for u in units:
        add(u["text"], "unit", [u["unit_id"]])

    # 2. Sub-phrases within a unit. A single line often carries both a heading
    #    and a qualifier ("Annual Report 2004 | Office of Water"), and the
    #    heading alone is the better label.
    for u in units:
        heading_like = (typography.level_of(u, hierarchy) in HEADING_LEVELS
                        or len(u["text"].split()) <= SUBPHRASE_MAX_WORDS)
        if not heading_like:
            continue
        parts = rake.extract_phrases(u["text"])
        if len(parts) < 2:
            continue
        for p in parts:
            if len(p) >= 2:
                add(" ".join(p), "subphrase", [u["unit_id"]])

    # 3. Graph compositions: headings split across units.
    g = phrase_graph.build(units)
    for comp in phrase_graph.compose(units, g):
        add(comp["text"], "composed", comp["unit_ids"],
            {"composition": {"parts": comp["parts"], "reasons": comp["reasons"]}})

    # Attach the anchor unit's geometry so downstream scorers can use position
    # and typography without re-joining to the unit list.
    for c in out:
        u = by_id.get(c["anchor_unit"])
        if u:
            c["page"] = u["page"]
            c["y"] = u["y"]
            c["x"] = u["x"]
            c["font_size"] = u["font_size"]
            c["bold"] = u.get("bold", False)
            c["page_height"] = u.get("page_height", 792.0)
            c["page_width"] = u.get("page_width", 612.0)
            c["source_span_ids"] = [sid for uid in c["unit_ids"]
                                    for sid in by_id[uid]["source_span_ids"]
                                    if uid in by_id]
        c["sources"] = sorted(c["sources"])

    out.sort(key=lambda c: (c.get("page", 1), c.get("y", 0.0), c.get("x", 0.0), c["key"]))
    return out, g
