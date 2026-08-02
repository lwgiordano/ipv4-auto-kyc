"""Environment-driven configuration. Secrets come from env only — never hardcode.

`validate_for_production()` is the production kill switch: every process that
boots with `environment="production"` runs it at startup and refuses to start on
any unsafe or stub configuration (see api/app.py, workers/*).
"""

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from kyc_tool.security import MAX_HMAC_SKEW_SECONDS

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
# HTTPX exposes one scalar timeout as four phase budgets: pool acquisition, connect, write, and
# response-header read. It is not a total callback-attempt clock, and multiplying by four does
# NOT produce one: each budget resets on I/O ACTIVITY, so a receiver dribbling response headers
# with gaps under the timeout can stall a bare send indefinitely. Measured: a 0.5s timeout —
# nominal 2.0s "envelope" — held a bare send 32s on drizzled headers. That is why the publisher
# ENFORCES this as a hard wall-clock WAIT-BOUND on the whole attempt (send + status + ack drain,
# `publisher._send_for_status`): past 4 × timeout it stops waiting, detaches the attempt, and
# records a retryable failure. `lease > 4 × timeout + margin` (checked below) therefore
# guarantees the PUBLISHER never sits on a claim past its bound. It does not retract the
# detached attempt itself — no in-process design can recall a request whose bytes may already
# be moving — so a late 2xx after failure accounting is the documented at-least-once residual
# (A6) the platform dedupes; the publisher tracks detached attempts and logs CRITICAL if they
# accumulate.
OUTBOX_ATTEMPT_DEADLINE_PHASES = 4

# PostgreSQL int4 (signed 32-bit) upper bound — the storage domain of counter columns like
# outbox.attempts. Config ceilings that feed those columns must not exceed it (re-audit F6).
PG_INT4_MAX = 2_147_483_647

# MAX_HMAC_SKEW_SECONDS (the ONE governed replay window) lives in `security` — the module that
# enforces it — and is imported above so the field domain and the verifier share a single source
# (re-audit `5b0f0b8..b75a320` R4-F1).
# Timestamp-safe ceilings for values that flow into `now() + interval 'N'` arithmetic: generous
# operational maxima far below PostgreSQL timestamptz overflow, so a huge value refuses at config time
# instead of raising DatetimeFieldOverflow deep inside a claim/mint (re-audit R4-F3).
_TS_SAFE_SECONDS = 30 * 24 * 3600  # 2_592_000 (30 days)
_TS_SAFE_HOURS = 10 * 365 * 24  # 87_600 (10 years)
_TS_SAFE_DAYS = 100 * 365  # 36_500 (100 years)


@dataclass(frozen=True)
class NumericSetting:
    """One numeric setting's authority (re-audit `5b0f0b8..b75a320` close-out #1): a unit, an inclusive
    domain [floor, ceiling], the downstream SINK the value flows into (protocol window / destructive
    horizon / lease-poll-backoff / physical DB column), and the owning PROCESS. The Pydantic field
    carries the same bounds (declaration), `production_config_violations` re-checks the whole registry
    so an unvalidated `model_copy` cannot bypass a bound (boundary), and runtime consumers re-check
    their own domain on direct calls (consumer)."""

    name: str
    unit: str
    floor: float
    ceiling: float
    sink: str
    process: str
    integer: bool = True  # most settings are whole units; the sub-second timers set this False
    require_finite: bool = True
    production_exact: float | None = None  # production must equal exactly this
    production_min: float | None = None  # otherwise production floor (inclusive)


