"""
ExhibitPro - Stage 5: Identity Composer

Given the units of a segment's head pages, produce the top presentable binder
labels using deterministic algorithms only. No AI, no vision models, no LLMs,
no embeddings.

The engine never invents words. It extracts, ranks, joins, normalises and
composes text that already exists on pages 1-2.

Evidence sources, deliberately independent
------------------------------------------
    bm25          corpus-rarity relevance, against a FROZEN IDF table
    rake          multi-word phrase extraction, connector-aware
    textrank      classical graph ranking, no neural variants
    graph         reconstruction of headings split across units
    typography    document hierarchy by character mass, not raw size
    presentability whether the string belongs on a binder tab at all

None of them decides alone. composer.compose() normalises each source across
the candidate set, applies contract weights, and elects the top N with a full
per-source audit trail.
"""

from . import lexicon, candidates, bm25, rake, textrank, graph, typography, presentability, composer  # noqa: F401

__all__ = ["lexicon", "candidates", "bm25", "rake", "textrank", "graph",
           "typography", "presentability", "composer"]
