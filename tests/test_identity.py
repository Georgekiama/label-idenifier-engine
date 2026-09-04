"""
Identity Composer invariants.

These guard the properties that must hold for ANY document, whatever the
accuracy number happens to be. A quality score can regress and be argued about;
a broken invariant means the output cannot be trusted at all.

The first test is the project's central promise: the engine never invents words.
"""

import glob
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import unit_assembly  # noqa: E402
from identity import composer, graph, lexicon, presentability, rake, textrank, typography  # noqa: E402

CORPUS = r"C:/Users/user/Downloads/corpus all pdfs"
WORD_RE = re.compile(r"[A-Za-z0-9']+")


def _sample(n=10):
    files = sorted(glob.glob(os.path.join(CORPUS, "*.pdf")))
    if not files:
        pytest.skip("source corpus not available")
    step = max(1, len(files) // n)
    return files[::step][:n]


@pytest.fixture(scope="module")
def sample():
    return _sample()


def _units(pdf, pages=(1, 2)):
    return unit_assembly.assemble(pdf, list(pages))


# --- The central promise ----------------------------------------------------

def test_engine_never_invents_words(sample):
    """Every word in every emitted label must already exist on pages 1-2.

    This is the non-negotiable principle of the whole project. The composer may
    extract, rank, join, normalise and compose; it may not generate. The only
    character it is permitted to add is the contract-declared joiner, which is
    punctuation and contributes no word.
    """
    for pdf in sample:
        units = _units(pdf)
        if not units:
            continue
        source_words = set()
        for u in units:
            source_words.update(w.lower() for w in WORD_RE.findall(u["text"]))
        out = composer.compose(units)
        for label in out["labels"]:
            for w in WORD_RE.findall(label["text"]):
                assert w.lower() in source_words, (
                    f"{os.path.basename(pdf)}: label {label['text']!r} contains "
                    f"{w!r}, which appears nowhere on pages 1-2")


def test_labels_are_traceable_to_spans(sample):
    """A printed label must lead back to the exact Stage 1 spans behind it."""
    for pdf in sample:
        units = _units(pdf)
        if not units:
            continue
        valid = {u["unit_id"] for u in units}
        for label in composer.compose(units)["labels"]:
            assert label["unit_ids"], f"{pdf}: label has no unit provenance"
            assert set(label["unit_ids"]) <= valid
            assert label["source_span_ids"]


# --- Determinism ------------------------------------------------------------

def test_composition_is_deterministic(sample):
    for pdf in sample[:5]:
        units = _units(pdf)
        a = composer.compose(units)
        b = composer.compose(units)
        assert [x["text"] for x in a["labels"]] == [x["text"] for x in b["labels"]]
        assert [x["score"] for x in a["labels"]] == [x["score"] for x in b["labels"]]


def test_textrank_is_deterministic(sample):
    """PageRank is iterative; fixed iterations and sorted node order must make
    it bit-for-bit reproducible."""
    for pdf in sample[:4]:
        texts = [u["text"] for u in _units(pdf)]
        if not texts:
            continue
        a = textrank.build_model(texts)["ranks"]
        b = textrank.build_model(texts)["ranks"]
        assert a == b


# --- Evidence sources -------------------------------------------------------

def test_rake_keeps_interior_connectors():
    """Classic RAKE splits on every stopword and would shatter these.

    This modification is the reason the engine can return "Motion to Compel"
    rather than "Motion" and "Compel" as separate candidates.
    """
    phrases = [" ".join(p) for p in rake.extract_phrases("Motion to Compel Discovery Responses")]
    assert any("motion" in p.lower() and "compel" in p.lower() for p in phrases), phrases

    phrases = [" ".join(p) for p in rake.extract_phrases("TREATY WITH THE TRIBES OF MIDDLE OREGON")]
    assert any("TREATY" in p and "OREGON" in p for p in phrases), phrases


def test_rake_still_splits_on_hard_stopwords():
    """The modification must not turn RAKE into 'never split'."""
    phrases = [" ".join(p) for p in rake.extract_phrases("The report was filed although the motion is pending")]
    assert len(phrases) >= 2, phrases


def test_typography_promotes_short_titles():
    """A title is SHORT by nature; character mass alone demotes it.

    Regression guard for the bug where a 17-character heading at 20pt was
    classified `outlier` and penalised as noise while a 173-character author
    block became H1.
    """
    units = [
        {"unit_id": "u1", "text": "BLACK HUCKLEBERRY", "font_size": 20.0, "bold": False},
        {"unit_id": "u2", "text": "x" * 2000, "font_size": 10.0, "bold": False},
    ]
    h = typography.build_hierarchy(units)
    assert typography.level_of(units[0], h) == "h1"
    assert typography.level_of(units[1], h) == "body"


def test_typography_still_rejects_a_stray_glyph():
    """A Bates stamp is large and short; it must NOT become a heading."""
    units = [
        {"unit_id": "u1", "text": "ABC00123", "font_size": 30.0, "bold": False},
        {"unit_id": "u2", "text": "y" * 2000, "font_size": 10.0, "bold": False},
    ]
    h = typography.build_hierarchy(units)
    assert typography.level_of(units[0], h) == "outlier"


def test_presentability_prefers_a_title_over_a_paragraph():
    title, _ = presentability.score("Motion to Compel Discovery Responses")
    prose, _ = presentability.score(
        "The above-named confederated bands of Indians cede to the United States "
        "all their right, title and interest in and to the lands described herein.")
    assert title > prose + 0.3, (title, prose)


def test_presentability_is_bounded(sample):
    for pdf in sample:
        for u in _units(pdf):
            s, reasons = presentability.score(u["text"])
            assert 0.0 <= s <= 1.0
            assert isinstance(reasons, list)


def test_graph_edges_carry_evidence(sample):
    """No edge may exist without a stated, measurable reason."""
    for pdf in sample[:5]:
        units = _units(pdf)
        if len(units) < 2:
            continue
        g = graph.build(units)
        for src, targets in g["edges"].items():
            for tgt, reasons in targets:
                assert reasons, f"edge {src}->{tgt} has no evidence"


# --- Composer contract ------------------------------------------------------

def test_top_labels_are_distinct(sample):
    """Near-duplicate suppression must stop the top 3 being one phrase thrice."""
    for pdf in sample:
        labels = composer.compose(_units(pdf))["labels"]
        texts = [lb["text"].lower() for lb in labels]
        assert len(texts) == len(set(texts))


def test_every_label_has_a_full_audit_trail(sample):
    """Every evidence source must be accounted for on every label, including
    the ones that contributed nothing - silence is not an audit."""
    for pdf in sample:
        for label in composer.compose(_units(pdf))["labels"]:
            sources = {c["source"] for c in label["contributions"]}
            assert set(composer.SOURCES) <= sources, sources
            for c in label["contributions"]:
                assert "contribution" in c and "weight" in c


def test_ablation_actually_disables_a_source(sample):
    """The ablation harness must really turn a source off, or its findings are
    meaningless."""
    pdf = sample[0]
    units = _units(pdf)
    enabled = [s for s in composer.SOURCES if s != "bm25"]
    out = composer.compose(units, enabled=enabled)
    for label in out["labels"]:
        bm = next(c for c in label["contributions"] if c["source"] == "bm25")
        assert bm["weight"] == 0.0 and bm["contribution"] == 0.0
        assert bm["disabled"] is True


def test_idf_table_is_frozen_and_versioned():
    """BM25 depends on corpus state; that state must be a pinned artefact."""
    table = lexicon.load_idf()
    if not table["present"]:
        pytest.skip("no IDF table shipped")
    assert table["version"] and table["version"] != "unknown"
    assert table["documents"] > 0
    assert table["avgdl"], "avgdl missing - BM25 length normalisation would be inert"
