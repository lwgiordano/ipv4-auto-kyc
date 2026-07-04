"""Outbound email (POC verification tokens).

TODO(integration): real provider (SES/Sendgrid/…) chosen by the platform team;
credentials via environment. The dev sender only logs — it never leaks token
values into log lines (PII/secret hygiene), just the case and recipient hash.
"""

import hashlib
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class LoggingEmailSender:
    """Dev/test stand-in. Records sends in memory for assertions."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        log.info(
            "email_sent_stub",
            to_sha=hashlib.sha256(to.encode()).hexdigest()[:12],
            subject=subject,
        )
