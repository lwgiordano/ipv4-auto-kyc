"""Wire, signing, event, callback, ordering, and retention claims.

Every value below is written out LITERALLY, as the document asserts it to TechCraft. It is not
computed from the code at import time, because a registry that derives its values from the
authority makes the authority test tautological: the point is that a human wrote a claim down and
a test independently proves the running system agrees (re-audit `82636da..9ac574f`, systemic
requirement). `tests/unit/test_contract_registry_authority.py` holds each literal against its
authority; `tests/unit/test_contract_rendering.py` proves the rendered PDF displays it.
"""

from dataclasses import dataclass

from docs.contracts import Claim, ClaimState, Registry

# ── the receiver's effectiveness decision, as a TOTAL transition table ─────────────────────────────
#
# Re-audit `4f23f23..97deeae` F2. The previous version was prose: "if the current source is a
# manual approval, record but do not apply; otherwise apply it as the current automatic decision."
# The "otherwise" is the defect. Automatic decision B is current; automatic decision A, older but
# delayed, arrives under a run id nobody has seen. It is not a duplicate and the current source is
# not manual, so the published algorithm says apply — and A replaces B. Prose with an "otherwise"
# branch cannot be checked for totality by reading it, which is why this is a table: every row is
# a condition, and a test asserts the rows cover the whole space in each phase.
#
# `docs/contracts/receiver_reference.py` implements exactly these rows and is driven by the
# scenario tests, so the table cannot drift from a working algorithm.


@dataclass(frozen=True)
class Transition:
    """One row. `condition` is evaluated in order within its phase; the first match wins."""

    phase: str  # "interim" (today) or "post-024" (after ordered delivery is activated)
    condition: str
    record: str  # what goes into the accepted-run ledger
    effective: str  # whether it becomes the case's current decision
    why: str


INTERIM = "interim"
POST_024 = "post-024"

RECEIVER_TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        phase=INTERIM,
        condition="the (case_id, run_id) is already in your accepted ledger",
        record="nothing new; the row is already there",
        effective="NO CHANGE — acknowledge with 2xx and stop",
        why="Delivery is at-least-once, so a duplicate is the expected case, not an error. "
            "Returning non-2xx to a duplicate makes us retry it forever.",
    ),
    Transition(
        phase=INTERIM,
        condition="the case has no currently effective decision",
        record="the callback",
        effective="YES — it becomes the current automatic decision",
        why="Nothing to conflict with.",
    ),
    Transition(
        phase=INTERIM,
        condition="the currently effective source for the case is a MANUAL approval",
        record="the callback",
        effective="NO",
        why="A reviewer decided this case. An automatic callback queued before that approval can "
            "arrive after it under a run id you have never seen; applying it silently overturns a "
            "human decision. Automatic authority returns only through the authenticated "
            "platform-owned release protocol.",
    ),
    Transition(
        phase=INTERIM,
        condition="the currently effective source is AUTOMATIC and this callback is a different "
                  "run for the same case",
        record="the callback",
        effective="NO — hold the case for review instead",
        why="This is the row that must not read 'otherwise apply'. Interim has no wire ordering "
            "authority, so you cannot tell a newer decision from an older one that was delayed. "
            "decided_at will not tell you either (section 5). Applying the arrival that happens "
            "to land second silently replaces a newer decision with an older one.",
    ),
    Transition(
        phase=POST_024,
        condition="the (case_id, run_id) is already in your accepted ledger",
        record="nothing new",
        effective="NO CHANGE — acknowledge with 2xx and stop",
        why="Same as interim: duplicates are expected.",
    ),
    Transition(
        phase=POST_024,
        condition="the callback carries no decision_sequence, or its decision_sequence is <= your "
                  "recorded high-water mark for the case",
        record="the callback",
        effective="NO, and the high-water mark does NOT move",
        why="It is superseded or unordered. Recording it keeps your audit trail complete without "
            "letting it take effect.",
    ),
    Transition(
        phase=POST_024,
        condition="the decision_sequence exceeds the high-water mark AND the current source is "
                  "MANUAL",
        record="the callback, AND advance the high-water mark to its decision_sequence",
        effective="NO",
        why="The manual approval stays in force, but the mark still moves: otherwise every later "
            "automatic decision for the case is compared against a stale mark and the first one "
            "after a manual release would be judged by the wrong baseline.",
    ),
    Transition(
        phase=POST_024,
        condition="the decision_sequence exceeds the high-water mark AND the current source is "
                  "AUTOMATIC or absent",
        record="the callback, AND advance the high-water mark to its decision_sequence",
        effective="YES",
        why="This is the only row that applies an automatic decision, and it does so on proven "
            "DECISION order rather than on arrival order.",
    ),
)

