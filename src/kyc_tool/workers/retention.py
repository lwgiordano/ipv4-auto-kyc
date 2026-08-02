"""Retention job: `python -m kyc_tool.workers.retention`.

Prunes, per compliance policy (default 7 years, KYC_RETENTION_DAYS):
- audit_log rows past retention
- delivered poc_email outbox rows past retention
- decision_callback BODIES past retention (redacted in place, the row itself kept — see below)
- expired, never-verified poc_tokens past retention

Raw evidence objects and check rows are deliberately NOT pruned here — they are the
decision record. PR 7b-core keeps the decision_callback ROW for the same reason — it is the
durable ordering authority (id = order, status = local_status, plus the recorded wire digest)
that 7b-activation reconciles the platform against, and deleting it would break that
reconciliation. Its BODY is a different matter: it carries checks[].source, which can be
reviewer-derived (reviewer:<id>), so the body is destroyed (redacted in place) past the window
rather than kept indefinitely. What survives is pseudonymous, not out of scope: case_id, run_id,
decision_sequence, status, delivered_at/resolved_at and the recorded wire digest are internal
ordinals plus a hash — still joinable back to the case (and, through it, the natural person it
concerns) — kept because that is exactly what 7b-activation's reconciliation needs. Retaining
that pseudonymous remainder past the window is a governed decision recorded in
`AUDIT_FINDINGS.md` (D9), not a determination that it falls outside any regulation's scope —
this module does not decide what is or is not personal data; it only bounds what it keeps and
documents the bound. poc_email rows are deleted outright — their sensitive body (the raw POC
token) is already destroyed at delivery by publisher._record_delivered.
"""

import structlog
from sqlalchemy import text

from kyc_tool.config import (
    get_settings,
    numeric_domain_of,
    numeric_value_violation,
    validate_for_production,
)
from kyc_tool.db.session import make_engine, make_session_factory, uow
from kyc_tool.outbox.fence import take_shared_fence

log = structlog.get_logger(__name__)


def prune(session_factory, retention_days: int) -> dict[str, int]:
    # Fail closed BEFORE opening a transaction (re-audit `5b0f0b8..b75a320` R4-F2): every statement
    # below prunes with a `now() - make_interval(days => N)` cutoff, so a NONPOSITIVE, non-integer or
    # boolean N makes the cutoff the FUTURE and deletes/redacts CURRENT immutable audit/evidence rows
    # (audit_log, poc_email, callback bodies, attempts, tokens). The Pydantic field and production
    # validation bound the setting, but prune() is called directly (tests, main()), so this destructive
    # sink re-checks its own domain via the shared registry checker.
    violation = numeric_value_violation(numeric_domain_of("retention_days"), retention_days)
    if violation:
        raise ValueError(f"retention refuses to prune — {violation}")
    counts: dict[str, int] = {}
    with uow(session_factory) as session:
        # FIRST statement of the transaction, before any DML (re-audit `cbb783b` F4): retention
        # updates the parent `outbox` and then deletes from the child `outbox_delivery_attempts`,
        # which is the exact opposite of a migration's child-then-parent lock order — a real
        # 40P01 cycle, and one the live-claim preflight cannot see because retention holds no
        # claim. Taking the shared maintenance fence here puts retention under the same global
        # order as every other witness writer: a migration waiting at the exclusive fence simply
        # queues until this transaction commits.
        take_shared_fence(session)
        counts["audit_log"] = session.execute(
            text("DELETE FROM audit_log WHERE at < now() - make_interval(days => :d)"),
            {"d": retention_days},
        ).rowcount
        counts["outbox_poc_email"] = session.execute(
            text(
                "DELETE FROM outbox WHERE kind='poc_email' AND status='delivered' "
                "AND delivered_at < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        ).rowcount
        # A decision_callback row is the durable ordering authority, so the ROW survives — but its
        # BODY does not. The body carried checks[].source, which can be reviewer-derived
        # (reviewer:<id>), so it is destroyed (redacted in place) past the window. What survives —
        # id (the order), case_id, run_id, decision_sequence, status, delivered_at and the recorded
        # wire digest — is everything 7b-activation reconciles against. It is pseudonymous, not
        # out of scope: a hash plus internal ordinals still joinable to a case. Retaining it past
        # the window is a governed decision (AUDIT_FINDINGS.md D9), not a personal-data
        # determination — this repo bounds what it keeps; it does not decide what the law calls it.
        # `dead` is included from migration 020 on. A callback that exhausted its attempts is
        # the ordinary outcome of a platform outage, and its body carries the same
        # reviewer-derived `checks[].source` as any other — leaving it forever because delivery
        # happened not to succeed inverted the policy this module states. A dead row has neither
        # `delivered_at` nor `resolved_at`, so it ages on `created_at`; the requeue endpoint
        # refuses a redacted row, so a scrubbed body cannot strand the claim path. Be precise
        # about what re-emission does and does not restore: `recalculate.requested` produces
        # a NEW decision under a new run_id — it does not reconstruct THIS body. `checks[]`
        # (status, points, source, reason codes) is not stored on `decisions` at all, and the
        # `enforcement_held.computed_decision` marker lives only in `audit_log`, which this
        # same prune deletes on the same clock. Past the window the original callback body is
        # GONE, which is the governed intent (AUDIT_FINDINGS D9) — not a recoverable copy.
        counts["outbox_callback_redacted"] = session.execute(
            text(
                "UPDATE outbox SET payload_json = '{\"redacted\": true}'::jsonb "
                "WHERE kind='decision_callback' "
                "AND status IN ('delivered','superseded','dead') "
                "AND payload_json <> '{\"redacted\": true}'::jsonb "
                "AND COALESCE(delivered_at, resolved_at, created_at) "
                "    < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        ).rowcount
        # Attempt rows are pruned ONLY for callbacks that already carry a terminal digest. That
        # restriction is load-bearing, not tidiness: for any non-delivered row the attempt IS the
        # only evidence that bytes were transmitted, and deleting it would silently reclassify the
        # row from `send_intent_witnessed` ("staged for the wire; only the platform can say more") to
        # `not_accepted` ("nothing was ever transmitted") — the one state in which this tool is
        # entitled to assert non-delivery on its own evidence. A retention job must never
        # manufacture that claim. Once `callback_wire_sha256` is set the row is
        # `delivery_witnessed` on the outbox row alone, so its attempts are genuinely redundant
        # and their unbounded growth is not worth keeping.
        counts["outbox_attempts_pruned"] = session.execute(
            text(
                "DELETE FROM outbox_delivery_attempts a USING outbox o "
                "WHERE o.id = a.outbox_id AND o.callback_wire_sha256 IS NOT NULL "
                "AND a.attempted_at < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        ).rowcount
        counts["poc_tokens_expired"] = session.execute(
            text(
                "DELETE FROM poc_tokens WHERE verified_at IS NULL "
                "AND expired_at < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        ).rowcount
    return counts


def main() -> None:
    settings = get_settings()
    # Process boundary (re-audit R4-F2): a production retention run validates its whole configuration
    # BEFORE it opens an engine, exactly like every other worker — so an unsafe production config
    # (including a nonpositive/aliased retention) refuses to start rather than deleting evidence.
    if settings.environment == "production":
        validate_for_production(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    counts = prune(session_factory, settings.retention_days)
    log.info("retention_pruned", retention_days=settings.retention_days, **counts)


if __name__ == "__main__":
    main()
