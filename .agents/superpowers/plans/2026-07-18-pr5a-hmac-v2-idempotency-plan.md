# PR 5a — HMAC v2 + per-case idempotency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind method+path into request signatures (HMAC v2), scope idempotency per-case, and roll it out safely behind a dual-accept window — closing the cross-case redirect hazard that keeps M2 frozen.

**Architecture:** Add a versioned v2 canonical signing scheme alongside v1 in `security.py`; make the verifier in `api/auth.py` "sticky v2" (any v2 header ⇒ v2-only, no v1 fallback). Migration 010 re-scopes event idempotency to `(case_id, idempotency_key)` and adds a durable, fail-closed v1 **observation witness**. Retire the duplicate `/v1/review-tasks/{id}/complete` endpoint, moving its validation floor onto the keyed `website.review_completed` event path. Callbacks dual-emit v1+v2 until an independent outbound sunset. Ships as a **non-hot stop/migrate/start cutover**.

**Tech Stack:** Python 3.11, FastAPI (sync + threadpool), SQLAlchemy 2 + Alembic (sync psycopg), Postgres, pytest + testcontainers-style ephemeral PG (`tests/pg.py`), `./manage.sh {fmt,lint,test}`.

## Plan-mode refinements (AUTHORITATIVE — override the task bodies below)

Verified against the code during the plan-mode pass (2026-07-18):
- **Constraint name:** the events unique is `uq_events_idempotency_key` (named in
  migration 001), NOT the PG auto-name. Task 3 drops/recreates that exact name.
- **Task 8 floor rolls back:** implement the `website.review_completed` floor
  inside `ingest_event` after the replay branch, before the `Run` (after
  `ingest.py:167`), by raising a private `_FloorReject(IngestOutcome)` so the
  `uow` rolls back the event insert (no orphan row; re-validates each attempt);
  catch it just outside the `uow` and return the 4xx. No call-site changes; covers
  the HTTP route AND the UI's direct `ingest_event` path.
- **Fixtures:** `test_migrations.py`, `test_production_config.py`, `test_ops_auth.py`
  ALREADY EXIST — extend them, don't create. `test_migrations.py` has an
  `EXPECTED_TABLES` set + `_config(url)` helper; add the two new tables + the
  downgrade-refusal test there. No `clean_db_010`: add `hmac_signature_stats` to
  `_ALL_TABLES` (`tests/conftest.py:33-46`) and re-seed the single
  `hmac_v1_observation` row (id=1, inactive) after the truncate (mirror the
  `broker_entities` re-seed). The `migrated` fixture runs `alembic upgrade head`,
  so 010 tables exist automatically.
- **Task 6 scope:** `require_read_access` (4 GET sites in `routes_read.py`) also
  becomes v2-aware + takes `request` (empty slot). `require_admin` (bearer) and
  the UI path are OUT of scope. `request` is already in scope at every call site.
- **Test invocation:** `./manage.sh test` ignores path args (runs the whole
  suite — the final gate). For per-task red-green, invoke `.venv/bin/pytest
  <path>::<test> -q` directly. `fmt`/`lint` via `./manage.sh`.

## Global Constraints

- **Spec is authoritative:** `.agents/superpowers/specs/2026-07-18-pr5a-hmac-v2-idempotency-design.md` (rev 6, Codex `REVIEW-CLEAN`). Every task traces to a spec section.
- **TDD, red first:** each behavior gets a failing test run against the **ephemeral dev-stack Postgres** (`bash scripts/dev.sh` or the `engine`/`clean_db` fixtures) — not "trust CI" — before implementation.
- **`KYC_Tool_Build_Package/` is immutable.** The endpoint retirement + nonce omission are recorded as deviations in `AUDIT_FINDINGS.md`; the workflow adoption stays an ADR.
- **M2 hard stop is untouched.** Nothing here enables `enforce_positive_decisions` or the M2 kill switch.
- **Bus discipline:** files are already CLAIMed for PR 5a; the parent agent is the sole committer/pusher/bus-writer; commit per task; do not RELEASE until Task 12.
- **v1 stays until sunset:** do NOT delete v1 signing/verification in this PR (dual-accept depends on it).
- **Canonical v2 value (verbatim):** `v2 \n key_id \n direction \n method \n raw_path+query \n timestamp \n slot \n sha256(hex, body)`; `slot` = idempotency key for event POSTs, else empty. `direction` ∈ {`platform->tool`, `tool->platform`}.
- **Precedence (verbatim):** any v2 header present ⇒ require complete valid v2, **no v1 fallback**, counted v2; v1 only when **no** v2 header, before `hmac_v1_inbound_sunset_at`, with a fail-closed witness write; after inbound sunset v1 rejects.

---

## File Structure

**Create:**
- `alembic/versions/010_hmac_v2_per_case_idempotency.py` — migration (drop global unique, add per-case unique, witness + counters tables, forward-only downgrade).
- `src/kyc_tool/ops/__init__.py`, `src/kyc_tool/ops/activate_hmac_v1_observation.py` — idempotent activation command.
- `src/kyc_tool/api/hmac_witness.py` — durable fail-closed witness + diagnostic counters + zero predicate.
- `tests/unit/test_hmac_v2.py`, `tests/unit/test_hmac_witness.py`, `tests/integration/test_hmac_dual_accept.py`, `tests/integration/test_review_completed_event.py`, `tests/unit/test_activate_observation.py`.

**Modify:**
- `src/kyc_tool/config.py` — v2 settings + production validations.
- `src/kyc_tool/security.py` — v2 canonical sign/verify (v1 kept).
- `src/kyc_tool/api/auth.py` — sticky-v2 verifier, sunset gating, witness call, classification.
- `src/kyc_tool/api/routes_events.py` — pass method + raw path+query into the verifier.
- `src/kyc_tool/events/ingest.py` — per-case ON CONFLICT + `website.review_completed` validation floor.
- `src/kyc_tool/db/tables.py` — Event unique change + witness/counter ORM models.
- `src/kyc_tool/api/routes_read.py` — retire `/complete`.
- `src/kyc_tool/outbox/publisher.py` — outbound dual-emit.
- `src/kyc_tool/api/routes_metrics.py` — surface witness + counters.
- `tests/conftest.py` — `sign_headers_v2` twin.
- `tests/integration/test_phase4_platform.py`, `tests/integration/test_phase2_adapters.py` — move off the retired endpoint.
- Docs: `docs/PLATFORM_INTEGRATION.md`, `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, `.env.example`, `docs/OVERVIEW.md`, `docs/PLATFORM_BRIEFING.md`, `docs/architecture-decisions.md` (ADR-003), `AUDIT_FINDINGS.md` (D3 + deviations), `.agents/ROADMAP.md` (status).

---

## Task 1: Config — v2 settings + production floor

**Files:**
- Modify: `src/kyc_tool/config.py` (Settings + `production_config_violations`)
- Test: `tests/unit/test_production_config.py`

**Interfaces:**
- Produces: `Settings.hmac_inbound_key_id/hmac_inbound_secret/hmac_inbound_extra_keys/hmac_outbound_key_id/hmac_outbound_secret: str|dict`, `Settings.hmac_v1_inbound_sunset_at/hmac_v1_outbound_sunset_at: str` (ISO-8601, "" = unset), `Settings.hmac_v1_observation_window_days: int`. Later tasks read these.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_production_config.py`:

