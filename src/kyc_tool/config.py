"""Environment-driven configuration. Secrets come from env only — never hardcode."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KYC_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://kyc:kyc@localhost:5432/kyc"

    # Platform integration (TODO(integration): real values from the platform team)
    platform_callback_url: str = "http://localhost:9999"  # decision callbacks POST here
    platform_hmac_secret: str = ""  # shared secret; empty only permitted when auth_disabled
    auth_disabled: bool = False  # test/dev escape hatch — never set in production
    hmac_max_skew_seconds: int = 300

    # Normative policy files (the spec package is the single source of truth)
    policy_dir: Path = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"

    # Object storage for raw evidence
    object_store: str = "fs"  # fs | s3
    object_store_root: Path = REPO_ROOT / ".substrate" / "state" / "evidence"
    s3_bucket: str = ""

    # Queue / workers
    worker_poll_seconds: float = 0.5
    job_lease_seconds: int = 120
    job_max_attempts: int = 5
    job_backoff_base_seconds: int = 5

    # Outbox delivery
    outbox_max_attempts: int = 8
    outbox_backoff_base_seconds: int = 10

    # POC tokens
    poc_token_ttl_hours: int = 72

    # Retention (compliance default: 7 years)
    retention_days: int = 7 * 365


def get_settings() -> Settings:
    return Settings()
