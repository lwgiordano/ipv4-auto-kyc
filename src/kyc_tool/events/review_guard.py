"""Review-record trust rules (PR 5b).

Reviewer identity is platform-asserted via the signed envelope (HMAC v2). These
helpers enforce that the asserted `actor` is a consistent, nonblank reviewer for
the two sensitive event types. The `actor.id == payload.reviewer_id` equality is
a CONSISTENCY check; the security boundary is the signature.
"""

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
