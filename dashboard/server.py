"""
ExhibitPro - Engine Audit Dashboard (backend)

A local inspection tool for the Document Identity Engine. Upload PDFs, run the
full pipeline, and see - on the page itself - which block won the TITLE role and
exactly which features paid for it.

This exists to make the engine falsifiable by a human. Every number the scoring
model produced is shown next to the page it came from, so a reviewer can say
"that is the wrong block, and here is the feature that misled it" instead of
"the accuracy is 0.26".

    pip install flask pymupdf pyyaml
    python dashboard/server.py
    open http://127.0.0.1:5000

Nothing is written outside a temp directory, and nothing leaves the machine.

Endpoints
---------
    GET  /                          the dashboard
    POST /api/analyze               multipart upload of one or more PDFs
    GET  /api/page/<doc_id>/<page>  rendered page PNG
    GET  /api/document/<doc_id>     re-run analysis (e.g. after a policy change)
    GET  /api/policies              available granularity policies
"""

import io
import os
import sys
import tempfile
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import page_census  # noqa: E402
import segmenter  # noqa: E402
import unit_assembly  # noqa: E402
import feature_matrix  # noqa: E402
import role_title  # noqa: E402
from contracts import loader as contracts  # noqa: E402

HERE = Path(__file__).resolve().parent
UPLOAD_DIR = Path(tempfile.gettempdir()) / "exhibitpro_dashboard"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

RENDER_ZOOM = 2.0          # 144 dpi: readable without being slow
MAX_RENDER_PAGES = 400

app = Flask(__name__, static_folder=None)


# --- analysis ---------------------------------------------------------------

def analyse(pdf_path: Path, policy: str = None):
    """Run the whole pipeline over one PDF and return everything a human needs
    to audit the result."""
    if policy:
        segmenter.set_policy(policy)

    census = page_census.census_pdf(pdf_path)
    seg_map = segmenter.segment_document(census, pdf_path)

    pages = {}
    for p in census["pages"]:
        pages[p["page"]] = {
            "page": p["page"],
            "width": p["width"],
            "height": p["height"],
            "rotation": p["rotation"],
            "size_class": p["size_class"],
            "orientation": p["orientation"],
            "modality": p["modality"],
            "word_count": p["word_count"],
            "is_slip_sheet": p["is_slip_sheet"],
            "is_blank": p["is_blank"],
            "is_spacer": p["is_spacer"],
            "bates": p["bates"],
            "bates_in_series": p["bates_in_series"],
            "page_label": p["page_label"],
            "text_head": p["text_head"],
        }

    segments = []
    for seg in seg_map["segments"]:
        entry = {
            "index": seg["index"],
            "start_page": seg["start_page"],
            "end_page": seg["end_page"],
            "page_count": seg["page_count"],
            "head_pages": seg["head_pages"],
            "opened_by": seg["opened_by"],
            "boundary_score": seg["boundary_score"],
            "signals": seg["signals"],
            "reabsorbed_inserts": seg.get("reabsorbed_inserts", []),
        }
        try:
            units = unit_assembly.assemble(pdf_path, seg["head_pages"])
            matrix = feature_matrix.build(
                units,
                segment_index=seg["index"],
                segment_start_page=seg["start_page"],
                segment_page_span=seg["page_count"],
            )
            title = role_title.assign(matrix)

            # Attach each unit's own score and audit so the page overlay can be
            # coloured by it, and clicking a box can show why it lost.
            by_id = {r["unit_id"]: r for r in matrix["rows"]}
            excluded = {e["unit_id"]: e["reasons"] for e in title["excluded"]}
            scored = {}
            for row in matrix["rows"]:
                s, contributions, fails = role_title.score_row(
                    row, matrix["segment"].get("flat_typography", False))
                scored[row["unit_id"]] = (s, contributions, fails)

            entry["units"] = [{
                "unit_id": u["unit_id"],
                "page": u["page"],
                "text": u["text"],
                "x": u["x"], "y": u["y"],
                "width": u["width"], "height": u["height"],
                "font_size": u["font_size"],
                "bold": u["bold"],
                "line_count": u["line_count"],
                "source_span_ids": u["source_span_ids"],
                "features": by_id[u["unit_id"]]["features"],
                "score": scored[u["unit_id"]][0],
                "contributions": scored[u["unit_id"]][1],
                "excluded_reasons": excluded.get(u["unit_id"], scored[u["unit_id"]][2]),
                "is_winner": u["unit_id"] == title.get("unit_id"),
            } for u in units]

            entry["title"] = {
                "value": title["value"],
                "candidate": title.get("candidate"),
                "unit_id": title.get("unit_id"),
                "confidence": title["confidence"],
                "score": title.get("score"),
                "margin": title.get("margin"),
                "contributions": title.get("contributions", []),
                "ranking": title.get("ranking", []),
                "excluded_count": len(title["excluded"]),
                "flat_typography": title.get("flat_typography"),
            }
            entry["segment_stats"] = matrix["segment"]
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["units"] = []
            entry["title"] = {"value": None, "confidence": "error"}
        segments.append(entry)

    return {
        "doc_id": census["document_id"],
        "filename": census["filename"],
        "total_pages": census["total_pages"],
        "bates_series": census["bates_series"],
        "pages": pages,
        "segments": segments,
        "candidate_boundaries": seg_map["candidate_boundaries"],
        "stats": seg_map["stats"],
        "policy": seg_map.get("policy"),
        "versions": {
            "census": page_census.CENSUS_VERSION,
            "segmenter": segmenter.SEGMENTER_VERSION,
            "assembly": unit_assembly.ASSEMBLY_VERSION,
            "feature_set": feature_matrix.FEATURE_SET_VERSION,
            "role": role_title.ROLE_VERSION,
            "contracts": contracts.versions(),
        },
    }


