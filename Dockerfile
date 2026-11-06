# =============================================================
# RKA — Research Knowledge Agent
# Multi-stage build: Node (web UI) → sqlite-vec → locked Python runtime
# =============================================================

# --- Stage 1: Build web UI ---
FROM node:26-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS web-builder
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ .
RUN npm run build

# --- Stage 2: Build sqlite-vec from source ---
FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS vec-builder

ARG SQLITE_VEC_VERSION=0.1.6
ARG SQLITE_VEC_SHA256=0664b8c3c3fa5c53d0c9182de20c5206cce74038bdfbcd77e37366e988d11dba

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/sqlite-vec

RUN curl -fsSL -o sqlite-vec.zip \
      "https://github.com/asg017/sqlite-vec/releases/download/v${SQLITE_VEC_VERSION}/sqlite-vec-${SQLITE_VEC_VERSION}-amalgamation.zip" \
    && echo "${SQLITE_VEC_SHA256}  sqlite-vec.zip" | sha256sum -c - \
    && unzip sqlite-vec.zip \
    && gcc -O3 -fPIC -shared sqlite-vec.c -o vec0.so -lm

# --- Stage 3: Locked dependency installer ---
FROM ghcr.io/astral-sh/uv:0.9.17@sha256:5cb6b54d2bc3fe2eb9a8483db958a0b9eebf9edff68adedb369df8e7b98711a2 AS uv-tool

# --- Stage 4: Locked Python dependency builder ---
FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS python-deps

# Build-only compiler for packages without a wheel on a supported architecture.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install exactly the dependency graph reviewed in uv.lock. --locked fails
# instead of resolving when pyproject.toml and the lock disagree.
COPY --from=uv-tool /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY README.md LICENSE ./
COPY rka/ rka/
COPY --from=web-builder /build/dist/ web/dist/
COPY --from=vec-builder /tmp/sqlite-vec/vec0.so /usr/local/lib/vec0.so
RUN uv sync --locked --no-dev --no-editable \
      --extra embeddings \
      --extra academic \
      --extra workspace \
    && uv cache clean

# --- Stage 5: Python runtime ---
FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2 AS runtime

WORKDIR /app

COPY --from=python-deps /app/.venv /app/.venv
COPY --from=web-builder /build/dist/ web/dist/
COPY --from=vec-builder /tmp/sqlite-vec/vec0.so /usr/local/lib/vec0.so

# Data volume
RUN mkdir -p /data
VOLUME /data

# Default environment
ENV PATH="/app/.venv/bin:${PATH}" \
    RKA_DATA_DIR=/data \
    RKA_DB_PATH=/data/rka.db \
    RKA_EMBEDDINGS_ENABLED=true \
    RKA_HOST=0.0.0.0 \
    RKA_PORT=9712 \
    RKA_LLM_ENABLED=false \
    RKA_SQLITE_VEC_PATH=/usr/local/lib/vec0.so \
    # v2.7.0.1: bound onnxruntime parallelism + persist FastEmbed cache.
    # These defaults are safe for any caller (Docker run, compose, direct).
    # Compose interpolation in docker-compose.yml can override RKA_EMBEDDING_THREADS.
    OMP_NUM_THREADS=2 \
    TOKENIZERS_PARALLELISM=false \
    RKA_EMBEDDING_THREADS=2 \
    RKA_EMBEDDING_CACHE_DIR=/data/fastembed_cache

LABEL org.opencontainers.image.title="RKA Core" \
      org.opencontainers.image.description="Local-first research memory and evidence infrastructure" \
      org.opencontainers.image.source="https://github.com/rka-project/rka-core" \
      org.opencontainers.image.licenses="MIT"

EXPOSE 9712

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:9712/api/health'); r.raise_for_status()" || exit 1

# Default: run the API server
CMD ["rka", "serve"]
