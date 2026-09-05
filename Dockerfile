# AgenticRobinhood — always-on options-closing daemon
# (FastAPI control/webhook API + monitor & reconcile asyncio loops, SQLite state).
#
# Designed for Coolify (or any Docker host): the container runs one long-lived process;
# state persists on a volume at /app/data; Coolify's reverse proxy terminates HTTPS and
# maps your domain to port 8000 (so no cloudflared tunnel is needed).
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency install first for better layer caching. We editable-install so that
# config.py's REPO_ROOT (parents[2] of the package file) resolves to /app at runtime,
# keeping config.yaml at /app/config.yaml and the DB at /app/data/agentic.db.
COPY pyproject.toml README.md config.example.yaml ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e ".[all]"

# Persistent state dir. A fresh Docker/Coolify volume mounted here inherits this
# ownership, so the non-root user can write the SQLite WAL files.
RUN mkdir -p /app/data \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

VOLUME ["/app/data"]
EXPOSE 8000

# Liveness via the control API's /health endpoint (curl isn't in slim; use python).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

# config.yaml is optional — without it the app falls back to config.example.yaml (paper).
# Mount your real config at /app/config.yaml (read-only) for live settings.
CMD ["agentic", "/app/config.yaml"]
