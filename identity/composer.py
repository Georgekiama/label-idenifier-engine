"""
Identity Composer - elect the best presentable binder labels.

Fuses several INDEPENDENT evidence sources. None decides alone:

    typography      hierarchy level by character mass, not raw size
    presentability  fitness for a binder tab
    position        first page, upper region, first occurrence
    bm25            corpus-rarity relevance against a frozen IDF table
    textrank        classical graph ranking
    rake            connector-aware multi-word phrase extraction
    graph_bonus     the candidate was composed from a coherent unit chain

Each source is normalised to 0-1 ACROSS THIS DOCUMENT'S candidate set before
weighting, so no source can dominate through raw scale, and a weight means the
same thing in every document.

The engine never invents language. Every returned label is text that already
appeared on pages 1-2, plus - for composed candidates only - the single joiner
character declared in the contract.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402
from identity import bm25, candidates as cand_engine, lexicon as lex  # noqa: E402
from identity import presentability, rake, textrank, typography  # noqa: E402

COMPOSER_VERSION = "1.0.0"

_C = _contracts.load("identity")["composer"]
WEIGHTS = _C["weights"]
PENALTIES = _C["penalties"]
DUPLICATE_OVERLAP = _C["duplicate_overlap"]
TOP_N = _C["top_n"]
CONFIDENCE = _C["confidence"]

SOURCES = ["typography", "presentability", "position", "bm25", "textrank",
           "rake", "graph_bonus"]

# Sources already expressed on a meaningful absolute 0-1 scale. These must NOT
# be min-max rescaled across the candidate set: doing so maps the worst
# candidate to 0 and the best to 1, so a document containing nothing but body
# text would report its body text as maximally prominent. Only the unbounded
# IR scores (BM25, RAKE, TextRank) need normalising to be comparable.
ABSOLUTE_SOURCES = frozenset({"typography", "presentability", "position",
                              "graph_bonus"})


def _normalise(values):
    """Scale a dict of raw scores to 0-1 across the candidate set."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        return {k: 0.5 for k in values}     # no discrimination: stay neutral
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def _position_score(c):
    """First page, high on the page. Identity concentrates at the top."""
    ph = c.get("page_height") or 792.0
    top = 1.0 - min(max(c.get("y", 0.0) / ph, 0.0), 1.0)
    return round(top, 4)


def _token_set(text):
    return {w for w in lex.tokens(text) if w not in lex.STOPWORDS}