# EVERY numeric Settings field appears here exactly once (guarded by a test). Adding a numeric setting
# without a domain is therefore impossible to ship.
NUMERIC_SETTINGS: tuple[NumericSetting, ...] = (
    NumericSetting("hmac_max_skew_seconds", "seconds", 1, MAX_HMAC_SKEW_SECONDS,
                   "signed-request replay window (v1/v2 verify)", "api",
                   production_exact=MAX_HMAC_SKEW_SECONDS),
    NumericSetting("worker_poll_seconds", "seconds", 0.01, 300,
                   "idle queue-worker sleep", "queue worker", integer=False, production_min=0.01),
    NumericSetting("job_lease_seconds", "seconds", 1, _TS_SAFE_SECONDS,
                   "jobs.claim lease expiry (now()+interval)", "queue worker/reaper",
                   production_min=1),
    NumericSetting("job_max_attempts", "attempts", 1, PG_INT4_MAX,
                   "jobs.max_attempts (int4)", "queue worker", production_min=1),
    NumericSetting("job_backoff_base_seconds", "seconds", 0, _TS_SAFE_SECONDS,
                   "queue retry backoff base (saturating)", "queue worker"),
    NumericSetting("outbox_lease_seconds", "seconds", 1, 3600,
                   "outbox claim lease expiry", "outbox publisher", production_min=1),
    NumericSetting("outbox_http_timeout_seconds", "seconds", 0.001, 3600,
                   "per-attempt HTTPX inactivity phase", "outbox publisher", integer=False),
    NumericSetting("outbox_lease_margin_seconds", "seconds", 0, 3600,
                   "DB-accounting margin in the lease rule", "outbox publisher", integer=False),
    NumericSetting("outbox_max_attempts", "attempts", 1, PG_INT4_MAX,
                   "outbox.attempts (int4)", "outbox publisher", production_min=1),
    NumericSetting("outbox_backoff_base_seconds", "seconds", 0, _TS_SAFE_SECONDS,
                   "outbox retry backoff base (saturating)", "outbox publisher"),
    NumericSetting("poc_token_ttl_hours", "hours", 1, _TS_SAFE_HOURS,
                   "POC token expiry (now()+interval)", "orchestration side-effects",
                   production_min=1),
    NumericSetting("ops_lock_timeout_seconds", "seconds", 1, 3600,
                   "ops maintenance lock wait", "ops CLI", production_min=1),
    NumericSetting("ops_statement_timeout_seconds", "seconds", 1, 7200,
                   "ops statement time budget", "ops CLI", production_min=1),
    NumericSetting("retention_days", "days", 1, _TS_SAFE_DAYS,
                   "DELETE cutoff for immutable audit/evidence", "retention worker",
                   production_min=1),
    NumericSetting("hmac_v1_observation_window_days", "days", 0, _TS_SAFE_DAYS,
                   "v1 sunset zero-witness observation window", "api", production_min=1),
)

_NUMERIC_BY_NAME = {ns.name: ns for ns in NUMERIC_SETTINGS}


def numeric_domain_of(name: str) -> NumericSetting:
    """The registered domain for a setting — the shared authority a consumer re-checks against."""
    return _NUMERIC_BY_NAME[name]


def numeric_value_violation(ns: NumericSetting, value) -> str | None:
    """One value against one domain (bool rejected — it is an int subclass; non-finite rejected).
    Returns a message or None. This is the SINGLE checker the field, the boundary and consumers share."""
    if isinstance(value, bool):
        return f"{ns.name} must be numeric ({ns.unit}), not a bool"
    if not isinstance(value, (int, float)):
        return f"{ns.name} must be numeric ({ns.unit}), got {type(value).__name__}"
    if ns.integer and not isinstance(value, int):
        return f"{ns.name} must be a whole number of {ns.unit}, got {value!r}"
    if ns.require_finite and isinstance(value, float) and not math.isfinite(value):
        return f"{ns.name} must be finite ({ns.unit})"
    if not (ns.floor <= value <= ns.ceiling):
        return (f"{ns.name}={value} is outside [{ns.floor}, {ns.ceiling}] {ns.unit} "
                f"(sink: {ns.sink}; owner: {ns.process})")
    return None


def numeric_domain_violations(settings: "Settings") -> list[str]:
    """Every registry domain violation for `settings` (independent of environment). Catches an
    unvalidated `model_copy(update=...)` that bypassed the Pydantic field bounds."""
    out = []
    for ns in NUMERIC_SETTINGS:
        v = numeric_value_violation(ns, getattr(settings, ns.name))
        if v:
            out.append(v)
    return out


