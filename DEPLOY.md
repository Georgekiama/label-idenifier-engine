# Deploying the audit dashboard

## Why not Vercel

Vercel is the easier platform, and it is the wrong shape for this app.

Vercel Python runs as **serverless functions**: each request may be handled by a
different ephemeral instance. This dashboard is stateful by design — you upload
a PDF in one request, and every later request for a page image
(`/api/page/<doc_id>/<n>`) re-reads that stored file. On Vercel the file often
will not be there, and with warm-instance reuse it would sometimes work and
sometimes not. A page preview that intermittently disappears is worse than one
that never loads, and the page preview *is* the product.

Two more, both smaller but real:

- **4.5 MB request body cap.** Median PDF in the benchmark corpus is 0.2 MB and
  p90 is 2.0 MB, so single files are usually fine — but multi-file upload sums
  into one request, which is the main way this tool gets used.
- **No system packages.** The OCR fallback needs the Tesseract *binary*;
  `pytesseract` is only a wrapper. A serverless Python runtime cannot install it,
  so scanned documents would fail on the platform.

Making it Vercel-native means moving uploads to blob storage and re-architecting
the page-image path. That is real work for no benefit over a container.

Timeouts, for the record, are **not** the problem: analysing the largest corpus
file (8.7 MB, 55 pages) takes 2.8 s.

## Render (recommended)

A long-running container: the process stays up, local disk persists for the
instance's life, no body cap, and `apt install tesseract-ocr` works.

1. Push this repo to GitHub.
2. Render → **New** → **Blueprint** → select the repo. It reads `render.yaml`.
3. Set `EP_USER` and `EP_PASSWORD` when prompted.
4. Deploy. Health check is `/healthz`.

**Use the Starter plan, not Free.** Free is 512 MB and spins down when idle;
rendering a large PDF to a pixmap will OOM, and a cold start makes the first
upload look broken.

Anything that runs a container works the same way — Fly.io, Railway, a VM with
`docker run`. Only the blueprint file is Render-specific.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EP_USER` / `EP_PASSWORD` | unset | HTTP basic auth. **Unset means the dashboard is open to anyone with the URL.** |
| `EP_MAX_UPLOAD_MB` | `40` | Per-request upload cap |
| `EP_MAX_PAGES` | `600` | Refuse documents longer than this |
| `EP_UPLOAD_TTL_MIN` | `180` | Delete stored uploads older than this |
| `EP_UPLOAD_DIR` | system temp | Where uploads are stored |
| `PORT` | `8000` | Injected by the host |

## Before you give someone the URL

This tool accepts arbitrary PDF uploads and renders them. Hosting it changes the
risk profile in ways that local use does not:

- **Set `EP_USER` and `EP_PASSWORD`.** Without them the dashboard, every uploaded
  document and every rendered page image are readable by anyone with the link.
  Auth is all-or-nothing by design — including `/healthz`.
- **Uploaded documents sit on the server's disk** until the TTL expires. If real
  client material is going to be uploaded, that is a data-handling decision, not
  a technical detail. `EP_UPLOAD_TTL_MIN` is the dial; shorter is better, and the
  dashboard's delete and *Clear all* buttons remove files immediately.
- **PDF parsing is an attack surface.** PyMuPDF parses untrusted input; keep it
  patched. The page and size caps bound the damage a single hostile file can do,
  they do not eliminate it.
- **Single worker by design.** The upload store is instance-local disk, so a
  second worker would not see the first worker's files. Scale by making the
  instance bigger, not by adding workers — or move the store to shared storage
  first.

For a handful of colleagues auditing the engine, basic auth on a Starter
instance is proportionate. For anything client-facing, put it behind your own
SSO and give it real storage.

## Local

```
pip install -r requirements.txt
python dashboard/server.py          # http://127.0.0.1:5000, no auth
```

Or the same container the host runs:

```
docker build -t exhibitpro-audit .
docker run --rm -p 8000:8000 -e EP_USER=you -e EP_PASSWORD=secret exhibitpro-audit
```
