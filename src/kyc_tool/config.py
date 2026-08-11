"""Environment-driven configuration. Secrets come from env only — never hardcode.

`validate_for_production()` is the production kill switch: every process that
boots with `environment="production"` runs it at startup and refuses to start on
any unsafe or stub configuration (see api/app.py, workers/*).
"""

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationError, field_validator, model_validator
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
    NumericSetting(
        "hmac_max_skew_seconds",
        "seconds",
        1,
        MAX_HMAC_SKEW_SECONDS,
        "signed-request replay window (v1/v2 verify)",
        "api",
        production_exact=MAX_HMAC_SKEW_SECONDS,
    ),
    NumericSetting(
        "worker_poll_seconds",
        "seconds",
        0.01,
        300,
        "idle queue-worker sleep",
        "queue worker",
        integer=False,
        production_min=0.01,
    ),
    NumericSetting(
        "job_lease_seconds",
        "seconds",
        1,
        _TS_SAFE_SECONDS,
        "jobs.claim lease expiry (now()+interval)",
        "queue worker/reaper",
        # Floor 30 in production (re-audit `3db5f13..a7df17b` F4): the heartbeat runs at lease/4,
        # so a 1-2s lease put the FIRST beat inside scheduler/DB jitter of the reaper — heartbeat
        # cadence, DB round-trip, and the adapter-plan DB margin all need real room.
        production_min=30,
    ),
    NumericSetting(
        # Fixed bounded manual-recovery grant (re-audit `7d1c435..827bc0f` F4): the requeue CAS sets
        # max_attempts = attempts + grant, so every exhaust/recover cycle grants EXACTLY this many
        # further attempts — never a doubling of whatever the ceiling had grown to.
        "job_recovery_attempt_grant",
        "attempts",
        1,
        1000,
        "ops requeue CAS budget grant (jobs.max_attempts = attempts + grant, int4-guarded)",
        "ops requeue (api/ui)",
        production_min=1,
    ),
    NumericSetting(
        # Governed adapter response containment (re-audit `7d1c435..827bc0f` F6): both the wire
        # bytes and the DECODED bytes of every governed upstream response are capped at this —
        # oversized Content-Length refuses preflight, chunked overflow and gzip expansion fail
        # closed mid-stream as non-retryable.
        "adapter_max_response_bytes",
        "bytes",
        1024,
        104_857_600,
        "governed adapter transport response cap (wire AND decoded)",
        "pipeline adapters",
        production_min=1024,
    ),
    NumericSetting(
        "job_max_attempts",
        "attempts",
        1,
        PG_INT4_MAX,
        "jobs.max_attempts (int4)",
        "queue worker",
        production_min=1,
    ),
    NumericSetting(
        "job_backoff_base_seconds",
        "seconds",
        0,
        _TS_SAFE_SECONDS,
        "queue retry backoff base (saturating)",
        "queue worker",
    ),
    NumericSetting(
        "outbox_lease_seconds",
        "seconds",
        1,
        3600,
        "outbox claim lease expiry",
        "outbox publisher",
        production_min=1,
    ),
    NumericSetting(
        "outbox_http_timeout_seconds",
        "seconds",
        0.001,
        3600,
        "per-attempt HTTPX inactivity phase",
        "outbox publisher",
        integer=False,
    ),
    NumericSetting(
        "outbox_lease_margin_seconds",
        "seconds",
        0,
        3600,
        "DB-accounting margin in the lease rule",
        "outbox publisher",
        integer=False,
    ),
    NumericSetting(
        "outbox_max_attempts",
        "attempts",
        1,
        PG_INT4_MAX,
        "outbox.attempts (int4)",
        "outbox publisher",
        production_min=1,
    ),
    NumericSetting(
        "outbox_backoff_base_seconds",
        "seconds",
        0,
        _TS_SAFE_SECONDS,
        "outbox retry backoff base (saturating)",
        "outbox publisher",
    ),
    NumericSetting(
        "poc_token_ttl_hours",
        "hours",
        1,
        _TS_SAFE_HOURS,
        "POC token expiry (now()+interval)",
        "orchestration side-effects",
        production_exact=72,
    ),  # security/platform contract: exactly 72h (re-audit R5-F8)
    NumericSetting(
        "ops_lock_timeout_seconds",
        "seconds",
        1,
        3600,
        "ops maintenance lock wait",
        "ops CLI",
        production_min=1,
    ),
    NumericSetting(
        "ops_statement_timeout_seconds",
        "seconds",
        1,
        7200,
        "ops statement time budget",
        "ops CLI",
        production_min=1,
    ),
    NumericSetting(
        "retention_days",
        "days",
        1,
        _TS_SAFE_DAYS,
        "DELETE cutoff for immutable audit/evidence",
        "retention worker",
        production_min=1,
    ),
    NumericSetting(
        "hmac_v1_observation_window_days",
        "days",
        0,
        _TS_SAFE_DAYS,
        "v1 sunset zero-witness observation window",
        "api",
        production_min=1,
    ),
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
        return (
            f"{ns.name}={value} is outside [{ns.floor}, {ns.ceiling}] {ns.unit} "
            f"(sink: {ns.sink}; owner: {ns.process})"
        )
    return None


