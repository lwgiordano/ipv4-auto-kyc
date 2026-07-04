"""Check store — stage 3 of adapters→validators→record.

Checks are immutable: the ONLY mutation ever performed is stamping
`superseded_by_check_id` on the prior live row, inside the same transaction
that inserts its successor. The partial unique index (AUDIT:D3) makes a second
live check of the same type impossible to persist.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from kyc_tool.db.audit import audit
from kyc_tool.db.tables import Check
from kyc_tool.domain.models import CheckStatus, CheckView


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

    new_check = Check(
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
        prior.superseded_by_check_id = new_check.id
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
    successor = write_check(
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
    # write_check already stamped the prior; nothing else to do — successor
    # exists purely to keep the supersession chain explicit for audit.
    _ = successor
