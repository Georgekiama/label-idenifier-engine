"""
Shared vocabulary and the frozen IDF table.

Determinism note
----------------
IDF is corpus-derived, which is exactly the kind of live state that would make
the same PDF produce different labels on different days. So the table is a
FROZEN, versioned, content-hashed artefact built once by tools/build_idf.py and
shipped as an input. Every label records the IDF version it was scored against.
"""

import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402

_C = _contracts.load("identity")
_L = _C["lexicon"]

CONNECTORS = frozenset(w.lower() for w in _L["connectors"])
HARD_STOPWORDS = frozenset(w.lower() for w in _L["hard_stopwords"])
FURNITURE = frozenset(w.lower() for w in _L["furniture"])
STOPWORDS = CONNECTORS | HARD_STOPWORDS

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*|\d+[A-Za-z]*")
# Punctuation that always ends a phrase.
SPLIT_RE = re.compile(r"[.,;:!?()\[\]{}\"“”/\|]+|\s+[-–—]\s+")

MIN_IR_TOKEN_CHARS = 2

IDF_PATH = Path(__file__).resolve().parent.parent / "contracts" / "idf.json"

_idf_cache = None


def tokens(text):
    """Lowercased word tokens. Deterministic and punctuation-free."""
    return [m.group(0).lower() for m in WORD_RE.finditer(text or "")]


def ir_tokens(text):
    """Tokens fit for information retrieval.

    Single characters and bare numerals carry no retrieval signal but dominate
    a raw frequency table: built without this filter, the corpus IDF's most
    common "terms" were "1", "2", "s", "u" and "c". Stopwords are kept here and
    filtered by the caller, because RAKE and TextRank need different subsets.
    """
    return [w for w in tokens(text)
            if len(w) >= MIN_IR_TOKEN_CHARS and not w.isdigit()]


def load_idf():
    """Load the frozen IDF table, or a neutral fallback if none is shipped.

    A missing table must not crash the engine: BM25 simply degrades to treating
    every term as equally rare, which is honest rather than silently wrong. The
    audit records which happened.
    """
    global _idf_cache
    if _idf_cache is not None:
        return _idf_cache
    if IDF_PATH.exists():
        with open(IDF_PATH, encoding="utf-8") as f:
            data = json.load(f)
        table = data.get("idf", {})
        _idf_cache = {
            "version": data.get("version", "unknown"),
            "documents": data.get("documents", 0),
            "idf": table,
            "max_idf": max(table.values()) if table else 1.0,
            "default_idf": data.get("default_idf", 1.0),
            "avgdl": data.get("avgdl"),
            "present": True,
        }
    else:
        _idf_cache = {"version": "absent", "documents": 0, "idf": {},
                      "max_idf": 1.0, "default_idf": 1.0, "avgdl": None,
                      "present": False}
    return _idf_cache


def idf(term):
    t = load_idf()
    return t["idf"].get(term, t["max_idf"] if t["present"] else 1.0)


def is_connector(w):
    return w.lower() in CONNECTORS


def is_stopword(w):
    return w.lower() in STOPWORDS


def stopword_ratio(text):
    ws = tokens(text)
    if not ws:
        return 1.0
    return sum(1 for w in ws if w in STOPWORDS) / len(ws)


def has_furniture(text):
    return any(w in FURNITURE for w in tokens(text))


def strip_edge_connectors(words):
    """Trim leading/trailing connectors: 'Motion to' is not a label."""
    i, j = 0, len(words)
    while i < j and words[i].lower().strip(".,;:") in CONNECTORS:
        i += 1
    while j > i and words[j - 1].lower().strip(".,;:") in CONNECTORS:
        j -= 1
    return words[i:j]
