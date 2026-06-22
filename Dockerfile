# syntax=docker/dockerfile:1
# Multi-stage build: build in a full environment, ship only runtime deps.

# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /build
COPY . .

RUN pip install --upgrade pip && \
    pip install build && \
    python -m build

# ---- Runtime stage ----
FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl[crawl,media,llm,dedup,gui] && \
    rm /tmp/*.whl

# The runtime entry point: override via docker-compose or docker run command.
ENTRYPOINT ["lcp"]
CMD ["--help"]