```python
from kyc_tool.config import Settings, production_config_violations


def _prod_base(**over):
    base = dict(
        environment="production", auth_disabled=False, read_auth_required=True,
        platform_hmac_secret="x" * 40, platform_callback_url="https://platform.example/hooks",
        object_store="s3", s3_bucket="evidence", ocr_engine="textract",
        email_provider="ses", adapters_profile="real",
        hmac_inbound_key_id="kyc-platform-1", hmac_inbound_secret="i" * 40,
        hmac_outbound_key_id="kyc-tool-1", hmac_outbound_secret="o" * 40,
        hmac_v1_inbound_sunset_at="2026-09-01T00:00:00Z",
        hmac_v1_outbound_sunset_at="2026-10-01T00:00:00Z",
        hmac_v1_observation_window_days=14,
    )
    base.update(over)
    return Settings(**base)


def test_production_requires_both_sunset_dates_and_window():
    assert production_config_violations(_prod_base()) == []
    assert any("inbound sunset" in v for v in production_config_violations(_prod_base(hmac_v1_inbound_sunset_at="")))
    assert any("outbound sunset" in v for v in production_config_violations(_prod_base(hmac_v1_outbound_sunset_at="")))
    assert any("observation window" in v for v in production_config_violations(_prod_base(hmac_v1_observation_window_days=0)))
    assert any("inbound secret" in v for v in production_config_violations(_prod_base(hmac_inbound_secret="")))
    assert any("outbound secret" in v for v in production_config_violations(_prod_base(hmac_outbound_secret="short")))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./manage.sh test -- tests/unit/test_production_config.py::test_production_requires_both_sunset_dates_and_window -v`
Expected: FAIL (unknown settings fields / no such violations).

- [ ] **Step 3: Add the settings**

In `src/kyc_tool/config.py`, inside `Settings` (near `hmac_max_skew_seconds`):

```python
    # HMAC v2 (path-bound canonical signing). Split inbound/outbound secrets +
    # key_id; v1's shared platform_hmac_secret stays the legacy secret until the
    # inbound sunset. extra_keys carries accepted-but-not-active keys for rotation.
    hmac_inbound_key_id: str = ""
    hmac_inbound_secret: str = ""
    hmac_inbound_extra_keys: dict[str, str] = {}   # key_id -> secret (also accepted)
    hmac_outbound_key_id: str = ""
    hmac_outbound_secret: str = ""
    # Bidirectional dual-accept sunsets (ISO-8601; "" = unset ⇒ dual-accept).
    # inbound gated by the zero-witness; outbound gated by staging E2E + sign-off.
    hmac_v1_inbound_sunset_at: str = ""
    hmac_v1_outbound_sunset_at: str = ""
    # Zero-observation window (days) the witness must be silent before inbound sunset.
    hmac_v1_observation_window_days: int = 0
```

- [ ] **Step 4: Add the production violations**

In `production_config_violations`, before `return v`:

```python
    if not settings.hmac_inbound_secret or len(settings.hmac_inbound_secret) < _MIN_HMAC_SECRET_LEN:
        v.append("hmac_inbound secret is missing/weak (v2 inbound verification)")
    if not settings.hmac_outbound_secret or len(settings.hmac_outbound_secret) < _MIN_HMAC_SECRET_LEN:
        v.append("hmac_outbound secret is missing/weak (v2 callback signing)")
    if not settings.hmac_inbound_key_id:
        v.append("hmac_inbound_key_id is empty")
    if not settings.hmac_outbound_key_id:
        v.append("hmac_outbound_key_id is empty")
    if not settings.hmac_v1_inbound_sunset_at:
        v.append("hmac_v1 inbound sunset date is unset (dual-accept-forever not allowed in production)")
    if not settings.hmac_v1_outbound_sunset_at:
        v.append("hmac_v1 outbound sunset date is unset (dual-accept-forever not allowed in production)")
    if settings.hmac_v1_observation_window_days < 1:
        v.append("hmac_v1 observation window days must be >= 1")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./manage.sh test -- tests/unit/test_production_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kyc_tool/config.py tests/unit/test_production_config.py
git commit -m "feat(pr5a): v2 HMAC settings + production config floor"
```

---

## Task 2: security.py — v2 canonical sign/verify

**Files:**
- Modify: `src/kyc_tool/security.py`
- Test: `tests/unit/test_hmac_v2.py` (create)

**Interfaces:**
- Produces:
  - `canonical_v2(*, key_id: str, direction: str, method: str, path_qs: str, timestamp: str, slot: str, body: bytes) -> str`
  - `sign_v2(secret: str, **fields) -> str` (same kwargs as canonical_v2 minus none; returns hex)
  - `verify_v2(secret: str, signature: str, *, max_skew_seconds: int, now: float|None = None, **fields) -> bool`
  - Constants `DIRECTION_INBOUND = "platform->tool"`, `DIRECTION_OUTBOUND = "tool->platform"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_hmac_v2.py`:

```python
import hashlib

from kyc_tool import security

FIELDS = dict(
    key_id="kyc-platform-1", direction=security.DIRECTION_INBOUND, method="POST",
    path_qs="/v1/cases/acme-1/events", timestamp="1000.0", slot="idem-1",
    body=b'{"event_type":"email.verified"}',
)


def test_canonical_v2_is_exact_and_binds_body_hash():
    c = security.canonical_v2(**FIELDS)
    lines = c.split("\n")
    assert lines[0] == "v2"
    assert lines[1:7] == ["kyc-platform-1", "platform->tool", "POST", "/v1/cases/acme-1/events", "1000.0", "idem-1"]
    assert lines[7] == hashlib.sha256(FIELDS["body"]).hexdigest()


def test_verify_v2_roundtrip_and_skew():
    sig = security.sign_v2("secret", **FIELDS)
    assert security.verify_v2("secret", sig, max_skew_seconds=300, now=1000.0, **FIELDS)
    assert not security.verify_v2("secret", sig, max_skew_seconds=300, now=2000.0, **FIELDS)  # skew
    assert not security.verify_v2("wrong", sig, max_skew_seconds=300, now=1000.0, **FIELDS)   # secret


def test_every_binding_dimension_is_load_bearing():
    sig = security.sign_v2("secret", **FIELDS)
    for field, bad in [
        ("method", "GET"), ("path_qs", "/v1/cases/acme-2/events"),
        ("direction", security.DIRECTION_OUTBOUND), ("key_id", "other"),
        ("timestamp", "1001.0"), ("slot", "idem-2"), ("body", b"{}"),
    ]:
        tampered = {**FIELDS, field: bad}
        assert not security.verify_v2("secret", sig, max_skew_seconds=300, now=1000.0, **tampered), field
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./manage.sh test -- tests/unit/test_hmac_v2.py -v`
Expected: FAIL (no `canonical_v2`).

