"""
Evaluate the Identity Composer, and prove each evidence source earns its weight.

Two questions, and the second is the one that keeps this honest:

  1. How good are the labels?  top1 / top3 accuracy against goldens/labels.csv.
  2. Does each evidence source actually contribute?  --ablate re-runs the whole
     corpus with one source disabled at a time and reports what accuracy loses.

Fusing several plausible-sounding signals is exactly the setup where a component
gets a weight it has not earned. A source whose ablation costs nothing is not
evidence; it is decoration, and the report says so.

Matching
--------
Token-set F1 against the harvested title, threshold in MATCH_F1. Exact string
equality would be wrong: a PDF's declared /Title and the title as typeset
legitimately differ in case, punctuation and trailing qualifiers.

    python tools/evaluate_labels.py --corpus "<pdfs>"
    python tools/evaluate_labels.py --corpus "<pdfs>" --ablate
    python tools/evaluate_labels.py --corpus "<pdfs>" --show 12
    python tools/evaluate_labels.py --corpus "<pdfs>" --check
"""

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unit_assembly  # noqa: E402
from identity import composer  # noqa: E402
from identity import presentability as _pres  # noqa: E402

LABELS = os.path.join("goldens", "labels.csv")
BASELINE = os.path.join("goldens", "baseline_identity.json")
TOLERANCE = 0.01
MATCH_F1 = 0.70

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


def load_units(corpus_dir, labels_path=LABELS, pages=(1, 2), limit=None):
    """Parse every labelled PDF once.

    Assembly dominates the runtime, and an ablation re-scores the same corpus
    once per evidence source. Parsing per run made the sweep eight times slower
    than it needed to be for no additional information.
    """
    labels = load_labels(labels_path)
    cache = {}
    for filename in sorted(labels):
        pdf = os.path.join(corpus_dir, filename)
        if not os.path.exists(pdf):
            continue
        if limit and len(cache) >= limit:
            break
        try:
            cache[filename] = unit_assembly.assemble(pdf, list(pages))
        except Exception as e:
            cache[filename] = e
    return labels, cache


def evaluate(corpus_dir, labels_path=LABELS, enabled=None, pages=(1, 2), limit=None,
             cache=None):
    if cache is None:
        labels, cache = load_units(corpus_dir, labels_path, pages, limit)
    else:
        labels = load_labels(labels_path)
    if not labels:
        return None
    rows = []
    for filename, units in sorted(cache.items()):
        truth = labels[filename]
        try:
            if isinstance(units, Exception):
                raise units
            out = composer.compose(units, enabled=enabled)
        except Exception as e:
            rows.append({"filename": filename, "truth": truth["title"],
                         "top1": None, "f1_top1": 0.0, "f1_best": 0.0,
                         "confidence": "error", "error": str(e)[:120],
                         "candidates": 0, "labels": []})
            continue
        got = [lb["text"] for lb in out["labels"]]
        f1s = [token_f1(truth["title"], g) for g in got]
        rows.append({
            "filename": filename,
            "truth": truth["title"],
            "top1": got[0] if got else None,
            "labels": got,
            "f1_top1": round(f1s[0], 4) if f1s else 0.0,
            "f1_best": round(max(f1s), 4) if f1s else 0.0,
            "best_rank": (f1s.index(max(f1s)) + 1) if f1s else None,
            "confidence": out["confidence"],
            "margin": out.get("margin"),
            "candidates": out.get("candidates", 0),
            "levels": [lb["hierarchy_level"] for lb in out["labels"]],
            "top1_words": len(got[0].split()) if got else 0,
            "top1_presentability": out["labels"][0]["presentability"] if got else 0.0,
        })

    n = len(rows)
    if not n:
        return None
    top1 = sum(1 for r in rows if r["f1_top1"] >= MATCH_F1)
    top3 = sum(1 for r in rows if r["f1_best"] >= MATCH_F1)
    # Second axis. Accuracy asks "did we reproduce the metadata title"; the
    # mission asks "is this printable on a binder tab". Those diverge for the
    # 39% of ground-truth titles that run past 8 words, so both are reported.
    shaped = sum(1 for r in rows if 2 <= r.get("top1_words", 0) <= 12)
    return {
        "documents": n,
        "binder_shaped_rate": round(shaped / n, 4),
        "mean_presentability": round(
            sum(r.get("top1_presentability", 0.0) for r in rows) / n, 4),
        "mean_label_words": round(sum(r.get("top1_words", 0) for r in rows) / n, 1),
        "top1_accuracy": round(top1 / n, 4),
        "top3_accuracy": round(top3 / n, 4),
        "mean_f1_top1": round(sum(r["f1_top1"] for r in rows) / n, 4),
        "errors": sum(1 for r in rows if r["confidence"] == "error"),
        "mean_candidates": round(sum(r["candidates"] for r in rows) / n, 1),
        "match_f1_threshold": MATCH_F1,
        "enabled_sources": sorted(enabled) if enabled else "all",
        "composer_version": composer.COMPOSER_VERSION,
        "rows": rows,
    }


