"""
ExhibitPro - Goldens: harvest title ground truth from PDF metadata

Why this exists
---------------
Stage 2 onwards needs to be measured against known-correct titles. Typing those
out by hand for a whole corpus is slow and unnecessary: many PDFs already carry
an author-declared title in their document info dictionary. On the 198-document
benchmark corpus, 74 (37%) carry a usable one.

These labels are TEST data, never training data. The engine does not read them
at build time or at run time; they exist so we can tell whether its output is
right. Nothing here changes how a label is produced.

The coverage bias is the important part
---------------------------------------
Metadata is not evenly distributed. On the benchmark corpus:

    all documents                    74 / 198   (37%)
    the 26 identified legal docs      6 /  26   (23%)
    documents with scanned p1-2       0 /   6   ( 0%)

Modern digitally-authored PDFs carry metadata; scans and court filings do not.
So this harvest covers the EASY cases and systematically misses the hard ones.
Calibrating escalation thresholds on it alone would set them optimistically and
under-escalate on exactly the documents that most need human review.

Every row therefore records `source`, and the coverage report prints what is
still missing, so the residual hand-labelling can be aimed at the gap instead of
at the whole corpus.

Output: goldens/labels.csv
    document_id,filename,title,source,confidence,needs_review

Usage
-----
    python tools/harvest_titles.py --corpus "<folder of PDFs>" --output goldens/labels.csv
    python tools/harvest_titles.py --corpus "<pdfs>" --census census_json --output goldens/labels.csv
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF is required. Install with: pip install pymupdf", file=sys.stderr)
    raise

# Titles that are really filenames, tool defaults, or placeholders. Anything
# matching is rejected - a wrong label is worse than a missing one, and a
# missing one simply routes the document to the hand-labelling queue.
JUNK_PATTERNS = [
    re.compile(r"^(untitled|no title|document\s*\d*|slide\s*\d+|print job|chapter\s*\d+)", re.I),
    re.compile(r"^microsoft\s+(word|powerpoint|excel)\b", re.I),
    # Any filename-like extension ending. Metadata is full of these:
    # "703 text_all.word", "H-3 text_all.word", "Megafauna Communities.p65".
    # A real title does not end in a two-to-six character dotted suffix; the
    # exception, an abbreviation like "Inc." or "U.S.", ends in the dot itself.
    re.compile(r"\.[A-Za-z0-9]{2,6}\s*$"),
    re.compile(r"text_all|final[_ ]?draft|^copy of", re.I),
    re.compile(r"^[a-z0-9_\-]{1,20}$", re.I),          # bare token, e.g. "fy09chart"
    re.compile(r"^\s*$"),
]
MIN_TITLE_CHARS = 12
MIN_TITLE_WORDS = 2


def is_usable(title: str) -> bool:
    t = (title or "").strip()
    if len(t) < MIN_TITLE_CHARS or len(t.split()) < MIN_TITLE_WORDS:
        return False
    return not any(rx.search(t) for rx in JUNK_PATTERNS)


def document_id(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def harvest(corpus_dir, census_dir=None):
    rows, gaps = [], []
    for path in sorted(glob.glob(os.path.join(corpus_dir, "*.pdf"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        doc = fitz.open(path)
        raw = ((doc.metadata or {}).get("title") or "").strip()
        toc = []
        try:
            toc = doc.get_toc() or []
        except Exception:
            pass
        doc.close()

        title, source = "", ""
        if is_usable(raw):
            title, source = re.sub(r"\s+", " ", raw), "pdf_metadata"
        elif toc and is_usable(toc[0][1]):
            # First bookmark is a weaker signal than /Title - it often names the
            # first SECTION rather than the document - so it is marked for review.
            title, source = re.sub(r"\s+", " ", toc[0][1]), "pdf_outline"

        scanned = None
        if census_dir:
            cpath = os.path.join(census_dir, stem + ".json")
            if os.path.exists(cpath):
                with open(cpath, encoding="utf-8") as f:
                    c = json.load(f)
                scanned = any(p["modality"] == "scanned" for p in c["pages"][:2])

        if title:
            rows.append({
                "document_id": document_id(path),
                "filename": os.path.basename(path),
                "title": title,
                "source": source,
                "confidence": "high" if source == "pdf_metadata" else "medium",
                "needs_review": "no" if source == "pdf_metadata" else "yes",
            })
        else:
            gaps.append((os.path.basename(path), scanned))
    return rows, gaps


def main():
    ap = argparse.ArgumentParser(description="Harvest title ground truth from PDF metadata")
    ap.add_argument("--corpus", required=True, help="Folder of source PDFs")
    ap.add_argument("--output", default=os.path.join("goldens", "labels.csv"))
    ap.add_argument("--census", help="Census folder, to report scanned coverage gaps")
    args = ap.parse_args()

    rows, gaps = harvest(args.corpus, args.census)
    total = len(rows) + len(gaps)
    if total == 0:
        print(f"No PDFs found in {args.corpus}", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    fields = ["document_id", "filename", "title", "source", "confidence", "needs_review"]
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["filename"]):
            w.writerow(r)

    by_source = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1

    print(f"Harvested {len(rows)} / {total} titles ({100*len(rows)/total:.0f}%) -> {args.output}")
    for src, n in sorted(by_source.items()):
        print(f"  {src:16s} {n}")
    print(f"\nCoverage gap: {len(gaps)} documents need a hand-written title.")
    scanned_gap = [g for g, s in gaps if s]
    if args.census:
        print(f"  of those, {len(scanned_gap)} have scanned first pages "
              f"(metadata never covers these - prioritise them)")
    print("  These are the hard cases. Label THESE, not the whole corpus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
