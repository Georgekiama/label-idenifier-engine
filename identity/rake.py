"""
RAKE - Rapid Automatic Keyword Extraction (Rose et al.), connector-aware.

Classic RAKE splits candidate phrases at every stopword. That is wrong for this
problem: `to`, `of` and `with` are stopwords, so `Motion to Compel` becomes
`Motion` + `Compel`, and `TREATY WITH THE TRIBES OF MIDDLE OREGON` disintegrates
entirely. Measured on the benchmark corpus, 30% of phrase-like units on page 1
carry an interior stopword.

The one modification: split on punctuation and on HARD stopwords only.
Connectors (`of to for and the in on with v vs ...`) are permitted inside a
candidate and trimmed from its edges. The scoring is unchanged from the paper -
word score is deg(w)/freq(w), phrase score is the sum over its words - with a
length normalisation so a long phrase cannot win on word count alone.

No embeddings, no model, no corpus. Same text in, same phrases out.
"""

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402
from identity import lexicon as lex  # noqa: E402

_R = _contracts.load("identity")["rake"]
LENGTH_ALPHA = _R["length_alpha"]
MAX_PHRASE_WORDS = _R["max_phrase_words"]
MIN_WORD_CHARS = _R["min_word_chars"]


def extract_phrases(text):
    """Split one string into RAKE candidate phrases (lists of words)."""
    phrases = []
    for chunk in lex.SPLIT_RE.split(text or ""):
        current = []
        for raw in chunk.split():
            w = raw.strip("\"'“”‘’()[]{}.,;:!?")
            if not w:
                continue
            low = w.lower()
            if low in lex.HARD_STOPWORDS:
                if current:
                    phrases.append(current)
                    current = []
                continue
            current.append(w)
            if len(current) >= MAX_PHRASE_WORDS:
                phrases.append(current)
                current = []
        if current:
            phrases.append(current)

    out = []
    for p in phrases:
        trimmed = lex.strip_edge_connectors(p)
        if trimmed:
            out.append(trimmed)
    return out


def build_scores(texts):
    """Word scores deg(w)/freq(w) over all phrases found in `texts`.

    Degree is co-occurrence degree within a phrase, exactly as in the paper: a
    word appearing in longer phrases accrues more degree, which is what makes
    RAKE prefer multi-word terms over isolated common words.
    """
    freq = collections.Counter()
    degree = collections.Counter()
    for text in texts:
        for phrase in extract_phrases(text):
            words = [w.lower() for w in phrase if len(w) >= MIN_WORD_CHARS]
            if not words:
                continue
            d = len(words) - 1
            for w in words:
                freq[w] += 1
                degree[w] += d
    scores = {}
    for w, f in freq.items():
        scores[w] = (degree[w] + f) / f      # deg includes the word itself
    return {"word_scores": scores, "freq": dict(freq)}


def score_phrase(text, model):
    """Score an arbitrary candidate string against a built RAKE model."""
    ws = model["word_scores"]
    words = [w.lower() for w in lex.tokens(text) if len(w) >= MIN_WORD_CHARS]
    content = [w for w in words if w not in lex.HARD_STOPWORDS]
    if not content:
        return 0.0
    total = sum(ws.get(w, 0.0) for w in content)
    return round(total / (len(content) ** LENGTH_ALPHA), 4)
