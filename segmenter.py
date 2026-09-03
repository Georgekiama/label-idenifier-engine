"""
ExhibitPro - Deliverable 02b
Stage 0.5: Compound Document Segmentation v1

Purpose
-------
A production intake PDF is rarely one document. It is a motion, then its
exhibits, then a medical record, then a scanned bank statement, then a Bates
production - bound into a single file. Stage 1 cannot label that as one thing.

This stage reads the Page Census (Stage 0) and finds the SEAMS: the page
boundaries where one sub-document ends and the next begins. It emits a segment
map. Stage 1 then deep-extracts only the HEAD PAGES of each segment, and Stages
2-6 produce one label per segment.

No text understanding happens here. Every signal is a structural measurement of
one page against the page before it.

Signal model
------------
Each boundary candidate between page i and page i+1 accumulates a score from
independent signals. Signals are weighted by how much they alone justify a cut:

  DECISIVE (fires alone, weight >= THRESHOLD)
    slip_sheet_ahead        1.60  page i+1 is a near-empty divider page
    bates_prefix_change     1.60  production prefix changes (ABC -> XYZ)

  STRONG (needs corroboration)
    modality_flip           1.00  native <-> scanned
    page_size_change        1.00  letter -> legal -> 11x17
    page_label_restart      1.00  "Page 7 of 9" then "Page 1 of 40"
    font_family_change      1.00  page resource fingerprint diverges

  MEDIUM
    rotation_change         0.80
    running_header_change   0.70  an established running header changes.
                                  Deliberately MEDIUM, not STRONG: a report's
                                  own section headers change too, so this can
                                  surface a seam for review but must never cut
                                  on its own. At 1.00 it raised false cuts on
                                  single-document controls from 0.2 to 1.6 each.
    bates_number_gap        0.70  same prefix, non-consecutive numbering

  WEAK
    blank_page              0.40  page i is blank

  CONTINUITY (negative - evidence the two pages belong to the same document)
    slip_sheet_behind      -1.60  a divider belongs to the document it opens
    running_header_match   -1.20  same header template, digits masked
    running_header_alternates -1.20  recto/verso alternating headers
    page_label_continues   -1.20  "Page 12" -> "Page 13"
    bates_consecutive      -1.20  ABC000041 -> ABC000042
    sentence_continuation  -1.00  prose runs across the break
    running_footer_match   -0.80  same footer template

A CUT requires (CHANGE minus CONTINUITY) >= threshold. A CANDIDATE for human
review requires CHANGE alone >= floor, ignoring continuity entirely - so a
production-wide Bates run can stop the engine cutting, but can never stop a
changed seam reaching a person.

A boundary score is CHANGE minus CONTINUITY, clamped at zero. This is what stops
a report from being split on its own appendix dividers: the running header
"JSC-63743 Page ##" holds every page of it together, so a divider page alone no
longer reaches the threshold, while a genuinely foreign attachment - carrying a
different header - still cuts.

A cut is made at score >= BOUNDARY_THRESHOLD (1.50). This deliberately means a
single STRONG signal is NOT enough - a letter-to-legal page size change inside
one brief must not split it. Two agreeing signals cut; one is recorded as a
CANDIDATE boundary for the audit ledger and for human escalation.

Tier B refinement (the adaptive part)
-------------------------------------
Span-level font analysis costs 33.7 ms/page versus 7.5 ms/page for the census -
too expensive for every page of a 300-page file. So it is spent only where it
can change the outcome: boundaries scoring inside the REFINE_BAND, just under
the threshold. For those, and only those, the two adjacent pages are parsed with
get_text("dict") and their char-weighted font SIZE histograms compared. A large
divergence adds font_size_change (0.60), which can lift a near-miss over the
line. Cost is bounded by the number of ambiguous seams, not by document length.

Determinism
-----------
    - Weights and thresholds are literal constants, stamped into every output as
      "config", and versioned by SEGMENTER_VERSION. Changing any weight REQUIRES
      bumping that version, or prior labels become unreproducible.
    - Signals are evaluated in a fixed order and reported sorted by name.
    - Refinement is driven only by the score band, so the same census always
      triggers the same Tier B calls.
    - Scores rounded to 3dp; no floating-point comparison depends on more.

Output: one JSON per document.

{
  "document_id": "...", "filename": "...", "total_pages": 88,
  "segmenter_version": "1.0.0", "census_version": "1.0.0",
  "config": {...weights and thresholds actually used...},
  "segments": [
    {"index": 1, "start_page": 1, "end_page": 14, "page_count": 14,
     "head_pages": [1, 2], "opened_by": "document_start",
     "boundary_score": null, "signals": []},
    ...
  ],
  "candidate_boundaries": [
    {"before_page": 42, "score": 1.0, "signals": [...]}   // scored, not cut
  ],
  "refined_boundaries": [42],
  "stats": {"segments": 5, "cuts": 4, "candidates": 3, "tier_b_pages": 6}
}

Usage
-----
    python segmenter.py --census "<census folder>" --output "<folder>" \
        [--pdf-dir "<folder of PDFs>"] [--report] [--validate]

    --pdf-dir enables Tier B refinement. Without it, segmentation runs on census
    data alone (still correct, just blind to the ambiguous band).
"""

