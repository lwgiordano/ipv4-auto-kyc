"""FastAPI application factory."""

import structlog
from fastapi import FastAPI

from kyc_tool import __version__
from kyc_tool.api.routes_events import router as events_router
from kyc_tool.api.routes_read import router as read_router
from kyc_tool.config import Settings, get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.policy.loader import PolicyBundle, load_policy


def create_app(
    settings: Settings | None = None,
    session_factory=None,
    policy: PolicyBundle | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))
    policy = policy or load_policy(settings.policy_dir)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    app = FastAPI(title="IPv4.Global KYC Tool", version=__version__)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.policy = policy

    app.include_router(events_router)
    app.include_router(read_router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "policy_bundle_hash": policy.bundle_hash,
        }

    return app
