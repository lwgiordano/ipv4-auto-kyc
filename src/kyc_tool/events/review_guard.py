"""Review-record trust rules (PR 5b).

Reviewer identity is platform-asserted via the signed envelope (HMAC v2). These
helpers enforce that the asserted `actor` is a consistent, nonblank reviewer for
the two sensitive event types. The `actor.id == payload.reviewer_id` equality is
a CONSISTENCY check; the security boundary is the signature.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from kyc_tool.db.tables import Event, ReviewTask

REVIEWER_ACTOR_INVALID = "actor_invalid"


def reviewer_actor_reason(actor: dict, payload: dict) -> str | None:
    """None if the actor is a valid reviewer whose id matches the payload's
    reviewer_id (both nonblank after strip, exact case-sensitive). Else
    REVIEWER_ACTOR_INVALID."""
    if (actor or {}).get("type") != "reviewer":
        return REVIEWER_ACTOR_INVALID
    actor_id = str((actor or {}).get("id", "")).strip()
    reviewer_id = str((payload or {}).get("reviewer_id", "")).strip()
    if not actor_id or not reviewer_id or actor_id != reviewer_id:
        return REVIEWER_ACTOR_INVALID
    return None


@dataclass(frozen=True, slots=True)
class WebsiteCompletionGuard:
    """Immutable scalar decision, safe to hand a pure validator (no ORM entity)."""

    eligible: bool
    reviewer_id: str | None  # actor-derived trusted id, only when eligible
    task_id: str | None
    skip_reason: str | None  # task_missing|wrong_type|wrong_case|task_not_open|actor_invalid


def evaluate_website_completion(
    session: Session, case_id: str, event: Event
) -> tuple[WebsiteCompletionGuard, ReviewTask | None]:
    """Lock the referenced ReviewTask FOR UPDATE and evaluate eligibility from the
    PERSISTED event (re-validated even though ingest checked it — pre-upgrade
    queued events never passed the floor). The ORM task is returned ONLY for the
    orchestration/side-effect layer; the scalar guard is what a validator sees."""
    payload = event.payload_json or {}
    actor = event.actor_json or {}
    task_id = payload.get("task_id")
    if not task_id:
        return WebsiteCompletionGuard(False, None, None, "task_missing"), None
    task = session.get(ReviewTask, task_id, with_for_update=True)
    if task is None:
        return WebsiteCompletionGuard(False, None, task_id, "task_missing"), None
    if task.task_type != "website":
        return WebsiteCompletionGuard(False, None, task_id, "wrong_type"), task
    if task.case_id != case_id:
        return WebsiteCompletionGuard(False, None, task_id, "wrong_case"), task
    if task.status != "open":
        return WebsiteCompletionGuard(False, None, task_id, "task_not_open"), task
    if reviewer_actor_reason(actor, payload) is not None:
        return WebsiteCompletionGuard(False, None, task_id, "actor_invalid"), task
    return WebsiteCompletionGuard(True, str(actor.get("id")).strip(), task_id, None), task