import argparse
import collections
import json
import statistics
import sys
import time
from pathlib import Path

SEGMENTER_VERSION = "1.4.0"

# --- Tunable model (stamped into every output; bump version if changed) -----

WEIGHTS = {
    "slip_sheet_ahead": 1.60,
    "bates_prefix_change": 1.60,
    "modality_flip": 1.00,
    "page_size_change": 1.00,
    "page_label_restart": 1.00,
    "font_family_change": 1.00,
    "running_header_change": 0.70,
    "rotation_change": 0.80,
    "bates_number_gap": 0.70,
    "font_size_change": 0.60,   # Tier B only
    "blank_page": 0.40,
    # Continuity: negative weights, subtracted from the change score.
    "running_header_match": -1.20,
    "running_header_alternates": -1.20,
    "slip_sheet_behind": -1.60,
    "running_footer_match": -0.80,
    "page_label_continues": -1.20,
    "bates_consecutive": -1.20,
    "sentence_continuation": -1.00,
}
BOUNDARY_THRESHOLD = 1.50
# Scores in this band are ambiguous enough that Tier B may change the outcome.
REFINE_BAND = (0.70, BOUNDARY_THRESHOLD)
# Anything at or above this, but below threshold, is reported for human review.
CANDIDATE_FLOOR = 0.70

# Relative tolerance used only when both pages are size_class "other".
OTHER_SIZE_TOLERANCE = 0.05
# Font family sets are "different" below this overlap coefficient.
FONT_OVERLAP_MIN = 0.50
# Char-weighted font size histograms are "different" above this L1 distance
# (range 0-2; 0 identical, 2 disjoint).
FONT_SIZE_L1_MIN = 0.60
# A head is the segment's first page plus the next, mirroring Stage 1's pages 1-2.
HEAD_PAGE_COUNT = 2
# An interior segment this short, flanked by matching pages, is an insert.
MAX_INSERT_PAGES = 2
# Safety bound on the re-join fixed-point loop.
MAX_REJOIN_PASSES = 50
# Console report only; does not affect output files.
REPORT_MAX_SEGMENTS = 12

REQUIRED_DOC_FIELDS = [
    "document_id", "filename", "total_pages", "segmenter_version",
    "census_version", "config", "segments", "candidate_boundaries", "stats",
]
REQUIRED_SEGMENT_FIELDS = [
    "index", "start_page", "end_page", "page_count", "head_pages",
    "opened_by", "boundary_score", "signals", "reabsorbed_inserts",
]


def font_overlap(a, b) -> float:
    """Szymkiewicz-Simpson overlap coefficient: |A n B| / min(|A|, |B|).

    Deliberately NOT Jaccard. A chapter-opening page often carries an extra
    display face on top of the body fonts, so its font set is a SUPERSET of the
    following page. Jaccard reads that as divergence - on the benchmark corpus
    (NewCenturySchlbk, Times, Times-ExtraBold) vs (Times) scored 0.33 and split
    a document on nearly every page. Overlap scores that pair 1.0, because one
    set is contained in the other, which is continuity rather than change.
    """
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 1.0
    return len(sa & sb) / min(len(sa), len(sb))


