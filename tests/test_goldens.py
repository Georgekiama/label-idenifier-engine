"""
ExhibitPro - regression gate

These tests enforce the two guarantees the project sells: that the engine is
DETERMINISTIC, and that it does not silently get worse at finding document
boundaries.

They skip rather than fail when the fixtures are absent, because the fixtures
are regenerated from a PDF corpus that is not committed to the repository.
Build them first:

    python tools/make_compound_fixtures.py --corpus "<pdfs>" --output goldens/fixtures
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import page_census  # noqa: E402
import segmenter  # noqa: E402
import evaluate_segmentation as ev  # noqa: E402

FIXTURES = os.path.join(ROOT, "goldens", "fixtures")
BASELINE = os.path.join(ROOT, "goldens", "baseline.json")
REALISTIC = os.path.join(ROOT, "goldens", "realistic")
BASELINE_REALISTIC = os.path.join(ROOT, "goldens", "baseline_realistic.json")
LABELS = os.path.join(ROOT, "goldens", "labels.csv")

# Both fixture sets are gated. The naive set (unrelated documents concatenated)
# measures the easy case; the realistic set adds continuous Bates productions,
# slip-sheet binders, scanned and rotated inserts, and single-document negative
# controls. A change that helps one and harms the other must be seen, so neither
# stands in for the other.
FIXTURE_SETS = [
    pytest.param(FIXTURES, BASELINE, id="naive"),
    pytest.param(REALISTIC, BASELINE_REALISTIC, id="realistic"),
]


def _has_fixtures(path=FIXTURES):
    return os.path.exists(os.path.join(path, ev.TRUTH_FILENAME))


requires_fixtures = pytest.mark.skipif(
    not _has_fixtures(), reason="goldens/fixtures not built - see tools/make_compound_fixtures.py"
)

_METRIC_CACHE = {}


def metrics_for(path):
    if path not in _METRIC_CACHE:
        _METRIC_CACHE[path] = ev.evaluate(path)
    return _METRIC_CACHE[path]


@pytest.fixture(scope="module")
def metrics():
    return metrics_for(FIXTURES)


# --- Determinism ------------------------------------------------------------

@requires_fixtures
def test_census_is_deterministic():
    """The same PDF must produce byte-identical census output on every run."""
    from pathlib import Path
    pdf = Path(sorted(p for p in os.listdir(FIXTURES) if p.endswith(".pdf"))[0])
    full = Path(FIXTURES) / pdf
    a = json.dumps(page_census.census_pdf(full), sort_keys=True)
    b = json.dumps(page_census.census_pdf(full), sort_keys=True)
    assert a == b


@requires_fixtures
def test_segmentation_is_deterministic():
    """Same census in, byte-identical segment map out - including the audit trail."""
    from pathlib import Path
    name = sorted(p for p in os.listdir(FIXTURES) if p.endswith(".pdf"))[0]
    full = Path(FIXTURES) / name
    census = page_census.census_pdf(full)
    a = json.dumps(segmenter.segment_document(json.loads(json.dumps(census)), full), sort_keys=True)
    b = json.dumps(segmenter.segment_document(json.loads(json.dumps(census)), full), sort_keys=True)
    assert a == b


# --- Structural invariants --------------------------------------------------

@requires_fixtures
def test_segments_tile_the_document_exactly():
    """Segments must cover every page once: no gaps, no overlaps, no lost pages.

    A dropped page is a dropped exhibit.
    """
    from pathlib import Path
    for name in sorted(p for p in os.listdir(FIXTURES) if p.endswith(".pdf")):
        full = Path(FIXTURES) / name
        rec = segmenter.segment_document(page_census.census_pdf(full), None)
        segs = rec["segments"]
        assert segs[0]["start_page"] == 1, name
        assert segs[-1]["end_page"] == rec["total_pages"], name
        for a, b in zip(segs, segs[1:]):
            assert b["start_page"] == a["end_page"] + 1, f"{name}: gap at page {a['end_page']}"
        covered = sum(s["page_count"] for s in segs)
        assert covered == rec["total_pages"], name


@requires_fixtures
def test_head_pages_lie_inside_their_segment():
    """Stage 1 must never be pointed at a page outside the segment it is labelling."""
    from pathlib import Path
    for name in sorted(p for p in os.listdir(FIXTURES) if p.endswith(".pdf")):
        full = Path(FIXTURES) / name
        rec = segmenter.segment_document(page_census.census_pdf(full), None)
        for s in rec["segments"]:
            assert s["head_pages"], f"{name} segment {s['index']} has no head pages"
            assert min(s["head_pages"]) >= s["start_page"], name
            assert max(s["head_pages"]) <= s["end_page"], name


# --- Quality gate -----------------------------------------------------------

@pytest.mark.parametrize("fixtures,baseline_path", FIXTURE_SETS)
def test_no_regression_against_baseline(fixtures, baseline_path):
    """Segmentation quality must not fall below the committed baseline.

    If this fails after a deliberate improvement elsewhere, re-record with:
        python tools/evaluate_segmentation.py --fixtures <set> --update-baseline
    and say why in the commit message.
    """
    if not _has_fixtures(fixtures):
        pytest.skip(f"{fixtures} not built")
    assert os.path.exists(baseline_path), f"no baseline at {baseline_path}"
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    ok, lines = ev.compare(metrics_for(fixtures), baseline)
    assert ok, "segmentation regressed:\n" + "\n".join(lines)


@requires_fixtures
def test_precision_stays_high(metrics):
    """A spurious cut splits one exhibit into two tabs. Guard it explicitly.

    Precision is gated separately from F1 so that a recall improvement cannot
    quietly pay for itself with false boundaries.
    """
    assert metrics["precision"] >= 0.85, metrics


def test_negative_controls_are_not_shredded():
    """Single real documents must not be split into many pieces.

    The realistic set includes 10 unmodified corpus documents with their own
    appendix dividers, figure plates and spacer pages. They contain no true
    seams, so this is the direct guard on the failure mode that recalibrating
    for recall tends to cause.
    """
    if not _has_fixtures(REALISTIC):
        pytest.skip("goldens/realistic not built - see tools/make_realistic_fixtures.py")
    nc = metrics_for(REALISTIC)["negative_controls"]
    assert nc["documents"] > 0, "no negative controls in the fixture set"
    per_doc = nc["false_cuts"] / nc["documents"]
    assert per_doc <= 0.5, f"{per_doc:.2f} false cuts per single document: {nc}"


def test_hard_seams_are_not_missed_silently():
    """A real boundary the engine cannot cut must still reach a human.

    Continuity evidence decides whether we CUT. It must never decide whether a
    seam is SEEN. In a continuous Bates production every page pair looks joined,
    and before this was enforced, 23 of 28 hard seams were absorbed with no cut
    and no review flag at all.
    """
    if not _has_fixtures(REALISTIC):
        pytest.skip("goldens/realistic not built - see tools/make_realistic_fixtures.py")
    hard = metrics_for(REALISTIC)["by_difficulty"].get("hard")
    if not hard:
        pytest.skip("no hard seams in the fixture set")
    assert hard["assisted_recall"] >= 0.45, (
        f"hard seams reaching a human: {hard['assisted_recall']:.3f} - {hard}"
    )


# --- Ground truth -----------------------------------------------------------

def test_harvested_labels_are_well_formed():
    """Every harvested title row must be complete and provenance-tagged."""
    if not os.path.exists(LABELS):
        pytest.skip("goldens/labels.csv not harvested - see tools/harvest_titles.py")
    import csv
    with open(LABELS, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows, "labels.csv is empty"
    for r in rows:
        assert len(r["document_id"]) == 16, r
        assert r["title"].strip(), r
        assert r["source"] in {"pdf_metadata", "pdf_outline", "hand"}, r
        assert r["confidence"] in {"high", "medium", "low"}, r
