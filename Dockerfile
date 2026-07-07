# One image for every process. The API is the default command; migrations and
# the workers run the same image with the command overridden (see bottom).
FROM python:3.11-slim-bookworm

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
COPY pyproject.toml alembic.ini ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e '.[s3]'

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
