"""Outbound email (POC verification tokens).

TODO(integration): real provider (SES/Sendgrid/…) chosen by the platform team;
credentials via environment. The dev sender only logs — it never leaks token
values into log lines (PII/secret hygiene), just the case and recipient hash.
The `file` sink exists so a CLOSED staging can complete the POC round-trip
(tokens are unreadable from the logging stub by design); production refuses it.
"""

import hashlib
import json
from pathlib import Path
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


class FileEmailSender:
    """Staging/dev sink: append each outbound email as a JSON line so the POC
    round-trip is testable end-to-end without a real provider (the logging stub
    deliberately hides the token). Writes RAW tokens to disk — for closed
    staging/dev only; production config validation refuses this provider."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, to: str, subject: str, body: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"to": to, "subject": subject, "body": body}) + "\n")
        log.info(
            "email_sent_file_sink",
            to_sha=hashlib.sha256(to.encode()).hexdigest()[:12],
            subject=subject,
            path=str(self.path),
        )


def make_email_sender(provider: str, *, file_path: Path | None = None) -> EmailSender:
    """Select the outbound email provider. Only dev/staging senders exist today;
    a non-stub value fails loudly rather than silently logging (real provider,
    e.g. SES: remediation item 12)."""
    if provider == "logging":
        return LoggingEmailSender()
    if provider == "file":
        if file_path is None:
            raise ValueError("email provider 'file' requires a sink path")
        return FileEmailSender(file_path)
    raise NotImplementedError(
        f"email provider {provider!r} is not implemented yet (remediation item 12)"
    )
