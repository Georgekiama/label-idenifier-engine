"""
ExhibitPro - Goldens: TITLE extraction evaluation and regression gate

Measures the Stage 1.5 -> 2 -> 3/4 slice against goldens/labels.csv.

Matching
--------
Exact string equality is the wrong test. A PDF's declared /Title and the title
as typeset on page 1 legitimately differ in case, punctuation, line-break
hyphenation and trailing boilerplate. So the comparison is token-set F1 over
normalised content words, and the match threshold is explicit rather than
hidden. Both the strict and lenient rates are reported so the threshold cannot
quietly flatter the result.

Reported metrics
----------------
    coverage        share of documents where a title was emitted at all
    accuracy        correct / documents WITH ground truth
    precision_emit  correct / titles actually emitted (the auto+review path)
    escalation      share where the engine declined to guess

accuracy alone would reward guessing. The engine is allowed to decline, so
precision_emit - was the label right WHEN IT SPOKE - is the number that reflects
what a reviewer experiences.

Usage
-----
    python tools/evaluate_titles.py --corpus "<pdfs>"
    python tools/evaluate_titles.py --corpus "<pdfs>" --check
    python tools/evaluate_titles.py --corpus "<pdfs>" --update-baseline
    python tools/evaluate_titles.py --corpus "<pdfs>" --show-errors 15
"""

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unit_assembly  # noqa: E402
import feature_matrix  # noqa: E402
import role_title  # noqa: E402

LABELS = os.path.join("goldens", "labels.csv")
BASELINE = os.path.join("goldens", "baseline_titles.json")
TOLERANCE = 0.01

MATCH_F1 = 0.70          # token-set F1 at or above this counts as correct
LENIENT_F1 = 0.50

WORD_RE = re.compile(r"[a-z0-9]+")
STOP = frozenset("the a an of and or for to in on at by with from".split())


def tokens(text):
    return {w for w in WORD_RE.findall((text or "").lower())
            if w not in STOP and len(w) > 1}


