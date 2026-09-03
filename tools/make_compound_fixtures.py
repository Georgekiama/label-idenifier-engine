"""Build synthetic compound PDFs from the corpus. Boundaries are known BY
CONSTRUCTION, so recall is measurable without hand-labelling anything."""
import fitz, glob, json, os, random, sys

SRC = r"C:/Users/user/Downloads/corpus all pdfs"
OUT = sys.argv[1]
N_DOCS, PARTS, MAX_PAGES = 40, (3, 6), 25

os.makedirs(OUT, exist_ok=True)
files = sorted(glob.glob(os.path.join(SRC, "*.pdf")))
rng = random.Random(20260903)   # fixed seed: the fixture set is reproducible
truth = {}

for i in range(N_DOCS):
    out = fitz.open()
    picks = rng.sample(files, rng.randint(*PARTS))
    seams, cursor, parts = [], 0, []
    for p in picks:
        src = fitz.open(p)
        n = min(src.page_count, MAX_PAGES)
        if cursor:
            seams.append(cursor + 1)          # 1-based page that OPENS a new doc
        out.insert_pdf(src, from_page=0, to_page=n - 1)
        parts.append({"file": os.path.basename(p), "pages": n})
        cursor += n
        src.close()
    name = f"compound_{i:03d}.pdf"
    out.save(os.path.join(OUT, name)); out.close()
    truth[name] = {"seams": seams, "total_pages": cursor, "parts": parts}

json.dump(truth, open(os.path.join(OUT, "_truth.json"), "w"), indent=1)
print(f"{len(truth)} compound PDFs, {sum(len(v['seams']) for v in truth.values())} true seams, "
      f"{sum(v['total_pages'] for v in truth.values())} pages")
