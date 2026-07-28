"""What the tool can honestly say about whether a decision callback reached the platform.

There are exactly four answers, and the distinctions between them are the whole point — collapsing
any two produces a reconciliation that is confidently wrong rather than usefully uncertain:

- ``delivery_witnessed`` — the terminal transaction committed with a digest. The tool sent these
  exact bytes and the receiver returned 2xx. Nothing to reconcile.
- ``send_intent_witnessed`` — the exact bytes were durably committed to an ADMITTED attempt
  row (``admission = 'admission_v1'`` — stamped only by the database's admission trigger,
  never by the writer) under a live claim, before any transmission was tried. An attempt
  that predates the admission authority (``legacy_unverified``) proves nothing: any writer
  could insert one with an arbitrary token, so its row classifies ``legacy_unwitnessed``,
  never staged-intent and never not-accepted. The send may then have happened (and even been
  accepted — the send-before-stamp residual) or the process may have died before the socket ever
  opened. Local evidence CANNOT tell those apart, which is why this state is named for what it
  proves — staged intent — and not "attempt_witnessed" or "transmitted": there is no atomic
  boundary between a database commit and the network, and pretending one exists is how the
  previous two designs went wrong. Only the platform's accepted-request ledger settles this row.
- ``legacy_unwitnessed`` — created before the attempt authority existed (``witness_generation =
  'legacy'``), in ANY status. A legacy `delivered` row's digest is unrecoverable by construction
  (``payload_json`` is jsonb and normalizes key order — a digest computed now would be a
  fabricated witness). A legacy `pending`/`dead` row is just as opaque: the pre-authority
  publisher sent HTTP before any durable record, so such a row may well have reached the
  platform. The absence of an attempt proves NOTHING about a legacy row. These reconcile on
  ``(case_id, run_id, decision_sequence)`` and local status alone.
- ``not_accepted`` — created under the attempt regime (``witness_generation = 'attempt_v1'``)
  with no attempt row: nothing was ever staged, so nothing was transmitted. This is the ONLY
  state in which "the platform does not have this callback" is a claim the tool can make on its
  own evidence — and it is only sound because the generation marker says the record-intent-first
  rule was in force for this row's whole life.

The reading a NULL digest USED to invite — "NULL means never delivered" — is exactly the error
this taxonomy exists to prevent. It is false for ``send_intent_witnessed`` and for every
``legacy_unwitnessed`` row, and those are precisely the rows an operator would be reconciling.
"""

DELIVERY_WITNESSED = "delivery_witnessed"
SEND_INTENT_WITNESSED = "send_intent_witnessed"
LEGACY_UNWITNESSED = "legacy_unwitnessed"
NOT_ACCEPTED = "not_accepted"

WITNESS_STATES = (DELIVERY_WITNESSED, SEND_INTENT_WITNESSED, LEGACY_UNWITNESSED, NOT_ACCEPTED)

# The single definition. Anything that classifies a row — reconciliation, ops tooling, tests —
# selects this expression rather than reimplementing the CASE, so there is no second copy to
# drift. `o` must be the alias bound to `outbox`.
WITNESS_SQL = f"""
CASE
    WHEN o.callback_wire_sha256 IS NOT NULL THEN '{DELIVERY_WITNESSED}'
    WHEN EXISTS (SELECT 1 FROM outbox_delivery_attempts a
                 WHERE a.outbox_id = o.id AND a.admission = 'admission_v1')
        THEN '{SEND_INTENT_WITNESSED}'
    WHEN EXISTS (SELECT 1 FROM outbox_delivery_attempts a WHERE a.outbox_id = o.id)
        THEN '{LEGACY_UNWITNESSED}'
    WHEN o.witness_generation = 'attempt_v1' THEN '{NOT_ACCEPTED}'
    ELSE '{LEGACY_UNWITNESSED}'
END
"""


# Every decision callback with its witness state. Deliberately a CONSTANT rather than a
# `witness_query(where=...)` helper: a function taking a SQL fragment invites callers to
# interpolate, and this repo does not need a second way to build a WHERE clause. Callers append
# their own BOUND predicate and ordering, e.g.
#
#     text(WITNESS_SELECT + " AND o.case_id = :c ORDER BY o.id DESC"), {"c": case_id}
#
# Order by `id` — enqueue order, which 013 established as the local ordering authority — never by
# a timestamp, which does not order these rows across replicas.
WITNESS_SELECT = f"""
SELECT o.id, o.case_id, o.run_id, o.decision_sequence, o.status, o.witness_generation,
       o.callback_wire_sha256, o.wire_version, ({WITNESS_SQL}) AS witness
FROM outbox o
WHERE o.kind = 'decision_callback'
"""
