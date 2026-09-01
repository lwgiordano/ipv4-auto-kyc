"""File email sink (audit round 2, F6).

Staging needs the POC token to complete the round-trip, and the logging stub
deliberately hides it. The `file` provider appends each email as a JSON line;
production config validation refuses it (raw tokens on disk).
"""

import json

import pytest

from kyc_tool.config import Settings
from kyc_tool.outbox.emails import FileEmailSender, LoggingEmailSender, make_email_sender


def test_file_sink_appends_json_lines(tmp_path):
    sink = FileEmailSender(tmp_path / "nested" / "emails.log")  # parent auto-created
    sink.send("noc@acme.example", "verify", "token: abc\nreference: tok-1")
    sink.send("noc@acme.example", "verify again", "token: def")

    lines = [json.loads(line) for line in sink.path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0] == {
        "to": "noc@acme.example",
        "subject": "verify",
        "body": "token: abc\nreference: tok-1",
    }


def test_factory_selects_file_sink(tmp_path):
    sender = make_email_sender("file", file_path=tmp_path / "emails.log")
    assert isinstance(sender, FileEmailSender)
    assert isinstance(make_email_sender("logging"), LoggingEmailSender)


def test_file_provider_requires_a_path():
    with pytest.raises(ValueError):
        make_email_sender("file")


def test_unknown_provider_still_fails_loudly():
    with pytest.raises(NotImplementedError):
        make_email_sender("ses")


def test_production_refuses_the_file_sink(tmp_path):
    # everything else production-safe; only the email provider is the file sink
    from kyc_tool.config import production_config_violations

    settings = Settings(
        environment="production",
        platform_hmac_secret="x" * 40,
        platform_callback_url="https://platform.example/hooks",
        auth_disabled=False,
        read_auth_required=True,
        object_store="s3",
        s3_bucket="evidence-bucket",
        ocr_engine="textract",
        email_provider="file",
        adapters_profile="real",
    )
    violations = production_config_violations(settings)
    assert any("file sink" in v for v in violations)
