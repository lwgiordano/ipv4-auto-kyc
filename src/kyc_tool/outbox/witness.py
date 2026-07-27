"""What the tool can honestly say about whether a decision callback reached the platform.

There are exactly four answers, and the distinctions between them are the whole point — collapsing
any two produces a reconciliation that is confidently wrong rather than usefully uncertain:

- ``delivery_witnessed`` — the terminal transaction committed with a digest. The tool sent these
  exact bytes and the receiver returned 2xx. Nothing to reconcile.
- ``attempt_witnessed`` — bytes were committed to an attempt row and handed to the network, but no
  terminal digest exists. This is the send-before-stamp gap: the receiver may well hold these
  bytes. Only the platform's own accepted-request ledger can settle it. The tool must not guess.
- ``legacy_unwitnessed`` — delivered before migration 013, so there is no digest and no attempt.
  The sent bytes are unrecoverable **by construction**: ``payload_json`` is jsonb and normalizes
  key order, so any digest computed from it now would be a fabricated witness for bytes nobody can
  reproduce. These rows are reconciled on ``(case_id, run_id, decision_sequence)`` and local
  status alone.
- ``not_accepted`` — no attempt was ever recorded, so nothing was transmitted. This is the only
  state in which "the platform does not have this callback" is a claim the tool can make on its
  own evidence.

The reading a NULL digest USED to invite — "NULL means never delivered" — is exactly the error
this taxonomy exists to prevent. It is false for both ``attempt_witnessed`` and
``legacy_unwitnessed``, and those are precisely the rows an operator would be reconciling.
"""

DELIVERY_WITNESSED = "delivery_witnessed"
ATTEMPT_WITNESSED = "attempt_witnessed"
LEGACY_UNWITNESSED = "legacy_unwitnessed"
NOT_ACCEPTED = "not_accepted"

WITNESS_STATES = (DELIVERY_WITNESSED, ATTEMPT_WITNESSED, LEGACY_UNWITNESSED, NOT_ACCEPTED)

# The single definition. Anything that classifies a row — reconciliation, ops tooling, tests —
# selects this expression rather than reimplementing the CASE, so there is no second copy to
# drift. `o` must be the alias bound to `outbox`.
WITNESS_SQL = f"""
CASE
    WHEN o.callback_wire_sha256 IS NOT NULL THEN '{DELIVERY_WITNESSED}'
    WHEN EXISTS (SELECT 1 FROM outbox_delivery_attempts a WHERE a.outbox_id = o.id)
        THEN '{ATTEMPT_WITNESSED}'
    WHEN o.status = 'delivered' THEN '{LEGACY_UNWITNESSED}'
    ELSE '{NOT_ACCEPTED}'
END
"""


def witness_query(where: str = "o.kind = 'decision_callback'") -> str:
    """`SELECT` of every decision callback with its witness state, newest first.

    Ordering is by `id` — enqueue order, which 013 established as the local ordering authority —
    not by any timestamp, because timestamps across replicas do not order these rows.
    """
    return (
        f"SELECT o.id, o.case_id, o.run_id, o.decision_sequence, o.status, "
        f"o.callback_wire_sha256, o.wire_version, ({WITNESS_SQL}) AS witness "
        f"FROM outbox o WHERE {where} ORDER BY o.id DESC"
    )
