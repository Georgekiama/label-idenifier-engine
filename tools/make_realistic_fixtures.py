"""
ExhibitPro - Goldens: realistic compound intake fixtures

Why the naive fixture was not enough
------------------------------------
tools/make_compound_fixtures.py concatenates randomly chosen, unrelated PDFs.
Every seam there is an easy seam: different producer, different fonts, different
page size. Real intake is harder in two specific ways, and both of them matter
more than anything the naive fixture measures.

  1. Real seams are often INVISIBLE to the change signals. In a discovery
     production, one continuous Bates run spans every sub-document, so
     bates_consecutive fires as CONTINUITY and actively argues against the cut.
     Consecutive filings from the same firm share fonts, page size and modality,
     so nothing changes at the seam except the words.

  2. Real documents contain internal structure that LOOKS like a seam. Appendix
     dividers, landscape figure plates, scanned inserts, "intentionally left
     blank" spacers. Recalibrating against easy seams alone would simply lower
     the threshold until these all split, trading a recall number for a
     precision collapse nobody measured.

So this generator builds four scenarios, records the DIFFICULTY of every seam,
and includes negative controls with no seams at all.

Scenarios
---------
  exhibit_binder      cover/transmittal, then [slip sheet + exhibit] x N.
                      Slip sheets announce each boundary.        -> easy
  bates_production    N documents under ONE continuous Bates run,
                      stamped across every page.                 -> hard
  mixed_intake        different producers: native filing, scanned
                      exhibit, landscape plate.                  -> medium
  single_document     ONE real corpus document, unmodified, with
                      whatever internal structure it already has.
                      Zero true seams; every cut is a false
                      positive.                                  -> negative control

Determinism
-----------
Seeded from --seed. Same seed plus same corpus gives byte-identical fixtures and
an identical truth file. Fixtures are not committed; the seed is.

Usage
-----
    python tools/make_realistic_fixtures.py \
        --corpus "<folder of source PDFs>" \
        --output goldens/realistic
"""

import argparse
import collections
import glob
import json
import os
import random
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF is required. Install with: pip install pymupdf", file=sys.stderr)
    raise

DEFAULT_SEED = 20260903
TRUTH_FILENAME = "_truth.json"

# Bates stamp appearance. Bottom-right, small, matching real production stamps.
BATES_FONT = "cour"
BATES_SIZE = 9
BATES_MARGIN_X = 130
BATES_MARGIN_Y = 26
BATES_PREFIXES = ["ACME", "GLOBEX", "INITECH", "UMBRLA", "STARK"]

SLIP_DESIGNATORS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]

MAX_PAGES_PER_PART = 20
SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")


# --- corpus profiling -------------------------------------------------------

def profile_corpus(files):
    """Cheap per-document profile used to pick SIMILAR or DISSIMILAR documents.

    Page-1 font families plus size class is enough to tell "same producer" from
    "different producer" without parsing spans.
    """
    profiles = {}
    for path in files:
        try:
            d = fitz.open(path)
            if d.page_count == 0:
                d.close()
                continue
            pg = d.load_page(0)
            fams = frozenset(
                SUBSET_PREFIX_RE.sub("", f[3]).split(",")[0]
                for f in pg.get_fonts(full=False) if len(f) > 3 and f[3]
            )
            r = pg.rect
            native = bool(pg.get_text("text").strip())
            profiles[path] = {
                "fonts": fams,
                "size": (round(r.width / 10), round(r.height / 10)),
                "native": native,
                "pages": d.page_count,
            }
            d.close()
        except Exception:
            continue
    return profiles


def similar_group(profiles, rng, want):
    """Return `want` documents that share a font profile - the same-producer case."""
    buckets = collections.defaultdict(list)
    for path, p in profiles.items():
        if p["fonts"] and p["native"]:
            buckets[(p["fonts"], p["size"])].append(path)
    usable = sorted((k for k, v in buckets.items() if len(v) >= want),
                    key=lambda k: (-len(buckets[k]), str(sorted(k[0]))))
    if not usable:
        return None
    key = usable[rng.randrange(min(len(usable), 5))]
    pool = sorted(buckets[key])
    return rng.sample(pool, want)


# --- page construction ------------------------------------------------------

