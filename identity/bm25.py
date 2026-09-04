"""
BM25 (Okapi) adapted to phrase scoring.

The adaptation, stated plainly
------------------------------
BM25 ranks DOCUMENTS against a QUERY. Here there is no query and only one
document, so the roles are inverted: the CANDIDATE PHRASE is the query and the
document's pages 1-2 are the document. The score answers

    "how well does this phrase represent this document,
     given how rare its terms are across the corpus?"

That is the useful question. Terms appearing in most documents - `united`,
`states`, `department`, `page` - contribute almost nothing, so institutional
boilerplate stops outranking the document's own heading. Measured on the
benchmark corpus, `states` appears in 39% of documents and `national` in 37%.

The formula is unmodified Okapi BM25 with term saturation (k1) and document
length normalisation (b), summed over the phrase's terms, then divided by
len^alpha because BM25 sums and would otherwise always prefer a longer phrase.

Determinism
-----------
IDF is corpus state, which would make the same PDF label differently as the
corpus grows. The table is therefore a FROZEN, versioned, content-hashed
artefact (contracts/idf.json) built once by tools/build_idf.py. A missing table
degrades to uniform IDF rather than crashing, and the audit records which.
"""

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402
from identity import lexicon as lex  # noqa: E402

_B = _contracts.load("identity")["bm25"]
K1 = _B["k1"]
B = _B["b"]
LENGTH_ALPHA = _B["length_alpha"]
UNSEEN = _B["unseen_term_idf"]


def build_model(texts):
    """Term frequencies for this document, plus the frozen IDF table."""
    tf = collections.Counter()
    for t in texts:
        tf.update(w for w in lex.ir_tokens(t) if w not in lex.STOPWORDS)
    table = lex.load_idf()
    doc_len = sum(tf.values()) or 1
    avgdl = table.get("avgdl") or doc_len
    return {
        "tf": tf,
        "doc_len": doc_len,
        "avgdl": avgdl,
        "idf_version": table["version"],
        "idf_present": table["present"],
        "max_idf": table["max_idf"],
        "table": table["idf"],
    }


def _idf(term, model):
    if term in model["table"]:
        return model["table"][term]
    if not model["idf_present"]:
        return 1.0                      # uniform: no corpus evidence either way
    # Unseen means rare means informative.
    return model["max_idf"] if UNSEEN == "max" else 0.0


def score_phrase(text, model):
    terms = [w for w in lex.ir_tokens(text) if w not in lex.STOPWORDS]
    if not terms:
        return 0.0
    tf, dl, avgdl = model["tf"], model["doc_len"], model["avgdl"]
    norm = K1 * (1.0 - B + B * (dl / avgdl))
    total = 0.0
    for t in terms:
        f = tf.get(t, 0)
        if not f:
            continue
        total += _idf(t, model) * (f * (K1 + 1.0)) / (f + norm)
    return round(total / (len(terms) ** LENGTH_ALPHA), 4)