def _overlap(a, b):
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def compose(units, enabled=None, top_n=TOP_N):
    """Elect the top labels. `enabled` restricts evidence sources for ablation."""
    active = set(SOURCES if enabled is None else enabled)

    if not units:
        return {"labels": [], "confidence": "escalate", "candidates": 0,
                "reason": "no units", "composer_version": COMPOSER_VERSION,
                "contract_versions": _contracts.versions()}

    hierarchy = typography.build_hierarchy(units)
    cands, graph = cand_engine.generate(units, hierarchy)
    if not cands:
        return {"labels": [], "confidence": "escalate", "candidates": 0,
                "reason": "no candidates", "composer_version": COMPOSER_VERSION,
                "contract_versions": _contracts.versions()}

    texts = [u["text"] for u in units]
    rake_model = rake.build_scores(texts)
    tr_model = textrank.build_model(texts)
    bm_model = bm25.build_model(texts)
    by_id = {u["unit_id"]: u for u in units}

    raw = {s: {} for s in SOURCES}
    detail = {}
    for c in cands:
        k = c["key"]
        anchor = by_id.get(c["anchor_unit"])
        typo, level = typography.score(anchor, hierarchy) if anchor else (0.0, "body")
        pres, pres_reasons = presentability.score(c["text"])
        raw["typography"][k] = typo
        raw["presentability"][k] = pres
        raw["position"][k] = _position_score(c)
        raw["bm25"][k] = bm25.score_phrase(c["text"], bm_model)
        raw["textrank"][k] = textrank.score_phrase(c["text"], tr_model)
        raw["rake"][k] = rake.score_phrase(c["text"], rake_model)
        raw["graph_bonus"][k] = 1.0 if "composed" in c["sources"] else 0.0
        detail[k] = {"level": level, "presentability_reasons": pres_reasons}

    norm = {s: (v if s in ABSOLUTE_SOURCES else _normalise(v))
            for s, v in raw.items()}

    scored = []
    for c in cands:
        k = c["key"]
        contributions, total = [], 0.0
        for s in SOURCES:
            w = WEIGHTS[s] if s in active else 0.0
            v = norm[s].get(k, 0.0)
            contrib = round(v * w, 4)
            total += contrib
            contributions.append({
                "source": s, "raw": raw[s].get(k), "normalised": round(v, 4),
                "weight": w, "contribution": contrib,
                "disabled": s not in active,
            })

        applied = []
        if lex.has_furniture(c["text"]):
            applied.append("furniture")
        if detail[k]["level"] == "outlier":
            applied.append("noise")
        if c.get("page", 1) > (units[0]["page"] if units else 1):
            applied.append("not_first_page")
        for name in applied:
            p = PENALTIES[name]
            total += p
            contributions.append({"source": name, "raw": 1.0, "normalised": 1.0,
                                  "weight": p, "contribution": p, "penalty": True})

        scored.append({
            "text": c["text"],
            "key": k,
            "score": round(total, 4),
            "sources": c["sources"],
            "unit_ids": c["unit_ids"],
            "source_span_ids": c.get("source_span_ids", []),
            "page": c.get("page"),
            "hierarchy_level": detail[k]["level"],
            "presentability": raw["presentability"][k],
            "presentability_reasons": detail[k]["presentability_reasons"],
            "composition": c.get("composition"),
            "contributions": contributions,
            # Total order: score, then reading order, then key. Cannot tie.
            "_order": (-round(total, 4), c.get("page", 1), c.get("y", 0.0),
                       c.get("x", 0.0), k),
        })

    scored.sort(key=lambda r: r["_order"])

    # Near-duplicate suppression. Without it the top 3 is one phrase three
    # times: the unit, its sub-phrase, and the composition containing both.
    elected, suppressed = [], []
    for r in scored:
        dup = next((e for e in elected
                    if _overlap(e["text"], r["text"]) >= DUPLICATE_OVERLAP), None)
        if dup is not None:
            suppressed.append({"text": r["text"], "score": r["score"],
                               "duplicate_of": dup["text"]})
            continue
        elected.append(r)
        if len(elected) >= top_n:
            break

    for r in scored:
        r.pop("_order", None)

    margin = (round(elected[0]["score"] - elected[1]["score"], 4)
              if len(elected) > 1 else (elected[0]["score"] if elected else None))
    if margin is None:
        state = "escalate"
    elif margin >= CONFIDENCE["auto_margin"]:
        state = "auto"
    elif margin >= CONFIDENCE["review_margin"]:
        state = "review"
    else:
        state = "escalate"

    return {
        "labels": [{
            "rank": i + 1,
            "text": r["text"],
            "score": r["score"],
            "hierarchy_level": r["hierarchy_level"],
            "presentability": r["presentability"],
            "sources": r["sources"],
            "unit_ids": r["unit_ids"],
            "source_span_ids": r["source_span_ids"],
            "composition": r["composition"],
            "contributions": r["contributions"],
            "presentability_reasons": r["presentability_reasons"],
        } for i, r in enumerate(elected)],
        "confidence": state,
        "margin": margin,
        "candidates": len(cands),
        "suppressed_duplicates": suppressed[:10],
        "typography": {
            "body_size": hierarchy["body_size"],
            "body_mass": hierarchy.get("body_mass"),
            "heading_sizes": hierarchy.get("heading_sizes", []),
        },
        "idf": {"version": bm_model["idf_version"], "present": bm_model["idf_present"]},
        "graph": {"median_gap": graph["median_gap"],
                  "edges": sum(len(v) for v in graph["edges"].values())},
        "enabled_sources": sorted(active),
        "composer_version": COMPOSER_VERSION,
        "contract_versions": _contracts.versions(),
    }