def add_slip_sheet(out, designator, width=612.0, height=792.0):
    """A real exhibit divider: one short centred line, nothing else."""
    page = out.new_page(width=width, height=height)
    text = f"EXHIBIT {designator}"
    fs = 28
    tw = fitz.get_text_length(text, fontname="helv", fontsize=fs)
    page.insert_text(((width - tw) / 2, height / 2), text, fontname="helv", fontsize=fs)
    return page


def add_cover_page(out, title, width=612.0, height=792.0):
    page = out.new_page(width=width, height=height)
    y = 160
    for line, fs in [(title, 20), ("", 0), ("Produced in response to", 11),
                     ("Request for Production of Documents", 11)]:
        if not line:
            y += 24
            continue
        tw = fitz.get_text_length(line, fontname="helv", fontsize=fs)
        page.insert_text(((width - tw) / 2, y), line, fontname="helv", fontsize=fs)
        y += fs + 14
    return page


def stamp_bates(doc, prefix, start, digits=6):
    """Stamp a continuous Bates run across EVERY page of the document.

    This is the signal that makes real production boundaries hard: the run is
    continuous across sub-document seams, so the engine's bates_consecutive
    continuity signal fires at exactly the places it should be cutting.
    """
    n = start
    for page in doc:
        r = page.rect
        page.insert_text(
            (r.width - BATES_MARGIN_X, r.height - BATES_MARGIN_Y),
            f"{prefix}{n:0{digits}d}",
            fontname=BATES_FONT, fontsize=BATES_SIZE,
        )
        n += 1
    return n


def append(out, path, max_pages=MAX_PAGES_PER_PART, rotate=None):
    src = fitz.open(path)
    n = min(src.page_count, max_pages)
    if n == 0:
        src.close()
        return 0
    before = out.page_count
    out.insert_pdf(src, from_page=0, to_page=n - 1)
    src.close()
    if rotate:
        for i in range(before, before + n):
            out.load_page(i).set_rotation(rotate)
    return n


# --- scenarios --------------------------------------------------------------

def scenario_exhibit_binder(out, rng, profiles, files):
    """Cover page, then slip-sheet-introduced exhibits. Boundaries announced."""
    seams, parts, detail = [], [], []
    add_cover_page(out, "EXHIBITS TO DECLARATION")
    cursor = 1
    for i in range(rng.randint(3, 5)):
        seams.append(cursor + 1)
        detail.append({"page": cursor + 1, "difficulty": "easy",
                       "reason": "slip_sheet_announces_boundary"})
        add_slip_sheet(out, SLIP_DESIGNATORS[i])
        cursor += 1
        src = rng.choice(files)
        n = append(out, src, max_pages=12)
        parts.append({"file": os.path.basename(src), "pages": n, "start_page": cursor + 1})
        cursor += n
    return seams, parts, detail


def scenario_bates_production(out, rng, profiles, files):
    """One continuous Bates run over several same-producer documents."""
    seams, parts, detail = [], [], []
    picks = similar_group(profiles, rng, rng.randint(3, 4))
    same_producer = picks is not None
    if not same_producer:
        picks = rng.sample(files, rng.randint(3, 4))
    cursor = 0
    for src in picks:
        n = append(out, src, max_pages=14)
        if n == 0:
            continue
        if cursor:
            seams.append(cursor + 1)
            detail.append({
                "page": cursor + 1,
                "difficulty": "hard",
                "reason": "continuous_bates_run" + ("_same_producer" if same_producer else ""),
            })
        parts.append({"file": os.path.basename(src), "pages": n, "start_page": cursor + 1})
        cursor += n
    prefix = BATES_PREFIXES[rng.randrange(len(BATES_PREFIXES))]
    stamp_bates(out, prefix, rng.randint(1, 400))
    return seams, parts, detail


