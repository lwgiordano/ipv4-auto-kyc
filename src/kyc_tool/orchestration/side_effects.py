"""Orchestration side effects described by adapters/events.

Adapters are fetch-only; anything stateful their semantics require (review
tasks, POC tokens, token emails) is requested in `normalized` and performed
here, inside the recording transaction.
"""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from kyc_tool.adapters.base import AdapterOutput
from kyc_tool.config import Settings
from kyc_tool.db.audit import audit
from kyc_tool.db.tables import Case, Check, PocToken, ReviewTask, Run
from kyc_tool.domain.models import AdapterStatus, CheckStatus
from kyc_tool.outbox.publisher import enqueue_poc_email
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.poc import hash_token


def _open_task_exists(session: Session, case_id: str, task_type: str) -> bool:
    return (
        session.execute(
            select(ReviewTask.id).where(
                ReviewTask.case_id == case_id,
                ReviewTask.task_type == task_type,
                ReviewTask.status == "open",
            )
        ).first()
        is not None
    )


def _live_check_exists(session: Session, case_id: str, check_type: str) -> bool:
    return (
        session.execute(
            select(Check.id).where(
                Check.case_id == case_id,
                Check.check_type == check_type,
                Check.superseded_by_check_id.is_(None),
            )
        ).first()
        is not None
    )


class SideEffects:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ---- adapter-output effects (RUN_ADAPTERS recording txn) ---------------

    def __call__(self, session: Session, run: Run, case: Case, output: AdapterOutput) -> None:
        if output.status is not AdapterStatus.OK:
            return
        if output.adapter_id == "website_manual_review":
            self._website_task(session, run, case, output.normalized)
        elif output.adapter_id == "rir_poc":
            self._poc_effects(session, run, case, output.normalized)

    def _website_task(self, session: Session, run: Run, case: Case, normalized: dict) -> None:
        request = normalized.get("task_request")
        if not request:
            return
        # AUDIT:D2 — no duplicate open tasks, no re-review of decided websites
        if _open_task_exists(session, case.id, "website"):
            return
        if _live_check_exists(session, case.id, "website_verified"):
            return
        task = ReviewTask(case_id=case.id, task_type="website", context_json=request)
        session.add(task)
        session.flush()
        audit(
            session,
            "review_task.created",
            case_id=case.id,
            run_id=run.id,
            task_id=task.id,
            task_type="website",
            domain=request.get("domain"),
        )

    def _poc_effects(self, session: Session, run: Run, case: Case, normalized: dict) -> None:
        if normalized.get("send_token"):
            # a re-submitted POC expires all outstanding tokens (02 §6)
            session.execute(
                text(
                    """
                    UPDATE poc_tokens SET expired_at = now()
                    WHERE case_id = :case_id AND verified_at IS NULL AND expired_at > now()
                    """
                ),
                {"case_id": case.id},
            )
            raw_token = secrets.token_urlsafe(24)
            token = PocToken(
                case_id=case.id,
                poc_handle=normalized["poc_handle"],
                rir_listed_email=normalized["rir_listed_email"],
                token_hash=hash_token(raw_token),
                # expired_at is the validity deadline; the validator treats
                # expired_at <= now as dead, so TTL enforcement needs no reaper
                expired_at=datetime.now(UTC) + timedelta(hours=self.settings.poc_token_ttl_hours),
            )
            session.add(token)
            session.flush()
            enqueue_poc_email(
                session,
                case_id=case.id,
                to=normalized["rir_listed_email"],  # RIR-listed address ONLY
                subject="IPv4.Global — verify your RIR contact",
                body=(
                    f"A verification was requested for POC {normalized['poc_handle']} "
                    f"({(normalized.get('rir') or '').upper()}).\n\n"
                    f"Your verification token: {raw_token}\n\n"
                    f"This token expires in {self.settings.poc_token_ttl_hours} hours. "
                    "Confirm on the IPv4.Global platform."
                ),
            )
            audit(
                session,
                "poc.token_sent",
                case_id=case.id,
                run_id=run.id,
                poc_handle=normalized["poc_handle"],
                token_id=token.id,
            )
        elif normalized.get("email_unavailable"):
            if _open_task_exists(session, case.id, "poc_email_unavailable"):
                return
            task = ReviewTask(
                case_id=case.id,
                task_type="poc_email_unavailable",
                context_json={
                    "poc_handle": normalized.get("poc_handle"),
                    "rir": normalized.get("rir"),
                    "org_handle": normalized.get("org_handle"),
                },
            )
            session.add(task)
            session.flush()
            audit(
                session,
                "review_task.created",
                case_id=case.id,
                run_id=run.id,
                task_id=task.id,
                task_type="poc_email_unavailable",
            )
        elif not normalized.get("associated"):
            audit(
                session,
                "poc.not_associated",
                case_id=case.id,
                run_id=run.id,
                poc_handle=normalized.get("poc_handle"),
                org_handle=normalized.get("org_handle"),
            )

    # ---- event effects (decide txn, after intents are built) ---------------

    def on_event(
        self, session: Session, case: Case, event, intents: list[CheckIntent]
    ) -> None:
        if event.event_type == "website.review_completed":
            payload = event.payload_json or {}
            task = session.get(ReviewTask, payload.get("task_id", ""))
            if task is not None and task.case_id == case.id and task.status == "open":
                task.status = "done"
                task.result = payload.get("result")
                task.reviewer_id = payload.get("reviewer_id")
                task.reason_codes = list(payload.get("reason_codes", []))
                task.completed_at = datetime.now(UTC)
                audit(
                    session,
                    "review_task.completed",
                    case_id=case.id,
                    task_id=task.id,
                    result=task.result,
                    actor=task.reviewer_id or "unknown",
                )
        elif event.event_type == "poc.token_verified":
            verified = any(
                i.check_type == "poc_verified" and i.status is CheckStatus.PASS
                for i in intents
            )
            if verified:
                raw_token = (event.payload_json or {}).get("token", "")
                session.execute(
                    text(
                        """
                        UPDATE poc_tokens SET verified_at = now()
                        WHERE case_id = :case_id AND token_hash = :digest
                          AND verified_at IS NULL
                        """
                    ),
                    {"case_id": case.id, "digest": hash_token(raw_token)},
                )