def production_numeric_violations(settings: "Settings") -> list[str]:
    """Production-only numeric rules: an exact governed value (skew) or a production floor."""
    out = []
    for ns in NUMERIC_SETTINGS:
        value = getattr(settings, ns.name)
        if isinstance(value, bool):
            continue  # already flagged by the domain check
        if ns.production_exact is not None and value != ns.production_exact:
            out.append(f"{ns.name} must be exactly {ns.production_exact} {ns.unit} in production "
                       f"(the governed {ns.sink}); {value} is not separately authorized")
        elif ns.production_min is not None and value < ns.production_min:
            out.append(f"{ns.name} must be >= {ns.production_min} {ns.unit} in production "
                       f"(sink: {ns.sink})")
    return out


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
    # 1..MAX_HMAC_SKEW_SECONDS; production must equal 300 exactly (see NUMERIC_SETTINGS). An unbounded
    # skew is a replay-window widener (re-audit R4-F1): a year-old signature verified under a 10y skew.
    hmac_max_skew_seconds: int = Field(default=MAX_HMAC_SKEW_SECONDS, ge=1, le=MAX_HMAC_SKEW_SECONDS)

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
    # Days the durable v1 witness must be silent before the inbound sunset. 0 = disabled (dev/test);
    # production requires >= 1 (NUMERIC_SETTINGS production_min).
    hmac_v1_observation_window_days: int = Field(default=0, ge=0, le=_TS_SAFE_DAYS)

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

    # Queue / workers. Every bound below mirrors NUMERIC_SETTINGS (declaration layer); a non-finite or
    # negative poll can kill the idle worker, and a negative/overflowing lease defeats per-case
    # serialization or raises DatetimeFieldOverflow at claim (re-audit R4-F3).
    worker_poll_seconds: float = Field(default=0.5, ge=0.01, le=300)
    job_lease_seconds: int = Field(default=120, ge=1, le=_TS_SAFE_SECONDS)
    # le=int4 max: jobs.max_attempts is a PostgreSQL int4 column, and enqueue() flushes this value
    # into it — a ceiling above int4 max makes a signed inbound event roll back with
    # NumericValueOutOfRange instead of queuing (re-audit `8aba2df..2cee937` R3-F4, the queue sibling
    # of the outbox R2 F6 bound). Bounded here AND in validate_for_production for unvalidated copies.
    job_max_attempts: int = Field(default=5, ge=1, le=PG_INT4_MAX)
    # ge=0 (0 = retry-when-due dev value); ceiling timestamp-safe. The worker uses the shared
    # saturating backoff helper so no accepted value overflows timestamp arithmetic at high attempts.
    job_backoff_base_seconds: int = Field(default=5, ge=0, le=_TS_SAFE_SECONDS)

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
    # Per HTTPX operation timeout for one callback attempt. This is NOT a whole-request wall-clock
    # deadline: HTTPX may spend the scalar budget separately in pool/connect/write/read-header
    # phases, and each resets on activity. It doubles as the publisher's wall-clock budget for
    # draining the acknowledgement body, which IS a real deadline (see `_send_for_status`).
    outbox_http_timeout_seconds: float = Field(default=10.0, ge=0.001, le=3600)
    # Extra room for DB commit/processing around the HTTPX phase budgets. The production kill
    # switch requires the lease to exceed pool+connect+write+read-header timeout phases plus this
    # margin, otherwise a slow-but-valid response can outlive the claim and be reclaimed by another
    # publisher before the first terminal write. Raising `outbox_http_timeout_seconds` raises the
    # required lease four-fold, so the two knobs must be moved together.
    outbox_lease_margin_seconds: float = Field(default=1.0, ge=0, le=3600)
    # ge=1: at least one delivery attempt must be permitted. A row is dead-lettered without a send
    # once its durable attempts reach this ceiling — including a row left at/over the ceiling when
    # this value is LOWERED (re-audit `b39b82a..b53daf4` F2). Each publisher enforces the ceiling it
    # STARTED with; the ceiling is process-local, so ANY fleet-wide change — raise OR lower — is a
    # DRAINED publisher cutover, never a rolling restart (re-audit `d3c0852..23e005e` F4 and
    # `d569a15..4938840` F5: lowering can send once past the new value; raising lets an old lower-max
    # publisher dead-letter — and irreversibly redact a POC token — before a new higher-max one
    # supplies the extra attempts). See DEPLOYMENT §8.
    # le=int4 max: outbox.attempts is a PostgreSQL int4 column. A ceiling above int4 max would let a
    # row's attempts climb past the column domain, overflowing the admission increment mid-write and
    # wedging the row claimed (re-audit `d569a15..4938840` F6). With the ceiling <= int4 max the
    # per-cycle "attempts >= ceiling ⇒ dead-letter before admission" check terminates the row at a
    # value that always fits, so no accepted config can overflow.
    outbox_max_attempts: int = Field(default=8, ge=1, le=PG_INT4_MAX)
    # ge=0: a zero base means "retry when due, no backoff growth" — a valid dev/test value that
    # production refuses below. Negative was accepted before and produced immediate unthrottled
    # re-sends (re-audit `d3c0852..23e005e` F5). The publisher saturates the exponential schedule so
    # no accepted value can overflow PostgreSQL's timestamptz.
    outbox_backoff_base_seconds: int = Field(default=10, ge=0, le=_TS_SAFE_SECONDS)

    # POC tokens. ge=1: a nonpositive TTL mints and emails a token that is already expired (R4-F3).
    poc_token_ttl_hours: int = Field(default=72, ge=1, le=_TS_SAFE_HOURS)

    # One-shot ops commands: how long a maintenance lock may be awaited before the command
    # refuses with the stable OPS_COMMAND_LOCK_TIMEOUT sentinel (nothing changed) instead of
    # hanging a window on an orphan transaction with no diagnosis.
    ops_lock_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    # The SEPARATE ceiling on total statement time for a one-shot ops command (its own governed
    # budget, not a constant derived from the lock — re-audit `538e55e..42e1c7d` F11). A statement
    # may legitimately wait most of the lock budget and THEN run its bounded query, so this must
    # exceed ops_lock_timeout_seconds; `binding.bind()` refuses if it does not. A runaway query
    # then refuses with OPS_COMMAND_STATEMENT_TIMEOUT instead of hanging the window. Default 360 =
    # the historical 60s lock + 300s work headroom, so the default behaviour is unchanged.
    ops_statement_timeout_seconds: int = Field(default=360, ge=1, le=7200)

    # Retention (compliance default: 7 years). ge=1 is LOAD-BEARING: retention prunes with a
    # `now() - interval 'N days'` cutoff, so a nonpositive N makes the cutoff the FUTURE and deletes
    # CURRENT immutable audit/evidence rows (re-audit R4-F2). prune() re-checks this on direct call.
    retention_days: int = Field(default=7 * 365, ge=1, le=_TS_SAFE_DAYS)

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
    # Numeric-setting registry (re-audit `5b0f0b8..b75a320` R4-F1/F2/F3, close-out #1). The Field
    # bounds enforce each domain at construction; these two calls re-check EVERY numeric setting at the
    # production boundary so an unvalidated model_copy(update=...) cannot bypass a bound, and apply the
    # production-only rules (exact 300s replay window; positive leases/TTLs/retention). This single
    # loop subsumes the former per-counter int4 checks.
    v.extend(numeric_domain_violations(settings))
    v.extend(production_numeric_violations(settings))

    # A claim lease shorter than one delivery attempt plus DB/processing margin expires WHILE
    # that attempt is in flight: admission then refuses the terminal for a request the receiver
    # may well have accepted, and the row is redelivered. The publisher enforces
    # `OUTBOX_ATTEMPT_DEADLINE_PHASES × timeout` as a hard wall-clock WAIT-bound on every
    # attempt (see `_send_for_status`), so requiring the lease to exceed that bound plus margin
    # makes "the publisher never waits on a claim past its attempt" an enforced property. A
    # DETACHED attempt's late effect remains the at-least-once residual — see
    # OUTBOX_ATTEMPT_DEADLINE_PHASES.
    # A zero DB-accounting margin leaves no room between the send deadline and lease expiry for the
    # failure/terminal write to land, so the admission budget would not actually cover accounting
    # (re-audit `42e1c7d..b39b82a` F2). Production requires a positive margin.
    if settings.outbox_lease_margin_seconds <= 0:
        v.append(
            "outbox_lease_margin_seconds must be > 0 (a zero DB-accounting margin leaves no room "
            "for the failure/terminal write between the send deadline and lease expiry)"
        )

    # A zero base is a valid dev/test value (retry-when-due) but in production it retries with no
    # throttle growth, so a persistently failing endpoint is re-hit every cycle (re-audit
    # `d3c0852..23e005e` F5). Production requires a positive base.
    if settings.outbox_backoff_base_seconds <= 0:
        v.append(
            "outbox_backoff_base_seconds must be > 0 in production (a zero base retries a failing "
            "endpoint every cycle with no exponential throttle)"
        )

    attempt_deadline = settings.outbox_http_timeout_seconds * OUTBOX_ATTEMPT_DEADLINE_PHASES
    required_lease = attempt_deadline + settings.outbox_lease_margin_seconds
    if settings.outbox_lease_seconds <= required_lease:
        v.append(
            f"outbox_lease_seconds ({settings.outbox_lease_seconds}) must exceed "
            f"the enforced per-attempt deadline ({OUTBOX_ATTEMPT_DEADLINE_PHASES} × "
            f"outbox_http_timeout_seconds = {attempt_deadline}) "
            f"+ outbox_lease_margin_seconds "
            f"({settings.outbox_lease_margin_seconds}) = {required_lease} — "
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
