"""Caller-owned transactions for immutable revisions and idempotent saves.

Only the active pointer is mutable. No function here commits, rolls back, or
recalculates a run. HTTP callers must authenticate before entering this layer.
"""

import hashlib
from functools import wraps
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from kyc_tool.configuration.models import (
    IDENTIFIER_FIELDS,
    ConfigurationConflict,
    ConfigurationInvalid,
    ConfigurationRequestConflict,
    ConfigurationUnavailable,
    ResolvedConfiguration,
    brokers_json,
    canonical_json,
    derive_points,
    validate_brokers,
    validate_mappings,
    validate_points,
    validate_policy,
)
from kyc_tool.policy.loader import read_policy_files
from kyc_tool.policy_store import repo as store
from kyc_tool.ui.salesforce_projection import FIELD_SOURCES


def database_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DBAPIError as exc:
            raise ConfigurationUnavailable(
                "configuration database operation unavailable; retry safely"
            ) from exc
        except store.BundleCorrupt as exc:
            raise ConfigurationUnavailable("configuration policy bundle is corrupt") from exc

    return wrapped


def transaction_limits(session):
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SET LOCAL statement_timeout = '30s'"))


def _revision(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ConfigurationInvalid("revision must be a positive bigint integer")


def _actor(actor):
    if type(actor) is not str or not actor.strip() or len(actor) > 200:
        raise ConfigurationInvalid("operator label must contain 1-200 characters")
    return actor.strip()


def _raw(session, bundle_hash):
    try:
        if store.load_bundle(session, bundle_hash) is None:
            raise ConfigurationUnavailable("configuration policy bundle is missing")
        files = session.execute(
            text("SELECT files_json FROM policy_bundles WHERE bundle_hash=:h"), {"h": bundle_hash}
        ).scalar_one()
        raw = store._decode_files(files)
        if validate_policy(raw).bundle_hash != bundle_hash:
            raise ConfigurationUnavailable("configuration policy bundle is inconsistent")
        return raw
    except (store.BundleCorrupt, ConfigurationInvalid) as exc:
        raise ConfigurationUnavailable("configuration policy bundle is corrupt") from exc


@database_errors
def get_revision(session, revision: int) -> ResolvedConfiguration:
    _revision(revision)
    row = (
        session.execute(text("SELECT * FROM configuration_revisions WHERE id=:r"), {"r": revision})
        .mappings()
        .first()
    )
    if row is None:
        raise ConfigurationUnavailable("configuration revision is missing")
    try:
        if row["schema_version"] != 1:
            raise ConfigurationInvalid("unsupported configuration schema")
        bundle = validate_policy(_raw(session, row["policy_bundle_hash"]))
        brokers = validate_brokers(row["brokers_json"])
        mappings = validate_mappings(row["mappings_json"])
        return ResolvedConfiguration(revision, bundle, brokers, mappings)
    except ConfigurationInvalid as exc:
        raise ConfigurationUnavailable("configuration snapshot is corrupt") from exc


@database_errors
def get_active(session, *, lock=False) -> ResolvedConfiguration | None:
    if lock:
        transaction_limits(session)
    query = "SELECT active_revision FROM configuration_state WHERE id=1"
    if lock:
        query += " FOR SHARE"
    revision = session.execute(text(query)).scalar_one_or_none()
    if revision is None:
        history = session.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM configuration_revisions) "
                "OR EXISTS(SELECT 1 FROM configuration_requests) "
                "OR EXISTS(SELECT 1 FROM runs WHERE configuration_revision IS NOT NULL)"
            )
        ).scalar_one()
        if history:
            raise ConfigurationUnavailable("active configuration pointer is missing despite recorded history")
        return None
    return get_revision(session, revision)


def _audit(session, *, action, actor, revision, mechanism, **detail):
    session.execute(
        text("INSERT INTO audit_log (actor, action, detail_json) VALUES (:m,:a,:d)"),
        {
            "m": mechanism,
            "a": action,
            "d": canonical_json(
                {
                    "operator_label": actor,
                    "revision": str(revision),
                    "authenticated_mechanism": mechanism,
                    **detail,
                }
            ),
        },
    )