# ── what 024 still needs from the platform, keyed to the live obligations ─────────────────────────
#
# Re-audit `4f23f23..97deeae` finding 10. The document asked for three things: where the accepted
# ledger lives, whether it distinguishes automatic from manual, and who signs. TechCraft could
# answer all three and 024 would remain non-buildable, because the live O1-O4 contract also needs
# decisions only the platform can make. Asking the wrong questions politely is still not asking.
#
# Keyed to the obligation ids in
# `.agents/superpowers/specs/2026-07-22-pr7b-activation-platform-ordering-design.md`, and the
# authority test parses that file's live obligation ids and requires every one of them covered.


@dataclass(frozen=True)
class PendingInput:
    """One decision 024 cannot be built without."""

    obligation: str  # O1-O4, as the live spec names them
    owner: str
    question: str
    answer_type: str
    authority: str
    blocks: str  # the deliverable that cannot be built until this is answered


PENDING_024_INPUTS: tuple[PendingInput, ...] = (
    PendingInput(
        obligation="O1",
        owner="TechCraft",
        question="Which platform principal is authorised to request a manual release, and what "
                 "identity does it present? Production requires a non-blank principal, and the "
                 "auth result must carry the verified HMAC VERSION, not just verified/not.",
        answer_type="principal identifier + key id + which HMAC version it signs with",
        authority="activation spec O1 (manual-release authority, executable and relationally bound)",
        blocks="the manual.release_requested request model and its admission gate",
    ),
    PendingInput(
        obligation="O1",
        owner="TechCraft",
        question="What is the authoritative deadline or TTL on a release request, and whose clock "
                 "is it measured against?",
        answer_type="duration + the clock that owns it (platform DB time, per O2)",
        authority="activation spec O1 (request model: release id, requested manual event, "
                  "authoritative deadline/TTL)",
        blocks="the release request model and the expiry reaper",
    ),
    PendingInput(
        obligation="O2",
        owner="TechCraft",
        question="Confirm the platform is the SOLE terminal and expiry authority: its CAS/reaper "
                 "transaction writes the result and enqueues a signed, retried "
                 "manual.release_outcome. We mirror that event and never choose expiry ourselves. "
                 "What is the reaper's cadence, and how is a lost outcome recovered?",
        answer_type="written confirmation + reaper cadence + outcome redelivery/recovery contract",
        authority="activation spec O2 (two-system convergence; no-traffic expiry)",
        blocks="the outcome mirror, the expiry path, and every convergence test",
    ),
    PendingInput(
        obligation="O3",
        owner="TechCraft",
        question="Confirm release_id is GLOBALLY unique, not unique per case, and that a reused "
                 "id is rejected outright rather than admitted on a second case.",
        answer_type="written confirmation of global uniqueness + who allocates the id",
        authority="activation spec O3 (release-id scope and governance)",
        blocks="the UNIQUE(release_id) constraint and its 409 path",
    ),
    PendingInput(
        obligation="O4",
        owner="both",
        question="Agree the complete WRITER-ROLE MATRIX for the 024 window: which processes on "
                 "each side write decisions, and confirm that during any old-image transition the "
                 "full maintenance stop applies, because an old-image writer does not take the "
                 "admission fence and the fence proves nothing about it.",
        answer_type="role list per side + written agreement on the old-image stop",
        authority="activation spec O4 (both decision writers fenced) + .agents/ROADMAP.md",
        blocks="the activation migration's admission fence and its preflight",
    ),
)

