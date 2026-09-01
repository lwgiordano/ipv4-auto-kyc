"""Append-only audit writer. Every event, adapter call, check, supersession,
score, and decision is attributable to a case and the event/run that caused it
— the compliance reviewer's reconstruction path."""

from sqlalchemy.orm import Session

from kyc_tool.db.tables import AuditLog


def audit(
    session: Session,
    action: str,
    *,
    case_id: str | None = None,
    actor: str = "system",
    **detail: object,
) -> None:
    session.add(
        AuditLog(case_id=case_id, actor=actor, action=action, detail_json=dict(detail))
    )