def require_numeric_domain(name: str, value) -> None:
    """Fail closed on a direct-call value outside a registered domain — the CONSUMER layer a queue
    claim, a retry, a poll or a token mint runs before it reaches PostgreSQL (re-audit R4-F3). Raises
    ValueError so the caller leaves its durable state unchanged."""
    violation = numeric_value_violation(numeric_domain_of(name), value)
    if violation:
        raise ValueError(violation)


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
    """Production-only numeric rules: an exact governed value (skew, TTL) or a production floor. Skips
    any setting the domain pass already rejected, so a malformed value (str/None/NaN supplied via
    model_copy) yields an aggregate ProductionConfigError rather than a raw TypeError from comparing
    it to a floor (re-audit `03dbfab..bc325e7` R5-F11)."""
    out = []
    invalid = {ns.name for ns in NUMERIC_SETTINGS if numeric_value_violation(ns, getattr(settings, ns.name))}
    for ns in NUMERIC_SETTINGS:
        if ns.name in invalid:
            continue  # comparing a domain-invalid value to exact/min would raise; it is already flagged
        value = getattr(settings, ns.name)
        if ns.production_exact is not None and value != ns.production_exact:
            out.append(
                f"{ns.name} must be exactly {ns.production_exact} {ns.unit} in production "
                f"(the governed {ns.sink}); {value} is not separately authorized"
            )
        elif ns.production_min is not None and value < ns.production_min:
            out.append(f"{ns.name} must be >= {ns.production_min} {ns.unit} in production (sink: {ns.sink})")
    return out


# Adapter rate limits are a NESTED numeric sink (dict[str, float]) the flat registry cannot see
# (re-audit `03dbfab..bc325e7` R5-F6): a NaN/negative rate removes the cap, Infinity gives a zero
# interval, and a tiny positive rate makes an effectively infinite sleep that dead-letters runs. The
# reviewed domain is a finite, strictly-positive requests/second in [min, max].
ADAPTER_RATE_MIN = 0.001
ADAPTER_RATE_MAX = 10_000.0

# The ONE canonical adapter registry (re-audit `f2929f8..6a4cd87` F8): rate keys are CLOSED against
# it, so a typo/case/whitespace variant ("companies_hose", "Companies_House", " gleif ") cannot pass
# and silently leave the real adapter unlimited.
ADAPTER_IDS = frozenset({
    "email_verification", "companies_house", "gleif", "floqer_company_enrichment",
    "rir_rdap", "rir_poc", "document_ocr", "website_manual_review",
})


def adapter_rate_violations(rates) -> list[str]:
    """Violations for an adapter_rate_limits mapping (empty ⇒ valid). Shared by the field validator,
    the production boundary, and the RateLimiter consumer. TOTAL over malformed input (re-audit
    `f2929f8..6a4cd87` F8): a non-dict container is a violation, never an AttributeError.

    `None` is a VIOLATION, not an empty mapping (re-audit `4f23f23..97deeae` F1). The field is
    `dict[str, float] = {}`, so `None` only arrives by `model_copy(update=...)` — and treating it
    as "no limits" is the exact fail-open that let `hmac_inbound_extra_keys=None` boot clean and
    then crash: `RateLimiter` clears this check and immediately calls `.items()` on it.
    """
    if type(rates) is not dict:
        return [f"adapter_rate_limits must be a mapping, got {type(rates).__name__}"]
    out = []
    for key, rate in rates.items():
        if key not in ADAPTER_IDS:
            out.append(
                f"adapter_rate_limits key {key!r} is not a registered adapter id "
                f"(closed set: {sorted(ADAPTER_IDS)}) — a typo would leave the real adapter unlimited"
            )
            continue
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            out.append(f"adapter_rate_limits[{key!r}] must be a number ({type(rate).__name__})")
        elif not math.isfinite(rate):
            out.append(f"adapter_rate_limits[{key!r}] must be finite (got {rate})")
        elif not (ADAPTER_RATE_MIN <= rate <= ADAPTER_RATE_MAX):
            out.append(
                f"adapter_rate_limits[{key!r}]={rate} must be in "
                f"[{ADAPTER_RATE_MIN}, {ADAPTER_RATE_MAX}] req/s"
            )
    return out


