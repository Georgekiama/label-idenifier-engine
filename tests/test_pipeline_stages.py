"""
ExhibitPro - Stage 1.5 / 2 / 3 invariants

These guard the properties that must hold for ANY document, independent of how
well the scorer performs. A quality number can regress and be argued about; a
broken invariant means the output cannot be trusted at all.
"""

import glob
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import unit_assembly  # noqa: E402
import feature_matrix  # noqa: E402
import role_title  # noqa: E402

CORPUS = r"C:/Users/user/Downloads/corpus all pdfs"


def _sample(n=12):
    files = sorted(glob.glob(os.path.join(CORPUS, "*.pdf")))
    if not files:
        pytest.skip("source corpus not available")
    step = max(1, len(files) // n)
    return files[::step][:n]


@pytest.fixture(scope="module")
def sample():
    return _sample()


def test_assembly_is_lossless(sample):
    """Assembly may regroup text. It may never invent, drop, or reorder it.

    assemble() raises on violation, so reaching the end is the assertion.
    """
    for pdf in sample:
        unit_assembly.assemble(pdf, [1, 2], verify=True)


def test_assembly_is_deterministic(sample):
    for pdf in sample[:5]:
        a = unit_assembly.assemble(pdf, [1, 2])
        b = unit_assembly.assemble(pdf, [1, 2])
        assert [u["text"] for u in a] == [u["text"] for u in b]
        assert [u["source_span_ids"] for u in a] == [u["source_span_ids"] for u in b]


def test_every_unit_keeps_its_provenance(sample):
    """A printed label must be traceable to the exact Stage 1 spans behind it."""
    for pdf in sample:
        for u in unit_assembly.assemble(pdf, [1, 2]):
            assert u["source_span_ids"], f"{pdf}: unit {u['unit_id']} has no provenance"
            assert u["line_count"] >= 1


def test_feature_matrix_carries_text_and_provenance(sample):
    """X is features PLUS payload: the Composer needs the string, the ledger
    needs the span ids. Scoring reads only `features`."""
    for pdf in sample:
        m = feature_matrix.build(unit_assembly.assemble(pdf, [1]))
        for row in m["rows"]:
            assert row["text"]
            assert row["source_span_ids"]
            assert row["features"]
            assert row["feature_set_version"]


def test_features_stay_in_their_declared_ranges(sample):
    """Expected ranges are part of every feature's specification, so a value
    outside one is a contract violation rather than a bad score."""
    unit_interval = ["font_rank", "center_score", "top_ratio", "block_area",
                     "whitespace_isolation", "left_indent_ratio",
                     "uppercase_ratio", "digit_ratio", "typographic_contrast"]
    for pdf in sample:
        m = feature_matrix.build(unit_assembly.assemble(pdf, [1, 2]))
        for row in m["rows"]:
            f = row["features"]
            for name in unit_interval:
                v = f[name]
                assert v is None or 0.0 <= v <= 1.0, f"{pdf} {name}={v}"
            assert f["zone"] in {"header", "body", "footer"}
            assert f["source_modality"] in {"native", "ocr"}
            assert f["word_count"] >= 0
            assert f["unit_line_count"] >= 1


def test_bold_is_null_not_zero_on_ocr_units():
    """Tesseract cannot report weight. Emitting 0.0 would assert 'not bold',
    a measurement that was never made - and 19% of the legal subset genuinely
    has no bold, so the two are indistinguishable downstream."""
    ocr_docs = [os.path.join(CORPUS, n) for n in ("000164.pdf", "000348.pdf", "000357.pdf")]
    ocr_docs = [p for p in ocr_docs if os.path.exists(p)]
    if not ocr_docs:
        pytest.skip("no OCR documents available")
    checked = 0
    for pdf in ocr_docs:
        m = feature_matrix.build(unit_assembly.assemble(pdf, [1]))
        for row in m["rows"]:
            if row["features"]["source_modality"] == "ocr":
                assert row["features"]["bold_flag"] is None
                checked += 1
    assert checked, "no OCR units were produced, so nothing was verified"


def test_title_assignment_is_deterministic(sample):
    for pdf in sample:
        m = feature_matrix.build(unit_assembly.assemble(pdf, [1, 2]))
        a, b = role_title.assign(m), role_title.assign(m)
        assert a["value"] == b["value"]
        assert a["confidence"] == b["confidence"]
        assert a.get("unit_id") == b.get("unit_id")


def test_escalate_emits_no_title(sample):
    """The charter is explicit: a wrong label is worse than no label. Below the
    review floor the engine must decline, not guess."""
    for pdf in sample:
        m = feature_matrix.build(unit_assembly.assemble(pdf, [1, 2]))
        out = role_title.assign(m)
        if out["confidence"] == "escalate":
            assert out["value"] is None


def test_excluded_units_record_a_reason(sample):
    """Nothing is silently dropped: an ineligible unit stays in the audit record
    with the rule that excluded it."""
    for pdf in sample:
        m = feature_matrix.build(unit_assembly.assemble(pdf, [1]))
        out = role_title.assign(m)
        for e in out["excluded"]:
            assert e["reasons"], f"{pdf}: unit {e['unit_id']} excluded with no reason"


def test_winner_is_traceable_to_spans(sample):
    for pdf in sample:
        m = feature_matrix.build(unit_assembly.assemble(pdf, [1, 2]))
        out = role_title.assign(m)
        if out.get("unit_id"):
            assert out["source_span_ids"]
            assert out["contributions"], "no per-feature audit trail for the winner"


def test_title_quality_has_not_regressed():
    """Gate the measured quality of the slice."""
    import json
    import evaluate_titles as et
    baseline_path = os.path.join(ROOT, "goldens", "baseline_titles.json")
    if not os.path.exists(baseline_path) or not os.path.isdir(CORPUS):
        pytest.skip("no titles baseline or corpus")
    r = et.evaluate(CORPUS, os.path.join(ROOT, "goldens", "labels.csv"))
    if r is None:
        pytest.skip("no labels")
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    ok, lines = et.compare(r, baseline)
    assert ok, "TITLE extraction regressed:\n" + "\n".join(lines)
