# ExhibitPro audit dashboard
#
# A container, not a serverless function, and deliberately so: an uploaded PDF
# is stored and re-read by later requests for page images, so the process must
# outlive a single request.

FROM python:3.11-slim

# Tesseract is the OCR fallback for pages with no embedded text. It is a system
# binary, not a Python package - pytesseract is only a wrapper - which is the
# main reason this cannot run on a serverless Python runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr libgl1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

ENV PYTHONUNBUFFERED=1 \
    EP_UPLOAD_DIR=/tmp/exhibitpro_uploads \
    PORT=8000

EXPOSE 8000

# One worker, several threads. PyMuPDF holds page data per process, so extra
# workers multiply memory rather than throughput, and the store is per-instance
# local disk - a second worker would not see the first worker's uploads.
# A generous timeout: analysing a large compound PDF is legitimately slow.
CMD ["sh", "-c", "gunicorn --chdir /app dashboard.server:app --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 180 --access-logfile -"]
