# syntax=docker/dockerfile:1
#
# FORGE — CPU-only, no GPU, no external services, no API keys.
#
#   docker build -t forge .
#   docker run -p 8000:8000 forge
#   curl localhost:8000/health
#
# The atlas is compiled during the image build, so the container starts ready.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# --- dependencies (cached layer) -------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- source ----------------------------------------------------------------
COPY forge/ ./forge/
COPY data/ ./data/
COPY tools/ ./tools/
COPY tests/ ./tests/
COPY README.md .

# --- compile the atlas at build time (this is the architecture, not a step) --
RUN python -m forge.build --siis data/siis_responses.json \
                          --catalog data/deeplinks.json \
                          --out atlas --version current \
    && python -m forge.build --siis data/siis_responses.json \
                             --catalog data/deeplinks.json \
                             --out atlas --version current --quiet

ENV FORGE_ATLAS=/app/atlas/current \
    FORGE_CATALOG=/app/data/deeplinks.json

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "forge.api:app", "--host", "0.0.0.0", "--port", "8000"]
