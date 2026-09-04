"""
Build the frozen IDF table used by the BM25 evidence source.

Why frozen
----------
IDF is corpus state. Computed live, the same PDF would produce a different label
as the corpus grew, and the audit ledger would be unreproducible. So the table is
built once, versioned, content-hashed with the other contracts, and shipped as an
input. Every label records the IDF version it was scored against.

What it buys
------------
Terms common across the corpus contribute almost nothing to a phrase's score, so
institutional boilerplate stops outranking the document's own heading. On the
198-document benchmark, `states` appears in 33% of documents, `national` and `department` in
32%, and `united` in 30%; a candidate made only of such terms now scores low.

Note on corpus choice: this benchmark is 198 *unrelated* documents from many
issuers, so its IDF mostly rediscovers the stopword list. Built over a single
firm's intake - the same letterhead and caption on every file - the effect is
far stronger. Rebuild against production intake when it exists.

    python tools/build_idf.py --corpus "<folder of PDFs>" --pages 1 2
"""

import argparse
import collections
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unit_assembly  # noqa: E402
from identity import lexicon as lex  # noqa: E402

OUT_PATH = os.path.join("contracts", "idf.json")


def build(corpus_dir, pages, version):
    import glob
    files = sorted(glob.glob(os.path.join(corpus_dir, "*.pdf")))
    if not files:
        raise SystemExit(f"no PDFs in {corpus_dir}")

    df = collections.Counter()
    lengths = []
    n = 0
    for path in files:
        try:
            units = unit_assembly.assemble(path, list(pages))
        except Exception:
            continue
        n += 1
        terms = []
        for u in units:
            terms.extend(w for w in lex.ir_tokens(u["text"]) if w not in lex.STOPWORDS)
        lengths.append(len(terms) or 1)
        df.update(set(terms))

    if not n:
        raise SystemExit("no documents could be read")

    # Okapi IDF with the +1 smoothing that keeps every value positive: a term in
    # every document should contribute little, never negative.
    idf = {t: round(math.log((n - c + 0.5) / (c + 0.5) + 1.0), 6)
           for t, c in df.items()}

    payload = {
        "version": version,
        "built_from": os.path.basename(os.path.abspath(corpus_dir)),
        "documents": n,
        "pages_per_document": list(pages),
        "terms": len(idf),
        "avgdl": round(sum(lengths) / len(lengths), 2),
        "default_idf": round(math.log((n + 0.5) / 0.5 + 1.0), 6),
        # Sorted so the file is byte-stable across runs and diffs cleanly.
        "idf": {t: idf[t] for t in sorted(idf)},
    }
    return payload, df, n


def main():
    ap = argparse.ArgumentParser(description="Build the frozen IDF table")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--pages", nargs="+", type=int, default=[1, 2])
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--version", default=None,
                    help="defaults to IDF-<corpus>-<n>docs")
    args = ap.parse_args()

    t0 = time.time()
    version = args.version
    payload, df, n = build(args.corpus, args.pages, version or "pending")
    if version is None:
        payload["version"] = f"IDF-{payload['built_from'].replace(' ', '_')}-{n}docs"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
        f.write("\n")

    print(f"IDF table -> {args.out}")
    print(f"  version   {payload['version']}")
    print(f"  documents {payload['documents']}   terms {payload['terms']}"
          f"   avgdl {payload['avgdl']}   ({time.time()-t0:.1f}s)")
    common = [(t, c) for t, c in df.most_common(10)]
    print("  most common terms (now heavily discounted):")
    for t, c in common:
        print(f"    df={c:3d} ({100*c/n:2.0f}%)  idf={payload['idf'][t]:.3f}  {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