# The published signature vector, bound as ONE record: change any part and the test that recomputes
# it through kyc_tool.security.sign_v2 fails. The body is stored as bytes so its length and hash
# are properties of the same object the document prints.
VECTOR_BODY = (
    b'{"event_type":"kyb.run_requested","occurred_at":"2026-08-04T12:00:00Z",'
    b'"actor":{"type":"user","id":"acct-42"},'
    b'"payload":{"company_legal_name":"ACME NETWORKS LTD"}}'
)

SIGNATURE_VECTOR = {
    "secret": "integration-test-secret-0123456789ab",
    "key_id": "techcraft-inbound-1",
    "direction": "platform->tool",
    "method": "POST",
    "path_qs": "/v1/cases/case-42/events",
    "timestamp": "1754400000",
    "slot": "evt-0001",
    "body": VECTOR_BODY,
    "body_bytes": 163,
    "body_sha256": "9beb5e66987b7818d9a0e24cd141d2f94048b1c8785e8d25b02a17303a5fe024",
    "canonical_lines": (
        "v2",
        "techcraft-inbound-1",
        "platform->tool",
        "POST",
        "/v1/cases/case-42/events",
        "1754400000",
        "evt-0001",
        "9beb5e66987b7818d9a0e24cd141d2f94048b1c8785e8d25b02a17303a5fe024",
    ),
    "signature": "0dc9ff66f1a9e096b0dcae311efe15799b88f9f7475f3999cee10aaebcbacdba",
}

# (event_type, required payload fields, optional payload fields, note)
EVENT_TABLE = (
    ("kyb.run_requested", ("company_legal_name",),
     ("address", "contact", "jurisdiction", "platform_account_id", "registration_number",
      "website"),
     "First event for a case creates it; there is no registration call. Incomplete submissions "
     "ingest fine, and missing evidence simply never passes a check."),
    ("email.verified", ("domain", "email", "verified_at"), (),
     "You verify the mailbox; we score it."),
    ("org_id.submitted", ("org_handle", "rir"), (),
     "rir is one of arin, ripe, apnic, lacnic, afrinic."),
    ("poc.submitted", ("poc_handle", "rir"), ("org_handle", "resource"),
     "Triggers our verification email to the RIR-listed address."),
    ("poc.token_verified", ("token", "token_id", "verified_at"), (),
     "You host the verify page and echo the raw token from the email link. We store only its "
     "digest, and tokens expire after 72 hours."),
    ("document.uploaded", ("doc_type", "object_ref"), (),
     "object_ref points at the object your side wrote under uploads/."),
    ("website.review_completed", ("result", "reviewer_id", "task_id"), ("reason_codes",),
     "result is pass or fail. SENSITIVE EVENT: see the reviewer-actor rule; the task must be an "
     "open website task on the same case."),
    ("reviewer.manual_approve", ("reviewer_id",), ("note",),
     "SENSITIVE EVENT: see the reviewer-actor rule. Handled inline: no run, no callback, and it "
     "returns 200 rather than 202."),
    ("recalculate.requested", (), (),
     "Re-scores from stored evidence with no new fetches, and does not re-run the broker screen."),
)