def size_changed(p, q) -> bool:
    """Compare pages by their classified size, not raw dimensions.

    Stage 0 already resolved rotation and absorbed scanner jitter into a named
    size class. Two pages differ in size when their class or orientation differs.
    When BOTH are unclassified ("other") there is no name to compare, so fall
    back to a relative dimension test - still generous enough to ignore jitter.
    """
    pc, qc = p.get("size_class", "other"), q.get("size_class", "other")
    if p.get("orientation") != q.get("orientation"):
        return True
    if pc == "other" and qc == "other":
        pw, ph = p["width"], p["height"]
        qw, qh = q["width"], q["height"]
        return (abs(pw - qw) > max(pw, qw) * OTHER_SIZE_TOLERANCE
                or abs(ph - qh) > max(ph, qh) * OTHER_SIZE_TOLERANCE)
    return pc != qc


def score_boundary(pages, i) -> list:
    """Score the seam between pages[i] and pages[i+1].

    Takes the whole page list rather than just the pair, because some continuity
    evidence spans more than two pages - see running_header_alternates.

    Returns a list of {name, weight, detail} for every signal that fired.
    Evaluated in a fixed order so the audit trail is byte-stable.
    """
    prev, nxt = pages[i], pages[i + 1]
    # Header signatures are read once here: they feed a CHANGE signal
    # (running_header_change) as well as the continuity signals below.
    ph, nh = prev.get("header_sig"), nxt.get("header_sig")
    fired = []

    def fire(name, detail):
        fired.append({"name": name, "weight": WEIGHTS[name], "detail": detail})

    # DECISIVE ---------------------------------------------------------------
    if nxt.get("is_slip_sheet"):
        fire("slip_sheet_ahead", {"text_head": nxt.get("text_head", "")[:80]})

    # Only series-validated stamps are trusted. A raw page-level Bates match is
    # not evidence: on the benchmark corpus the unvalidated version matched
    # state+ZIP in mailing addresses and shattered a 1000-page document into 956
    # segments. Stage 0 promotes a match to bates_in_series only when the prefix
    # recurs across pages with constant digit width and ascending numbers.
    pb, nb = prev.get("bates"), nxt.get("bates")
    if pb and nb and prev.get("bates_in_series") and nxt.get("bates_in_series"):
        if pb["prefix"] != nb["prefix"]:
            fire("bates_prefix_change", {"from": pb["prefix"], "to": nb["prefix"]})
        elif nb["number"] - pb["number"] != 1:
            fire("bates_number_gap", {"from": pb["number"], "to": nb["number"]})

    # STRONG -----------------------------------------------------------------
    if prev.get("modality") != nxt.get("modality"):
        fire("modality_flip", {"from": prev.get("modality"), "to": nxt.get("modality")})

    if size_changed(prev, nxt):
        fire("page_size_change", {
            "from": [prev["width"], prev["height"]],
            "to": [nxt["width"], nxt["height"]],
        })

    pl, nl = prev.get("page_label"), nxt.get("page_label")
    if pl and nl and (nl["num"] <= pl["num"] or nl["total"] != pl["total"]):
        fire("page_label_restart", {"from": pl, "to": nl})

    # Only meaningful when both pages actually declare fonts; a scanned page has
    # none, and that case is already covered by modality_flip.
    if prev.get("font_families") and nxt.get("font_families"):
        ov = font_overlap(prev["font_families"], nxt["font_families"])
        if ov < FONT_OVERLAP_MIN:
            fire("font_family_change", {
                "overlap": round(ov, 3),
                "from": prev["font_families"][:6],
                "to": nxt["font_families"][:6],
            })

    # An ESTABLISHED running header that suddenly changes is a new document.
    #
    # This is the only signal that survives a same-producer discovery
    # production, where fonts, page size, modality and the Bates run are all
    # continuous across the real boundaries. On the realistic fixtures those
    # seams carried NO change evidence whatsoever - change_score was 0.00, so
    # they could not even be queued for review.
    #
    # "Established" is load-bearing: the header must have been stable across the
    # previous pair. Otherwise this fires on page 2 of every document, where a
    # title page gives way to the running header. The third condition excludes
    # recto/verso alternation, which is continuity, not change.
    if (i > 0 and ph and nh and ph != nh
            and pages[i - 1].get("header_sig") == ph
            and pages[i - 1].get("header_sig") != nh):
        fire("running_header_change", {"from": ph[:40], "to": nh[:40]})

    # MEDIUM -----------------------------------------------------------------
    if prev.get("rotation") != nxt.get("rotation"):
        fire("rotation_change", {"from": prev.get("rotation"), "to": nxt.get("rotation")})

    # WEAK -------------------------------------------------------------------
    if prev.get("is_blank"):
        fire("blank_page", {"page": prev["page"]})

    # CONTINUITY (negative weights - evidence the pages belong together) ------
    #
    # Scoring only change is not enough. A 112-page NASA report split into 15
    # segments on its own appendix dividers, and a conference proceedings cut
    # mid-sentence, because nothing in the model could argue for staying joined.
    # These signals let a document defend its own integrity.

    # A slip sheet belongs to the document it introduces, never to the one
    # before it. Without this, every divider is cut on BOTH sides and becomes a
    # one-page orphan segment: on the realistic exhibit-binder fixtures that
    # produced 12 false cuts.
    #
    # It must not fire when the NEXT page is itself a divider, or two adjacent
    # short pages (a cover sheet followed by the first exhibit's slip sheet)
    # cancel each other exactly and the real boundary between them disappears.
    if prev.get("is_slip_sheet") and not nxt.get("is_slip_sheet"):
        fire("slip_sheet_behind", {"page": prev["page"]})

    if ph and nh and ph == nh:
        fire("running_header_match", {"sig": ph})
    elif nh and i > 0 and pages[i - 1].get("header_sig") == nh:
        # Recto/verso alternation. Journals and books run one header on the
        # left-hand page and another on the right, so consecutive headers never
        # match while alternate ones always do. On the benchmark corpus this
        # pattern ("... | caspar and penne" alternating with the article title)
        # cut a 226-page article into 7 pieces. Matching against the page two
        # back recognises the alternation as continuity.
        fire("running_header_alternates", {"sig": nh})

    pf, nf = prev.get("footer_sig"), nxt.get("footer_sig")
    if pf and nf and pf == nf and pf != ph:
        fire("running_footer_match", {"sig": pf})

    if pl and nl and nl["num"] == pl["num"] + 1 and nl["total"] == pl["total"]:
        fire("page_label_continues", {"from": pl["num"], "to": nl["num"]})

    if (pb and nb and prev.get("bates_in_series") and nxt.get("bates_in_series")
            and pb["prefix"] == nb["prefix"] and nb["number"] - pb["number"] == 1):
        fire("bates_consecutive", {"from": pb["number"], "to": nb["number"]})

    # Prose running across the page break: the previous page stops without
    # terminal punctuation and the next resumes in lower case.
    if prev.get("ends_mid_sentence") and nxt.get("starts_lowercase"):
        fire("sentence_continuation", {})

    return fired