class Settings(BaseSettings):
    # hide_input_in_errors (re-audit `6feca36..4f23f23` F1): this class holds HMAC secrets, and
    # Pydantic otherwise embeds the rejected `input_value` in ValidationError text. A deployment
    # typo in a rotation key id therefore printed a WORKING 32-character credential into startup
    # logs, CI output, and any incident transcript that captured the boot failure. Field names and
    # our own violation messages carry key ids only, never secret material.
    model_config = SettingsConfigDict(
        env_prefix="KYC_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_bool_for_numeric(cls, data):
        # A Python bool is an int subclass; Pydantic coerces True/False to 1/0 for an int field,
        # silently collapsing a programmatically-supplied numeric horizon (re-audit R5-F6). Reject it
        # at the raw-input stage; numeric env strings ("2555") are unaffected.
        if isinstance(data, dict):
            for ns in NUMERIC_SETTINGS:
                if isinstance(data.get(ns.name), bool):
                    raise ValueError(f"{ns.name} must be a number, not a bool")
            # Nested rate VALUES too (re-audit `f2929f8..6a4cd87` F8): Pydantic coerces a raw/env-JSON
            # True to 1.0 before the field validator sees it, silently minting a 1 req/s cap.
            rates = data.get("adapter_rate_limits")
            if isinstance(rates, dict):
                for key, rate in rates.items():
                    if isinstance(rate, bool):
                        raise ValueError(f"adapter_rate_limits[{key!r}] must be a number, not a bool")
        return data

    @field_validator("adapter_rate_limits")
    @classmethod
    def _validate_adapter_rates(cls, v):
        problems = adapter_rate_violations(v)
        if problems:
            raise ValueError("; ".join(problems))
        return v

    @field_validator("hmac_inbound_key_id", "hmac_outbound_key_id")
    @classmethod
    def _validate_active_key_ids(cls, v, info):
        """The ACTIVE key ids go through the same grammar as the rotation-map keys, at
        CONSTRUCTION (re-audit `4f23f23..97deeae` F7).

        Only rotation keys had a field validator, so `Settings(hmac_inbound_key_id=" padded ")`,
        a tab-only id, and a non-ASCII outbound id all constructed successfully and were caught
        only at the production boundary. Staging is where a rotation is rehearsed, so a value that
        works in staging and refuses in production is exactly backwards — the rehearsal has to
        fail on what production will fail on. An empty id stays legal here because it is the
        unconfigured default outside production, where the boundary still refuses it.
        """
        if v == "":
            return v
        problems = hmac_key_id_violations(v, info.field_name)
        if problems:
            raise ValueError("; ".join(problems))
        return v

    @model_validator(mode="after")
    def _refuse_duplicate_rotation_key_ids(self):
        """Duplicate key ids in the RAW JSON, which `dict` has already collapsed.

        `OPS.CONFIG.ROTATION_KEYS` promises duplicates are refused, and the dict validator cannot
        keep that promise: `{"old": "A", "old": "B"}` is last-key-wins before any validator runs
        (re-audit `4f23f23..97deeae` finding 11). An operator who pastes a key twice with two
        different secrets silently ends up verifying against only one of them, and the other —
        which the peer may still be signing with — is simply gone.

        Re-parsing the environment string is the only place the duplicate is still visible. A
        value set programmatically rather than through the environment cannot be checked this way,
        which is why the dict-level rules above remain the primary layer.
        """
        raw = os.environ.get("KYC_HMAC_INBOUND_EXTRA_KEYS")
        if raw and raw.strip().startswith("{"):
            seen: list[str] = []
            try:
                json.loads(raw, object_pairs_hook=lambda pairs: seen.extend(k for k, _ in pairs))
            except ValueError:
                return self  # malformed JSON is already Pydantic's to report
            duplicates = sorted({k for k in seen if seen.count(k) > 1})
            if duplicates:
                raise ValueError(
                    f"KYC_HMAC_INBOUND_EXTRA_KEYS repeats key_id(s) {duplicates}; JSON keeps only "
                    "the last, so the earlier secret would be silently dropped"
                )
        return self

    @field_validator("hmac_inbound_extra_keys")
    @classmethod
    def _validate_hmac_extra_keys(cls, v, info):
        # Declaration layer for rotation credentials (re-audit `82636da..9ac574f` F1). The active
        # key_id is validated against whatever this model already parsed; field order puts
        # hmac_inbound_key_id first, so the collision check has it in every real construction.
        active = (info.data or {}).get("hmac_inbound_key_id", "")
        problems = hmac_extra_key_violations(v, active)
        if problems:
            raise ValueError("; ".join(problems))
        return v

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
    # Fixed bounded manual-recovery grant (re-audit `7d1c435..827bc0f` F4) — see NUMERIC_SETTINGS.
    job_recovery_attempt_grant: int = Field(default=5, ge=1, le=1000)
    # Governed adapter response cap, wire AND decoded bytes (re-audit `7d1c435..827bc0f` F6).
    adapter_max_response_bytes: int = Field(default=5_242_880, ge=1024, le=104_857_600)
    # PR 10b slice 1: run every governed adapter fetch (real network transports only) in a
    # fork-per-call child the worker TERMINATES at the absolute plan deadline — the
    # unconditionally-killable occupancy bound the in-process header/chunk/EOF proofs cannot
    # give. Off by default: fork-per-call is a real per-fetch cost; enable it where hostile-drip
    # occupancy matters more than fetch latency.
    adapter_hard_kill_boundary: bool = False
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
    # The drained-cutover START GATE (re-audit `f2929f8..6a4cd87` F4): during a
    # KYC_OUTBOX_MAX_ATTEMPTS cutover the operator sets this to the REVIEWED target in every new
    # task definition; a publisher-bearing process whose live ceiling differs then REFUSES TO BOOT,
    # so "attest every new task definition carries the exact value" is executable, not prose.
    # Unset (None) = no cutover in progress; the gate is inert.
    outbox_max_attempts_attested: int | None = Field(default=None, ge=1, le=PG_INT4_MAX)
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
    """Every reason this configuration is unsafe for production. Empty == safe. TOTAL: NEVER RAISES.

    Pure and side-effect-free so /readyz can report the same list a startup boot would reject on.

    Totality is structural, not a claim about having audited every line (re-audit
    `4f23f23..97deeae` F1). The individual validators below check exact built-in types before
    calling `len`/`strip`/`==`/iteration, and this wrapper converts anything they still did not
    anticipate into a FAIL-CLOSED violation. `model_copy(update=...)` bypasses Pydantic entirely,
    so this function receives arbitrary objects on every field; an exception escaping here reaches
    a boot path or `/readyz` as a 500, where a crash reads as an outage rather than as a refusal —
    and a `/readyz` that cannot answer is indistinguishable from one that answered "unsafe".

    Partial results are kept: whatever was diagnosed before the failure is still reported, so the
    operator sees the real violations alongside the one the checks could not classify.
    """
    v: list[str] = []
    try:
        _run_isolated_sections(settings, v)
    except Exception as exc:  # noqa: BLE001 — fail closed on ANY unanticipated value
        # NO EXCEPTION TEXT (re-audit `4f23f23..122cc67` finding 6). `str(exc)` on a hostile value
        # is that value's own `__str__`, and on a Pydantic/`len`/`strip` failure it routinely
        # interpolates the offending value — which for this model is a live credential, printed
        # into a startup log or a /readyz body. The type name alone is enough to diagnose a
        # boundary bug and cannot carry a secret.
        v.append(
            f"configuration could not be fully evaluated ({type(exc).__name__}) — refusing "
            "production boot; some setting holds a type no check anticipated"
        )
    return v


def _run_isolated_sections(settings: Settings, v: list[str]) -> None:
    """Run each check group in its own failure domain.

    One malformed field used to abort the whole aggregate (re-audit `4f23f23..122cc67` finding 6):
    a hostile value in an early section meant a genuinely broken `object_store` later in the list
    was never diagnosed, so the operator fixed one violation, rebooted, and met the next one.
    Isolating the groups means a single unanticipated value costs exactly its own section.
    """
    for name, section in SECTIONS:
        try:
            section(settings, v)
        except Exception as exc:  # noqa: BLE001 — type name only; see the caller
            v.append(
                f"the {name} configuration section could not be evaluated "
                f"({type(exc).__name__}) — refusing production boot"
            )


def _section_auth(settings: Settings, v: list[str]) -> None:
    if settings.auth_disabled:
        v.append("auth_disabled is True (inbound HMAC verification is off)")
    v.extend(hmac_secret_violations(settings.platform_hmac_secret, "platform_hmac_secret"))


def _section_callback_url(settings: Settings, v: list[str]) -> None:
    raw_url = settings.platform_callback_url
    if type(raw_url) is not str:
        v.append(f"platform_callback_url must be a string, got {type(raw_url).__name__}")
        raw_url = ""
    parsed = urlparse(raw_url)
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
            f"platform_callback_url must not carry a query or fragment ({settings.platform_callback_url!r})"
        )


