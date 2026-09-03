"""
ExhibitPro - Goldens: segmentation evaluation and regression gate

Measures Stage 0.5 against the synthetic compound fixtures, whose boundaries are
known by construction, and compares the result to a committed baseline.

Reports four numbers, and the fourth is the one that matters for the product:

    precision           of the cuts we made, how many were real
    recall              of the real boundaries, how many we found
    f1                  the balance
    assisted_recall     real boundaries either CUT or flagged as a candidate
                        for human review

assisted_recall is the honest measure of the shipped system, because the
engine's contract is not "always cut correctly" - it is "never invent a
boundary silently, and surface the doubtful ones". A boundary that lands in the
review queue is handled. One that is silently absorbed is not.

Exit codes
----------
    0  metrics meet or beat the baseline (within tolerance)
    1  regression against the baseline
    2  fixtures or corpus unavailable - nothing measured

Usage
-----
    # measure and print
    python tools/evaluate_segmentation.py --fixtures goldens/fixtures

    # gate a build against the committed baseline
    python tools/evaluate_segmentation.py --fixtures goldens/fixtures --check

    # record a new baseline (do this deliberately, and say why in the commit)
    python tools/evaluate_segmentation.py --fixtures goldens/fixtures --update-baseline
"""

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import page_census  # noqa: E402
import segmenter  # noqa: E402

BASELINE_PATH = os.path.join("goldens", "baseline.json")
TRUTH_FILENAME = "_truth.json"
# Metrics may drift down by this much before the gate fails. Small enough that a
# real regression trips it, loose enough that a rounding change does not.
TOLERANCE = 0.01


def load_truth(fixtures_dir):
    path = os.path.join(fixtures_dir, TRUTH_FILENAME)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def evaluate(fixtures_dir, threshold=None):
    """Run Stage 0 + Stage 0.5 over the fixtures and score against known seams."""
    meta = load_truth(fixtures_dir)
    if meta is None:
        return None
    truth = meta["documents"]

    if threshold is not None:
        segmenter.BOUNDARY_THRESHOLD = threshold

    tp = fp = fn = assisted = 0
    per_doc = []

    with tempfile.TemporaryDirectory() as tmp:
        for name in sorted(truth):
            pdf = os.path.join(fixtures_dir, name)
            if not os.path.exists(pdf):
                continue
            from pathlib import Path
            census = page_census.census_pdf(Path(pdf))
            rec = segmenter.segment_document(census, Path(pdf))

            predicted = {s["start_page"] for s in rec["segments"] if s["index"] > 1}
            candidates = {c["before_page"] for c in rec["candidate_boundaries"]}
            real = set(truth[name]["seams"])

            hit = predicted & real
            miss = real - predicted
            spurious = predicted - real
            tp += len(hit)
            fp += len(spurious)
            fn += len(miss)
            assisted += len(hit) + len(miss & candidates)

            per_doc.append({
                "document": name,
                "true_seams": len(real),
                "tp": len(hit), "fp": len(spurious), "fn": len(miss),
                "recovered_as_candidate": sorted(miss & candidates),
                "missed_entirely": sorted(miss - candidates),
            })

    total_real = tp + fn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / total_real if total_real else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    assisted_recall = assisted / total_real if total_real else 0.0

    return {
        "fixture_seed": meta.get("seed"),
        "fixture_documents": len(per_doc),
        "true_seams": total_real,
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "assisted_recall": round(assisted_recall, 4),
        "config": {
            "boundary_threshold": segmenter.BOUNDARY_THRESHOLD,
            "candidate_floor": segmenter.CANDIDATE_FLOOR,
            "segmenter_version": segmenter.SEGMENTER_VERSION,
            "census_version": page_census.CENSUS_VERSION,
        },
        "per_document": per_doc,
    }


GATED_METRICS = ["precision", "recall", "f1", "assisted_recall"]


def compare(result, baseline):
    """Return (ok, list of human-readable lines)."""
    lines, ok = [], True
    for k in GATED_METRICS:
        now, was = result[k], baseline.get(k)
        if was is None:
            lines.append(f"  {k:16s} {now:.4f}  (no baseline)")
            continue
        delta = now - was
        if delta < -TOLERANCE:
            ok = False
            lines.append(f"  {k:16s} {now:.4f}  was {was:.4f}  {delta:+.4f}  REGRESSION")
        else:
            lines.append(f"  {k:16s} {now:.4f}  was {was:.4f}  {delta:+.4f}")
    return ok, lines


def main():
    ap = argparse.ArgumentParser(description="Evaluate Stage 0.5 against known-boundary fixtures")
    ap.add_argument("--fixtures", default=os.path.join("goldens", "fixtures"))
    ap.add_argument("--baseline", default=BASELINE_PATH)
    ap.add_argument("--check", action="store_true", help="Fail (exit 1) on regression")
    ap.add_argument("--update-baseline", action="store_true", help="Record current metrics as the baseline")
    ap.add_argument("--threshold", type=float, help="Override BOUNDARY_THRESHOLD for this run")
    ap.add_argument("--report", help="Write the full per-document result to this path")
    args = ap.parse_args()

    result = evaluate(args.fixtures, args.threshold)
    if result is None:
        print(f"No fixtures at {args.fixtures}. Build them first:", file=sys.stderr)
        print("  python tools/make_compound_fixtures.py --corpus <pdfs> --output goldens/fixtures",
              file=sys.stderr)
        return 2

    print(f"Segmentation goldens: {result['fixture_documents']} compound PDFs, "
          f"{result['true_seams']} true seams")
    print(f"  threshold={result['config']['boundary_threshold']} "
          f"segmenter={result['config']['segmenter_version']} "
          f"census={result['config']['census_version']}")
    print(f"  TP {result['tp']}  FP {result['fp']}  FN {result['fn']}")

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"  full report -> {args.report}")

    if args.update_baseline:
        payload = {k: result[k] for k in GATED_METRICS}
        payload["recorded_with"] = result["config"]
        payload["fixture_seed"] = result["fixture_seed"]
        payload["true_seams"] = result["true_seams"]
        os.makedirs(os.path.dirname(os.path.abspath(args.baseline)), exist_ok=True)
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nBaseline written to {args.baseline}:")
        for k in GATED_METRICS:
            print(f"  {k:16s} {result[k]:.4f}")
        return 0

    if not os.path.exists(args.baseline):
        print(f"\nNo baseline at {args.baseline}. Record one with --update-baseline.")
        for k in GATED_METRICS:
            print(f"  {k:16s} {result[k]:.4f}")
        return 0

    with open(args.baseline, encoding="utf-8") as f:
        baseline = json.load(f)
    ok, lines = compare(result, baseline)
    print()
    for ln in lines:
        print(ln)

    if args.check and not ok:
        print("\nFAIL: segmentation regressed against the baseline.", file=sys.stderr)
        return 1
    print("\nOK" if ok else "\n(regression present; not gated - pass --check to fail the build)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