def _insert_revision(session, *, parent, bundle_hash, brokers, mappings, actor, kind):
    return session.execute(
        text(
            "INSERT INTO configuration_revisions "
            "(parent_revision,policy_bundle_hash,brokers_json,mappings_json,created_by,change_kind) "
            "VALUES (:p,:h,:b,:m,:a,:k) RETURNING id"
        ),
        {
            "p": parent,
            "h": bundle_hash,
            "b": canonical_json(brokers),
            "m": canonical_json(mappings),
            "a": actor,
            "k": kind,
        },
    ).scalar_one()


@database_errors
def save_section(session, *, section, expected_revision, request_id: UUID, value, actor) -> dict:
    _revision(expected_revision)
    actor = _actor(actor)
    if type(request_id) is not UUID:
        raise ConfigurationInvalid("request_id must be a UUID")
    if section not in ("points", "brokers", "mappings"):
        raise ConfigurationInvalid("unknown configuration section")
    canonical_json(value)  # aggregate bound, in addition to HTTP's pre-decode bound
    transaction_limits(session)
    # Pre-lock validation has no write side effects. The check-type set is fixed.
    displayed = get_active(session)
    if displayed is None:
        raise ConfigurationUnavailable("live configuration has not been activated")
    if section == "points":
        value = validate_points(value, displayed.bundle.rubric)
    elif section == "brokers":
        value = brokers_json(validate_brokers(value))
    else:
        value = validate_mappings(value)
    digest = hashlib.sha256(
        canonical_json({"section": section, "base": expected_revision, "value": value}).encode()
    ).hexdigest()
    current = session.execute(
        text("SELECT active_revision FROM configuration_state WHERE id=1 FOR UPDATE")
    ).scalar_one_or_none()
    if current is None:
        raise ConfigurationUnavailable("active configuration pointer disappeared")
    active = get_revision(session, current)
    receipt = (
        session.execute(text("SELECT * FROM configuration_requests WHERE request_id=:id"), {"id": request_id})
        .mappings()
        .first()
    )
    if receipt is not None:
        if receipt["request_digest"] != digest:
            raise ConfigurationRequestConflict("request ID already records a different payload")
        get_revision(session, receipt["result_revision"])
        return {
            "revision": str(receipt["result_revision"]),
            "current_revision": str(current),
            "changed": receipt["changed"],
            "replayed": True,
        }
    if expected_revision != current:
        raise ConfigurationConflict("configuration changed; reload before saving")
    brokers, mappings, bundle_hash = brokers_json(active.brokers), active.mappings, active.bundle.bundle_hash
    if section == "points":
        raw = _raw(session, bundle_hash)
        candidate = derive_points(raw, value)
        bundle_hash = store.store_bundle(session, candidate)
        changed = bundle_hash != active.bundle.bundle_hash
    elif section == "brokers":
        changed, brokers = value != brokers, value
    else:
        changed, mappings = value != mappings, value
    revision = current
    if changed:
        revision = _insert_revision(
            session,
            parent=current,
            bundle_hash=bundle_hash,
            brokers=brokers,
            mappings=mappings,
            actor=actor,
            kind=section,
        )
        get_revision(session, revision)  # verify complete persisted candidate before pointing to it
        session.execute(text("UPDATE configuration_state SET active_revision=:r WHERE id=1"), {"r": revision})
    session.execute(
        text(
            "INSERT INTO configuration_requests (request_id,request_digest,result_revision,changed) "
            "VALUES (:id,:d,:r,:c)"
        ),
        {"id": request_id, "d": digest, "r": revision, "c": changed},
    )
    _audit(
        session,
        action="configuration.save",
        actor=actor,
        revision=revision,
        mechanism="admin_token",
        section=section,
        changed=changed,
        request_id=str(request_id),
    )
    return {
        "revision": str(revision),
        "current_revision": str(revision),
        "changed": changed,
        "replayed": False,
    }


