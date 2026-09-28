from types import SimpleNamespace

from kyc_tool.ui import integrations
from kyc_tool.ui.integrations import _store_location


def _settings(*, kind: str = "fs", root: str = "evidence", bucket: str = ""):
    return SimpleNamespace(object_store=kind, object_store_root=root, s3_bucket=bucket)


def test_local_location_reports_an_ordinary_cwd_relative_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert _store_location(_settings(root="var/evidence")) == "var/evidence"


def test_local_location_hides_an_absolute_directory_outside_cwd(tmp_path, monkeypatch):
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.chdir(working)

    assert _store_location(_settings(root=str(tmp_path / "host-account"))) == (
        "Local directory (path hidden)"
    )


def test_local_location_hides_the_filesystem_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert _store_location(_settings(root="/")) == "Local directory (path hidden)"


def test_local_location_hides_a_directory_with_a_hidden_component(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert _store_location(_settings(root="var/.private/evidence")) == (
        "Local directory (path hidden)"
    )


def test_s3_location_reports_the_configured_bucket():
    assert _store_location(_settings(kind="s3", bucket="kyc-evidence")) == (
        "bucket kyc-evidence"
    )


class _Result:
    def __iter__(self):
        return iter(())

    def one(self):
        return SimpleNamespace(total=0, with_identifiers=0)


class _Session:
    def execute(self, _query):
        return _Result()


def _integration_report(monkeypatch, *, provider: str, file_path: str = "emails.log"):
    monkeypatch.setattr(integrations.configuration_repo, "get_active", lambda _session: None)
    settings = SimpleNamespace(
        object_store="fs",
        object_store_root="evidence",
        s3_bucket="",
        platform_hmac_secret="",
        platform_callback_url="http://localhost:9999",
        auth_disabled=False,
        email_provider=provider,
        email_file_path=file_path,
    )
    return integrations.integration_report(settings, _Session(), {})["email_sender"]


def test_email_sender_report_keeps_the_logging_stub_truthful(monkeypatch):
    assert _integration_report(monkeypatch, provider="logging") == {
        "status": "stub",
        "detail": "LoggingEmailSender — POC token emails are logged, not sent",
        "todo": "TODO(integration): outbound email provider (AUDIT:C4)",
    }


def test_email_sender_report_identifies_the_file_sink_without_its_path(monkeypatch, tmp_path):
    sink = tmp_path / "private" / "tokens.jsonl"

    report = _integration_report(monkeypatch, provider="file", file_path=str(sink))

    assert report == {
        "status": "dev",
        "detail": "Closed staging: messages are written to a file, not emailed.",
        "todo": "TODO(integration): production outbound email provider (AUDIT:C4)",
    }
    assert str(sink) not in str(report)
    assert not sink.parent.exists()


def test_email_sender_report_marks_an_unimplemented_provider_as_needing_configuration(monkeypatch):
    report = _integration_report(monkeypatch, provider="ses")

    assert report == {
        "status": "needs-config",
        "detail": "Configured email provider is not implemented",
        "todo": "TODO(integration): implement the configured outbound email provider (AUDIT:C4)",
    }


def test_email_sender_report_does_not_echo_an_unsupported_provider_value(monkeypatch):
    private_value = "/Users/private-account/email-secret"

    report = _integration_report(monkeypatch, provider=private_value)

    assert report["detail"] == "Configured email provider is not implemented"
    assert private_value not in str(report)
