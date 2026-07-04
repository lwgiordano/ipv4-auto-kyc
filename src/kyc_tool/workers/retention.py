"""Retention job: `python -m kyc_tool.workers.retention`.

Prunes, per compliance policy (default 7 years, KYC_RETENTION_DAYS):
- audit_log rows past retention
- delivered outbox rows past retention
- expired, never-verified poc_tokens past retention

Raw evidence objects and check rows are deliberately NOT pruned here — they
are the decision record; removing them is a compliance decision that gets its
own sign-off (runbook §retention).
"""

import structlog
from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow

log = structlog.get_logger(__name__)


def prune(session_factory, retention_days: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    with uow(session_factory) as session:
        counts["audit_log"] = session.execute(
            text("DELETE FROM audit_log WHERE at < now() - make_interval(days => :d)"),
            {"d": retention_days},
        ).rowcount
        counts["outbox_delivered"] = session.execute(
            text(
                "DELETE FROM outbox WHERE status='delivered' "
                "AND delivered_at < now() - make_interval(days => :d)"
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
    session_factory = make_session_factory(make_engine(settings.database_url))
    counts = prune(session_factory, settings.retention_days)
    log.info("retention_pruned", retention_days=settings.retention_days, **counts)


if __name__ == "__main__":
    main()