def legacy_brokers(session):
    rows = session.execute(
        text(
            "SELECT id,name,policy,aliases,domains,email_domains,org_ids,poc_handles,asns,notes "
            "FROM broker_entities ORDER BY id"
        )
    ).mappings()
    values = []
    for row in rows:
        value = dict(row)
        for field in IDENTIFIER_FIELDS:
            if value[field] is None:
                value[field] = []  # legacy SQL NULL arrays mean no identifiers, not truncation
        values.append(value)
    canonical_json(values)
    return validate_brokers(values)


@database_errors
def preflight(session, *, policy):
    transaction_limits(session)
    active = get_active(session)
    if active is not None:
        return {
            "ready": True,
            "active_revision": str(active.revision),
            "blocking_jobs": [],
            "blocking_runs": [],
            "problems": [],
        }
    jobs = list(session.execute(text("SELECT id FROM jobs WHERE status <> 'done' ORDER BY id")).scalars())
    runs = list(
        session.execute(
            text(
                "SELECT id FROM runs WHERE configuration_revision IS NULL AND state <> 'COMPLETE' ORDER BY id"
            )
        ).scalars()
    )
    problems = []
    try:
        legacy_brokers(session)
        _baseline_raw(session, policy)
    except (ConfigurationInvalid, ConfigurationUnavailable):
        problems.append("legacy broker snapshot or policy baseline is invalid")
    return {
        "ready": not jobs and not runs and not problems,
        "active_revision": None,
        "blocking_jobs": jobs,
        "blocking_runs": runs,
        "problems": problems,
    }


def _baseline_raw(session, policy):
    exists = session.execute(
        text("SELECT 1 FROM policy_bundles WHERE bundle_hash=:h"), {"h": policy.bundle_hash}
    ).first()
    if exists:
        return _raw(session, policy.bundle_hash)
    if policy.policy_dir is None:
        raise ConfigurationUnavailable("baseline policy bytes are unavailable")
    try:
        raw = read_policy_files(policy.policy_dir)
        if validate_policy(raw).bundle_hash != policy.bundle_hash:
            raise ConfigurationUnavailable("baseline policy bytes differ from supplied bundle")
        return raw
    except (OSError, ConfigurationInvalid) as exc:
        raise ConfigurationUnavailable("baseline policy bytes are invalid") from exc


@database_errors
def bootstrap(session, *, policy, actor) -> ResolvedConfiguration:
    """Trusted ops primitive; caller enforces admin configuration and attestation."""
    actor = _actor(actor)
    transaction_limits(session)
    # NOWAIT avoids lock-order deadlocks with legacy writers/FK checks. These
    # relation locks fence actual writes, not merely cooperative new callers.
    # Even an absent-row SELECT FOR SHARE is an admission witness: fence its
    # relation lock so no transaction can observe legacy mode and write later.
    session.execute(text("LOCK TABLE public.configuration_state IN ACCESS EXCLUSIVE MODE NOWAIT"))
    session.execute(
        text(
            "LOCK TABLE public.events, public.cases, public.runs, public.jobs, "
            "public.broker_entities, public.configuration_revisions, "
            "public.configuration_requests IN SHARE ROW EXCLUSIVE MODE NOWAIT"
        )
    )
    active = get_active(session)
    if active is not None:
        return active
    report = preflight(session, policy=policy)
    if not report["ready"]:
        raise ConfigurationUnavailable("activation blocked by legacy work or invalid baseline; run preflight")
    raw = _baseline_raw(session, policy)
    bundle_hash = store.store_bundle(session, raw)
    brokers = brokers_json(legacy_brokers(session))
    mappings = validate_mappings({key: key for key in FIELD_SOURCES})
    revision = _insert_revision(
        session,
        parent=None,
        bundle_hash=bundle_hash,
        brokers=brokers,
        mappings=mappings,
        actor=actor,
        kind="baseline",
    )
    resolved = get_revision(session, revision)
    session.execute(
        text("INSERT INTO configuration_state (id,active_revision) VALUES (1,:r)"), {"r": revision}
    )
    _audit(
        session,
        action="configuration.activate",
        actor=actor,
        revision=revision,
        mechanism="administrative_cli",
    )
    return resolved
