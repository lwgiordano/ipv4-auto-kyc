"""Check store — stage 3 of adapters→validators→record.

Checks are immutable: the ONLY mutation ever performed is stamping
`superseded_by_check_id` on the prior live row, inside the same transaction
that inserts its successor. The partial unique index (AUDIT:D3) makes a second
live check of the same type impossible to persist.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from kyc_tool.db.audit import audit
from kyc_tool.db.tables import Check
from kyc_tool.domain.models import CheckStatus, CheckView
from kyc_tool.validators.normalize import canon_id


def live_checks(session: Session, case_id: str) -> list[Check]:
    return list(
        session.execute(
            select(Check)
            .where(Check.case_id == case_id, Check.superseded_by_check_id.is_(None))
            .order_by(Check.created_at)
        )
        .scalars()
        .all()
    )


def all_checks(session: Session, case_id: str) -> list[Check]:
    return list(
        session.execute(
            select(Check).where(Check.case_id == case_id).order_by(Check.created_at)
        )
        .scalars()
        .all()
    )


def as_view(check: Check) -> CheckView:
    return CheckView(
        check_type=check.check_type,
        status=CheckStatus(check.status),
        points_awarded=check.points_awarded,
        category=check.category,
        source=check.source,
        reason_codes=tuple(check.reason_codes or ()),
        source_detail=dict(check.source_detail_json or {}),
    )


def write_check(
    session: Session,
    *,
    case_id: str,
    check_type: str,
    status: CheckStatus,
    points_awarded: int,
    category: str,
    source: str,
    reason_codes: list[str] | None = None,
    source_detail: dict | None = None,
    created_by_run_id: str | None = None,
) -> Check:
    """Insert a new check, superseding the live check of the same type (if any)
    in the same transaction. Points are only ever awarded on PASS."""
    prior = session.execute(
        select(Check)
        .where(
            Check.case_id == case_id,
            Check.check_type == check_type,
            Check.superseded_by_check_id.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()

    # Stamp the prior BEFORE inserting its successor: the live partial-unique
    # index evaluates per statement, and the deferrable FK lets the stamp
    # reference an id that only exists later in this same transaction.
    new_id = uuid.uuid4().hex
    if prior is not None:
        prior.superseded_by_check_id = new_id
        session.flush()

    new_check = Check(
        id=new_id,
        case_id=case_id,
        check_type=check_type,
        status=status.value,
        points_awarded=points_awarded if status is CheckStatus.PASS else 0,
        category=category,
        source=source,
        source_detail_json=source_detail or {},
        reason_codes=list(reason_codes or []),
        created_by_run_id=created_by_run_id,
    )
    session.add(new_check)
    session.flush()

    if prior is not None:
        audit(
            session,
            "check.superseded",
            case_id=case_id,
            check_type=check_type,
            old_check_id=prior.id,
            new_check_id=new_check.id,
            run_id=created_by_run_id,
        )
    audit(
        session,
        "check.created",
        case_id=case_id,
        check_type=check_type,
        check_id=new_check.id,
        status=status.value,
        points=new_check.points_awarded,
        reason_codes=list(reason_codes or []),
        run_id=created_by_run_id,
    )
    return new_check


def supersede_without_replacement(
    session: Session, check: Check, *, run_id: str | None, reason: str
) -> None:
    """Cascade path (e.g. ORG-ID change invalidates a verified POC): the old
    check is superseded by a successor carrying the cascade reason, status
    needs_review, zero points."""
    write_check(
        session,
        case_id=check.case_id,
        check_type=check.check_type,
        status=CheckStatus.NEEDS_REVIEW,
        points_awarded=0,
        category=check.category,
        source=check.source,
        reason_codes=[reason],
        source_detail={"cascaded_from": check.id},
        created_by_run_id=run_id,
    )


def apply_check_intents(
    session: Session,
    *,
    case_id: str,
    intents: list,
    rubric,
    run_id: str | None,
) -> list[Check]:
    """Record validated intents and apply the spec's dynamic cascade rules
    (scoring_rubric.json dynamic_rules):

    - every intent supersedes the prior live check of its type (write_check);
    - an ORG-ID change revalidates a live verified POC — a POC whose recorded
      org association no longer matches the new ORG-ID handle is superseded
      too (points removed).
    """
    written: list[Check] = []
    for intent in intents:
        item = rubric.item(intent.check_type)
        written.append(
            write_check(
                session,
                case_id=case_id,
                check_type=intent.check_type,
                status=intent.status,
                points_awarded=item.points,
                category=item.category,
                source=intent.source or item.source,
                reason_codes=list(intent.reason_codes),
                source_detail=dict(intent.source_detail),
                created_by_run_id=run_id,
            )
        )

    # ORG-ID → POC cascade
    org_intents = [i for i in intents if i.check_type == "org_id_match"]
    if org_intents:
        new_handle = (org_intents[-1].source_detail or {}).get("org_handle")
        live_poc = session.execute(
            select(Check).where(
                Check.case_id == case_id,
                Check.check_type == "poc_verified",
                Check.superseded_by_check_id.is_(None),
            )
        ).scalar_one_or_none()
        if live_poc is not None:
            # canonicalize both handles: a case/format-only difference
            # ("org-acme-1" vs "ORG-ACME-1") must not supersede a valid POC.
            new_org = canon_id(new_handle)
            poc_org = canon_id((live_poc.source_detail_json or {}).get("org_handle"))
            if not new_org or poc_org != new_org:
                supersede_without_replacement(
                    session,
                    live_poc,
                    run_id=run_id,
                    reason="poc_not_associated",
                )
    return written