def _section_providers_and_storage(settings: Settings, v: list[str]) -> None:
    # Type first for every string-declared setting these identity checks read (F1): `!=`/`not`
    # are satisfied by a list or an object, so the check below is only meaningful once the value
    # is known to be a string.
    for name in ("object_store", "s3_bucket", "ocr_engine", "email_provider", "adapters_profile",
                 "ui_admin_token"):
        v.extend(string_setting_violations(getattr(settings, name), name))

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
    # The token gates the ALWAYS-mounted /v1/ops requeue endpoints, not just the optional console
    # (re-audit `f2929f8..6a4cd87` F3): production requires it unconditionally so the RUNBOOK's
    # dead-letter recovery path exists in the secure configuration.
    if not settings.ui_admin_token:
        v.append(
            "ui_admin_token is empty — it authenticates the always-mounted /v1/ops requeue "
            "endpoints (and the console when ui_enabled)"
        )


def _section_hmac(settings: Settings, v: list[str]) -> None:
    # HMAC v2 (PR 5a): split secrets/key_ids, both sunset dates, and a positive
    # observation window are all required in production — this is what enforces
    # the spec's "fixed sunset" (no dual-accept-forever) and the durable witness.
    v.extend(hmac_secret_violations(
        settings.hmac_inbound_secret, "hmac_inbound_secret (v2 inbound verification)"))
    v.extend(hmac_secret_violations(
        settings.hmac_outbound_secret, "hmac_outbound_secret (v2 callback signing)"))
    # Rotation keys are FULL verification credentials (api/auth.py resolves a v2 secret through
    # them), so they carry the same floor as the active key (re-audit `82636da..9ac574f` F1: the
    # boundary validated only the active pair, so a one-character secondary secret — or one under
    # an empty key id — authenticated real requests while the kill switch stayed green). Re-checked
    # HERE as well as at construction because an unvalidated model_copy(update=...) bypasses the
    # field validator.
    # Both key ids go through the same total grammar (F2): a non-str or whitespace-only active id
    # previously crashed this function or booted clean.
    v.extend(hmac_key_id_violations(settings.hmac_inbound_key_id, "hmac_inbound_key_id"))
    v.extend(hmac_key_id_violations(settings.hmac_outbound_key_id, "hmac_outbound_key_id"))
    v.extend(hmac_extra_key_violations(settings.hmac_inbound_extra_keys, settings.hmac_inbound_key_id))
    v.extend(hmac_sunset_violations(
        settings.hmac_v1_inbound_sunset_at, "hmac_v1_inbound_sunset_at"))
    v.extend(hmac_sunset_violations(
        settings.hmac_v1_outbound_sunset_at, "hmac_v1_outbound_sunset_at"))