def total_score(signals) -> float:
    """Net evidence for a cut: change signals minus continuity signals.

    Clamped at zero. A strongly-joined page pair is simply "no boundary"; there
    is no meaningful ranking below that, and letting scores go negative would
    make the candidate band harder to reason about.
    """
    return round(max(0.0, sum(s["weight"] for s in signals)), 3)


def change_score(signals) -> float:
    """Change evidence ALONE, ignoring every continuity signal.

    This exists because continuity must be able to prevent a CUT without being
    able to hide a seam from REVIEW.

    In a discovery production one continuous Bates run spans every sub-document,
    so bates_consecutive fires at every single page pair - including the real
    document boundaries. Netted against change evidence it drove those seams
    below the candidate floor as well as below the cut threshold, so they were
    not merely missed, they were missed SILENTLY: on the realistic fixtures,
    hard-seam recall was 0.179 and assisted recall was also 0.179, meaning not
    one of the 23 missed boundaries reached a human.

    A signal that fires at nearly every page pair carries no discriminative
    information. It is entitled to argue against cutting; it is not entitled to
    suppress the observation that something changed here.
    """
    return round(sum(s["weight"] for s in signals if s["weight"] > 0), 3)


# --- Tier B: span-level font size fingerprint -------------------------------

def size_histogram(page) -> dict:
    """Char-weighted font size histogram for one page, sizes rounded to 0.5pt.

    Weighting by character count rather than span count means one giant heading
    cannot outvote the body text, so the histogram describes what the page is
    mostly SET IN - which is what identifies a document's typography.
    """
    hist = collections.Counter()
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                key = round(span.get("size", 0.0) * 2) / 2.0
                hist[key] += len(text)
    total = sum(hist.values())
    if not total:
        return {}
    return {k: v / total for k, v in hist.items()}


