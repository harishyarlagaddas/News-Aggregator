# Daily batch image for ECS Fargate (linux/amd64).
# Bakes in the embedding model and Chromium so nothing is downloaded at run time.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HF_HOME=/opt/hf \
    NEWSAGG_CONFIG_DIR=/app/config \
    NEWSAGG_LOCAL_MODEL_PATH=/opt/models/all-MiniLM-L6-v2

WORKDIR /app

# CPU-only torch keeps the image ~1.5 GB smaller than the default CUDA build.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[local-embeddings,browser]"

# Headless Chromium plus its system libraries, for the JS-rendered page fallback.
# libatomic1 is needed by Playwright's bundled Node driver and is absent from slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Pre-download the embedding model into the image.
RUN python -c "from sentence_transformers import SentenceTransformer as S; \
S('sentence-transformers/all-MiniLM-L6-v2').save('/opt/models/all-MiniLM-L6-v2')" \
    # safetensors writes model.safetensors as root with mode 0600; make it readable by the app user.
    && chmod -R a+rX /opt/models

COPY config ./config

RUN useradd --create-home --uid 10001 app && chown -R app /app /opt/hf
USER app

ENTRYPOINT ["news-agg"]
CMD ["run"]