GATED = ["top1_accuracy", "top3_accuracy"]


def compare(result, baseline):
    lines, ok = [], True
    for k in GATED:
        now, was = result[k], baseline.get(k)
        if was is None:
            lines.append(f"  {k:18s} {now:.4f}  (no baseline)")
            continue
        d = now - was
        if d < -TOLERANCE:
            ok = False
            lines.append(f"  {k:18s} {now:.4f}  was {was:.4f}  {d:+.4f}  REGRESSION")
        else:
            lines.append(f"  {k:18s} {now:.4f}  was {was:.4f}  {d:+.4f}")
    return ok, lines


def ablate(corpus_dir, labels_path, pages, limit):
    """Disable one source at a time and report what accuracy loses.

    A source whose removal costs nothing is not evidence. This is the report
    that decides whether a weight in the contract is defensible.
    """
    labels, cache = load_units(corpus_dir, labels_path, pages, limit)
    full = evaluate(corpus_dir, labels_path, None, pages, limit, cache)
    if full is None:
        return None
    print(f"\nABLATION over {full['documents']} documents "
          f"(full model: top1 {full['top1_accuracy']:.3f}, top3 {full['top3_accuracy']:.3f})")
    print(f"\n  {'disabled source':18s} {'top1':>7s} {'delta':>8s} {'top3':>7s} {'delta':>8s}   verdict")
    results = {}
    for src in composer.SOURCES:
        enabled = [s for s in composer.SOURCES if s != src]
        r = evaluate(corpus_dir, labels_path, enabled, pages, limit, cache)
        d1 = r["top1_accuracy"] - full["top1_accuracy"]
        d3 = r["top3_accuracy"] - full["top3_accuracy"]
        # Removing a useful source should HURT, so a negative delta is a source
        # earning its weight.
        if d1 <= -0.02:
            verdict = "load-bearing"
        elif d1 >= 0.02:
            verdict = "HARMFUL - drop or re-weight"
        else:
            verdict = "no measurable effect"
        results[src] = {"top1": r["top1_accuracy"], "delta_top1": round(d1, 4),
                        "top3": r["top3_accuracy"], "delta_top3": round(d3, 4),
                        "verdict": verdict}
        print(f"  {src:18s} {r['top1_accuracy']:7.3f} {d1:+8.3f} "
              f"{r['top3_accuracy']:7.3f} {d3:+8.3f}   {verdict}")
    return {"full": {k: full[k] for k in GATED}, "ablation": results}


def main():
    ap = argparse.ArgumentParser(description="Evaluate the Identity Composer")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--labels", default=LABELS)
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--pages", nargs="+", type=int, default=[1, 2])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ablate", action="store_true")
    ap.add_argument("--show", type=int, default=0, help="show N worst misses")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args()

    if args.ablate:
        out = ablate(args.corpus, args.labels, args.pages, args.limit)
        if out and args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
        return 0

    r = evaluate(args.corpus, args.labels, None, args.pages, args.limit)
    if r is None:
        print(f"No labels at {args.labels}", file=sys.stderr)
        return 2

    print(f"Identity Composer: {r['documents']} documents "
          f"(match at token-F1 >= {MATCH_F1})")
    print(f"  top1 accuracy   {r['top1_accuracy']:.3f}")
    print(f"  top3 accuracy   {r['top3_accuracy']:.3f}   "
          f"(the correct label appears somewhere in the three offered)")
    print(f"  mean F1 (top1)  {r['mean_f1_top1']:.3f}")
    print(f"  candidates/doc  {r['mean_candidates']}")
    print(f"  -- presentability axis --")
    print(f"  binder-shaped   {r['binder_shaped_rate']:.3f}   (top1 is 2-12 words)")
    print(f"  mean presentability {r['mean_presentability']:.3f}")
    print(f"  mean label words    {r['mean_label_words']}")
    if r["errors"]:
        print(f"  errors          {r['errors']}")

    if args.show:
        worst = sorted((x for x in r["rows"] if x["f1_best"] < MATCH_F1),
                       key=lambda x: x["f1_best"])[:args.show]
        print("\n  worst misses:")
        for w in worst:
            print(f"    {w['filename']}  (f1 {w['f1_best']:.2f})")
            print(f"       truth: {w['truth'][:64]}")
            for i, g in enumerate(w["labels"], 1):
                print(f"       #{i}   : {g[:64]}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(r, f, indent=2, ensure_ascii=False)

    if args.update_baseline:
        payload = {k: r[k] for k in GATED}
        payload["documents"] = r["documents"]
        payload["composer_version"] = r["composer_version"]
        os.makedirs(os.path.dirname(os.path.abspath(args.baseline)), exist_ok=True)
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nBaseline written to {args.baseline}")
        return 0

    if os.path.exists(args.baseline):
        with open(args.baseline, encoding="utf-8") as f:
            ok, lines = compare(r, json.load(f))
        print()
        for ln in lines:
            print(ln)
        if args.check and not ok:
            print("\nFAIL: Identity Composer regressed.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
