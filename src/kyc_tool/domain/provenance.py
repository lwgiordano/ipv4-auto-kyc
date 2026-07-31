"""Decision-pointer provenance: one vocabulary for every read surface.

`cases.latest_decision_row_id` / `cases.latest_manual_decision_row_id` are the only
authorities for "which decision row speaks for this case". Every surface that dereferences
one (canonical API, UI list/full views, the Salesforce projection, the console) must
classify the outcome the same way, because the failure modes mean different things:

- ``latest_decision_row`` / ``latest_manual_row`` — the pointer resolved under the hardened
  predicate (`id + case_id` [+ `manual IS TRUE`]); the returned row is authoritative.
- ``unresolved_pointer_drift`` — the pointer is SET but the hardened predicate rejected it
  (wrong case, wrong kind, or no such row). This is integrity drift, forbidden at the
  database from `022` on; it is never evidence that no decision exists, and no surface may
  fall back to sorting or to a definite negative.
- ``unresolved_legacy_order`` — the pointer is NULL while same-case rows exist: a pre-`014`
  history whose write order has no durable record. Honest ignorance; heals on the next
  decision.
- ``no_decisions`` / ``no_manual_decisions`` — the pointer is NULL and no row exists: a true
  negative.

Pure by design (import-linter: domain imports nothing heavier than the stdlib) so both the
API layer (ORM) and the UI layer (raw SQL) classify through the same function instead of
re-deriving the taxonomy inline — which is how the drift case came to be reported three
different ways on three surfaces.
"""

LATEST_ROW = "latest_decision_row"
LATEST_MANUAL_ROW = "latest_manual_row"
POINTER_DRIFT = "unresolved_pointer_drift"
LEGACY_ORDER = "unresolved_legacy_order"
NO_DECISIONS = "no_decisions"
NO_MANUAL_DECISIONS = "no_manual_decisions"

# Provenances under which a surface holds an AUTHORITATIVE decision tuple. Everything else
# serves blanks/nulls — never a guess, and never a definite negative fabricated from absence.
AUTHORITATIVE = frozenset({LATEST_ROW, LATEST_MANUAL_ROW})


def classify(*, pointer_set: bool, row_resolved: bool, any_rows: bool, manual: bool = False) -> str:
    """Classify one pointer dereference.

    `pointer_set`: the case carries a non-NULL pointer. `row_resolved`: the hardened predicate
    returned the row. `any_rows`: any same-case (manual, when `manual=True`) rows exist at all —
    only consulted when the pointer is NULL, so callers may pass a lazy/cheap answer when
    `pointer_set` is true.
    """
    if pointer_set:
        if row_resolved:
            return LATEST_MANUAL_ROW if manual else LATEST_ROW
        return POINTER_DRIFT
    if any_rows:
        return LEGACY_ORDER
    return NO_MANUAL_DECISIONS if manual else NO_DECISIONS