def token_f1(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    p, r = inter / len(tb), inter / len(ta)
    return 2 * p * r / (p + r)


def load_labels(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["filename"]: r for r in csv.DictReader(f)}


def predict(pdf_path, pages=(1, 2)):
    """Run the slice on one PDF: assemble -> features -> TITLE."""
    units = unit_assembly.assemble(pdf_path, list(pages))
    matrix = feature_matrix.build(units)
    return role_title.assign(matrix)


def evaluate(corpus_dir, labels_path=LABELS, show_errors=0):
    labels = load_labels(labels_path)
    if not labels:
        return None

    results = []
    for filename, row in sorted(labels.items()):
        pdf = os.path.join(corpus_dir, filename)
        if not os.path.exists(pdf):
            continue
        try:
            out = predict(pdf)
        except Exception as e:                       # a crash is a wrong answer
            results.append({"filename": filename, "truth": row["title"],
                            "predicted": None, "confidence": "error",
                            "f1": 0.0, "error": str(e)[:120]})
            continue
        predicted = out.get("value")
        results.append({
            "filename": filename,
            "truth": row["title"],
            "predicted": predicted,
            "candidate": out.get("candidate"),
            "confidence": out["confidence"],
            "margin": out.get("margin"),
            "f1": round(token_f1(row["title"], predicted), 4),
            "candidate_f1": round(token_f1(row["title"], out.get("candidate")), 4),
        })

    n = len(results)
    if not n:
        return None
    emitted = [r for r in results if r["predicted"]]
    correct = [r for r in results if r["f1"] >= MATCH_F1]
    lenient = [r for r in results if r["f1"] >= LENIENT_F1]
    escalated = [r for r in results if r["confidence"] == "escalate"]
    errors = [r for r in results if r["confidence"] == "error"]
    # How often the top candidate was right even when confidence suppressed it -
    # separates a scoring problem from a calibration problem.
    cand_correct = [r for r in results if r.get("candidate_f1", 0) >= MATCH_F1]

    by_state = {}
    for state in ("auto", "review", "escalate", "error"):
        rows = [r for r in results if r["confidence"] == state]
        if rows:
            hit = sum(1 for r in rows if r["f1"] >= MATCH_F1)
            by_state[state] = {"n": len(rows), "correct": hit,
                               "accuracy": round(hit / len(rows), 4)}

    summary = {
        "documents": n,
        "coverage": round(len(emitted) / n, 4),
        "accuracy": round(len(correct) / n, 4),
        "accuracy_lenient": round(len(lenient) / n, 4),
        "precision_emit": round(len(correct) / len(emitted), 4) if emitted else 0.0,
        "escalation_rate": round(len(escalated) / n, 4),
        "errors": len(errors),
        "candidate_accuracy": round(len(cand_correct) / n, 4),
        "match_f1_threshold": MATCH_F1,
        "by_confidence": by_state,
        "feature_set_version": feature_matrix.FEATURE_SET_VERSION,
        "role_version": role_title.ROLE_VERSION,
    }
    if show_errors:
        summary["worst"] = sorted(
            (r for r in results if r["f1"] < MATCH_F1),
            key=lambda r: r["f1"]
        )[:show_errors]
    return summary


GATED = ["accuracy", "precision_emit", "candidate_accuracy"]


def compare(result, baseline):
    lines, ok = [], True
    for k in GATED:
        now, was = result[k], baseline.get(k)
        if was is None:
            lines.append(f"  {k:20s} {now:.4f}  (no baseline)")
            continue
        d = now - was
        if d < -TOLERANCE:
            ok = False
            lines.append(f"  {k:20s} {now:.4f}  was {was:.4f}  {d:+.4f}  REGRESSION")
        else:
            lines.append(f"  {k:20s} {now:.4f}  was {was:.4f}  {d:+.4f}")
    return ok, lines


def main():
    ap = argparse.ArgumentParser(description="Evaluate TITLE extraction against harvested labels")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--labels", default=LABELS)
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--show-errors", type=int, default=0)
    ap.add_argument("--report")
    args = ap.parse_args()

    r = evaluate(args.corpus, args.labels, args.show_errors)
    if r is None:
        print(f"No labels at {args.labels} - run tools/harvest_titles.py", file=sys.stderr)
        return 2

    print(f"TITLE goldens: {r['documents']} documents with ground truth "
          f"(match at token-F1 >= {MATCH_F1})")
    print(f"  coverage         {r['coverage']:.3f}   (a title was emitted)")
    print(f"  accuracy         {r['accuracy']:.3f}   (correct / all documents)")
    print(f"  precision_emit   {r['precision_emit']:.3f}   (correct / titles emitted)")
    print(f"  escalation       {r['escalation_rate']:.3f}   (declined to guess)")
    print(f"  candidate_acc    {r['candidate_accuracy']:.3f}   (top candidate right, before confidence)")
    if r["errors"]:
        print(f"  errors           {r['errors']}")
    print("\n  by confidence state:")
    for state, s in r["by_confidence"].items():
        print(f"    {state:9s} n={s['n']:<4d} correct={s['correct']:<4d} accuracy={s['accuracy']:.3f}")

    if r.get("worst"):
        print("\n  worst misses:")
        for w in r["worst"]:
            t = (w["truth"] or "")[:46]
            p = (w["predicted"] or w.get("candidate") or "<none>")[:46]
            print(f"    [{w['confidence']:8s} f1={w['f1']:.2f}] {w['filename']}")
            print(f"        truth: {t}")
            print(f"        got  : {p}")

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(r, f, indent=2, ensure_ascii=False)

    if args.update_baseline:
        payload = {k: r[k] for k in GATED}
        payload["documents"] = r["documents"]
        payload["recorded_with"] = {"feature_set": r["feature_set_version"],
                                    "role": r["role_version"]}
        os.makedirs(os.path.dirname(os.path.abspath(args.baseline)), exist_ok=True)
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nBaseline written to {args.baseline}")
        return 0

    if not os.path.exists(args.baseline):
        print(f"\nNo baseline at {args.baseline}. Record one with --update-baseline.")
        return 0

    with open(args.baseline, encoding="utf-8") as f:
        baseline = json.load(f)
    ok, lines = compare(r, baseline)
    print()
    for ln in lines:
        print(ln)
    if args.check and not ok:
        print("\nFAIL: TITLE extraction regressed.", file=sys.stderr)
        return 1
    print("\nOK" if ok else "\n(regression present; pass --check to fail the build)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
