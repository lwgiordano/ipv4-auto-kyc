"""Behavioral tests for the handoff document renderer."""

import importlib.util
import signal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDERER_PATH = REPO_ROOT / "scripts" / "handoff" / "render_docs.py"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("handoff_render_docs_test", RENDERER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def renderer():
    return _load_renderer()


def _render_with_fake_browser(renderer, monkeypatch, tmp_path, *, effective_uid):
    root = tmp_path / "package"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "TECHCRAFT_HANDOFF.md").write_text("# Handoff\n", encoding="utf-8")
    browser = tmp_path / "chromium"
    browser.touch()

    launches = []
    signals = []

    class Process:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_timeout = timeout
            return 0

    def popen(command, **kwargs):
        process = Process()
        launches.append((command, kwargs, process))
        pdf_arg = next(arg for arg in command if arg.startswith("--print-to-pdf="))
        pdf = Path(pdf_arg.removeprefix("--print-to-pdf="))
        pdf.write_bytes(b"%PDF-1.4\n" + (b"x" * 1001) + b"\n%%EOF")
        return process

    monkeypatch.setattr(renderer.os, "geteuid", lambda: effective_uid)
    monkeypatch.setattr(
        renderer.shutil,
        "which",
        lambda name: str(browser) if name == "chromium" else None,
    )
    monkeypatch.setattr(renderer.subprocess, "Popen", popen)
    monkeypatch.setattr(renderer.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    renderer.render(root)

    assert (docs / "TECHCRAFT_HANDOFF.html").is_file()
    assert (docs / "TECHCRAFT_HANDOFF.pdf").is_file()
    assert len(launches) == 1
    command, kwargs, process = launches[0]
    assert kwargs["start_new_session"] is True
    assert signals == [(process.pid, signal.SIGTERM)]
    assert process.wait_timeout == 5
    return command


def test_renderer_disables_chromium_sandbox_only_for_root(renderer, monkeypatch, tmp_path):
    command = _render_with_fake_browser(
        renderer,
        monkeypatch,
        tmp_path,
        effective_uid=0,
    )

    assert command.count("--no-sandbox") == 1


def test_renderer_keeps_chromium_sandbox_for_normal_user(renderer, monkeypatch, tmp_path):
    command = _render_with_fake_browser(
        renderer,
        monkeypatch,
        tmp_path,
        effective_uid=501,
    )

    assert "--no-sandbox" not in command