def hist_l1(a, b) -> float:
    """L1 distance between two normalised histograms. 0 = identical, 2 = disjoint."""
    keys = set(a) | set(b)
    return sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def refine(doc_rec, pdf_path, pending):
    """Tier B pass over ambiguous seams only.

    `pending` is a list of (index_of_next_page, signals). Mutates signals in
    place, appending font_size_change where the histograms diverge. Returns the
    number of pages actually parsed.
    """
    import fitz

    wanted = sorted({p for idx, _ in pending for p in (idx, idx + 1)})
    doc = fitz.open(pdf_path)
    try:
        cache = {}
        for pno in wanted:
            if 1 <= pno <= doc.page_count:
                cache[pno] = size_histogram(doc.load_page(pno - 1))
    finally:
        doc.close()

    for idx, signals in pending:
        a, b = cache.get(idx), cache.get(idx + 1)
        if not a or not b:
            continue
        d = hist_l1(a, b)
        if d > FONT_SIZE_L1_MIN:
            signals.append({
                "name": "font_size_change",
                "weight": WEIGHTS["font_size_change"],
                "detail": {"l1": round(d, 3)},
            })
    return len(cache)


# --- Segment assembly -------------------------------------------------------

def pages_continuous(a, b) -> bool:
    """Do two non-adjacent pages look like the same document?

    Used to decide whether whatever sits between them is an INSERT rather than a
    new document. Either a shared running header, or matching presentation
    (same size class, orientation and overlapping fonts) counts as continuity.
    """
    ha, hb = a.get("header_sig"), b.get("header_sig")
    if ha and hb and ha == hb:
        return True
    return (
        a.get("size_class") == b.get("size_class")
        and a.get("orientation") == b.get("orientation")
        and a.get("modality") == b.get("modality")
        and font_overlap(a.get("font_families", []), b.get("font_families", [])) >= FONT_OVERLAP_MIN
    )


def rejoin_inserts(segments, pages_by_no):
    """Re-absorb short interior segments whose neighbours match each other.

    A landscape scan of a figure, a plate, or a fold-out table dropped into the
    middle of a report trips modality_flip and rotation_change and looks exactly
    like a document boundary from the seam alone. But if the page BEFORE it and
    the page AFTER it clearly belong to the same document, then the thing between
    them is an insert, not a new document, and both cuts around it are wrong.

    Runs to a fixed point. Each absorption is recorded on the surviving segment
    so the audit ledger can explain why a cut that scored above threshold did not
    produce a segment.
    """
    absorbed_any = []
    guard = 0
    while len(segments) > 2 and guard < MAX_REJOIN_PASSES:
        guard += 1
        merged = False
        for k in range(1, len(segments) - 1):
            seg = segments[k]
            if seg["page_count"] > MAX_INSERT_PAGES:
                continue
            before = pages_by_no.get(segments[k - 1]["end_page"])
            after = pages_by_no.get(segments[k + 1]["start_page"])
            if not before or not after or not pages_continuous(before, after):
                continue
            host, nxt = segments[k - 1], segments[k + 1]
            record = {
                "pages": [seg["start_page"], seg["end_page"]],
                "dropped_cut_score": seg["boundary_score"],
                "dropped_signals": [x["name"] for x in seg["signals"]],
                "reason": "neighbours_continuous",
            }
            host.setdefault("reabsorbed_inserts", []).append(record)
            host["reabsorbed_inserts"].extend(nxt.pop("reabsorbed_inserts", []))
            absorbed_any.append(record)
            host["end_page"] = nxt["end_page"]
            host["page_count"] = host["end_page"] - host["start_page"] + 1
            del segments[k:k + 2]
            merged = True
            break
        if not merged:
            break
    for n, seg in enumerate(segments):
        seg["index"] = n + 1
        seg.setdefault("reabsorbed_inserts", [])
    return segments, absorbed_any


