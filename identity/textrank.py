"""
TextRank (Mihalcea & Tarau) - the classical graph algorithm only.

No neural variants, no embeddings. A word co-occurrence graph is built over the
text of pages 1-2, PageRank is run over it, and a candidate phrase scores as the
normalised sum of its words' ranks.

Determinism
-----------
PageRank is iterative, so two things are pinned:

  - a FIXED iteration count rather than a convergence tolerance. A tolerance
    test can stop one iteration earlier or later depending on floating-point
    accumulation order and produce a different last digit.
  - nodes are processed in sorted order, so accumulation order is identical on
    every run and every machine.

Same text in, byte-identical ranks out.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402
from identity import lexicon as lex  # noqa: E402

_T = _contracts.load("identity")["textrank"]
WINDOW = _T["window"]
DAMPING = _T["damping"]
ITERATIONS = _T["iterations"]
LENGTH_ALPHA = _T["length_alpha"]


def build_graph(texts):
    """Undirected co-occurrence graph over content words."""
    adjacency = {}

    def link(a, b):
        if a == b:
            return
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    for text in texts:
        words = [w for w in lex.tokens(text)
                 if w not in lex.STOPWORDS and len(w) > 2 and not w.isdigit()]
        for i, w in enumerate(words):
            adjacency.setdefault(w, set())
            for j in range(i + 1, min(i + WINDOW, len(words))):
                link(w, words[j])
    return adjacency


def pagerank(adjacency):
    """Classical PageRank, deterministic by construction."""
    nodes = sorted(adjacency)
    n = len(nodes)
    if n == 0:
        return {}
    rank = {v: 1.0 / n for v in nodes}
    for _ in range(ITERATIONS):
        nxt = {}
        for v in nodes:                      # sorted: fixed accumulation order
            acc = 0.0
            for u in sorted(adjacency[v]):
                deg = len(adjacency[u]) or 1
                acc += rank[u] / deg
            nxt[v] = (1.0 - DAMPING) / n + DAMPING * acc
        rank = nxt
    top = max(rank.values()) or 1.0
    return {v: r / top for v, r in rank.items()}


def build_model(texts):
    return {"ranks": pagerank(build_graph(texts))}


def score_phrase(text, model):
    ranks = model["ranks"]
    words = [w for w in lex.tokens(text)
             if w not in lex.STOPWORDS and len(w) > 2 and not w.isdigit()]
    if not words:
        return 0.0
    total = sum(ranks.get(w, 0.0) for w in words)
    return round(total / (len(words) ** LENGTH_ALPHA), 4)
