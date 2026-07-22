"""FastAPI application factory."""

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from kyc_tool import __version__
from kyc_tool.api.routes_events import router as events_router
from kyc_tool.api.routes_metrics import router as metrics_router
from kyc_tool.api.routes_read import router as read_router
from kyc_tool.config import (
    REPO_ROOT,
    Settings,
    get_settings,
    production_config_violations,
    validate_for_production,
)
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.policy.loader import PolicyBundle, load_policy
from kyc_tool.policy_store.repo import attest, load_bundle, seed_and_verify


def _alembic_head() -> str | None:
    """Head revision the migration chain declares (for the /readyz drift check)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def create_app(
    settings: Settings | None = None,
    session_factory=None,
    policy: PolicyBundle | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    # Production kill switch: refuse to boot on unsafe/stub configuration.
    if settings.environment == "production":
        validate_for_production(settings)
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))
    policy = policy or load_policy(settings.policy_dir)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    h = seed_and_verify(session_factory, settings.policy_dir)
    attest(flag=settings.enforce_bundle_pinning, bundle_hash=h)

    app = FastAPI(title="IPv4.Global KYC Tool", version=__version__)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.policy = policy

    app.include_router(events_router)
    app.include_router(read_router)
    app.include_router(metrics_router)
    if settings.ui_enabled:
        from kyc_tool.ui.routes import router as ui_router  # deferred: reads console.html

        app.include_router(ui_router)

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness only — the process is up and the policy bundle loaded."""
        return {
            "ok": True,
            "version": __version__,
            "policy_bundle_hash": policy.bundle_hash,
        }

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness — safe to receive traffic: configuration is valid, the
        database is reachable and migrated to head, and (in S3 mode) the
        evidence bucket is accessible. 503 on any failing check."""
        checks: dict[str, dict] = {}
        ready = True

        violations = (
            production_config_violations(settings)
            if settings.environment == "production"
            else []
        )
        checks["config"] = {"ok": not violations, "violations": violations}
        ready = ready and not violations

        try:
            with session_factory() as session:
                session.execute(text("SELECT 1"))
                current = session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
            head = _alembic_head()
            checks["database"] = {"ok": True}
            checks["migration"] = {
                "ok": current == head,
                "current": current,
                "head": head,
            }
            ready = ready and current == head
        except Exception as exc:  # noqa: BLE001 — any failure means not-ready
            checks["database"] = {"ok": False, "error": str(exc)[:200]}
            ready = False

        if settings.object_store == "s3":
            try:
                from kyc_tool.storage.object_store import make_object_store

                store = make_object_store(
                    "s3", fs_root=settings.object_store_root, s3_bucket=settings.s3_bucket
                )
                store.verify_access()
                checks["object_store"] = {"ok": True}
            except Exception as exc:  # noqa: BLE001
                checks["object_store"] = {"ok": False, "error": str(exc)[:200]}
                ready = False

        # PR 6 (Task 10): UNCONDITIONALLY (regardless of enforce_bundle_pinning
        # or the checks above) confirm the process's loaded policy bundle is
        # durably resolvable — a missing/corrupt row means neither a flag-on
        # worker nor a bundle-pinning-epoch activation could resolve it.
        bundle_hash = app.state.policy.bundle_hash
        try:
            with app.state.session_factory() as session:
                pinnable = load_bundle(session, bundle_hash) is not None
            checks["policy_bundle"] = {"ok": pinnable, "bundle_hash": bundle_hash}
            ready = ready and pinnable
        except Exception as exc:  # noqa: BLE001 — any failure means not-ready
            checks["policy_bundle"] = {"ok": False, "bundle_hash": bundle_hash, "error": str(exc)[:200]}
            ready = False

        return JSONResponse(
            status_code=200 if ready else 503, content={"ready": ready, "checks": checks}
        )

    return app
