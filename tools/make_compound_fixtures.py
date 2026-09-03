"""
ExhibitPro - Goldens: synthetic compound fixture generator

Why this exists
---------------
Production intake is compound: one PDF holding a motion, its exhibits, a medical
record, a scanned statement. The engine's job is to find the seams. But you
cannot measure seam-finding on a corpus of single documents - every cut is a
false positive by construction, so precision looks perfect and RECALL IS
INVISIBLE. Tuning against that corpus drives the engine toward "never cut",
which scores beautifully and fails completely in production.

This builds compound PDFs by concatenating known source documents. The
boundaries are therefore known BY CONSTRUCTION - no hand-labelling, no
judgement calls, no human in the loop. That makes recall measurable.

The first run of this fixture against the shipped segmenter found recall of
0.481 against precision of 0.956: the engine was silently missing more than half
of all real document boundaries.

Determinism
-----------
The RNG is seeded from --seed (default 20260903), so the same seed and the same
source corpus always produce byte-identical fixtures and an identical truth
file. Regenerating is free; the fixtures themselves are not committed.

Usage
-----
    python tools/make_compound_fixtures.py \
        --corpus "<folder of source PDFs>" \
        --output goldens/fixtures
"""

import argparse
import glob
import json
import os
import random
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF is required. Install with: pip install pymupdf", file=sys.stderr)
    raise

DEFAULT_SEED = 20260903
DEFAULT_COUNT = 40
DEFAULT_MIN_PARTS = 3
DEFAULT_MAX_PARTS = 6
# Cap pages taken from each source so one long report cannot dominate a fixture.
DEFAULT_MAX_PAGES = 25

TRUTH_FILENAME = "_truth.json"


def build(corpus_dir, out_dir, seed, count, min_parts, max_parts, max_pages):
    files = sorted(glob.glob(os.path.join(corpus_dir, "*.pdf")))
    if len(files) < max_parts:
        raise SystemExit(f"Need at least {max_parts} source PDFs in {corpus_dir}, found {len(files)}")

    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(seed)
    truth = {}

    for i in range(count):
        out = fitz.open()
        picks = rng.sample(files, rng.randint(min_parts, max_parts))
        seams, cursor, parts = [], 0, []
        for path in picks:
            src = fitz.open(path)
            n = min(src.page_count, max_pages)
            if n == 0:
                src.close()
                continue
            if cursor:
                # 1-based page number that OPENS the next sub-document. This is
                # exactly the convention the segment map uses for start_page, so
                # predicted and true seams compare directly.
                seams.append(cursor + 1)
            out.insert_pdf(src, from_page=0, to_page=n - 1)
            parts.append({"file": os.path.basename(path), "pages": n,
                          "start_page": cursor + 1, "end_page": cursor + n})
            cursor += n
            src.close()

        name = f"compound_{i:03d}.pdf"
        out.save(os.path.join(out_dir, name))
        out.close()
        truth[name] = {"seams": seams, "total_pages": cursor, "parts": parts}

    meta = {
        "seed": seed,
        "count": count,
        "min_parts": min_parts,
        "max_parts": max_parts,
        "max_pages_per_part": max_pages,
        "source_corpus_files": len(files),
        "documents": truth,
    }
    with open(os.path.join(out_dir, TRUTH_FILENAME), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)

    seams = sum(len(v["seams"]) for v in truth.values())
    pages = sum(v["total_pages"] for v in truth.values())
    print(f"{len(truth)} compound PDFs | {seams} true seams | {pages} pages -> {out_dir}")
    return meta


def main():
    ap = argparse.ArgumentParser(description="Build synthetic compound PDF fixtures with known boundaries")
    ap.add_argument("--corpus", required=True, help="Folder of source PDFs")
    ap.add_argument("--output", required=True, help="Folder to write fixtures into")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--min-parts", type=int, default=DEFAULT_MIN_PARTS)
    ap.add_argument("--max-parts", type=int, default=DEFAULT_MAX_PARTS)
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = ap.parse_args()
    build(args.corpus, args.output, args.seed, args.count,
          args.min_parts, args.max_parts, args.max_pages)


if __name__ == "__main__":
    main()