- [ ] **Step 3: Implement in `security.py`** (append; keep existing v1 `sign`/`verify` untouched)

```python
DIRECTION_INBOUND = "platform->tool"
DIRECTION_OUTBOUND = "tool->platform"
_V2_VALID_DIRECTIONS = frozenset({DIRECTION_INBOUND, DIRECTION_OUTBOUND})


def canonical_v2(
    *, key_id: str, direction: str, method: str, path_qs: str, timestamp: str, slot: str, body: bytes
) -> str:
    if direction not in _V2_VALID_DIRECTIONS:
        raise ValueError(f"bad direction {direction!r}")
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join(["v2", key_id, direction, method, path_qs, timestamp, slot, body_hash])


def sign_v2(secret: str, **fields) -> str:
    message = canonical_v2(**fields).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_v2(
    secret: str, signature: str, *, max_skew_seconds: int = 300, now: float | None = None, **fields
) -> bool:
    try:
        ts = float(fields["timestamp"])
    except (TypeError, ValueError, KeyError):
        return False
    if not math.isfinite(ts):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_seconds:
        return False
    try:
        expected = sign_v2(secret, **fields)
    except ValueError:
        return False
    return hmac.compare_digest(expected, signature or "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./manage.sh test -- tests/unit/test_hmac_v2.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/security.py tests/unit/test_hmac_v2.py
git commit -m "feat(pr5a): v2 canonical HMAC sign/verify (path/method-bound)"
```

---

## Task 3: Migration 010 + ORM models (per-case unique, witness, counters, forward-only downgrade)

**Files:**
- Create: `alembic/versions/010_hmac_v2_per_case_idempotency.py`
- Modify: `src/kyc_tool/db/tables.py` (Event unique → per-case; add `HmacV1Observation`, `HmacSignatureStat`)
- Test: `tests/integration/test_migrations.py` (create)

**Interfaces:**
- Produces: table `hmac_v1_observation` (single row id=1: `observation_started_at` nullable, `accepted_count` bigint, `last_accepted_at` nullable); table `hmac_signature_stats` (`key` PK text, `count` bigint, `last_at` timestamptz); unique `uq_events_case_idempotency (case_id, idempotency_key)`; dropped `uq` on `events.idempotency_key`. ORM: `HmacV1Observation`, `HmacSignatureStat`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_migrations.py`:

```python
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _alembic_cfg(engine):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


def test_010_upgrade_downgrade_clean_on_fresh_db(engine):
    cfg = _alembic_cfg(engine)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "009")
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='hmac_v1_observation'"))}
    assert {"observation_started_at", "accepted_count", "last_accepted_at"} <= cols


def test_010_downgrade_refuses_after_cross_case_reuse(engine, clean_db):
    cfg = _alembic_cfg(engine)
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        for cid in ("case-a", "case-b"):
            c.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": cid})
            c.execute(text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence, sequence_backfilled) VALUES "
                "(:id, :c, 'shared-key', 'h', 'email.verified', '{}', '{}', 1, false)"),
                {"id": cid + "-ev", "c": cid})
    with pytest.raises(Exception) as exc:
        command.downgrade(cfg, "009")
    assert "cross-case" in str(exc.value).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./manage.sh test -- tests/integration/test_migrations.py -v`
Expected: FAIL (no revision 010).

- [ ] **Step 3: Change the Event unique in `tables.py`**

Line ~62: `idempotency_key: Mapped[str] = mapped_column(Text, unique=True)` → drop the column-level `unique=True`:
```python
    idempotency_key: Mapped[str] = mapped_column(Text)
```
Add to the `Event` class body a table arg (import `UniqueConstraint` is already present):
```python
    __table_args__ = (UniqueConstraint("case_id", "idempotency_key", name="uq_events_case_idempotency"),)
