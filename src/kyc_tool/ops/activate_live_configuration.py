"""Explicit drained activation; default is bounded read-only preflight.

The attestation is operator-supplied, not fleet discovery or personal identity.
This does not enable platform ordering activation or positive enforcement.
"""

import argparse
import json
import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.configuration import repo
from kyc_tool.configuration.models import ConfigurationInvalid, ConfigurationUnavailable
from kyc_tool.db.session import make_engine, make_session_factory, uow
from kyc_tool.ops import binding
from kyc_tool.policy.loader import load_policy


def _bind(session):
    try:
        binding.bind(
            session,
            lock_timeout_seconds=5,
            statement_timeout_seconds=30,
            min_revision="024",
            require_columns={
                "configuration_state": ("id", "active_revision"),
                "configuration_revisions": (
                    "id",
                    "schema_version",
                    "parent_revision",
                    "policy_bundle_hash",
                    "brokers_json",
                    "mappings_json",
                    "created_at",
                    "created_by",
                    "change_kind",
                ),
                "configuration_requests": ("request_id", "request_digest", "result_revision", "changed"),
                "runs": ("id", "configuration_revision", "state"),
                "jobs": ("id", "status"),
                "broker_entities": (
                    "id",
                    "name",
                    "policy",
                    "aliases",
                    "domains",
                    "email_domains",
                    "org_ids",
                    "poc_handles",
                    "asns",
                    "notes",
                ),
                "policy_bundles": ("bundle_hash", "files_json"),
                "audit_log": ("actor", "action", "detail_json"),
            },
        )
    except binding.BindingRefused as exc:
        raise ConfigurationUnavailable(str(exc)) from exc


@repo.database_errors
def activate(session_factory, *, policy, settings, actor, apply=False, attest_writers_stopped=False):
    if settings.enforce_bundle_pinning is not True:
        raise ConfigurationInvalid("live configuration requires per-run bundle pinning")
    if type(settings.ui_admin_token) is not str or not settings.ui_admin_token.strip():
        raise ConfigurationInvalid("live configuration requires nonempty admin configuration")
    if apply and not attest_writers_stopped:
        raise ConfigurationInvalid("apply requires --attest-writers-stopped")
    if apply:
        with uow(session_factory) as session:
            _bind(session)
            resolved = repo.bootstrap(session, policy=policy, actor=actor)
            result = {
                "ready": True,
                "active_revision": str(resolved.revision),
                "blocking_jobs": [],
                "blocking_runs": [],
                "problems": [],
            }
        return result  # only after the caller-owned transaction committed
    with session_factory() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        _bind(session)
        return repo.preflight(session, policy=policy)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--attest-writers-stopped", action="store_true")
    parser.add_argument("--operator-label", default="operator")
    args = parser.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        result = activate(
            make_session_factory(engine),
            policy=load_policy(settings.policy_dir),
            settings=settings,
            actor=args.operator_label,
            apply=args.apply,
            attest_writers_stopped=args.attest_writers_stopped,
        )
    except (ConfigurationInvalid, ConfigurationUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
