"""Environment-driven configuration. Secrets come from env only — never hardcode.

`validate_for_production()` is the production kill switch: every process that
boots with `environment="production"` runs it at startup and refuses to start on
any unsafe or stub configuration (see api/app.py, workers/*).
"""

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_sunset(iso: str) -> datetime | None:
    """Parse an ISO-8601 dual-accept sunset timestamp as timezone-aware UTC.

    Returns None for the empty string (unset ⇒ dual-accept stays open). Raises
    ValueError for a malformed or timezone-naive value so a bad date fails the
    production kill switch at boot (validate_for_production) instead of raising
    deep inside request auth or callback delivery at runtime.
    """
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"sunset timestamp is timezone-naive: {iso!r}")
    return dt

# Provider identifiers whose implementation is a dev/test stub. Selecting any of
# these in production is refused at startup — the real providers land with the
# executable-contract work (remediation item 12).
STUB_OCR_ENGINE = "json_scan"
STUB_EMAIL_PROVIDER = "logging"
STUB_ADAPTERS_PROFILE = "fixture"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
_MIN_HMAC_SECRET_LEN = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KYC_", env_file=".env", extra="ignore")

    # Deployment environment. In "production", validate_for_production() runs at
    # process start and refuses to boot on any unsafe/stub configuration.
    environment: Literal["development", "test", "production"] = "development"

    database_url: str = "postgresql+psycopg://kyc:kyc@localhost:5432/kyc"

    # Platform integration (TODO(integration): real values from the platform team)
    platform_callback_url: str = "http://localhost:9999"  # decision callbacks POST here
    platform_hmac_secret: str = ""  # shared secret; empty only permitted when auth_disabled
    auth_disabled: bool = False  # test/dev escape hatch — never set in production
    hmac_max_skew_seconds: int = 300

    # HMAC v2 (PR 5a — path-bound canonical signing). Split inbound/outbound
    # secrets + key_id; v1's shared platform_hmac_secret stays required until
    # BOTH v1 sunsets have passed (it verifies inbound v1 and signs the outbound
    # v1 dual-emit). extra_keys carries accepted-but-not-active keys
    # (key_id -> secret) so a secret can be rotated without a flag day.
    hmac_inbound_key_id: str = ""
    hmac_inbound_secret: str = ""
    hmac_inbound_extra_keys: dict[str, str] = {}
    hmac_outbound_key_id: str = ""
    hmac_outbound_secret: str = ""
    # Bidirectional dual-accept sunsets (ISO-8601; "" = unset ⇒ dual-accept).
    # inbound is gated by the zero-witness; outbound by a staging callback E2E +
    # TechCraft sign-off (never inferred from inbound telemetry).
    hmac_v1_inbound_sunset_at: str = ""
    hmac_v1_outbound_sunset_at: str = ""
    # Days the durable v1 witness must be silent before the inbound sunset.
    hmac_v1_observation_window_days: int = 0

    # M3 compatibility gate: don't emit the new `event_sequence` callback field
    # until the platform has agreed to consume it. Off until the cutover.
    callback_include_event_sequence: bool = False

    # Operator authentication. Both default OFF so local dev/test run open;
    # validate_for_production() forces them on. read_auth_required gates the
    # /v1 read GETs behind a signed request; ui_admin_token is the bearer
    # credential guarding every mutating /ui endpoint.
    read_auth_required: bool = False
    ui_admin_token: str = ""

    # SAFETY OVERLAY (temporary — remove once remediation items 3–5 land):
    # while the approval-grade validators are known-permissive, the tool must
    # not emit an auto-enforceable positive decision. When False, decide()'s
    # approve / approve_buy_locked outcomes are held for manual review at
    # emission; the computed decision is preserved in the audit trail and the
    # callback carries an `enforcement_held` marker.
    enforce_positive_decisions: bool = False

    # PR 6 (item 7A): when True, the pipeline worker loads each run's recorded
    # policy bundle by hash and scores under it, refusing if unloadable. Off by
    # default; activated via a drained worker-pool cutover (docs/DEPLOYMENT.md §…).
    enforce_bundle_pinning: bool = False

    # Normative policy files (the spec package is the single source of truth)
    policy_dir: Path = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"

    # Object storage for raw evidence
    object_store: str = "fs"  # fs | s3
    object_store_root: Path = REPO_ROOT / ".substrate" / "state" / "evidence"
    s3_bucket: str = ""

    # Pluggable providers — dev/test stubs by default; production requires real
    # ones. Wired in workers/pipeline_worker.build_adapters (ocr/adapters) and
    # workers/outbox_worker.build_publisher (email); a non-stub value that is
    # not yet implemented fails loudly rather than silently using a stub.
    ocr_engine: str = STUB_OCR_ENGINE
    email_provider: str = STUB_EMAIL_PROVIDER  # logging | file | (real: item 12)
    adapters_profile: str = STUB_ADAPTERS_PROFILE  # fixture | real
    # Sink path for email_provider="file" (closed staging/dev only): each token
    # email is appended as a JSON line so the POC round-trip is testable.
    email_file_path: Path = REPO_ROOT / ".substrate" / "state" / "poc-emails.log"

    # Queue / workers
    worker_poll_seconds: float = 0.5
    job_lease_seconds: int = 120
    job_max_attempts: int = 5
    job_backoff_base_seconds: int = 5

    # Per-upstream rate limits (requests/second per adapter_id); JSON in env,
    # e.g. KYC_ADAPTER_RATE_LIMITS='{"rir_rdap": 2, "companies_house": 5}'.
    # Process-local — divide by worker count when scaling out.
    adapter_rate_limits: dict[str, float] = {}

    # Outbox delivery. The lease is the crash-detection window for ONE claimed delivery
    # attempt — deliberately its own knob, never derived from the retry backoff: admission
    # (DB trigger + publisher fence) refuses evidence from an expired claim, so a lease that
    # collapses toward zero would make every claim unable to deliver anything at all.
    #
    # `le=3600` is declared here rather than silently clamped at the claim SQL (re-audit
    # `cbb783b` F5: the claim used min(lease, 3600), so a configured 7200 became one hour and
    # nothing said so). The upper bound is the outer limit on how long a crashed publisher can
    # hold a row hostage; the lower bound that matters is enforced against the HTTP budget in
    # `production_config_violations`, because a lease shorter than one delivery attempt makes
    # every claim expire mid-flight.
    outbox_lease_seconds: int = Field(default=300, ge=1, le=3600)
    # The absolute wall-clock HTTP budget the publisher gives one callback attempt. HTTPX's own
    # scalar timeout is an inactivity timeout per socket operation; the publisher also enforces
    # this value with a monotonic deadline while consuming the response body.
    outbox_http_timeout_seconds: float = Field(default=10.0, gt=0)
    # Extra room for DB commit/processing around the absolute HTTP budget. The production kill
    # switch requires the lease to exceed timeout + margin, otherwise a slow-but-valid response can
    # outlive the claim and be reclaimed by another publisher before the first terminal write.
    outbox_lease_margin_seconds: float = Field(default=1.0, ge=0)
    outbox_max_attempts: int = 8
    outbox_backoff_base_seconds: int = 10

    # POC tokens
    poc_token_ttl_hours: int = 72

    # Retention (compliance default: 7 years)
    retention_days: int = 7 * 365

    # Ops console (/ui): debug/ops tooling whose composer + requeue endpoints
    # MUTATE. Disabled by default; when enabled in production it MUST have an
    # admin token (validate_for_production enforces this) and the mutations are
    # gated by that token (api/auth.require_admin).
    ui_enabled: bool = False


