"""Outbox publisher process: `python -m kyc_tool.workers.outbox_worker`."""

import sys

from kyc_tool.config import get_settings, validate_for_production
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.outbox.emails import make_email_sender
from kyc_tool.outbox.publisher import OutboxPublisher, OutboxSaturated

# Nonzero exit code the process uses when the orphan-cap circuit breaker trips — distinct from a
# generic crash so a supervisor's restart policy can recognise saturation (re-audit
# `538e55e..42e1c7d` F6).
SATURATION_EXIT_CODE = 3


def build_publisher() -> OutboxPublisher:
    settings = get_settings()
    if settings.environment == "production":
        validate_for_production(settings)  # fail-closed on stub/unsafe config
    session_factory = make_session_factory(make_engine(settings.database_url))
    return OutboxPublisher(
        session_factory,
        settings,
        email_sender=make_email_sender(
            settings.email_provider, file_path=settings.email_file_path
        ),
    )


def run() -> int:
    """Run the publisher loop; return the process exit code. Saturation exits nonzero so a
    supervisor restarts the process (a fresh process drops the wedged daemon attempts)."""
    publisher = build_publisher()
    try:
        publisher.run_forever()
    except OutboxSaturated as exc:
        print(f"outbox_worker: {exc}", file=sys.stderr)
        return SATURATION_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(run())