def _section_numeric(settings: Settings, v: list[str]) -> None:
    # Numeric-setting registry (re-audit `5b0f0b8..b75a320` R4-F1/F2/F3, close-out #1). The Field
    # bounds enforce each domain at construction; these two calls re-check EVERY numeric setting at the
    # production boundary so an unvalidated model_copy(update=...) cannot bypass a bound, and apply the
    # production-only rules (exact 300s replay window; positive leases/TTLs/retention). This single
    # loop subsumes the former per-counter int4 checks.
    v.extend(numeric_domain_violations(settings))
    v.extend(production_numeric_violations(settings))
    v.extend(adapter_rate_violations(settings.adapter_rate_limits))  # nested numeric sink (R5-F6)
    # Fields the domain pass rejected: every dependent cross-field rule below SKIPS them so a
    # malformed operand (str/None via model_copy) yields the aggregate diagnosis, never a raw
    # TypeError mid-arithmetic (re-audit `f2929f8..6a4cd87` F8 extending R5-F11 to cross-field).
    _invalid = {
        ns.name for ns in NUMERIC_SETTINGS
        if numeric_value_violation(ns, getattr(settings, ns.name))
    }

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
    if "outbox_lease_margin_seconds" not in _invalid and settings.outbox_lease_margin_seconds <= 0:
        v.append(
            "outbox_lease_margin_seconds must be > 0 (a zero DB-accounting margin leaves no room "
            "for the failure/terminal write between the send deadline and lease expiry)"
        )

    # A zero base is a valid dev/test value (retry-when-due) but in production it retries with no
    # throttle growth, so a persistently failing endpoint is re-hit every cycle (re-audit
    # `d3c0852..23e005e` F5). Production requires a positive base.
    if "outbox_backoff_base_seconds" not in _invalid and settings.outbox_backoff_base_seconds <= 0:
        v.append(
            "outbox_backoff_base_seconds must be > 0 in production (a zero base retries a failing "
            "endpoint every cycle with no exponential throttle)"
        )

    _lease_fields = {"outbox_http_timeout_seconds", "outbox_lease_margin_seconds", "outbox_lease_seconds"}
    if _lease_fields & _invalid:
        return  # operands already diagnosed; the arithmetic below would raise on them
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

    return


