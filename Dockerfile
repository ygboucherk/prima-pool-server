# ─────────────────────────────────────────────────────────────
# prima-pool-server — control plane container
# ─────────────────────────────────────────────────────────────
# Builds the server package and runs it with uvicorn.
# All configuration is via environment variables (PRIMA_POOL_*).
# ─────────────────────────────────────────────────────────────
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package (build isolation needs setuptools/wheel).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# ── Runtime ─────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRIMA_POOL_HOST=0.0.0.0 \
    PRIMA_POOL_PORT=8000

WORKDIR /app

# Copy the installed package from the build stage.
COPY --from=base /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=base /usr/local/bin/prima-pool-server /usr/local/bin/prima-pool-server

# SQLite state database (mount a volume here to persist state).
ENV PRIMA_POOL_STORE_PATH=/data/store.db
VOLUME ["/data"]

EXPOSE 8000

# Default command; override host/port via env.
CMD ["prima-pool-server", "--host", "0.0.0.0", "--port", "8000"]