def stored_path(doc_id):
    p = UPLOAD_DIR / f"{doc_id}.pdf"
    return p if p.exists() else None


# --- routes -----------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/api/policies")
def policies():
    return jsonify({
        "policies": {k: {"threshold": v["boundary_threshold"],
                         "description": v.get("description", "").strip()}
                     for k, v in segmenter.POLICIES.items()},
        "default": segmenter.DEFAULT_POLICY,
        "active": segmenter.ACTIVE_POLICY,
    })


@app.route("/api/analyze", methods=["POST"])
def analyze():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files uploaded"}), 400
    policy = request.form.get("policy") or segmenter.DEFAULT_POLICY

    results, errors = [], []
    for f in files:
        name = os.path.basename(f.filename or "upload.pdf")
        if not name.lower().endswith(".pdf"):
            errors.append({"filename": name, "error": "not a PDF"})
            continue
        try:
            data = f.read()
            doc_id = page_census.hashlib.sha256(data).hexdigest()[:16]
            dest = UPLOAD_DIR / f"{doc_id}.pdf"
            if not dest.exists():
                dest.write_bytes(data)
            result = analyse(dest, policy)
            result["filename"] = name          # show what the user called it
            results.append(result)
        except Exception as e:
            traceback.print_exc()
            errors.append({"filename": name, "error": f"{type(e).__name__}: {e}"})

    return jsonify({"documents": results, "errors": errors, "policy": policy})


@app.route("/api/document/<doc_id>")
def redo(doc_id):
    path = stored_path(doc_id)
    if not path:
        return jsonify({"error": "unknown document; re-upload it"}), 404
    policy = request.args.get("policy") or segmenter.DEFAULT_POLICY
    try:
        return jsonify(analyse(path, policy))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/page/<doc_id>/<int:page>")
def page_image(doc_id, page):
    path = stored_path(doc_id)
    if not path:
        return jsonify({"error": "unknown document"}), 404
    doc = fitz.open(str(path))
    try:
        if page < 1 or page > doc.page_count or page > MAX_RENDER_PAGES:
            return jsonify({"error": "page out of range"}), 404
        zoom = float(request.args.get("zoom", RENDER_ZOOM))
        zoom = max(0.5, min(zoom, 4.0))
        pix = doc.load_page(page - 1).get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        png = pix.tobytes("png")
    finally:
        doc.close()
    resp = send_file(io.BytesIO(png), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def main():
    host = os.environ.get("EP_HOST", "127.0.0.1")
    port = int(os.environ.get("EP_PORT", "5000"))
    print("ExhibitPro engine audit dashboard")
    print(f"  contracts: {contracts.versions()}")
    print(f"  uploads:   {UPLOAD_DIR}")
    print(f"  open:      http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