# Each entry is its own failure domain; see `_run_isolated_sections`.
SECTIONS: tuple[tuple[str, object], ...] = (
    ("auth", _section_auth),
    ("callback URL", _section_callback_url),
    ("providers and storage", _section_providers_and_storage),
    ("HMAC", _section_hmac),
    ("numeric", _section_numeric),
)


def validate_for_production(settings: Settings) -> None:
    """Refuse to start a production process on any unsafe configuration."""
    violations = production_config_violations(settings)
    if violations:
        raise ProductionConfigError("refusing to start in production — fix all of: " + "; ".join(violations))


class ProcessRole(StrEnum):
    """Every executable entry point's role. dev_worker is DEV-ONLY (it always wires fixture adapters);
    the rest are production roles."""

    API = "api"
    PIPELINE_WORKER = "pipeline_worker"
    OUTBOX_WORKER = "outbox_worker"
    RETENTION = "retention"
    DEV_WORKER = "dev_worker"


_KEY_ID_MAX_LEN = 128


def hmac_key_id_violations(key_id: object, label: str) -> list[str]:
    """One TOTAL grammar for every HMAC key id, active or rotation (re-audit `6feca36..4f23f23`
    F2). Takes `object`, not `str`: `model_copy(update=...)` skips Pydantic, so the boundary
    receives whatever the caller passed and must return violations rather than raise. A key id is
    matched verbatim against the X-KYC-Key-Id header, so it must be a non-blank, untrimmed,
    bounded, printable-ASCII string."""
    # EXACT type, not isinstance (re-audit `4f23f23..122cc67` finding 6): a `str` subclass
    # overriding `__eq__`/`strip` controls every operation below, so `isinstance` hands the
    # boundary's own logic to the value it is judging.
    if type(key_id) is not str:
        return [f"{label} must be a string, got {type(key_id).__name__}"]
    if not key_id.strip():
        return [f"{label} is blank/whitespace"]
    problems = []
    if key_id != key_id.strip():
        problems.append(f"{label} has surrounding whitespace; the header is compared verbatim")
    if len(key_id) > _KEY_ID_MAX_LEN:
        problems.append(f"{label} exceeds {_KEY_ID_MAX_LEN} characters")
    if any(not (32 <= ord(ch) < 127) for ch in key_id):
        problems.append(f"{label} contains control or non-ASCII characters")
    return problems


def string_setting_violations(value: object, label: str) -> list[str]:
    """A production setting declared `str` really is a non-blank string.

    Most checks on these fields are equality or truthiness — `ocr_engine == STUB_OCR_ENGINE`,
    `if not s3_bucket` — and a list, dict or object satisfies neither branch, so the boundary
    certified it and the value crashed later inside the provider factory or the storage client
    (re-audit `4f23f23..97deeae` F1). Identity checks cannot substitute for a type check.
    """
    if type(value) is not str:
        return [f"{label} must be a string, got {type(value).__name__}"]
    if not value.strip():
        return [f"{label} is blank/whitespace"]
    return []