class ProductionConfigError(RuntimeError):
    """Raised at startup when a production process is misconfigured. The message
    lists every violation so the operator can fix them all at once."""


def production_config_violations(settings: Settings) -> list[str]:
    """Every reason this configuration is unsafe for production. Empty == safe.

    Pure and side-effect-free so /readyz can report the same list a startup
    boot would reject on.
    """
    v: list[str] = []

    if settings.auth_disabled:
        v.append("auth_disabled is True (inbound HMAC verification is off)")

    secret = settings.platform_hmac_secret or ""
    if not secret:
        v.append("platform_hmac_secret is empty")
    elif len(secret) < _MIN_HMAC_SECRET_LEN:
        v.append(f"platform_hmac_secret is weak (< {_MIN_HMAC_SECRET_LEN} chars)")

    parsed = urlparse(settings.platform_callback_url or "")
    if parsed.scheme != "https":
        v.append(f"platform_callback_url is not HTTPS ({settings.platform_callback_url!r})")
    if (parsed.hostname or "") in _LOCAL_HOSTS:
        v.append(f"platform_callback_url points at localhost ({settings.platform_callback_url!r})")
    raw_callback = settings.platform_callback_url or ""
    if "?" in raw_callback or "#" in raw_callback:
        # We append /kyc/decision to this base; a query or fragment — even a bare
        # "?"/"#" that urlparse reports as an empty component — would land the
        # suffix inside it and misdirect the signed callback (audit finding 1/2).
        v.append(
            f"platform_callback_url must not carry a query or fragment "
            f"({settings.platform_callback_url!r})"
        )

    if settings.object_store != "s3":
        v.append(f"object_store is {settings.object_store!r}, not s3")
    elif not settings.s3_bucket:
        v.append("object_store is s3 but s3_bucket is empty")

    if settings.ocr_engine == STUB_OCR_ENGINE:
        v.append(f"ocr_engine is the dev stub ({STUB_OCR_ENGINE!r})")
    if settings.email_provider == STUB_EMAIL_PROVIDER:
        v.append(f"email_provider is the dev stub ({STUB_EMAIL_PROVIDER!r})")
    if settings.email_provider == "file":
        v.append("email_provider is the staging file sink (writes raw tokens to disk)")
    if settings.adapters_profile == STUB_ADAPTERS_PROFILE:
        v.append(f"adapters_profile is the fixture stub ({STUB_ADAPTERS_PROFILE!r})")

    if not settings.read_auth_required:
        v.append("read_auth_required is False (the read API would be unauthenticated)")
    if settings.ui_enabled and not settings.ui_admin_token:
        v.append("ui_enabled is True but ui_admin_token is empty (unauthenticated ops console)")

    # HMAC v2 (PR 5a): split secrets/key_ids, both sunset dates, and a positive
    # observation window are all required in production — this is what enforces
    # the spec's "fixed sunset" (no dual-accept-forever) and the durable witness.
    if not settings.hmac_inbound_secret or len(settings.hmac_inbound_secret) < _MIN_HMAC_SECRET_LEN:
        v.append("hmac_inbound secret is missing/weak (v2 inbound verification)")
    if not settings.hmac_outbound_secret or len(settings.hmac_outbound_secret) < _MIN_HMAC_SECRET_LEN:
        v.append("hmac_outbound secret is missing/weak (v2 callback signing)")
    if not settings.hmac_inbound_key_id:
        v.append("hmac_inbound_key_id is empty")
    if not settings.hmac_outbound_key_id:
        v.append("hmac_outbound_key_id is empty")
    for label, iso in (
        ("inbound", settings.hmac_v1_inbound_sunset_at),
        ("outbound", settings.hmac_v1_outbound_sunset_at),
    ):
        if not iso:
            v.append(f"hmac_v1 {label} sunset date is unset (dual-accept-forever is not allowed)")
            continue
        try:
            parse_sunset(iso)
        except ValueError:
            v.append(f"hmac_v1 {label} sunset date is not timezone-aware ISO-8601 ({iso!r})")
    if settings.hmac_v1_observation_window_days < 1:
        v.append("hmac_v1 observation window days must be >= 1")

    # A claim lease shorter than one delivery attempt's absolute HTTP budget plus DB/processing
    # margin expires WHILE that attempt is in flight: admission then refuses the terminal for a
    # request the receiver may well have accepted, and the row retries forever. HTTPX's scalar
    # timeout is not a whole-attempt deadline, so the publisher enforces this same budget with a
    # monotonic clock and production must leave margin for the terminal transaction.
    required_lease = settings.outbox_http_timeout_seconds + settings.outbox_lease_margin_seconds
    if settings.outbox_lease_seconds <= required_lease:
        v.append(
            f"outbox_lease_seconds ({settings.outbox_lease_seconds}) must exceed "
            f"outbox_http_timeout_seconds + outbox_lease_margin_seconds ({required_lease}) — "
            f"a lease that expires mid-attempt makes every delivery unwitnessable"
        )

    return v


def validate_for_production(settings: Settings) -> None:
    """Refuse to start a production process on any unsafe configuration."""
    violations = production_config_violations(settings)
    if violations:
        raise ProductionConfigError("refusing to start in production — fix all of: " + "; ".join(violations))


def get_settings() -> Settings:
    return Settings()