def head_pages(start, end, pages_by_no):
    """Pages Stage 1 should deep-extract for this segment.

    Normally the first two pages, mirroring Stage 1's existing window. If the
    first page is a slip sheet it carries the exhibit marker but no content
    identity, so the window widens by one to reach the actual document.
    """
    want = HEAD_PAGE_COUNT
    if pages_by_no.get(start, {}).get("is_slip_sheet"):
        want += 1
    return list(range(start, min(end, start + want - 1) + 1))


def segment_document(census, pdf_path=None):
    pages = census["pages"]
    pages_by_no = {p["page"]: p for p in pages}
    total = census["total_pages"]

    scored = []      # (before_page, signals)
    for i in range(len(pages) - 1):
        signals = score_boundary(pages, i)
        if signals:
            scored.append([pages[i + 1]["page"], signals])

    # Tier B only for seams that are ambiguous AND could flip.
    tier_b_pages = 0
    refined = []
    if pdf_path is not None:
        pending = [(bp, sig) for bp, sig in scored
                   if REFINE_BAND[0] <= total_score(sig) < REFINE_BAND[1]]
        if pending:
            tier_b_pages = refine(census, pdf_path, pending)
            refined = sorted(bp for bp, _ in pending)

    cuts, candidates = [], []
    for before_page, signals in scored:
        s = total_score(signals)
        cs = change_score(signals)
        entry = {
            "before_page": before_page,
            "score": s,
            "change_score": cs,
            "signals": sorted(signals, key=lambda x: x["name"]),
        }
        if s >= BOUNDARY_THRESHOLD:
            cuts.append(entry)
        elif cs >= CANDIDATE_FLOOR:
            # Deliberately tested against CHANGE score, not net score. Continuity
            # decides whether we cut; it must never decide whether a human gets
            # to look. Anything that changed materially at this seam is surfaced,
            # even when the document argues convincingly that it is one document.
            entry["suppressed_by_continuity"] = round(cs - s, 3) if cs > s else 0.0
            candidates.append(entry)

    starts = [1] + [c["before_page"] for c in cuts]
    cut_by_start = {c["before_page"]: c for c in cuts}

    segments = []
    for n, start in enumerate(starts):
        end = (starts[n + 1] - 1) if n + 1 < len(starts) else total
        cut = cut_by_start.get(start)
        segments.append({
            "index": n + 1,
            "start_page": start,
            "end_page": end,
            "page_count": end - start + 1,
            "head_pages": head_pages(start, end, pages_by_no),
            "opened_by": "document_start" if cut is None else "boundary",
            "boundary_score": None if cut is None else cut["score"],
            "signals": [] if cut is None else cut["signals"],
        })

    segments, absorbed = rejoin_inserts(segments, pages_by_no)
    for seg in segments:
        seg["head_pages"] = head_pages(seg["start_page"], seg["end_page"], pages_by_no)

    return {
        "document_id": census["document_id"],
        "filename": census["filename"],
        "total_pages": total,
        "segmenter_version": SEGMENTER_VERSION,
        "census_version": census.get("census_version"),
        "config": {
            "weights": WEIGHTS,
            "boundary_threshold": BOUNDARY_THRESHOLD,
            "candidate_floor": CANDIDATE_FLOOR,
            "refine_band": list(REFINE_BAND),
            "other_size_tolerance": OTHER_SIZE_TOLERANCE,
            "font_overlap_min": FONT_OVERLAP_MIN,
            "font_size_l1_min": FONT_SIZE_L1_MIN,
            "head_page_count": HEAD_PAGE_COUNT,
            "max_insert_pages": MAX_INSERT_PAGES,
        },
        "segments": segments,
        "candidate_boundaries": candidates,
        "refined_boundaries": refined,
        "stats": {
            "segments": len(segments),
            "cuts": len(cuts),
            "candidates": len(candidates),
            "tier_b_pages": tier_b_pages,
            "reabsorbed_inserts": len(absorbed),
        },
    }


