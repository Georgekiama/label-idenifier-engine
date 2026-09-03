"""
ExhibitPro - Deliverable 02a
Stage 0: Page Census v1

Purpose
-------
Produce a cheap, per-page structural record for EVERY page of a PDF, fast enough
to run on large compound documents. The census is the input to Stage 0.5
(Segmentation), which finds the seams between the sub-documents inside a
compound PDF. Only then does Stage 1 spend real extraction cost, and only on
the head pages of each segment.

Why two tiers
-------------
Measured on the 198-PDF benchmark corpus (4,580 pages):

    geometry only              0.29 ms/page
    get_fonts(full=False)      0.74 ms/page
    get_text("text")           6.39 ms/page
    get_text("dict")          33.73 ms/page   <-- 5x everything else

Tier A (this module) uses only the cheap calls: ~7.5 ms/page, so a 300-page
compound PDF is censused in ~2.3 seconds. Span-level font SIZE analysis
(get_text("dict")) is deliberately excluded here; Stage 0.5 requests it for the
handful of pages where it can actually change a boundary decision.

Font families come from the page RESOURCE table (get_fonts), not from parsing
spans. That gives a usable typographic fingerprint for 1/45th of the cost.

Determinism
-----------
    - No randomness, no corpus-dependent state, no wall-clock values in output.
    - All floats rounded (geometry 2dp).
    - All sets serialised as sorted lists.
    - Regex libraries are literal constants in this file, versioned by
      CENSUS_VERSION. Changing a pattern REQUIRES bumping that version.
    - document_id is the sha256[:16] content hash used by Stage 1, so census and
      extraction records join on a stable key.

Output: one JSON per PDF.

{
  "document_id": "<sha256[:16]>",
  "filename": "<name.pdf>",
  "total_pages": <int>,
  "census_version": "1.5.0",
  "bates_series": [{"prefix": "ABC", "digits": 6, "first": 1, "last": 240,
                    "pages": [1, 2, ...], "page_count": 240}],
  "pages": [
    {
      "page": 1,
      "width": 612.0, "height": 792.0, "rotation": 0,
      "size_class": "Letter", "orientation": "portrait",
      "modality": "native" | "scanned",
      "char_count": <int>, "word_count": <int>, "line_count": <int>,
      "image_count": <int>,
      "font_families": ["Arial", "TimesNewRoman"],
      "font_classes": ["sans", "serif"],
      "bates": {"raw": "ABC001234", "prefix": "ABC", "number": 1234, "digits": 6} | null,
      "bates_in_series": false,
      "page_label": {"num": 3, "total": 12} | null,
      "is_blank": false, "is_spacer": false, "is_slip_sheet": false,
      "header_sig": "jsc-##### page ##", "footer_sig": "...",
      "ends_mid_sentence": true, "starts_lowercase": false,
      "content_tokens": ["deposition", "plaintiff", "transcript", ...],
      "text_head": "<first 160 chars, whitespace-normalised>",
      "text_sha": "<sha1[:12] of normalised full page text>"
    }, ...
  ]
}

Usage
-----
    python page_census.py --input "<folder of PDFs>" --output "<folder>" [--validate]
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF is required. Install with: pip install pymupdf", file=sys.stderr)
    raise

CENSUS_VERSION = "1.5.0"

# --- Pattern library (versioned by CENSUS_VERSION) --------------------------

# Bates-style production stamp: 3-8 letter prefix + 4-10 digits.
#
# Deliberately conservative. An earlier 2-letter/space-separated version matched
# US state + ZIP in mailing addresses ("IL 60439", "WA 98230") on 39 of the 198
# benchmark documents, which then drove spurious segmentation. Two constraints
# close that hole: the prefix must be at least 3 letters (state codes are 2), and
# a space is not an accepted separator (real stamps run together or hyphenate).
# Page-level matches are still only CANDIDATES - see validate_bates_series().
BATES_RE = re.compile(r"\b([A-Z]{3,8})[-_]?(\d{4,10})\b")
# "Page 3 of 12" / "Page 3/12"
PAGE_LABEL_RE = re.compile(r"\bPage\s+(\d{1,4})\s*(?:of|/)\s*(\d{1,4})\b", re.I)

# Font family classification. Order matters: first match wins, so mono is tested
# before serif ("Courier New" must not fall through to serif).
FONT_CLASS_RULES = [
    ("mono", re.compile(r"courier|mono|consol|lucida\s*console", re.I)),
    ("serif", re.compile(r"times|georgia|garamond|book|roman|serif|minion|cambria|palatino|century", re.I)),
    ("sans", re.compile(r"arial|helvetica|calibri|verdana|tahoma|segoe|futura|gill|sans", re.I)),
]
# Subset prefixes look like "KEAJAN+MicrosoftSansSerif"
SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")
STYLE_SUFFIX_RE = re.compile(r"[-_](Bold|Italic|Oblique|Regular|Light|Medium|Roman|BoldItalic)$", re.I)

# Standard page sizes in points, portrait orientation (width, height).
#
# Raw dimensions cannot be compared directly: scanned pages carry scanner jitter,
# and on the benchmark corpus one 194-page document produced 67 distinct page
# sizes (596.16x778.56, 600.96x781.92, ...) that are all just Letter. Classifying
# into a named size makes the comparison robust AND makes the audit trail
# readable - "Letter -> Legal" instead of two float pairs.
PAGE_SIZE_CLASSES = [
    ("Letter", 612.0, 792.0),
    ("Legal", 612.0, 1008.0),
    ("A4", 595.0, 842.0),
    ("A3", 842.0, 1191.0),
    ("Ledger", 792.0, 1224.0),
    ("Executive", 522.0, 756.0),
]
# Fractional tolerance for matching a page to a standard size. 5% absorbs
# scanner jitter while keeping Letter (792pt tall) and Legal (1008pt) distinct.
SIZE_CLASS_TOLERANCE = 0.05

# Bates series validation. A prefix is only accepted as a real production series
# if it recurs across at least this many pages with a constant digit width and
# predominantly ascending numbers.
MIN_SERIES_PAGES = 3
MIN_ASCENDING_RATIO = 0.80

# A page is "blank" below this many characters with no images.
BLANK_CHAR_MAX = 3
# A slip sheet is a near-empty divider page that names what follows.
SLIP_SHEET_WORD_MAX = 15
SLIP_SHEET_CHAR_MAX = 120
# Spacer pages that are short but carry no identity. A real slip sheet announces
# what comes next; these announce nothing and must not open a segment.
SPACER_RE = re.compile(r"intentionally\s+(left\s+)?blank|this\s+page\s+is\s+blank", re.I)

# Running header/footer signature length. Digits are masked so that "Page 12"
# and "Page 13" produce the same signature - the point is to recognise the same
# TEMPLATE across pages, not the same text.
EDGE_SIG_LEN = 48
DIGIT_MASK_RE = re.compile(r"\d")
# Text that ends without terminal punctuation is mid-sentence.
TERMINAL_PUNCT = ".!?:;”’\")]}"

# Content-token fingerprint. Consecutive pages of one document share subject
# vocabulary - the same names, terms and jargon. Across a document boundary that
# vocabulary turns over. This is the ONLY signal that survives a same-producer
# Bates production, where fonts, page size, modality and the stamp run are all
# continuous and nothing structural changes at the seam except the words.
#
# It is a set-overlap measurement over normalised tokens, not text understanding:
# no semantics, no model, no ordering.
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")
TOKEN_CAP = 60          # keep the N most frequent; caps census size and cost
MIN_TOKEN_LEN = 4
STOPWORDS = frozenset("""
that this with from have been were will would could should their there these
those which what when where than then them they your yours about above after
again against because before being below between both during each further here
itself more most other over same some such only once under until very
shall must may can any all not but for and the are was has had its his her
page section chapter table figure appendix exhibit paragraph
""".split())

REQUIRED_PAGE_FIELDS = [
    "page", "width", "height", "rotation", "size_class", "orientation",
    "modality", "char_count", "word_count",
    "line_count", "image_count", "font_families", "font_classes", "bates",
    "page_label", "is_blank", "is_spacer", "is_slip_sheet", "header_sig",
    "footer_sig", "ends_mid_sentence", "starts_lowercase", "content_tokens",
    "text_head", "text_sha", "bates_in_series",
]
REQUIRED_DOC_FIELDS = [
    "document_id", "filename", "total_pages", "census_version", "bates_series", "pages",
]


def compute_document_id(pdf_path: Path) -> str:
    """sha256[:16] of raw file bytes. Matches Stage 1 so records join cleanly."""
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def normalise_font_name(raw: str) -> str:
    """KEAJAN+MicrosoftSansSerif,Bold  ->  MicrosoftSansSerif

    Vendor subset prefixes and style suffixes are stripped so the same typeface
    used bold and regular collapses to one family. Per the catalogue, vendor
    names are never features in themselves; they are only an identity key for
    comparing one page against the next.
    """
    name = SUBSET_PREFIX_RE.sub("", raw or "")
    name = name.split(",")[0]
    name = STYLE_SUFFIX_RE.sub("", name)
    return name.strip()


def classify_font(name: str) -> str:
    for cls, rx in FONT_CLASS_RULES:
        if rx.search(name):
            return cls
    return "other"


def extract_bates(text: str):
    """Return the LAST Bates-like match in reading order.

    Production stamps sit in the footer, so the final match on the page is the
    right one. Returning a single canonical stamp (rather than every match)
    keeps the downstream discontinuity test unambiguous.
    """
    last = None
    for m in BATES_RE.finditer(text):
        last = m
    if last is None:
        return None
    prefix, digits = last.group(1), last.group(2)
    return {
        "raw": last.group(0),
        "prefix": prefix,
        "number": int(digits),
        "digits": len(digits),
    }


def edge_signature(raw_text: str):
    """Return (header_sig, footer_sig) - the first and last visible lines with
    digits masked, lowercased and truncated.

    A running header like "JSC-63743 Page 12" becomes "jsc-##### page ##", which
    is identical on every page of that report. Stage 0.5 uses the match as
    CONTINUITY evidence: pages carrying the same running header belong to the
    same document, however much their fonts or page sizes vary.
    """
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        return "", ""

    def sig(s):
        return DIGIT_MASK_RE.sub("#", s.lower())[:EDGE_SIG_LEN]

    return sig(lines[0]), sig(lines[-1])


def content_tokens(text: str):
    """Deterministic content-word fingerprint for one page.

    Lowercased alphabetic tokens of 4+ characters, stopwords removed, kept as
    the TOKEN_CAP most frequent. Ties break alphabetically so the output is
    stable regardless of dict ordering, and the list is sorted so two censuses
    of the same page are byte-identical.

    Structural words ("page", "section", "exhibit") are treated as stopwords:
    they recur across unrelated documents and would inflate the overlap between
    two pages that share nothing but a template.
    """
    counts = {}
    for m in TOKEN_RE.finditer(text):
        w = m.group(0).lower()
        if len(w) < MIN_TOKEN_LEN or w in STOPWORDS:
            continue
        counts[w] = counts.get(w, 0) + 1
    if not counts:
        return []
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return sorted(w for w, _ in ranked[:TOKEN_CAP])


def classify_page_size(width: float, height: float, rotation: int):
    """Return (size_class, orientation).

    Classification is done on the page as presented to a reader: rotation is
    applied first, then the short and long edges are matched against standard
    sizes. Orientation is reported separately so a landscape exhibit inside a
    portrait filing is visible as an orientation change rather than a new size.
    """
    w, h = (height, width) if rotation % 180 == 90 else (width, height)
    orientation = "landscape" if w > h else "portrait"
    short, long_ = min(w, h), max(w, h)
    for name, sw, sh in PAGE_SIZE_CLASSES:
        if (abs(short - sw) <= sw * SIZE_CLASS_TOLERANCE
                and abs(long_ - sh) <= sh * SIZE_CLASS_TOLERANCE):
            return name, orientation
    return "other", orientation


def extract_page_label(text: str):
    m = PAGE_LABEL_RE.search(text)
    if not m:
        return None
    return {"num": int(m.group(1)), "total": int(m.group(2))}


def census_page(page, page_number: int) -> dict:
    rect = page.rect
    raw_text = page.get_text("text")
    norm = re.sub(r"\s+", " ", raw_text).strip()

    try:
        images = page.get_images(full=False)
    except Exception:
        images = []
    try:
        fonts = page.get_fonts(full=False)
    except Exception:
        fonts = []

    families = sorted({normalise_font_name(f[3]) for f in fonts if len(f) > 3 and f[3]})
    classes = sorted({classify_font(fam) for fam in families})

    char_count = len(norm)
    word_count = len(norm.split()) if norm else 0
    line_count = len([ln for ln in raw_text.splitlines() if ln.strip()])
    image_count = len(images)

    is_blank = char_count <= BLANK_CHAR_MAX and image_count == 0
    # A spacer ("This page intentionally left blank") is short but announces
    # nothing, so it is not a divider and must never open a segment.
    is_spacer = bool(SPACER_RE.search(norm))
    # A slip sheet is short but not empty, and carries no image payload of its own.
    #
    # The native-modality requirement is essential. A SCANNED page yields no
    # extractable text, so on word count alone it looks exactly like a divider -
    # which classified all 13 pages of a scanned document as slip sheets and cut
    # it into 13 segments. A page we cannot read is not a page we can call short.
    is_slip_sheet = (
        not is_blank
        and not is_spacer
        and norm != ""
        and word_count <= SLIP_SHEET_WORD_MAX
        and char_count <= SLIP_SHEET_CHAR_MAX
        and image_count <= 1
    )
    header_sig, footer_sig = edge_signature(raw_text)
    stripped = norm.rstrip()
    first_alpha = next((c for c in norm if c.isalpha()), "")

    size_class, orientation = classify_page_size(rect.width, rect.height, int(page.rotation))

    return {
        "page": page_number,
        "width": round(rect.width, 2),
        "height": round(rect.height, 2),
        "rotation": int(page.rotation),
        "size_class": size_class,
        "orientation": orientation,
        "modality": "native" if norm else "scanned",
        "char_count": char_count,
        "word_count": word_count,
        "line_count": line_count,
        "image_count": image_count,
        "font_families": families,
        "font_classes": classes,
        "bates": extract_bates(norm),
        "page_label": extract_page_label(norm),
        "is_blank": is_blank,
        "is_spacer": is_spacer,
        "is_slip_sheet": is_slip_sheet,
        "header_sig": header_sig,
        "footer_sig": footer_sig,
        "ends_mid_sentence": bool(stripped) and stripped[-1] not in TERMINAL_PUNCT,
        "starts_lowercase": first_alpha.islower(),
        "content_tokens": content_tokens(norm),
        "text_head": norm[:160],
        "text_sha": hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12],
    }


def validate_bates_series(pages) -> list:
    """Promote page-level Bates candidates to document-level SERIES.

    A Bates number is a property of a production run, not of a page. A single
    page match proves nothing - any capitalised token followed by digits looks
    like one. A series is only credible when the same prefix recurs across
    several pages with a constant digit width and ascending numbers.

    A compound PDF may legitimately contain more than one production
    (ABC000001-ABC000050 followed by XYZ000001-XYZ000120), so each prefix is
    validated independently rather than picking one winner per document. That
    is precisely the case Stage 0.5 needs to detect as a boundary.

    Mutates each page record, setting bates_in_series. Returns the series list.
    """
    by_prefix = {}
    for p in pages:
        b = p.get("bates")
        if b:
            by_prefix.setdefault(b["prefix"], []).append((p["page"], b))

    series = []
    for prefix in sorted(by_prefix):
        hits = by_prefix[prefix]
        if len(hits) < MIN_SERIES_PAGES:
            continue
        widths = {b["digits"] for _, b in hits}
        if len(widths) != 1:
            continue
        nums = [b["number"] for _, b in hits]
        if len(nums) > 1:
            ascending = sum(1 for a, c in zip(nums, nums[1:]) if c > a)
            if ascending / (len(nums) - 1) < MIN_ASCENDING_RATIO:
                continue
        series.append({
            "prefix": prefix,
            "digits": widths.pop(),
            "first": min(nums),
            "last": max(nums),
            "pages": [pg for pg, _ in hits],
            "page_count": len(hits),
        })

    accepted = {s["prefix"] for s in series}
    for p in pages:
        b = p.get("bates")
        p["bates_in_series"] = bool(b and b["prefix"] in accepted)
    return series


def census_pdf(pdf_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    pages = []
    try:
        total = doc.page_count
        for i in range(total):
            pages.append(census_page(doc.load_page(i), i + 1))
    finally:
        doc.close()
    series = validate_bates_series(pages)
    return {
        "document_id": compute_document_id(pdf_path),
        "filename": pdf_path.name,
        "total_pages": total,
        "census_version": CENSUS_VERSION,
        "bates_series": series,
        "pages": pages,
    }


def validate(output_dir: Path) -> int:
    files = sorted(output_dir.glob("*.json"))
    if not files:
        print("VALIDATE: no census JSON found.")
        return 1
    problems = 0
    for jf in files:
        data = json.loads(jf.read_text(encoding="utf-8"))
        missing = [k for k in REQUIRED_DOC_FIELDS if k not in data]
        if missing:
            problems += 1
            print(f"  [FAIL] {jf.name}: missing doc fields {missing}")
            continue
        if len(data["pages"]) != data["total_pages"]:
            problems += 1
            print(f"  [FAIL] {jf.name}: {len(data['pages'])} page records vs total_pages={data['total_pages']}")
            continue
        for p in data["pages"]:
            miss = [k for k in REQUIRED_PAGE_FIELDS if k not in p]
            if miss:
                problems += 1
                print(f"  [FAIL] {jf.name} page {p.get('page')}: missing {miss}")
                break
    if problems == 0:
        print(f"VALIDATE: OK - all {len(files)} census files match the schema.")
    else:
        print(f"VALIDATE: {problems} file(s) had schema problems.")
    return problems


def main():
    ap = argparse.ArgumentParser(description="ExhibitPro Stage 0 - Page Census v1")
    ap.add_argument("--input", required=True, help="Folder containing PDFs")
    ap.add_argument("--output", required=True, help="Folder to write one census JSON per PDF into")
    ap.add_argument("--validate", action="store_true", help="Schema check after the run")
    args = ap.parse_args()

    in_dir, out_dir = Path(args.input), Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(in_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {in_dir}")
        return

    print(f"Stage 0 - Page Census v{CENSUS_VERSION}: {len(pdfs)} PDFs from {in_dir}")
    t0 = time.time()
    ok, pages_done, failed = 0, 0, []
    for pdf in pdfs:
        try:
            rec = census_pdf(pdf)
            (out_dir / f"{pdf.stem}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            ok += 1
            pages_done += rec["total_pages"]
        except Exception as e:
            failed.append((pdf.name, str(e)))
            print(f"  [ERROR] {pdf.name}: {e}")
    el = time.time() - t0

    print(f"\nDone. {ok} succeeded, {len(failed)} failed.")
    if pages_done:
        print(f"{pages_done} pages in {el:.1f}s = {1000*el/pages_done:.2f} ms/page")
    for name, err in failed:
        print(f"  - {name}: {err}")

    if args.validate:
        validate(out_dir)


if __name__ == "__main__":
    main()
