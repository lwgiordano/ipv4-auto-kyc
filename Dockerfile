# One image, three processes. The API is the default command; the pipeline
# worker, outbox worker, and migrations run the same image with a different
# command (see docs/RUNBOOK.md and the deploy notes at the bottom of this file).
#
#   docker build -t kyc-tool .
#   docker run --rm -p 8000:8000 -e KYC_DATABASE_URL=... kyc-tool   # API
#
FROM python:3.11-slim-bookworm

# psycopg[binary] bundles its own libpq, so no build toolchain or libpq-dev is
# needed — the slim base is enough.
#
# KYC_POLICY_DIR: the spec package is not part of the installed Python package,
#   so pin the loader at the copy baked into the image (see COPY below).
# KYC_OBJECT_STORE: fs is the default; set =s3 + KYC_S3_BUCKET in production.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KYC_POLICY_DIR=/app/KYC_Tool_Build_Package/machine_readable \
    KYC_OBJECT_STORE=fs \
    KYC_OBJECT_STORE_ROOT=/data/evidence

WORKDIR /app

# Unprivileged runtime user + the one directory the fs object store writes to.
RUN useradd --system --create-home --uid 10001 app \
    && mkdir -p /data/evidence \
    && chown -R app:app /data

# Dependencies first, so they cache unless pyproject or src actually change.
# Editable install keeps a single copy of the code at /app/src, which makes
# REPO_ROOT resolve to /app (config.py) and satisfies alembic's
# prepend_sys_path=src. [s3] pulls boto3 so the same image can use S3 in prod.
COPY pyproject.toml alembic.ini ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e '.[s3]'

# Application payload: migrations + the normative (immutable) spec package.
COPY alembic/ ./alembic/
COPY KYC_Tool_Build_Package/ ./KYC_Tool_Build_Package/

USER app
EXPOSE 8000

# API-oriented; worker containers should disable/override this healthcheck
# since they serve no HTTP. urlopen exits non-zero on any non-2xx or refusal.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]

# Default process: the read/ingest API. Other services override the command:
#   migrations      : alembic upgrade head
#   pipeline worker : python -m kyc_tool.workers.pipeline_worker
#   outbox worker   : python -m kyc_tool.workers.outbox_worker
#   retention (cron): python -m kyc_tool.workers.retention
CMD ["uvicorn", "kyc_tool.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