def validate(out_dir: Path) -> int:
    files = sorted(out_dir.glob("*.json"))
    if not files:
        print("VALIDATE: no segment maps found.")
        return 1
    problems = 0
    for jf in files:
        d = json.loads(jf.read_text(encoding="utf-8"))
        miss = [k for k in REQUIRED_DOC_FIELDS if k not in d]
        if miss:
            problems += 1
            print(f"  [FAIL] {jf.name}: missing {miss}")
            continue
        segs = d["segments"]
        for s in segs:
            m = [k for k in REQUIRED_SEGMENT_FIELDS if k not in s]
            if m:
                problems += 1
                print(f"  [FAIL] {jf.name} segment {s.get('index')}: missing {m}")
                break
        else:
            # Segments must tile the document exactly: contiguous, no gaps or overlap.
            if segs[0]["start_page"] != 1 or segs[-1]["end_page"] != d["total_pages"]:
                problems += 1
                print(f"  [FAIL] {jf.name}: segments do not span 1..{d['total_pages']}")
                continue
            for a, b in zip(segs, segs[1:]):
                if b["start_page"] != a["end_page"] + 1:
                    problems += 1
                    print(f"  [FAIL] {jf.name}: gap/overlap at page {a['end_page']}")
                    break
    if problems == 0:
        print(f"VALIDATE: OK - all {len(files)} segment maps are well-formed and tile completely.")
    else:
        print(f"VALIDATE: {problems} file(s) had problems.")
    return problems


def main():
    ap = argparse.ArgumentParser(description="ExhibitPro Stage 0.5 - Compound Document Segmentation v1")
    ap.add_argument("--census", required=True, help="Folder of Stage 0 census JSON")
    ap.add_argument("--output", required=True, help="Folder to write segment maps into")
    ap.add_argument("--pdf-dir", help="Folder of source PDFs; enables Tier B refinement")
    ap.add_argument("--report", action="store_true", help="Print documents that segmented into >1 part")
    ap.add_argument("--validate", action="store_true", help="Check segment maps tile the document")
    args = ap.parse_args()

    census_dir, out_dir = Path(args.census), Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else None

    census_files = sorted(census_dir.glob("*.json"))
    if not census_files:
        print(f"No census JSON found in {census_dir}")
        return

    print(f"Stage 0.5 - Segmentation v{SEGMENTER_VERSION}: {len(census_files)} documents")
    if pdf_dir is None:
        print("  (Tier B refinement disabled - pass --pdf-dir to enable)")

    t0 = time.time()
    multi, tier_b_total, cand_total, failed = [], 0, 0, []
    for cf in census_files:
        try:
            census = json.loads(cf.read_text(encoding="utf-8"))
            pdf_path = None
            if pdf_dir is not None:
                p = pdf_dir / census["filename"]
                pdf_path = p if p.exists() else None
            rec = segment_document(census, pdf_path)
            (out_dir / cf.name).write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tier_b_total += rec["stats"]["tier_b_pages"]
            cand_total += rec["stats"]["candidates"]
            if rec["stats"]["segments"] > 1:
                multi.append(rec)
        except Exception as e:
            failed.append((cf.name, str(e)))
            print(f"  [ERROR] {cf.name}: {e}")
    el = time.time() - t0

    print(f"\nDone in {el:.1f}s. {len(census_files)-len(failed)} maps written, {len(failed)} failed.")
    print(f"  documents split into >1 segment: {len(multi)}")
    print(f"  ambiguous boundaries flagged for review: {cand_total}")
    print(f"  Tier B pages parsed: {tier_b_total}")
    for name, err in failed:
        print(f"  - {name}: {err}")

    if args.report and multi:
        print("\nSegmented documents:")
        for rec in sorted(multi, key=lambda r: -r["stats"]["segments"]):
            segs = rec["segments"]
            shown = segs[:REPORT_MAX_SEGMENTS]
            spans = ", ".join(f"{x['start_page']}-{x['end_page']}" for x in shown)
            more = "" if len(segs) == len(shown) else f", +{len(segs)-len(shown)} more"
            print(f"  {rec['filename']:16s} {rec['total_pages']:4d}p -> {rec['stats']['segments']} segments [{spans}{more}]")
            for x in shown[1:]:
                names = ", ".join(f"{g['name']}({g['weight']:+g})" for g in x["signals"])
                print(f"      cut before p{x['start_page']}  score={x['boundary_score']}  {names}")

    if args.validate:
        validate(out_dir)


if __name__ == "__main__":
    main()