def hmac_secret_violations(secret: object, label: str) -> list[str]:
    """One TOTAL grammar for every HMAC secret — legacy, inbound, outbound, rotation.

    Takes `object` and checks the exact built-in type FIRST (re-audit `4f23f23..97deeae` F1).
    `len()` was being called on whatever `model_copy(update=...)` put there: a 40-entry dict has
    `len` 40 and so cleared the 32-character floor, certifying the configuration, and the resolver
    then handed that dict to `sign_v2`, which raised `AttributeError` on `.encode` INSIDE signature
    verification — a clean production boot followed by a 500 on an authenticated request. An int
    raised in `len()` before the aggregate could even be built.

    The floor is measured on the TRIMMED value: 32 spaces is 32 characters and cleared the old
    check while being a trivially guessable credential.
    """
    if type(secret) is not str:
        return [f"{label} must be a string, got {type(secret).__name__}"]
    if not secret.strip():
        return [f"{label} is blank/whitespace"]
    problems = []
    if len(secret.strip()) < _MIN_HMAC_SECRET_LEN:
        problems.append(f"{label} is weak (< {_MIN_HMAC_SECRET_LEN} non-whitespace chars)")
    if secret != secret.strip():
        problems.append(
            f"{label} has surrounding whitespace, which is part of the key and is almost always a "
            ".env transcription accident — every signature would differ from the peer's"
        )
    return problems


def hmac_sunset_violations(iso: object, label: str) -> list[str]:
    """One TOTAL grammar for both v1 sunset dates. Same reason as the secrets: an int reached
    `.replace` inside the parser and raised instead of producing a violation."""
    if type(iso) is not str:
        return [f"{label} must be a string, got {type(iso).__name__}"]
    if not iso.strip():
        return [f"{label} is unset (dual-accept-forever is not allowed)"]
    try:
        parse_sunset(iso)
    except (ValueError, TypeError):
        return [f"{label} is not timezone-aware ISO-8601 ({iso!r})"]
    return []


def hmac_extra_key_violations(extra: object, active_key_id: object) -> list[str]:
    """Every rotation key must clear the SAME floor as the active key (re-audit
    `82636da..9ac574f` F1). `api/auth._inbound_secret` resolves a v2 verification secret out of
    this mapping, so a weak or blank-keyed entry is a live authentication credential, not
    inert configuration. Returns violations; empty means the mapping is safe to verify against.

    Checked in BOTH layers: the field validator refuses at construction, and
    `production_config_violations` re-checks so an unvalidated `model_copy(update=...)` cannot
    slip a mapping past the boundary."""
    # TYPE FIRST, then emptiness (re-audit `4f23f23..97deeae` F1). This read `extra == {}` before
    # the isinstance check, so an object with a hostile `__eq__` raised out of the comparison and
    # took the whole aggregate down before it could classify anything.
    #
    # ONLY {} is the empty map. `None` used to short-circuit here as "safe", but `_inbound_secret`
    # then called `.get` on it and raised AttributeError inside signature verification — a clean
    # boot followed by a 500 on an authenticated request (re-audit `6feca36..4f23f23` F2).
    if type(extra) is not dict:
        return [
            "hmac_inbound_extra_keys must be a mapping of key_id -> secret ({} when unused), got "
            f"{type(extra).__name__}"
        ]
    if not extra:
        return []
    problems: list[str] = []
    seen: dict[str, str] = {}
    active_trimmed = active_key_id.strip() if type(active_key_id) is str else None
    for key_id, secret in extra.items():
        if type(key_id) is not str or type(secret) is not str:
            problems.append(
                f"hmac_inbound_extra_keys entry {key_id!r} must map str -> str "
                f"(got {type(key_id).__name__} -> {type(secret).__name__})"
            )
            continue
        id_problems = hmac_key_id_violations(key_id, f"hmac_inbound_extra_keys key_id {key_id!r}")
        if any("blank" in p for p in id_problems):
            problems.append(
                "hmac_inbound_extra_keys has a blank/whitespace key_id — an empty X-KYC-Key-Id "
                "header would resolve to its secret"
            )
            continue
        problems.extend(id_problems)
        trimmed = key_id.strip()
        if active_trimmed is not None and trimmed == active_trimmed:
            problems.append(
                f"hmac_inbound_extra_keys key_id {trimmed!r} collides with the ACTIVE inbound "
                "key_id; the active secret wins and this rotation entry is silently dead"
            )
        if trimmed in seen:
            problems.append(f"hmac_inbound_extra_keys key_id {trimmed!r} is duplicated")
        seen[trimmed] = secret
        problems.extend(hmac_secret_violations(
            secret, f"hmac_inbound_extra_keys secret for {trimmed!r} (rotation keys verify real "
                    "requests)"))
    return problems


_DEV_ONLY_ROLES = frozenset({ProcessRole.DEV_WORKER})
# Roles that run an outbox publisher — the cutover start gate applies to exactly these (the same
# closed set the cutover record names).
_PUBLISHER_ROLES = frozenset({ProcessRole.OUTBOX_WORKER, ProcessRole.DEV_WORKER})