WIRE = Registry(
    name="wire",
    claims=(
        # ── ingestion ─────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.INGEST.PATH",
            value="POST /v1/cases/{case_id}/events",
            authority="kyc_tool.api.routes_events.post_event",
        ),
        Claim(
            id="WIRE.INGEST.HEADERS",
            value=("Idempotency-Key", "X-KYC-Timestamp", "X-KYC-Key-Id", "X-KYC-Signature-V2"),
            authority="kyc_tool.api.routes_events.post_event + kyc_tool.api.auth",
        ),
        Claim(
            id="WIRE.INGEST.STATUS",
            value=(
                (202, "accepted and queued — the normal result for a new event"),
                (200, "replay of a seen Idempotency-Key (stored response verbatim), or an inline "
                      "reviewer.manual_approve, which runs without a queued job"),
                (400, "Idempotency-Key header missing"),
                (401, "signature invalid"),
                (409, "same Idempotency-Key with a different payload, or a review-task state "
                      "conflict"),
                (422, "malformed envelope, malformed payload, or an invalid reviewer actor"),
                (404, "review task not found"),
            ),
            authority="kyc_tool.events.ingest.ingest_event",
            note="A queued event is 202. Treating 200 as the success case would misread every "
                 "normal submission.",
        ),
        Claim(
            id="WIRE.INGEST.EXTRA_FIELDS",
            value={
                "envelope": "forbid",
                "payload": "allow",
                "prose": "Unknown fields at the TOP LEVEL of the envelope are rejected with 422. "
                         "Unknown fields inside payload (and inside actor) are preserved. This is "
                         "not a symmetric must-ignore contract.",
            },
            authority="kyc_tool.api.schemas.EventEnvelope.model_config / PAYLOAD_MODELS",
        ),
        Claim(
            id="WIRE.INGEST.ORDERING",
            value="Events for one case are serialized by the order we admit them under that "
                  "case's lock. Send same-case events one at a time and wait for acceptance if "
                  "order matters: two submitted concurrently are admitted in lock-acquisition "
                  "order, which need not match your send order.",
            authority="kyc_tool.events.ingest (Case FOR UPDATE admission)",
        ),
        Claim(
            id="WIRE.ACTOR.SENSITIVE",
            value={
                "events": ("website.review_completed", "reviewer.manual_approve"),
                "actor_type": "reviewer",
                "rule": "actor.type must equal 'reviewer', and actor.id must equal "
                        "payload.reviewer_id exactly — case-sensitive, after trimming surrounding "
                        "whitespace, with neither blank. Anything else is 422.",
                "trap": "The generic envelope example shows actor.type 'user'. Copying it for "
                        "either sensitive event fails.",
            },
            authority="kyc_tool.events.review_guard.reviewer_actor_reason",
        ),
        Claim(
            id="WIRE.EVENT.TABLE",
            value=EVENT_TABLE,
            authority="kyc_tool.api.schemas.EventType / PAYLOAD_MODELS required+optional fields",
        ),
        # ── signing ───────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.SIGN.CANONICAL",
            value=("v2", "key_id", "direction", "method", "path?query", "timestamp", "slot",
                   "sha256(body) hex"),
            authority="kyc_tool.security.canonical_v2",
            note="Eight lines, LF-joined, in this order.",
        ),
        Claim(
            id="WIRE.SIGN.DIRECTIONS",
            value={"platform_to_tool": "platform->tool", "tool_to_platform": "tool->platform"},
            authority="kyc_tool.security.DIRECTION_INBOUND / DIRECTION_OUTBOUND",
            note="Literal tokens. Prose like 'inbound' will not verify.",
        ),
        Claim(
            id="WIRE.SIGN.SKEW_SECONDS",
            value=300,
            authority="kyc_tool.security.MAX_HMAC_SKEW_SECONDS",
        ),
        Claim(
            id="WIRE.SIGN.VECTOR",
            value=SIGNATURE_VECTOR,
            authority="kyc_tool.security.sign_v2 via docs.contracts.signing_example.sign",
            note="Verify your implementation against this before writing any other code.",
        ),
        Claim(
            id="WIRE.SIGN.V1_SUNSET",
            value="Both v1 sunset dates are set with you at cutover and are unset in our config "
                  "today. The inbound date additionally cannot take effect until an observation "
                  "window records zero v1 traffic, so a date alone never cuts off a live sender. "
                  "Ship v2 from day one and none of this applies.",
            authority=".env.example (both sunset vars unset) + docs/DEPLOYMENT.md §2",
        ),
        Claim(
            id="WIRE.SIGN.ROTATION",
            value=(
                "INBOUND (your key, verifying your calls to us): we accept an overlap. Add your "
                "new key id alongside the old one in our rotation map, switch your signer to the "
                "new id, wait until no request arrives under the old id, then we remove it. "
                "Rotation secrets are full verification credentials and carry the same floor as "
                "the active key: at least 32 characters, a non-blank key id, and no collision "
                "with the active id.",
                "OUTBOUND (our key, signing our callbacks to you): THE OVERLAP IS YOURS TO HOLD. "
                "We sign with exactly one outbound key and have no second-key facility, so the "
                "order is: you start accepting old and new, THEN we switch our signer, then you "
                "confirm callbacks are arriving under the new key id, then you retire the old. "
                "Switching us first means every callback fails verification until you catch up, "
                "and they dead-letter.",
            ),
            authority="kyc_tool.config.hmac_extra_key_violations + kyc_tool.api.auth._inbound_secret "
                      "+ kyc_tool.outbox.publisher (one outbound signer)",
        ),
        # ── callbacks ─────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.CALLBACK.PATH",
            value="POST <your-base>/kyc/decision",
            authority="kyc_tool.outbox.publisher (callback URL + '/kyc/decision')",
        ),
        Claim(
            id="WIRE.CALLBACK.FIELDS",
            value=("case_id", "run_id", "event_id", "decision", "score", "gates",
                   "buy_enablement", "checks", "decided_at"),
            authority="kyc_tool.api.schemas.DecisionCallback (required fields)",
        ),
        Claim(
            id="WIRE.CALLBACK.OPTIONAL_FIELDS",
            value=("event_sequence", "enforcement_held"),
            authority="kyc_tool.api.schemas.DecisionCallback (optional fields)",
            note="Tolerate and preserve both. enforcement_held carries the computed decision "
                 "while the enforcement hold is active; the outer decision stays authoritative.",
        ),
        Claim(
            id="WIRE.CALLBACK.GATES",
            value=("score_met", "legal_proof", "control_proof", "broker_ok", "no_hard_conflict"),
            authority="kyc_tool.api.schemas.GatesBody",
        ),
        Claim(
            id="WIRE.CALLBACK.DECISIONS",
            value=("approve", "approve_buy_locked", "manual_review_insufficient", "reject"),
            authority="kyc_tool.api.schemas.DecisionCallback.decision literal",
        ),
        Claim(
            id="WIRE.CALLBACK.DELIVERY",
            value=(
                "Each automated decision enqueues one callback row in our transactional outbox.",
                "An eligible row is delivered AT LEAST ONCE until you return 2xx or it "
                "dead-letters, so duplicates are expected; dedupe on (case_id, run_id).",
                "SUPPRESSED means zero sends: a row we can locally prove obsolete is never put on "
                "the wire at all.",
                "DEAD-LETTERED does NOT mean zero sends. A dead row exhausted its attempts "
                "without us ever witnessing a 2xx, and it may have made up to eight HTTP "
                "attempts. If one of those committed on your side and the response was lost, you "
                "have applied a decision we recorded as undelivered. Before we requeue a dead "
                "row, check your accepted ledger for that (case_id, run_id).",
                "Manual approvals by your reviewers send nothing at all.",
                "Do not assume a one-to-one match between decisions we make and callbacks you "
                "receive.",
            ),
            authority="AUDIT_FINDINGS.md A6 + kyc_tool.outbox.publisher module docstring",
        ),
        Claim(
            id="WIRE.CALLBACK.RECEIVER_TXN",
            value=(
                "Verify the signature.",
                "In ONE transaction: dedupe on (case_id, run_id); decide whether this callback "
                "becomes EFFECTIVE using the algorithm below; record it in your accepted ledger "
                "either way.",
                "COMMIT.",
                "Only then return 2xx.",
                "If the commit fails or its outcome is uncertain, return non-2xx or drop the "
                "connection so we retry.",
            ),
            authority="the accepted receiver design (activation design doc) + publisher "
                      "terminalizes on 2xx",
            exclusive_terms=("return 2xx", "COMMIT"),
            note="A 2xx returned before your commit is unrecoverable: we mark the row delivered "
                 "and at-least-once cannot help you. This is the single most important "
                 "requirement on your side.",
        ),
        Claim(
            id="WIRE.CALLBACK.EFFECTIVENESS",
            value=RECEIVER_TRANSITIONS,
            authority="AUDIT_FINDINGS.md A6 residual reverts + the accepted receiver design + "
                      "docs/contracts/receiver_reference.py (executable, scenario-tested)",
            note="ACKNOWLEDGING a callback and APPLYING it are different decisions: always "
                 "acknowledge and record, then consult this table for whether it takes effect. "
                 "Rows are evaluated in order within a phase and the first match wins; the rows "
                 "are exhaustive, so there is no 'otherwise' branch to fall through to. Automatic "
                 "authority over a manual-current case returns ONLY through the authenticated "
                 "platform-owned release protocol, never by a callback arriving.",
        ),
        Claim(
            id="WIRE.CALLBACK.RETRY",
            value={
                "attempts": 8,
                "base_seconds": 10,
                "delays": (10, 20, 40, 80, 160, 320, 640),
                "jitter": False,
                "backoff_total_seconds": 1270,
                # backoff plus every attempt's 40s hard wall, rounded UP: an operator waiting on a
                # dead-letter should never be told a shorter window than the real worst case.
                "worst_case_seconds": 1590,
                "backoff_total_minutes": 22,
                "worst_case_minutes": 27,
            },
            authority="kyc_tool.queue.backoff.saturating_backoff_seconds + Settings "
                      "outbox_backoff_base_seconds / outbox_max_attempts",
        ),
        Claim(
            id="WIRE.CALLBACK.WAIT_BOUND",
            value="Respond within 10 seconds. Each attempt carries a 40-second hard wall (4 x the "
                  "10s HTTP phase timeout). Past it we STOP WAITING, detach the attempt, and "
                  "account a retryable failure. We cannot retract a request already in flight, so "
                  "a detached attempt may still reach you and still take effect. Dedupe is "
                  "mandatory, not advisory.",
            authority="kyc_tool.outbox.publisher (detached attempt accounting; no cancellation)",
        ),
        Claim(
            id="WIRE.CALLBACK.COMPLETION",
            value="For an automated decision that produces an eligible callback, that callback is "
                  "the completion signal and nothing needs polling. It is not a universal "
                  "completion signal: manual approvals produce no callback at all, a suppressed "
                  "row is never sent, and a dead-lettered row was sent without a witnessed "
                  "success rather than not sent.",
            authority="AUDIT_FINDINGS.md A6 + kyc_tool.events.ingest (manual approve is inline)",
        ),
        # ── ordering ──────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.ORDERING.NO_DECIDED_AT",
            value="decided_at is a display timestamp and is not an ordering key. The wire value "
                  "is stamped from the deciding worker's wall clock, and different workers decide "
                  "for one case, so ordinary clock skew between hosts can give the older decision "
                  "the later timestamp. The database column of that name is worse: it holds "
                  "transaction-start time, which inverts against the lock-serialized commit "
                  "order. Our own migration tooling is barred from falling back to it.",
            authority="AUDIT_FINDINGS.md D-7bcore + docs/RUNBOOK.md step 0.5 + "
                      "kyc_tool.db.tables.Case.latest_decision_row_id",
            exclusive_terms=("ordering key", "decided_at is"),
        ),
        Claim(
            id="WIRE.ORDERING.INTERIM",
            value="Until ordered delivery is activated: dedupe exact repeats on (case_id, "
                  "run_id), and resolve same-case conflicts either through an ordering authority "
                  "you own or by holding them for review. A reviewer's manual approval sends you "
                  "nothing and an automatic callback queued before it can arrive after it, so "
                  "treat a manual approval as authoritative over any automatic decision that "
                  "arrives later for that case.",
            authority="AUDIT_FINDINGS.md A6 residual reverts",
        ),
        Claim(
            id="WIRE.ORDERING.SEQUENCE_DOMAINS",
            value=(
                "event_sequence is INGEST PROVENANCE: the per-case ordinal of the event that "
                "triggered a run. It is on the wire today. It is NEVER an ordering authority for "
                "decisions, and you must not sort or dedupe decisions by it.",
                "decision_sequence is the CALLBACK-ORDER AUTHORITY. It is allocated per case when "
                "the decision is made, and it goes on the wire with the activation unit (024). "
                "The post-024 high-water mark in section 3 is a high-water mark over "
                "decision_sequence and nothing else.",
                "They differ whenever work completes out of admission order, which is normal: an "
                "event admitted first can decide second. Order them by event_sequence and the "
                "later decision loses to the earlier one.",
            ),
            authority=".agents/ROADMAP.md D1 (PR 2 event_sequence / PR 7b decision_sequence) + "
                      "the activation spec wire-emission section",
            note="Two ordinals for one case, and only one of them orders decisions. Getting this "
                 "backwards is silent: both are monotonic per case, so a receiver built on the "
                 "wrong one looks correct until two runs overlap.",
            exclusive_terms=("INGEST PROVENANCE", "CALLBACK-ORDER AUTHORITY"),
        ),
        Claim(
            id="WIRE.ORDERING.PENDING_INPUTS",
            value=PENDING_024_INPUTS,
            authority=".agents/superpowers/specs/2026-07-22-pr7b-activation-platform-ordering-"
                      "design.md open blockers O1-O4 (parsed live by the authority test)",
            state=ClaimState.PENDING,
            note="These are the decisions 024 cannot be built without, and every one of them is "
                 "the platform's to make. Answering the three questions in section 1.1 alone "
                 "leaves the unit blocked.",
        ),
        Claim(
            id="WIRE.ORDERING.BOOTSTRAP_024",
            value="Ordered delivery needs a signed bootstrap of per-case high-water marks from "
                  "your accepted-run ledger, because your ledger is the authority on what you "
                  "actually applied. The design is accepted but NOT BUILT, and its schema is not "
                  "final. Do not implement against a draft. What we already know it must carry: "
                  "the complete universe of durable callback rows rather than a per-case latest; "
                  "your accepted-run ledger kept separate from the currently effective source "
                  "per case (an automatic callback versus a manual approval); a floor for "
                  "manual-current cases; exact two-sided coverage so both sides can prove "
                  "convergence; request and response digests; and a freshly signed response "
                  "envelope. We will publish the schema when the activation unit is built.",
            authority=".agents/ROADMAP.md PR 7b-activation (migration 024, pending)",
            state=ClaimState.PENDING,
        ),
        Claim(
            id="WIRE.ORDERING.INTEGRITY_MISMATCH",
            value="integrity_mismatch is a POST-ACTIVATION terminal state. No code path emits it "
                  "today; it arrives with the activation unit.",
            authority=".agents/ROADMAP.md PR 7b-activation",
            state=ClaimState.PENDING,
        ),
        # ── retention ─────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.RETENTION.BY_KIND",
            value=(
                ("decision callbacks",
                 "Bodies are redacted in place after the retention window; the row and its "
                 "ordering identity remain."),
                ("POC verification emails",
                 "The body is redacted as soon as the row reaches delivered or dead, not at the "
                 "retention window; delivered rows are DELETED later. A dead POC email cannot be "
                 "requeued because its token is already gone, so recovery is a fresh "
                 "poc.submitted."),
            ),
            authority="kyc_tool.workers.retention + kyc_tool.outbox.publisher terminal writes",
        ),
        Claim(
            id="WIRE.RETENTION.WINDOW_DAYS",
            value=2555,
            authority="kyc_tool.config.Settings.retention_days default",
        ),
    ),
)