def scenario_mixed_intake(out, rng, profiles, files):
    """Different producers, including a scanned exhibit and a landscape plate."""
    seams, parts, detail = [], [], []
    native = [f for f in files if profiles.get(f, {}).get("native")]
    scanned = [f for f in files if f in profiles and not profiles[f]["native"]]
    plan = []
    plan.append((rng.choice(native or files), None, "native_filing"))
    if scanned:
        plan.append((rng.choice(scanned), None, "scanned_exhibit"))
    plan.append((rng.choice(native or files), 90, "landscape_plate"))
    plan.append((rng.choice(native or files), None, "native_filing"))
    rng.shuffle(plan)

    cursor = 0
    for src, rot, kind in plan:
        n = append(out, src, max_pages=10, rotate=rot)
        if n == 0:
            continue
        if cursor:
            seams.append(cursor + 1)
            detail.append({"page": cursor + 1, "difficulty": "medium",
                           "reason": f"producer_change_{kind}"})
        parts.append({"file": os.path.basename(src), "pages": n,
                      "start_page": cursor + 1, "kind": kind})
        cursor += n
    return seams, parts, detail


def scenario_single_document(out, rng, profiles, files):
    """NEGATIVE CONTROL. One real document, untouched, internal structure intact.

    Long documents are preferred because they are the ones carrying appendix
    dividers, figure plates and spacer pages - the structures most likely to be
    mistaken for document boundaries. There are no true seams here; every cut
    the engine makes is a false positive.
    """
    long_docs = sorted(f for f, p in profiles.items() if p["pages"] >= 30)
    src = rng.choice(long_docs or files)
    n = append(out, src, max_pages=120)
    return [], [{"file": os.path.basename(src), "pages": n, "start_page": 1}], []


SCENARIOS = {
    "exhibit_binder": scenario_exhibit_binder,
    "bates_production": scenario_bates_production,
    "mixed_intake": scenario_mixed_intake,
    "single_document": scenario_single_document,
}
# Weighted so the negative control is a real share of the set: precision must be
# measured against hard negatives, not assumed.
SCENARIO_PLAN = (["exhibit_binder"] * 10 + ["bates_production"] * 12
                 + ["mixed_intake"] * 12 + ["single_document"] * 10)


def build(corpus_dir, out_dir, seed):
    files = sorted(glob.glob(os.path.join(corpus_dir, "*.pdf")))
    if len(files) < 10:
        raise SystemExit(f"Need at least 10 source PDFs in {corpus_dir}, found {len(files)}")

    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(seed)
    print(f"Profiling {len(files)} source PDFs...")
    profiles = profile_corpus(files)

    truth = {}
    plan = list(SCENARIO_PLAN)
    rng.shuffle(plan)

    for i, scenario in enumerate(plan):
        out = fitz.open()
        seams, parts, detail = SCENARIOS[scenario](out, rng, profiles, files)
        if out.page_count == 0:
            out.close()
            continue
        name = f"real_{i:03d}_{scenario}.pdf"
        out.save(os.path.join(out_dir, name))
        total = out.page_count
        out.close()
        truth[name] = {
            "scenario": scenario,
            "total_pages": total,
            "seams": seams,
            "seam_detail": detail,
            "parts": parts,
        }

    by_scenario = collections.Counter(v["scenario"] for v in truth.values())
    by_difficulty = collections.Counter(
        d["difficulty"] for v in truth.values() for d in v["seam_detail"]
    )
    meta = {
        "seed": seed,
        "generator": "make_realistic_fixtures",
        "scenarios": dict(by_scenario),
        "seams_by_difficulty": dict(by_difficulty),
        "documents": truth,
    }
    with open(os.path.join(out_dir, TRUTH_FILENAME), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)

    seams = sum(len(v["seams"]) for v in truth.values())
    pages = sum(v["total_pages"] for v in truth.values())
    negatives = sum(1 for v in truth.values() if not v["seams"])
    print(f"\n{len(truth)} fixtures | {seams} true seams | {pages} pages -> {out_dir}")
    print(f"  scenarios:  {dict(by_scenario)}")
    print(f"  difficulty: {dict(by_difficulty)}")
    print(f"  negative controls (zero true seams): {negatives}")
    return meta


def main():
    ap = argparse.ArgumentParser(description="Build realistic compound intake fixtures")
    ap.add_argument("--corpus", required=True, help="Folder of source PDFs")
    ap.add_argument("--output", required=True, help="Folder to write fixtures into")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args()
    build(args.corpus, args.output, args.seed)


if __name__ == "__main__":
    main()