def validate_process_role(settings: Settings, role: ProcessRole) -> None:
    """The single process-role authority (re-audit `03dbfab..bc325e7` R5-F1). Called by EVERY
    executable entry point BEFORE it creates a database engine, network client, or object store.

    In a production environment a DEV-ONLY role (fixture adapters — synthetic checks/decisions/
    callbacks, and it would consume real `run_transition` jobs) is refused CATEGORICALLY, independent
    of whether the rest of the configuration is otherwise valid; a production role runs the full
    production kill switch. Outside production this is a no-op — dev/staging may run any role."""
    role = ProcessRole(role)
    # Cutover start gate (re-audit `f2929f8..6a4cd87` F4) — enforced in EVERY environment whenever
    # the operator has set an attested target, because the drained cutover is rehearsed in staging
    # too. A publisher-bearing process whose live ceiling differs from the attested target would
    # recreate the mixed-ceiling extra-send / premature-dead-letter hazard the drained cutover
    # exists to prevent; it refuses BEFORE any engine/store access.
    # SCOPE — PER-PROCESS MITIGATION, NOT FLEET ATTESTATION (re-audit `3db5f13..a7df17b` F6): both
    # values here come from the SAME process environment, so this catches a task definition that
    # missed the reviewed pair or disagrees with itself — it CANNOT catch a fleet whose task
    # definitions are internally consistent but mutually different (old (8,8) next to new (12,12)
    # both boot). The independent authority — a DB CAS cutover record + orchestrator-inventory
    # receipt every publisher must match before claiming — needs a table and is reserved into
    # migration 028 (PR 10b); the drained STOP/ATTEST-ZERO procedure remains the operative control
    # until then.
    attested = settings.outbox_max_attempts_attested
    if (
        role in _PUBLISHER_ROLES
        and attested is not None
        and settings.outbox_max_attempts != attested
    ):
        raise ProductionConfigError(
                f"cutover start gate: this {role.value} carries outbox_max_attempts="
                f"{settings.outbox_max_attempts} but the attested target is "
                f"{settings.outbox_max_attempts_attested} (KYC_OUTBOX_MAX_ATTEMPTS_ATTESTED) — a "
                "stale task definition; refusing to start (DEPLOYMENT §8 drained cutover)"
            )
    if settings.environment != "production":
        return
    if role in _DEV_ONLY_ROLES:
        raise ProductionConfigError(
            f"process role {role.value!r} is DEV-ONLY (fixture adapters) and must never run in a "
            "production environment — refusing before any database/network/store access"
        )
    validate_for_production(settings)


class ConfigLoadError(RuntimeError):
    """Configuration failed to load. Carries locations and reasons, never values."""


def _sanitized_validation_report(error: ValidationError) -> str:
    """Location, type and message for each failure — and nothing else.

    `hide_input_in_errors=True` keeps the offending value out of `str()` and `repr()`, which is
    where a traceback prints it. It does NOT remove it from `error.errors()` or `error.json()`
    (re-audit `4f23f23..97deeae` F6), so anything that logs structured validation detail — a JSON
    log formatter, an error reporter, a CI annotation — still ships the live secret. This builds
    the report from three fields explicitly rather than filtering a dict, so a future Pydantic
    version that adds another value-bearing field cannot widen it by default.
    """
    lines = []
    for detail in error.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in detail.get("loc", ())) or "(model)"
        lines.append(f"{where}: {detail.get('type', 'invalid')}: {detail.get('msg', '')}")
    return "; ".join(lines) or "invalid configuration"


def get_settings() -> Settings:
    """The ONE settings loader. Every executable entry point goes through it.

    A raw `ValidationError` escaping here would print the rejected value — which, for the settings
    this model holds, is a live credential — into startup logs, CI output, and incident
    transcripts. `from None` matters as much as the message: without it the original error stays
    on the `__context__` chain and Python prints it under "During handling of the above exception".
    """
    report = None
    try:
        return Settings()
    except ValidationError as error:
        report = _sanitized_validation_report(error)
    # RAISED OUTSIDE THE except BLOCK, deliberately (re-audit `4f23f23..122cc67` finding 7).
    # `raise ... from None` inside the handler suppresses the PRINTING of `__context__`, but the
    # raw Pydantic error stays attached — and its `.json()` still carries the live secret, which
    # structured error collectors read by walking cause/context chains. Leaving the block first
    # means the new error is created with no implicit context at all.
    raise ConfigLoadError(f"invalid configuration — {report}")
