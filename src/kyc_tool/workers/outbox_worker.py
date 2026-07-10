"""Outbox publisher process: `python -m kyc_tool.workers.outbox_worker`."""

from kyc_tool.config import get_settings, validate_for_production
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.outbox.emails import make_email_sender
from kyc_tool.outbox.publisher import OutboxPublisher


def build_publisher() -> OutboxPublisher:
    settings = get_settings()
    if settings.environment == "production":
        validate_for_production(settings)  # fail-closed on stub/unsafe config
    session_factory = make_session_factory(make_engine(settings.database_url))
    return OutboxPublisher(
        session_factory, settings, email_sender=make_email_sender(settings.email_provider)
    )


if __name__ == "__main__":
    build_publisher().run_forever()
