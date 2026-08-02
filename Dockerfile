# One image for every process. The API is the default command; migrations and
# the workers run the same image with the command overridden (see bottom).
# Base pinned by digest (PR 10a supply-chain): the tag stays for readability, the digest is the
# authority — a re-tagged upstream image cannot silently change the build. Refresh deliberately.
FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

# psycopg[binary] bundles libpq, so the slim base needs no apt packages.
# KYC_POLICY_DIR points the loader at the spec package copied in below; it is
# not part of the installed wheel.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KYC_POLICY_DIR=/app/KYC_Tool_Build_Package/machine_readable \
    KYC_OBJECT_STORE=fs \
    KYC_OBJECT_STORE_ROOT=/data/evidence

WORKDIR /app

RUN useradd --system --create-home --uid 10001 app \
    && mkdir -p /data/evidence \
    && chown -R app:app /data

# Editable install keeps the code only at /app/src, so REPO_ROOT resolves to
# /app and alembic's prepend_sys_path=src works. [s3] adds boto3 for prod S3.
COPY pyproject.toml alembic.ini requirements.lock ./
COPY src/ ./src/
# requirements.lock is a CONSTRAINTS file (PR 10a): pyproject declares WHAT installs, the lock pins
# the exact versions, so two builds of the same commit resolve identical dependency trees.
# Regenerate with: .venv/bin/pip freeze --exclude-editable > requirements.lock
RUN pip install --no-cache-dir -e '.[s3]' -c requirements.lock

COPY alembic/ ./alembic/
COPY KYC_Tool_Build_Package/ ./KYC_Tool_Build_Package/

USER app
EXPOSE 8000

# Worker containers serve no HTTP; override or disable this there.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]

# Override the command for the other processes:
#   alembic upgrade head
#   python -m kyc_tool.workers.pipeline_worker
#   python -m kyc_tool.workers.outbox_worker
#   python -m kyc_tool.workers.retention
CMD ["uvicorn", "kyc_tool.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