```
Add two models near the end of `tables.py` (follow the file's `Mapped`/`mapped_column` style):
```python
class HmacV1Observation(Base):
    __tablename__ = "hmac_v1_observation"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    observation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_count: Mapped[int] = mapped_column(BigInteger, default=0)
    last_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HmacSignatureStat(Base):
    __tablename__ = "hmac_signature_stats"
    key: Mapped[str] = mapped_column(Text, primary_key=True)  # v2_accepted | rejected
    count: Mapped[int] = mapped_column(BigInteger, default=0)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```
(Ensure `BigInteger`, `DateTime`, `datetime` are imported at the top of `tables.py`; add to the existing sqlalchemy import line / `from datetime import datetime` if missing.)

- [ ] **Step 4: Write the migration**

Create `alembic/versions/010_hmac_v2_per_case_idempotency.py`:

```python
"""hmac v2 per-case idempotency + v1 observation witness

Revision ID: 010
Revises: 009

Re-scopes event idempotency from global to (case_id, idempotency_key) [D3] and
adds the durable, fail-closed v1 acceptance witness + diagnostic counters.
NON-HOT: the old image's ON CONFLICT (idempotency_key) breaks once the global
unique drops — deploy stop/migrate/start. Downgrade is forward-only after
cross-case reuse (refuses rather than deleting immutable audit events).
"""

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("events_idempotency_key_key", "events", type_="unique")
    op.create_unique_constraint("uq_events_case_idempotency", "events", ["case_id", "idempotency_key"])

    op.create_table(
        "hmac_v1_observation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("observation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # seed INACTIVE — observation_started_at NULL; the clock starts only at the
    # post-cutover activation command, never here.
    op.execute("INSERT INTO hmac_v1_observation (id, accepted_count) VALUES (1, 0)")

    op.create_table(
        "hmac_signature_stats",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Forward-only after cross-case reuse: the old global unique cannot be
    # recreated once (case_a,key) and (case_b,key) coexist. Refuse loudly.
    dupes = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM (SELECT idempotency_key FROM events "
            "GROUP BY idempotency_key HAVING count(DISTINCT case_id) > 1) d"
        )
    ).scalar_one()
    if dupes:
        raise RuntimeError(
            f"cross-case idempotency reuse present ({dupes} keys); migration 010 is "
            "forward-only — refusing downgrade (would require deleting immutable audit events)"
        )
    op.drop_table("hmac_signature_stats")
    op.drop_table("hmac_v1_observation")
    op.drop_constraint("uq_events_case_idempotency", "events", type_="unique")
    op.create_unique_constraint("events_idempotency_key_key", "events", ["idempotency_key"])
```

> **Verify the v1 unique name first:** run `\d events` (or check migration 001) for the actual constraint name; `events_idempotency_key_key` is Postgres's default for `unique=True` but confirm and adjust both drop/recreate names if different.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./manage.sh test -- tests/integration/test_migrations.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/010_hmac_v2_per_case_idempotency.py src/kyc_tool/db/tables.py tests/integration/test_migrations.py
git commit -m "feat(pr5a): migration 010 — per-case idempotency + v1 witness (forward-only downgrade)"
```

---

## Task 4: Ingest — per-case ON CONFLICT (D3)

**Files:**
- Modify: `src/kyc_tool/events/ingest.py` (the `on_conflict_do_nothing` + replay lookup)
- Test: `tests/integration/test_ingest.py` (add D3 cases)

**Interfaces:**
- Consumes: `uq_events_case_idempotency` (Task 3).
- Produces: cross-case reuse → two runs; same-case same-key different-payload → 409; same-case replay → stored snapshot.

- [ ] **Step 1: Write the failing test** — add to `tests/integration/test_ingest.py`:

```python
def test_d3_cross_case_reuse_yields_two_independent_runs(session_factory, policy, clean_db):
    from kyc_tool.events.ingest import ingest_event
    env = envelope("email.verified", {"email": "a@acme.example", "verified": True})
    a = ingest_event(session_factory, policy, case_id="case-a", idempotency_key="k1", envelope=env)
    b = ingest_event(session_factory, policy, case_id="case-b", idempotency_key="k1", envelope=env)
    assert a.status_code == 202 and b.status_code == 202
    assert a.body["run_id"] != b.body["run_id"]  # not a replay of A


def test_d3_same_case_same_key_replays(session_factory, policy, clean_db):
    from kyc_tool.events.ingest import ingest_event
    env = envelope("email.verified", {"email": "a@acme.example", "verified": True})
    a = ingest_event(session_factory, policy, case_id="case-a", idempotency_key="k1", envelope=env)
    a2 = ingest_event(session_factory, policy, case_id="case-a", idempotency_key="k1", envelope=env)
    assert a2.status_code == 200 and a2.body == a.body  # stored snapshot
```

(Reuse the module's existing `envelope` helper / fixtures; mirror the file's current style.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./manage.sh test -- tests/integration/test_ingest.py -k d3 -v`
Expected: FAIL (cross-case second call replays A's run → run_ids equal).

- [ ] **Step 3: Retarget the conflict + replay lookup in `ingest.py`**

`.on_conflict_do_nothing(index_elements=["idempotency_key"])` →
```python
            .on_conflict_do_nothing(index_elements=["case_id", "idempotency_key"])
```
Replay lookup (was `where(Event.idempotency_key == idempotency_key)`) →
```python
            existing = session.execute(
                select(Event).where(
                    Event.case_id == case_id, Event.idempotency_key == idempotency_key
                )
            ).scalar_one()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./manage.sh test -- tests/integration/test_ingest.py -v`
Expected: PASS (existing + the two new).

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/events/ingest.py tests/integration/test_ingest.py
git commit -m "feat(pr5a): scope event idempotency per-case (D3)"
```

---

## Task 5: Witness module — durable, fail-closed, zero predicate

**Files:**
- Create: `src/kyc_tool/api/hmac_witness.py`
- Test: `tests/unit/test_hmac_witness.py` (create)

**Interfaces:**
- Produces:
  - `record_v1_accepted(session) -> None` — atomic `UPDATE hmac_v1_observation SET accepted_count=accepted_count+1, last_accepted_at=now() WHERE id=1`; raises if 0 rows (fail-closed signal to caller).
  - `bump_stat(session, key: str) -> None` — best-effort upsert into `hmac_signature_stats`.
  - `inbound_v1_zero(session, window_days: int, now: datetime) -> bool` — the zero predicate.
  - `observation_state(session) -> dict` — for /metrics.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_hmac_witness.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from kyc_tool.api import hmac_witness as w

pytestmark = pytest.mark.postgres


def test_inactive_never_zero_regardless_of_wallclock(session_factory, clean_db_010):
    with session_factory() as s:
        assert w.inbound_v1_zero(s, window_days=1, now=datetime(2030, 1, 1, tzinfo=UTC)) is False


def test_zero_needs_observation_age_and_no_recent_accept(session_factory, clean_db_010):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory() as s:
        s.execute(text("UPDATE hmac_v1_observation SET observation_started_at=:t WHERE id=1"), {"t": start})
        s.commit()
    with session_factory() as s:
        assert w.inbound_v1_zero(s, 14, now=start + timedelta(days=10)) is False   # too young
        assert w.inbound_v1_zero(s, 14, now=start + timedelta(days=20)) is True    # old + no accepts
        w.record_v1_accepted(s); s.commit()
    with session_factory() as s:
        assert w.inbound_v1_zero(s, 14, now=start + timedelta(days=20)) is False   # recent accept


def test_record_v1_accepted_raises_when_row_absent(session_factory, clean_db_010):
    with session_factory() as s:
        s.execute(text("DELETE FROM hmac_v1_observation")); s.commit()
    with session_factory() as s, pytest.raises(w.WitnessUnavailable):
        w.record_v1_accepted(s)
```

Add a `clean_db_010` fixture to `tests/conftest.py` that runs `alembic upgrade head` on the clean DB (or reuse `clean_db` if it already migrates to head — check and use the existing one).

- [ ] **Step 2: Run test to verify it fails**

Run: `./manage.sh test -- tests/unit/test_hmac_witness.py -v`
Expected: FAIL (no module).

- [ ] **Step 3: Implement `src/kyc_tool/api/hmac_witness.py`**

```python
"""Durable, cross-replica v1 acceptance witness + diagnostic counters (spec §6).

The v1 witness is FAIL-CLOSED: if we cannot record an accepted v1 request, the
caller must reject it, so real v1 traffic is never silently invisible. v2 /
rejected counters are diagnostic (best-effort).
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


class WitnessUnavailable(RuntimeError):
    """The durable v1 witness update did not land — caller must fail closed."""


def record_v1_accepted(session: Session) -> None:
    rows = session.execute(
        text(
            "UPDATE hmac_v1_observation SET accepted_count = accepted_count + 1, "
            "last_accepted_at = now() WHERE id = 1"
        )
    ).rowcount
    if rows != 1:
        raise WitnessUnavailable("hmac_v1_observation row missing")


def bump_stat(session: Session, key: str) -> None:
    session.execute(
        text(
            "INSERT INTO hmac_signature_stats (key, count, last_at) VALUES (:k, 1, now()) "
            "ON CONFLICT (key) DO UPDATE SET count = hmac_signature_stats.count + 1, last_at = now()"
        ),
        {"k": key},
    )


def inbound_v1_zero(session: Session, window_days: int, now: datetime) -> bool:
    row = session.execute(
        text("SELECT observation_started_at, last_accepted_at FROM hmac_v1_observation WHERE id = 1")
    ).one_or_none()
    if row is None or row.observation_started_at is None:
        return False  # inactive / unseeded ⇒ "never started", not "zero"
    if (now - row.observation_started_at).days < window_days:
        return False
    if row.last_accepted_at is not None and (now - row.last_accepted_at).days < window_days:
        return False
    return True


def observation_state(session: Session) -> dict:
    row = session.execute(
        text("SELECT observation_started_at, accepted_count, last_accepted_at "
             "FROM hmac_v1_observation WHERE id = 1")
    ).one_or_none()
    if row is None:
        return {"active": False}
    return {
        "active": row.observation_started_at is not None,
        "observation_started_at": row.observation_started_at.isoformat() if row.observation_started_at else None,
        "accepted_count": row.accepted_count,
        "last_accepted_at": row.last_accepted_at.isoformat() if row.last_accepted_at else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./manage.sh test -- tests/unit/test_hmac_witness.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/api/hmac_witness.py tests/unit/test_hmac_witness.py tests/conftest.py
git commit -m "feat(pr5a): durable fail-closed v1 observation witness + zero predicate"
```

---

## Task 6: Auth — sticky-v2 verifier, sunset gating, witness + classification

**Files:**
- Modify: `src/kyc_tool/api/auth.py` (`require_valid_signature`), `src/kyc_tool/api/routes_events.py` (pass method + path_qs)
- Modify: `tests/conftest.py` (add `sign_headers_v2`)
- Test: `tests/integration/test_hmac_dual_accept.py` (create)

**Interfaces:**
- Consumes: `security.verify_v2`, `hmac_witness.record_v1_accepted/bump_stat`, config secrets + sunsets.
- Produces: `require_valid_signature(settings, request, body, *, session_factory)` — v2-aware. Any v2 header ⇒ v2-only; valid v1 counts + witness; classification drives telemetry.

- [ ] **Step 1: Write the failing test** — `tests/integration/test_hmac_dual_accept.py`:

```python
import pytest
from fastapi.testclient import TestClient

from kyc_tool.api.app import create_app
from tests.conftest import envelope, sign_headers, sign_headers_v2

pytestmark = pytest.mark.postgres


def _client(settings, session_factory, policy):
    return TestClient(create_app(settings, session_factory=session_factory, policy=policy))


def test_v2_header_present_but_invalid_never_falls_back_to_v1(dual_accept_settings, session_factory, policy, clean_db):
    c = _client(dual_accept_settings, session_factory, policy)
    body = _event_body()
    headers = sign_headers(body)                      # a VALID v1 signature ...
    headers["X-KYC-Signature-V2"] = "deadbeef"        # ... plus a bogus v2 assertion
    headers["X-KYC-Key-Id"] = "kyc-platform-1"
    r = c.post("/v1/cases/acme/events", content=body, headers=headers)
    assert r.status_code == 401  # v2 asserted ⇒ v2-only, no v1 fallback


def test_valid_v2_authenticates_and_binds_path(dual_accept_settings, session_factory, policy, clean_db):
    c = _client(dual_accept_settings, session_factory, policy)
    body = _event_body()
    good = sign_headers_v2(body, method="POST", path_qs="/v1/cases/acme/events")
    assert c.post("/v1/cases/acme/events", content=body, headers=good).status_code == 202
    # same signature replayed to a different case path → 401 (path-bound)
    assert c.post("/v1/cases/evil/events", content=body, headers=good).status_code == 401


def test_v1_only_accepted_before_inbound_sunset(dual_accept_settings, session_factory, policy, clean_db):
    c = _client(dual_accept_settings, session_factory, policy)
    body = _event_body()
    assert c.post("/v1/cases/acme/events", content=body, headers=sign_headers(body)).status_code == 202
```

Add helpers/fixtures to `tests/conftest.py`:
```python
def sign_headers_v2(body: bytes, *, method: str, path_qs: str, key: str | None = None,
                    key_id: str = "kyc-platform-1", secret: str = TEST_SECRET) -> dict:
    timestamp = str(time.time())
    idem = key or uuid.uuid4().hex
    sig = security.sign_v2(secret, key_id=key_id, direction=security.DIRECTION_INBOUND,
                           method=method, path_qs=path_qs, timestamp=timestamp, slot=idem, body=body)
    return {"Content-Type": "application/json", "Idempotency-Key": idem,
            "X-KYC-Timestamp": timestamp, "X-KYC-Key-Id": key_id, "X-KYC-Signature-V2": sig}


@pytest.fixture()
def dual_accept_settings(settings):
    # v1 secret = TEST_SECRET (sign_headers), v2 inbound secret also TEST_SECRET for the twin;
    # both sunsets far future ⇒ dual-accept.
    return settings.model_copy(update={
        "platform_hmac_secret": TEST_SECRET, "hmac_inbound_key_id": "kyc-platform-1",
        "hmac_inbound_secret": TEST_SECRET, "hmac_v1_inbound_sunset_at": "2999-01-01T00:00:00Z",
        "hmac_v1_observation_window_days": 14,
    })
```
(`_event_body()` returns `json.dumps(envelope("email.verified", {...})).encode()`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./manage.sh test -- tests/integration/test_hmac_dual_accept.py -v`
Expected: FAIL (verifier is v1-only; v2 path binding not enforced).

- [ ] **Step 3: Rewrite `require_valid_signature` in `auth.py`**

```python
from datetime import UTC, datetime

from kyc_tool import security
from kyc_tool.api import hmac_witness


def _sunset_passed(iso: str, now: datetime) -> bool:
    if not iso:
        return False
    return now >= datetime.fromisoformat(iso.replace("Z", "+00:00"))


def require_valid_signature(settings, request, body: bytes, *, session_factory=None) -> None:
    if settings.auth_disabled:
        return
    headers = request.headers
    now = datetime.now(UTC)
    has_v2 = bool(headers.get("X-KYC-Signature-V2") or headers.get("X-KYC-Key-Id"))

    if has_v2:
        key_id = headers.get("X-KYC-Key-Id", "")
        secret = _inbound_secret(settings, key_id)
        ok = bool(secret) and security.verify_v2(
            secret, headers.get("X-KYC-Signature-V2", ""),
            max_skew_seconds=settings.hmac_max_skew_seconds,
            key_id=key_id, direction=security.DIRECTION_INBOUND,
            method=request.method, path_qs=request.url.path + (("?" + request.url.query) if request.url.query else ""),
            timestamp=headers.get("X-KYC-Timestamp", ""),
            slot=headers.get("Idempotency-Key", ""), body=body,
        )
        if not ok:
            _bump(session_factory, "rejected")
            raise HTTPException(status_code=401, detail="invalid v2 signature")
        _bump(session_factory, "v2_accepted")
        return

    # v1 path — only before the inbound sunset, and only if we can record it.
    if _sunset_passed(settings.hmac_v1_inbound_sunset_at, now):
        raise HTTPException(status_code=401, detail="v1 signatures retired (inbound sunset)")
    if not settings.platform_hmac_secret:
        raise HTTPException(status_code=401, detail="authentication not configured")
    if not security.verify(settings.platform_hmac_secret, headers.get("X-KYC-Timestamp", ""),
                           body, headers.get("X-KYC-Signature", ""),
                           max_skew_seconds=settings.hmac_max_skew_seconds):
        _bump(session_factory, "rejected")
        raise HTTPException(status_code=401, detail="invalid signature")
    _record_v1(session_factory)  # FAIL-CLOSED


def _inbound_secret(settings, key_id: str) -> str:
    if key_id == settings.hmac_inbound_key_id:
        return settings.hmac_inbound_secret
    return settings.hmac_inbound_extra_keys.get(key_id, "")


def _bump(session_factory, key: str) -> None:
    if session_factory is None:
        return
    try:
        with session_factory() as s:
            hmac_witness.bump_stat(s, key); s.commit()
    except Exception:
        pass  # diagnostic counters are best-effort


def _record_v1(session_factory) -> None:
    if session_factory is None:
        return
    try:
        with session_factory() as s:
            hmac_witness.record_v1_accepted(s); s.commit()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="v1 witness unavailable") from exc
```

Update `require_read_access` similarly (reads carry an **empty slot**; pass `slot=""`). In `routes_events.py` and `routes_read.py`, change the call sites to `require_valid_signature(settings, request, body, session_factory=request.app.state.session_factory)`.

> Note: `require_valid_signature` now takes the `request` (for method + path), not just `headers`. Update every caller. Reads/GETs classify empty-slot v2 the same way.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./manage.sh test -- tests/integration/test_hmac_dual_accept.py tests/integration/test_ui.py tests/integration/test_ingest.py -v`
Expected: PASS (new dual-accept + no regressions in existing signed-endpoint tests).

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/api/auth.py src/kyc_tool/api/routes_events.py src/kyc_tool/api/routes_read.py tests/conftest.py tests/integration/test_hmac_dual_accept.py
git commit -m "feat(pr5a): sticky-v2 verifier, inbound sunset gate, fail-closed v1 witness"
```

---

## Task 7: Activation command (`kyc_tool.ops.activate_hmac_v1_observation`)

**Files:**
- Create: `src/kyc_tool/ops/__init__.py`, `src/kyc_tool/ops/activate_hmac_v1_observation.py`
- Test: `tests/unit/test_activate_observation.py`

**Interfaces:**
- Produces: `activate(session_factory) -> bool` (True = newly activated, False = already active), and a `python -m kyc_tool.ops.activate_hmac_v1_observation` entrypoint. Compare-and-set; never resets a live window.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_activate_observation.py`:

```python
import pytest
from sqlalchemy import text

from kyc_tool.ops.activate_hmac_v1_observation import activate

pytestmark = pytest.mark.postgres


def test_first_activation_then_rerun_no_reset(session_factory, clean_db_010):
    assert activate(session_factory) is True
    with session_factory() as s:
        first = s.execute(text("SELECT observation_started_at FROM hmac_v1_observation WHERE id=1")).scalar_one()
    assert first is not None
    assert activate(session_factory) is False  # idempotent
    with session_factory() as s:
        second = s.execute(text("SELECT observation_started_at FROM hmac_v1_observation WHERE id=1")).scalar_one()
    assert second == first  # NOT reset
```

- [ ] **Step 2: Run test to verify it fails** — `./manage.sh test -- tests/unit/test_activate_observation.py -v` → FAIL (no module).

- [ ] **Step 3: Implement**

`src/kyc_tool/ops/__init__.py`: `"""Operator one-shot management commands."""`

`src/kyc_tool/ops/activate_hmac_v1_observation.py`:
```python
"""Start the v1 observation clock AFTER the non-hot cutover (spec §6a).

Non-network, idempotent compare-and-set: sets observation_started_at only when
NULL. A rerun reports "already active" and never resets a live window.
Run once, post-drain: `python -m kyc_tool.ops.activate_hmac_v1_observation`.
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory


def activate(session_factory) -> bool:
    with session_factory() as s:
        row = s.execute(
            text("UPDATE hmac_v1_observation SET observation_started_at = now() "
                 "WHERE observation_started_at IS NULL RETURNING id")
        ).one_or_none()
        s.commit()
        return row is not None


def main() -> int:
    sf = make_session_factory(make_engine(get_settings().database_url))
    if activate(sf):
        print("v1 observation activated (clock started now)")
        return 0
    print("v1 observation already active — no change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
(Confirm `make_engine`/`make_session_factory` signatures against `db/session.py`; mirror how `workers/retention.py` builds them.)

- [ ] **Step 4: Run test to verify it passes** — `./manage.sh test -- tests/unit/test_activate_observation.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/ops/ tests/unit/test_activate_observation.py
git commit -m "feat(pr5a): idempotent v1 observation activation command"
```

---

## Task 8: Retire `/complete` + validation floor on the keyed event

**Files:**
- Modify: `src/kyc_tool/api/routes_read.py` (remove the route), `src/kyc_tool/events/ingest.py` (validation floor)
- Modify: `tests/integration/test_phase4_platform.py`, `tests/integration/test_phase2_adapters.py` (drive the keyed event)
- Test: `tests/integration/test_review_completed_event.py` (create)

**Interfaces:**
- Produces: posting `website.review_completed` to `/v1/cases/{id}/events` validates task exists (404) / `task_type=="website"` (422) / task.case==path case (409) / status=="open" (409) BEFORE creating a run; the retired route is gone (404).

- [ ] **Step 1: Write the failing test** — `tests/integration/test_review_completed_event.py`:

```python
import pytest
pytestmark = pytest.mark.postgres

# Helper: open a website review task for a case, return its id (reuse the
# pipeline path that creates one, or insert a ReviewTask row directly).

def test_retired_route_is_gone(client):
    assert client.post("/v1/review-tasks/whatever/complete").status_code == 404


def test_keyed_completion_validation_matrix(client, engine, open_website_task):
    case_id, task_id = open_website_task  # fixture: seeds case + open website task
    def post(body_case, payload):
        return client.post(f"/v1/cases/{body_case}/events",
                           json=_wrc_envelope(payload), headers=...)  # signed

    assert post(case_id, {"task_id": "nope", "result": "pass", "reviewer_id": "r"}).status_code == 404
    assert post(case_id, {"task_id": task_id, "result": "pass", "reviewer_id": "r", "wrong_type": True}).status_code in (409, 422)
    assert post("other-case", {"task_id": task_id, "result": "pass", "reviewer_id": "r"}).status_code == 409
    ok = post(case_id, {"task_id": task_id, "result": "pass", "reviewer_id": "r"})
    assert ok.status_code == 202
    # no check is written on a rejected completion:
    # (assert via GET /v1/cases/{other}/checks that website_verified is absent)
```

Flesh out `_wrc_envelope`, the signing headers (v1 `sign_headers` under dual-accept), and an `open_website_task` fixture (drive a `kyb.run_requested` that queues a website task, or insert a `ReviewTask(status="open", task_type="website")`).

- [ ] **Step 2: Run test to verify it fails** — `./manage.sh test -- tests/integration/test_review_completed_event.py -v` → FAIL (route still there / no validation).

- [ ] **Step 3a: Remove the route** — delete the entire `complete_review_task` function from `routes_read.py` (lines ~128–…). Remove now-unused imports (`ReviewTask` if unused elsewhere in that file — check first).

- [ ] **Step 3b: Add the validation floor in `ingest.py`** — a helper called from `ingest_event` right after the genuine-new-event branch begins (after replay resolution, before `Run` creation), for `website.review_completed` only:

```python
def _validate_website_review_completed(session, case_id: str, payload: dict) -> IngestOutcome | None:
    """PR 5a floor (spec §4): the keyed event must reference a real, open,
    same-case website task. Full FOR UPDATE + actor binding is PR 5b."""
    task_id = payload.get("task_id")
    task = session.get(ReviewTask, task_id) if task_id else None
    if task is None:
        return IngestOutcome(404, {"error": "review task not found", "task_id": task_id})
    if task.task_type != "website":
        return IngestOutcome(422, {"error": "not a website review task", "task_id": task_id})
    if task.case_id != case_id:
        return IngestOutcome(409, {"error": "task belongs to a different case", "task_id": task_id})
    if task.status != "open":
        return IngestOutcome(409, {"error": "review task not open", "task_id": task_id})
    return None
```
Call it in `ingest_event` after the sequence is committed but **before** building the `Run` (return the outcome directly on rejection so no run/check is created). Import `ReviewTask` in `ingest.py`.

> The rejection returns before `Run`/audit `run.created`; the event row itself is still recorded (idempotent) with the 4xx snapshot — matching how other validation-failed events behave. Confirm against existing ingest patterns; if the project prefers rejecting pre-insert, raise `HTTPException` from the route instead. Pick one and note it in the RELEASE.

- [ ] **Step 4: Migrate the two existing suites** — in `test_phase4_platform.py` / `test_phase2_adapters.py`, replace any `POST /v1/review-tasks/{id}/complete` with a signed `website.review_completed` event POST to `/v1/cases/{id}/events`. Run them.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./manage.sh test -- tests/integration/test_review_completed_event.py tests/integration/test_phase4_platform.py tests/integration/test_phase2_adapters.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kyc_tool/api/routes_read.py src/kyc_tool/events/ingest.py tests/integration/test_review_completed_event.py tests/integration/test_phase4_platform.py tests/integration/test_phase2_adapters.py
git commit -m "feat(pr5a): retire /complete; validate website.review_completed on the event path"
```

---

## Task 9: Outbound dual-emit on callbacks

**Files:**
- Modify: `src/kyc_tool/outbox/publisher.py` (`_deliver_decision_callback`)
- Test: `tests/integration/test_phase4_platform.py` (or a focused publisher unit test)

**Interfaces:**
- Consumes: `security.sign` (v1) + `security.sign_v2` (v2 outbound), config outbound secret/key_id + `hmac_v1_outbound_sunset_at`.
- Produces: callbacks carry `X-KYC-Signature-V2` + `X-KYC-Key-Id` always; also `X-KYC-Signature` (v1) until the outbound sunset.

- [ ] **Step 1: Write the failing test** (focused unit against a fake http client capturing headers):

```python
def test_callback_dual_emits_until_outbound_sunset(monkeypatch):
    # settings with outbound secret + key_id, outbound sunset far future
    # capture headers from a fake http client; assert both X-KYC-Signature and
    # X-KYC-Signature-V2 present, and X-KYC-Key-Id set; verify_v2 over
    # direction=tool->platform, method=POST, path=/kyc/decision passes.
    ...
```
(Write the concrete assertions against `OutboxPublisher(_deliver_decision_callback)` with an injected `http_client` stub that records the last request; sign path_qs = the callback URL's path `/kyc/decision`.)

- [ ] **Step 2: Run test to verify it fails** → FAIL (only v1 emitted).

- [ ] **Step 3: Implement** — in `_deliver_decision_callback`:

```python
    url = f"{self.settings.platform_callback_url.rstrip('/')}/kyc/decision"
    path_qs = "/kyc/decision"
    v2 = security.sign_v2(
        self.settings.hmac_outbound_secret, key_id=self.settings.hmac_outbound_key_id,
        direction=security.DIRECTION_OUTBOUND, method="POST", path_qs=path_qs,
        timestamp=timestamp, slot="", body=body,
    )
    headers = {
        "Content-Type": "application/json", "X-KYC-Timestamp": timestamp,
        "X-KYC-Key-Id": self.settings.hmac_outbound_key_id, "X-KYC-Signature-V2": v2,
    }
    from datetime import UTC, datetime
    outbound_sunset = self.settings.hmac_v1_outbound_sunset_at
    sunset_passed = bool(outbound_sunset) and datetime.now(UTC) >= datetime.fromisoformat(outbound_sunset.replace("Z", "+00:00"))
    if not sunset_passed:
        headers["X-KYC-Signature"] = security.sign(self.settings.platform_hmac_secret, timestamp, body)
    response = self.http.post(url, content=body, headers=headers)
    response.raise_for_status()
```

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/outbox/publisher.py tests/integration/test_phase4_platform.py
git commit -m "feat(pr5a): dual-emit v1+v2 callback signatures until the outbound sunset"
```

---

## Task 10: Metrics — surface witness + counters

**Files:**
- Modify: `src/kyc_tool/api/routes_metrics.py`
- Test: `tests/integration/test_ui.py` (extend `test_overview_shape`/metrics) or a focused test

**Interfaces:**
- Produces: `/v1/metrics` gains `"hmac": {"v1_accepted": ..., "v2_accepted": ..., "rejected": ..., "observation": {...}}` from the DB (cross-replica durable), not in-process.

- [ ] **Step 1: Write the failing test**

```python
def test_metrics_exposes_hmac_witness(client, session_factory):
    from kyc_tool.api import hmac_witness
    with session_factory() as s:
        hmac_witness.record_v1_accepted(s); hmac_witness.bump_stat(s, "v2_accepted"); s.commit()
    hmac = client.get("/v1/metrics").json()["hmac"]
    assert hmac["v1_accepted"] >= 1 and hmac["v2_accepted"] >= 1
    assert "observation" in hmac
```

- [ ] **Step 2: Run to verify it fails** → FAIL (no `hmac` key).

- [ ] **Step 3: Implement** — in `routes_metrics.metrics`, add to the returned dict:

```python
            "hmac": {
                "v1_accepted": session.execute(text("SELECT accepted_count FROM hmac_v1_observation WHERE id=1")).scalar() or 0,
                **{r[0]: r[1] for r in session.execute(text("SELECT key, count FROM hmac_signature_stats"))},
                "observation": __import__("kyc_tool.api.hmac_witness", fromlist=["observation_state"]).observation_state(session),
            },
```
(Prefer a clean top-of-file `from kyc_tool.api import hmac_witness` and `"observation": hmac_witness.observation_state(session)` — the inline import above is a placeholder to avoid a second edit; use the clean import.)

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/api/routes_metrics.py tests/integration/test_ui.py
git commit -m "feat(pr5a): expose DB-backed hmac v1/v2 witness in /v1/metrics"
```

---

## Task 11: The three-phase attack repro (the security proof)

**Files:**
- Test: `tests/integration/test_hmac_dual_accept.py` (add the lifecycle test)

**Interfaces:** consumes everything above. This is the spec's headline security assertion (§1/§7).

- [ ] **Step 1: Write the test**

```python
def test_cross_case_redirect_lifecycle(session_factory, policy, settings, clean_db):
    from datetime import UTC, datetime, timedelta
    # (a) v1-only, before inbound sunset: captured event for A replayed to B SUCCEEDS (residual risk)
    pre = settings.model_copy(update={"platform_hmac_secret": TEST_SECRET,
        "hmac_v1_inbound_sunset_at": "2999-01-01T00:00:00Z", "hmac_v1_observation_window_days": 14,
        "hmac_inbound_key_id": "kyc-platform-1", "hmac_inbound_secret": TEST_SECRET})
    c = _client(pre, session_factory, policy)
    body = _event_body()
    h = sign_headers(body, key="k-shared")
    assert c.post("/v1/cases/A/events", content=body, headers=h).status_code == 202
    assert c.post("/v1/cases/B/events", content=body, headers=h).status_code == 202  # redirect works under v1

    # (b) v2 captured for A replayed to B → 401
    v2 = sign_headers_v2(body, method="POST", path_qs="/v1/cases/A/events", key="k-v2")
    assert c.post("/v1/cases/B/events", content=body, headers=v2).status_code == 401

    # (c) v1-only AFTER inbound sunset → 401
    post = settings.model_copy(update={"platform_hmac_secret": TEST_SECRET,
        "hmac_v1_inbound_sunset_at": "2000-01-01T00:00:00Z", "hmac_inbound_key_id": "kyc-platform-1",
        "hmac_inbound_secret": TEST_SECRET, "hmac_v1_observation_window_days": 14})
    c2 = _client(post, session_factory, policy)
    assert c2.post("/v1/cases/A/events", content=body, headers=sign_headers(body, key="k2")).status_code == 401
```

- [ ] **Step 2–4: Run → PASS** (all three phases). If (a) fails to *succeed*, the residual-risk documentation is wrong; if (b)/(c) fail to *401*, path-binding/sunset is broken.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_hmac_dual_accept.py
git commit -m "test(pr5a): three-phase cross-case redirect lifecycle proof"
```

---

## Task 12: Operator contract docs + governance + verification RELEASE

**Files:** `docs/PLATFORM_INTEGRATION.md`, `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, `.env.example`, `docs/OVERVIEW.md`, `docs/PLATFORM_BRIEFING.md`, `docs/architecture-decisions.md`, `AUDIT_FINDINGS.md`, `.agents/ROADMAP.md`, `AGENT_BUS.md`

- [ ] **Step 1: PLATFORM_INTEGRATION.md** — add the v2 signing quickstart (copy-paste function + worked vectors — reuse the Task 2 test vectors), the one-recipe slot rule, and remove `/v1/review-tasks/{id}/complete` from §7; document review completion as the keyed `website.review_completed` event.
- [ ] **Step 2: DEPLOYMENT.md** — the non-hot **stop/migrate/start** cutover + the `python -m kyc_tool.ops.activate_hmac_v1_observation` activation step; rewrite the `:38-42` perimeter paragraph to key on "inbound v1 actually disabled (zero-witness + inbound sunset)," not "PR 5 lands"; document the new settings.
- [ ] **Step 3: RUNBOOK.md** — correct the `:11` "all revisions downgrade cleanly" line for 010's forward-only refusal; add the cutover + activation ordering.
- [ ] **Step 4: .env.example + OVERVIEW.md + PLATFORM_BRIEFING.md** — replace the single-secret story with split secrets + key_id + two sunset dates + observation window; PLATFORM_BRIEFING `:153-158` widens staging on "inbound v1 disabled," not "PR 5 lands."
- [ ] **Step 5: architecture-decisions.md** — ADR-003: per-case idempotency (D3), endpoint retirement, nonce omission, bidirectional sunset.
- [ ] **Step 6: AUDIT_FINDINGS.md** — record D3 + the two deviations (endpoint retired, `request_nonces` omitted) with rationale.
- [ ] **Step 7: ROADMAP.md** — mark PR 5a status (spec+plan done → in build → shipped when green).
- [ ] **Step 8: Full verification** (the artifact, spec §3):

```bash
./manage.sh fmt && ./manage.sh lint          # ruff + import-linter clean
./manage.sh test                              # full suite GREEN vs ephemeral Postgres
```
Capture: commands + pass/fail, the DB/golden witness (local green + CI run URL), the three-phase attack repro result, the anchor SHA.

- [ ] **Step 9: Bus RELEASE + push** — append `RELEASE [CLAUDE]` with the §3 verification artifact fields (commands+results, DB witness, adversarial repro line, anchor SHA), then push. Ask Codex to audit the code range to `AUDIT-CLEAN`.

```bash
git add -A && git commit -m "docs(pr5a): operator contract, ADR-003, deviations, ROADMAP + RELEASE"
git push -u origin claude/project-setup-verify-kpfgjs
```

---

## Self-Review (author checklist — completed)

- **Spec coverage:** §2 v2 contract → T2/T6; endpoint/slot matrix → T6/T8; §3 sunsets → T1/T6/T9; §4 retire+floor → T8; §5 D3+010+forward-only → T3/T4; §6 witness fail-closed/zero → T5, §6a activation+non-hot → T3(seed)/T7; §7 tests → T2/T4/T5/T6/T8/T11; §9 surfaces → T12. All mapped.
- **Placeholder scan:** the only intentionally-sketched blocks are the two doc-heavy test fixtures in T8/T9 (marked with `...`), which the implementer fills using the patterns cited; every source-code step carries complete code. Constraint names (`events_idempotency_key_key`, `make_session_factory`) carry an explicit "verify against the DB/`db/session.py`" note.
- **Type consistency:** `canonical_v2`/`sign_v2`/`verify_v2` kwargs identical across T2/T6/T9; `record_v1_accepted`/`bump_stat`/`inbound_v1_zero`/`observation_state` signatures identical across T5/T6/T7/T10; `IngestOutcome` reused in T8.
- **Ordering:** config → security → migration/tables → ingest → witness → auth → activation → retire → dual-emit → metrics → attack proof → docs/RELEASE. Each task ends green and independently reviewable.

**Open verification items the implementer MUST resolve at Task 3/7 (flagged, not guessed):** the exact existing unique-constraint name on `events.idempotency_key`, and `db/session.py`'s engine/session-factory constructor names.
