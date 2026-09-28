"""Consistent, read-only assembly of the public Salesforce mirror projection."""

from fastapi import HTTPException
from sqlalchemy import or_, select, text

from kyc_tool.api.schemas import SalesforceProjectionResponse
from kyc_tool.configuration import repo as configuration_repo
from kyc_tool.configuration.models import ConfigurationUnavailable
from kyc_tool.db.tables import Case, Check, DecisionRow, PocToken, ReviewTask, Run
from kyc_tool.domain import provenance
from kyc_tool.ui.salesforce_projection import (
    SALESFORCE_FIELD_CONTRACT,
    project_salesforce_fields,
)


def _resolve_decision_pointer(session, case, *, manual=False):
    pointer = case.latest_manual_decision_row_id if manual else case.latest_decision_row_id
    row = None
    if pointer is not None:
        conditions = [DecisionRow.id == pointer, DecisionRow.case_id == case.id]
        if manual:
            conditions.append(DecisionRow.manual.is_(True))
        row = session.execute(select(DecisionRow).where(*conditions)).scalar_one_or_none()
        any_rows = True
    else:
        query = select(DecisionRow.id).where(DecisionRow.case_id == case.id)
        if manual:
            query = query.where(DecisionRow.manual.is_(True))
        any_rows = session.execute(query.limit(1)).first() is not None
    return row, provenance.classify(
        pointer_set=pointer is not None,
        row_resolved=row is not None,
        any_rows=any_rows,
        manual=manual,
    )


def _decision_values(row):
    if row is None:
        return None
    return {
        "decision": row.decision,
        "score": row.score,
        "gates_json": row.gates_json,
        "manual": row.manual,
        "reviewer_id": row.reviewer_id,
        "decided_at": row.decided_at,
    }


def build_salesforce_projection(session_factory, case_id) -> SalesforceProjectionResponse:
    """Read and project every input from one repeatable-read PostgreSQL snapshot."""
    try:
        with session_factory() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            projection_timestamp = session.execute(text("SELECT transaction_timestamp()")).scalar_one()
            case = session.get(Case, case_id)
            if case is None:
                raise HTTPException(status_code=404, detail="case not found")

            active = configuration_repo.get_active(session)
            pointed_decision, decision_provenance = _resolve_decision_pointer(session, case)
            pointed_manual, manual_provenance = _resolve_decision_pointer(session, case, manual=True)

            pointed_run = None
            run_provenance = "not_applicable"
            if pointed_decision is not None and not pointed_decision.manual:
                pointed_run = session.execute(
                    select(Run).where(
                        Run.id == pointed_decision.run_id,
                        Run.case_id == case_id,
                    )
                ).scalar_one_or_none()
                run_provenance = "resolved" if pointed_run is not None else "unresolved"

            checks = list(
                session.execute(
                    select(Check).where(Check.case_id == case_id).order_by(Check.created_at, Check.id)
                ).scalars()
            )
            open_task_types = list(
                session.execute(
                    select(ReviewTask.task_type)
                    .where(ReviewTask.case_id == case_id, ReviewTask.status == "open")
                    .order_by(ReviewTask.created_at, ReviewTask.id)
                ).scalars()
            )
            poc_token_outstanding = (
                session.execute(
                    select(PocToken.id)
                    .where(
                        PocToken.case_id == case_id,
                        PocToken.verified_at.is_(None),
                        or_(
                            PocToken.expired_at.is_(None),
                            PocToken.expired_at > projection_timestamp,
                        ),
                    )
                    .limit(1)
                ).first()
                is not None
            )

            case_values = {
                "id": case.id,
                "status": case.status,
                "buy_status": case.buy_status,
                "broker_status": case.broker_status,
                "current_score": case.current_score,
                "latest_decision": case.latest_decision,
                "submitted_json": case.submitted_json,
            }
            check_values = [
                {
                    "id": check.id,
                    "check_type": check.check_type,
                    "status": check.status,
                    "points_awarded": check.points_awarded,
                    "category": check.category,
                    "source": check.source,
                    "source_detail_json": check.source_detail_json,
                    "reason_codes": check.reason_codes,
                    "superseded_by_check_id": check.superseded_by_check_id,
                    "created_at": check.created_at,
                }
                for check in checks
            ]
            mappings = active.mappings if active else None
            projected = project_salesforce_fields(
                case=case_values,
                checks=check_values,
                open_task_types=open_task_types,
                poc_token_outstanding=poc_token_outstanding,
                latest_decision=_decision_values(pointed_decision),
                latest_manual_decision=_decision_values(pointed_manual),
                mappings=mappings,
            )

            fields = {}
            for source_field, contract in SALESFORCE_FIELD_CONTRACT.items():
                destination = mappings[source_field] if mappings is not None else source_field
                fields[destination] = {
                    "source_field": source_field,
                    "source_identity": contract.source_identity,
                    "value_type": contract.value_type,
                    "nullable": contract.nullable,
                    "value": projected[destination],
                }

            if pointed_decision is None:
                decision_authority = {
                    "provenance": decision_provenance,
                    "decision_row_id": None,
                    "run_id": None,
                    "decision_kind": None,
                    "decision": None,
                    "run_provenance": "not_applicable",
                }
            else:
                decision_authority = {
                    "provenance": decision_provenance,
                    "decision_row_id": pointed_decision.id,
                    "run_id": None if pointed_decision.manual else pointed_decision.run_id,
                    "decision_kind": "manual" if pointed_decision.manual else "automatic",
                    "decision": pointed_decision.decision,
                    "run_provenance": run_provenance,
                }

            if pointed_manual is None:
                manual_authority = {
                    "provenance": manual_provenance,
                    "decision_row_id": None,
                    "reviewer_id": None,
                    "decided_at": None,
                }
            else:
                manual_authority = {
                    "provenance": manual_provenance,
                    "decision_row_id": pointed_manual.id,
                    "reviewer_id": pointed_manual.reviewer_id,
                    "decided_at": pointed_manual.decided_at,
                }

            return SalesforceProjectionResponse.model_validate(
                {
                    "case_id": case.id,
                    "mapping_revision": str(active.revision) if active else None,
                    "configuration_revision": (
                        str(pointed_run.configuration_revision)
                        if pointed_run is not None and pointed_run.configuration_revision is not None
                        else None
                    ),
                    "decision_authority": decision_authority,
                    "manual_approval_authority": manual_authority,
                    "fields": fields,
                    "projection_timestamp": projection_timestamp,
                }
            )
    except ConfigurationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Configuration is unavailable; retry safely.",
        ) from exc
