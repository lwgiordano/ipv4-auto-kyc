"""Outbox publisher process: `python -m kyc_tool.workers.outbox_worker`."""

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.outbox.publisher import OutboxPublisher


def build_publisher() -> OutboxPublisher:
    settings = get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    return OutboxPublisher(session_factory, settings)


if __name__ == "__main__":
    build_publisher().run_forever()
