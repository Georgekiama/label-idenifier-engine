"""
Presentability: is this string fit to print on a binder tab?

Deliberately separate from relevance. The most topically representative string
in a document is very often a whole sentence, and a sentence is not a label. A
paralegal should recognise the document at a glance, which is a constraint on
FORM - length, compactness, punctuation, case - not on topic.

Returns a 0-1 score plus every rule that fired, so the audit can say precisely
why a candidate was judged unsuitable rather than merely that it scored low.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contracts import loader as _contracts  # noqa: E402
from identity import lexicon as lex  # noqa: E402

_P = _contracts.load("identity")["presentability"]
IDEAL_WORDS = tuple(_P["ideal_words"])
ACCEPTABLE_WORDS = tuple(_P["acceptable_words"])
MAX_CHARS = _P["max_chars"]
IDEAL_CHARS = _P["ideal_chars"]
PEN = _P["penalties"]
BON = _P["bonuses"]
STOPWORD_RATIO_MAX = _P["stopword_ratio_max"]
DIGIT_RATIO_MAX = _P["digit_ratio_max"]
OPENING_ARTICLES = frozenset(_P.get("opening_articles", ["a", "an", "the"]))

NEUTRAL = 0.55

SENTENCE_PUNCT_RE = re.compile(r"[.!?]\s+[A-Za-z]")
TRAILING_PUNCT_RE = re.compile(r"[,;:\-–—/]\s*$")
WS_ARTIFACT_RE = re.compile(r"\s{3,}")
REPEAT_CHAR_RE = re.compile(r"(.)\1{4,}")
BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"))


def _is_title_case(words):
    """Most content words capitalised - the conventional shape of a title."""
    content = [w for w in words if w[:1].isalpha() and not lex.is_stopword(w)]
    if len(content) < 2:
        return False
    return sum(1 for w in content if w[:1].isupper()) / len(content) >= 0.75


def score(text):
    """Return (score 0-1, [{rule, delta}]) for one candidate string."""
    t = (text or "").strip()
    if not t:
        return 0.0, [{"rule": "empty", "delta": -1.0}]

    words = t.split()
    wc = len(words)
    nonspace = [c for c in t if not c.isspace()]
    digits = sum(1 for c in t if c.isdigit())
    letters = [c for c in t if c.isalpha()]

    s = NEUTRAL
    reasons = []

    def apply(rule, delta):
        nonlocal s
        s += delta
        reasons.append({"rule": rule, "delta": round(delta, 4)})

    # --- Length: the dominant term. A binder tab has finite width. ---------
    #
    # Word count and character count measure the SAME thing, so they are not
    # summed - the worse of the two applies. Stacking them drove a legitimate
    # 13-word document title to zero, which is a scoring bug, not strictness.
    length_penalties = []
    if wc > ACCEPTABLE_WORDS[1]:
        # Scaled by how far past acceptable it runs, so a 40-word paragraph
        # loses decisively rather than by the same fixed amount as a long title.
        over = min((wc - ACCEPTABLE_WORDS[1]) / ACCEPTABLE_WORDS[1], 2.0)
        length_penalties.append(("too_many_words",
                                 PEN["too_many_words"] * (0.5 + 0.5 * over)))
    if len(t) > MAX_CHARS:
        over = min((len(t) - MAX_CHARS) / MAX_CHARS, 2.0)
        length_penalties.append(("over_max_chars",
                                 PEN["over_max_chars"] * (0.5 + 0.5 * over)))

    if length_penalties:
        rule, delta = min(length_penalties, key=lambda kv: kv[1])
        apply(rule, delta)
    elif IDEAL_WORDS[0] <= wc <= IDEAL_WORDS[1]:
        apply("ideal_length", BON["ideal_length"])
    elif wc < ACCEPTABLE_WORDS[0]:
        apply("too_few_words", PEN["too_few_words"])
    elif wc < IDEAL_WORDS[0]:
        apply("short_of_ideal", PEN["too_few_words"] * 0.4)

    # --- Shape: a label is a noun phrase, not a sentence or a fragment. ----
    if lex.is_connector(words[-1].strip(".,;:")):
        apply("ends_with_connector", PEN["ends_with_connector"])
    first = words[0].strip(".,;:").lower()
    # An article opening a title is normal English ("A Design of ..."); a
    # dangling preposition means the phrase was cut mid-thought.
    if lex.is_connector(first) and first not in OPENING_ARTICLES:
        apply("starts_with_connector", PEN["starts_with_connector"])
    if SENTENCE_PUNCT_RE.search(t):
        apply("sentence_punctuation", PEN["sentence_punctuation"])
    if TRAILING_PUNCT_RE.search(t):
        apply("trailing_punctuation", PEN["trailing_punctuation"])
    if WS_ARTIFACT_RE.search(t) or REPEAT_CHAR_RE.search(t):
        apply("repeated_whitespace_artifact", PEN["repeated_whitespace_artifact"])
    for open_c, close_c in BRACKETS:
        if t.count(open_c) != t.count(close_c):
            apply("unbalanced_brackets", PEN["unbalanced_brackets"])
            break

    if lex.stopword_ratio(t) > STOPWORD_RATIO_MAX:
        apply("high_stopword_ratio", PEN["high_stopword_ratio"])

    if nonspace and digits / len(nonspace) > DIGIT_RATIO_MAX:
        apply("digit_heavy", PEN["digit_heavy"])

    if lex.has_furniture(t):
        apply("furniture_word", PEN["furniture_word"])

    # --- Case: ALL CAPS is conventional for legal headings when short, and
    # merely a shouted paragraph when long. ---------------------------------
    if letters:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.85:
            if wc <= IDEAL_WORDS[1]:
                apply("caps_heading", BON["caps_heading"])
            else:
                apply("all_caps_verbose", PEN["all_caps_verbose"])
        elif _is_title_case(words):
            apply("title_case_consistent", BON["title_case_consistent"])

    if len(t) <= IDEAL_CHARS and IDEAL_WORDS[0] <= wc <= IDEAL_WORDS[1]:
        apply("compact", 0.08)

    return round(max(0.0, min(1.0, s)), 4), reasons
