"""FastAPI application factory."""

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from kyc_tool import __version__
from kyc_tool.api.routes_events import install_event_contract_openapi
from kyc_tool.api.routes_events import router as events_router
from kyc_tool.api.routes_metrics import router as metrics_router
from kyc_tool.api.routes_ops import router as ops_router
from kyc_tool.api.routes_read import router as read_router
from kyc_tool.config import (
    REPO_ROOT,
    ProcessRole,
    Settings,
    get_settings,
    production_config_violations,
    validate_process_role,
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
    # Production kill switch via the shared process-role authority (re-audit R5-F1): refuse to boot on
    # unsafe/stub configuration before any DB/store access.
    validate_process_role(settings, ProcessRole.API)
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))
    policy = policy or load_policy(settings.policy_dir)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    # The selected policy (injected or on-disk) is the single startup identity: seed/verify
    # THAT bundle — not settings.policy_dir, which can differ from an injected policy — so the
    # attestation and /readyz describe exactly what the app serves. Fail boot on any mismatch.
    if policy.policy_dir is not None:
        seeded = seed_and_verify(session_factory, policy.policy_dir)
        if seeded != policy.bundle_hash:
            raise RuntimeError(
                f"startup bundle mismatch: seeded {seeded} != selected policy {policy.bundle_hash}"
            )
    else:  # DB-reconstructed injection: no directory to seed — require the exact hash present
        with session_factory() as session:
            if load_bundle(session, policy.bundle_hash) is None:
                raise RuntimeError(f"selected policy {policy.bundle_hash} is not resolvable from the store")
    attest(flag=settings.enforce_bundle_pinning, bundle_hash=policy.bundle_hash)

    app = FastAPI(title="IPv4.Global KYC Tool", version=__version__)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.policy = policy

    app.include_router(events_router)
    app.include_router(read_router)
    app.include_router(metrics_router)
    # ALWAYS mounted (re-audit `f2929f8..6a4cd87` F3): the RUNBOOK's dead-letter recovery must exist
    # in the secure production configuration, where the optional /ui console is disabled.
    app.include_router(ops_router)
    if settings.ui_enabled:
        from kyc_tool.ui.configuration_routes import router as configuration_router
        from kyc_tool.ui.routes import router as ui_router  # deferred: reads console.html

        app.include_router(ui_router)
        app.include_router(configuration_router)

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

        violations = production_config_violations(settings) if settings.environment == "production" else []
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

        try:
            from kyc_tool.configuration import repo as configuration_repo

            with app.state.session_factory() as session:
                active = configuration_repo.get_active(session)
            valid = active is None or settings.enforce_bundle_pinning is True
            checks["configuration"] = {
                "ok": valid,
                "active": active is not None,
                "revision": str(active.revision) if active else None,
                "pinning_enabled": settings.enforce_bundle_pinning is True,
            }
            ready = ready and valid
        except Exception:  # noqa: BLE001 — corruption and unavailable authority are not-ready
            checks["configuration"] = {"ok": False, "error": "Configuration authority unavailable."}
            ready = False

        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "checks": checks})

    install_event_contract_openapi(app)
    return app
