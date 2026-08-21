"""Renders the TechCraft Platform Integration Contract from `docs.contracts.wire`.

Run from the repo root:  python -m docs.generators.techcraft_integration_contract

Prose in this file explains and warns. Every value TechCraft builds against comes from the
registry, so a change in the code moves the document through a failing authority test rather than
through someone remembering to edit a sentence.
"""

import argparse
import re
from datetime import date

from docs.contracts.companion import ARTIFACT_NAME, artifact_digest
from docs.contracts.signing_example import published_snippet
from docs.contracts.wire import WIRE
from docs.generators.render import INCH, Doc, DocumentManifest, escape

# The document's own identity: the title every page footer and the PDF metadata
# publish, owned here rather than passed to build() (Wave-2 audit finding 3).
MANIFEST = DocumentManifest(
    title="KYC Tool — Platform Integration Contract",
    out="techcraft-integration-contract.pdf",
)
OUT = MANIFEST.out

# Release inputs. These are NOT registry claims — they change per send, and nothing in the repo
# governs them — so they are required arguments with NO defaults (re-audit `6feca36..4f23f23` F11).
# The document asks TechCraft eleven questions and previously shipped "[integration contact - fill
# in]" beside a hardcoded date: a document that asks for a reply and then names nowhere to send it
# is worse than one that asks nothing, and a stale date silently becomes a deadline that has
# already passed. A build that cannot name both must fail rather than emit a placeholder.
_PLACEHOLDER = re.compile(r"fill[ -]?in|\bTBD\b|\[|\]|<|>", re.IGNORECASE)


def validate_release_inputs(contact: str, due_date: str) -> tuple[str, str]:
    """Refuse anything a reader could not act on. Raises ValueError."""
    contact = (contact or "").strip()
    if not contact:
        raise ValueError("--integration-contact is required")
    if _PLACEHOLDER.search(contact):
        raise ValueError(f"--integration-contact is still a placeholder: {contact!r}")
    if "@" not in contact and "://" not in contact:
        raise ValueError(
            f"--integration-contact must be an address or a URL TechCraft can reply to: {contact!r}"
        )
    due_date = (due_date or "").strip()
    if not due_date:
        raise ValueError("--response-due-date is required")
    try:
        parsed = date.fromisoformat(due_date)
    except ValueError as exc:
        raise ValueError(f"--response-due-date must be ISO-8601 YYYY-MM-DD: {due_date!r}") from exc
    return contact, parsed.isoformat()


# Claims this document is REQUIRED to display. The rendering test asserts each reaches a flowable
# exactly once and appears in the extracted PDF text.
REQUIRED_CLAIMS = (
    "WIRE.INGEST.PATH",
    "WIRE.INGEST.HEADERS",
    "WIRE.INGEST.STATUS",
    "WIRE.INGEST.EXTRA_FIELDS",
    "WIRE.INGEST.ORDERING",
    "WIRE.ACTOR.SENSITIVE",
    "WIRE.EVENT.TABLE",
    "WIRE.SIGN.CANONICAL",
    "WIRE.SIGN.DIRECTIONS",
    "WIRE.SIGN.SKEW_SECONDS",
    "WIRE.SIGN.COMPANION",
    "WIRE.SIGN.VECTOR",
    "WIRE.SIGN.V1_SUNSET",
    "WIRE.SIGN.ROTATION",
    "WIRE.SIGN.ROTATION_RETIREMENT",
    "WIRE.CALLBACK.PATH",
    "WIRE.CALLBACK.FIELDS",
    "WIRE.CALLBACK.OPTIONAL_FIELDS",
    "WIRE.CALLBACK.GATES",
    "WIRE.CALLBACK.DECISIONS",
    "WIRE.CALLBACK.DELIVERY",
    "WIRE.CALLBACK.VALIDATION",
    "WIRE.CALLBACK.RECEIVER_TXN",
    "WIRE.CALLBACK.EFFECTIVENESS",
    "WIRE.CALLBACK.LEGEND",
    "WIRE.CALLBACK.RELEASE",
    "WIRE.CALLBACK.RETRY",
    "WIRE.CALLBACK.WAIT_BOUND",
    "WIRE.CALLBACK.COMPLETION",
    "WIRE.ORDERING.NO_DECIDED_AT",
    "WIRE.ORDERING.SEQUENCE_DOMAINS",
    "WIRE.ORDERING.INTERIM",
    "WIRE.ORDERING.BOOTSTRAP_024",
    "WIRE.ORDERING.PENDING_INPUTS",
    "WIRE.ORDERING.INTEGRITY_MISMATCH",
    "WIRE.RETENTION.BY_KIND",
    "WIRE.RETENTION.WINDOW_DAYS",
)


def build(*, contact: str, due_date: str) -> Doc:
    contact, due_date = validate_release_inputs(contact, due_date)
    doc = Doc(WIRE, MANIFEST)
    doc.title()
    doc.p(
        "<b>Audience: TechCraft integration developers.</b> This covers your side of the wire: "
        "what to POST, what callbacks to receive and verify, and what to stand up (the "
        "/kyc/decision endpoint, the token verify page, uploads/ bucket writes). Hosting and "
        "operating the tool is covered separately in the Staging Integration and Production "
        "Readiness Guide."
    )

    doc.h2("How it works")
    doc.claim_paragraph("WIRE.INGEST.ORDERING")
    doc.p(
        "Each automated decision enqueues one signed callback to your endpoint. Both directions "
        "use the same HMAC-v2 scheme with separate key pairs, and delivery is at-least-once, so "
        "you dedupe on (case_id, run_id). Section 3 is the exact delivery contract."
    )
    doc.why(
        f"Send answers to the section 1 questions to <b>{escape(contact)}</b> by <b>{escape(due_date)}</b>."
    )

    # ── 1. asks ────────────────────────────────────────────────────────────────────────────────
    doc.section("asks", "1. Answers we need from you")
    doc.p(
        "<b>1.1 Ordered decision delivery.</b> Today there is no wire ordering authority. The "
        "design that fixes it is accepted but unbuilt, and we are not publishing a schema you "
        "could implement against yet:"
    )
    doc.claim_paragraph("WIRE.ORDERING.BOOTSTRAP_024")
    doc.p(
        "What we need from you now is not code. Tell us which system holds your accepted-run "
        "ledger, whether it distinguishes an automatic decision from a manual approval as the "
        "currently effective source for a case, and who signs on your side. You implement once we "
        "publish the schema."
    )
    doc.p(
        "Those three answers are necessary and not sufficient. The activation unit is blocked on "
        "decisions only your side can make, listed below with the deliverable each one blocks. "
        "An answer is screened but cannot clear its obligation yet — the note above the table "
        "says why — and we cannot build 024 without all of them."
    )
    # The PENDING alert renders BEFORE any answer row (gate finding 10): the note carries the
    # no-reply-can-RESOLVE statement, and putting it after the table handed the reader the rows
    # first and the truth second.
    doc.claim_note("WIRE.ORDERING.PENDING_INPUTS")
    doc.claim_table(
        "WIRE.ORDERING.PENDING_INPUTS",
        # the deliverable column has to fit `manual.release_requested` whole
        [0.35 * INCH, 0.8 * INCH, 2.35 * INCH, 1.25 * INCH, 1.95 * INCH],
    )
    doc.p(
        "<b>1.2 Dedupe commitment.</b> Confirm you dedupe callbacks on (case_id, run_id) and "
        "drop a late duplicate that arrives after a newer decision."
    )
    doc.p(
        "<b>1.3 Callback URL and key exchange.</b> Production and staging HTTPS base URLs (we "
        "append /kyc/decision), plus key ids and secrets both directions. Our production boot "
        "refuses HTTP, localhost, and any query or fragment in the base URL, so a wrong value "
        "fails at deploy time rather than at first delivery. <b>Secrets move over an encrypted "
        "channel</b>: a shared vault entry or an age/GPG file to a published key, never email, "
        "chat, or ticket text."
    )
    doc.p(
        "<b>1.4 Document upload path.</b> document.uploaded events carry an object_ref your "
        "side wrote to the shared object store. Confirm the bucket, the uploads/ key "
        "convention, and the IAM split: you write uploads/, we read them, and our staging "
        "namespace adapter-raw/ is written and swept only by us."
    )

    # ── 2. events ──────────────────────────────────────────────────────────────────────────────
    doc.section("events", "2. Events you send us")
    doc.claim_paragraph("WIRE.INGEST.PATH", prefix="<b>Endpoint: </b>")
    doc.p(
        "The first event for a case creates it; there is no registration call. <b>No inbound "
        "rate limit:</b> bursts queue on our side, since we throttle our own registry calls "
        "rather than you. We return no 429 today; if a limit is ever added it will be 429 with "
        "Retry-After, announced in advance."
    )
    doc.claim_table(
        "WIRE.INGEST.HEADERS",
        [1.35 * INCH, 1.75 * INCH, 3.6 * INCH],
        code_columns=(0,),
    )
    doc.space()
    doc.p("Envelope:")
    doc.code(
        '{"event_type": "kyb.run_requested", "occurred_at": "2026-08-04T12:00:00Z",\n'
        ' "actor": {"type": "user", "id": "acct-123"}, "payload": { ... }}'
    )
    doc.h2("Unknown fields are not symmetric")
    extra = WIRE.value("WIRE.INGEST.EXTRA_FIELDS")
    doc.claim_prose(
        "WIRE.INGEST.EXTRA_FIELDS",
        escape(extra["prose"]) + f" (envelope: <b>{escape(extra['envelope'])}</b>; payload: "
        f"<b>{escape(extra['payload'])}</b>.) In the other direction, ignore fields we add to "
        "callbacks: we add without notice and never remove or repurpose one without a version "
        "bump agreed with you.",
    )

    doc.h2("Sensitive events: the reviewer-actor rule")
    actor = WIRE.value("WIRE.ACTOR.SENSITIVE")
    doc.claim_mixed(
        "WIRE.ACTOR.SENSITIVE",
        [
            (
                "p",
                "<b>" + escape(", ".join(actor["events"])) + "</b> both require an actor identifying "
                "the reviewer. " + escape(actor["rule"]),
            ),
            ("why", "<b>Trap:</b> " + escape(actor["trap"])),
        ],
    )

    doc.claim_table(
        "WIRE.EVENT.TABLE",
        # column 0 fits `website.review_completed`, the longest event name, whole; the payload
        # columns fit `platform_account_id`. _token_cell raises rather than clipping, so a name
        # that outgrows its column fails the build instead of printing one character short.
        [1.75 * INCH, 1.4 * INCH, 1.4 * INCH, 2.15 * INCH],
        code_columns=(0, 1, 2),
    )
    doc.space()
    doc.h2("Response codes")
    doc.claim_table("WIRE.INGEST.STATUS", [0.6 * INCH, 6.1 * INCH])

    # ── 3. callbacks ───────────────────────────────────────────────────────────────────────────
    doc.section("callbacks", "3. Callbacks we send you")
    doc.claim_paragraph("WIRE.CALLBACK.PATH", prefix="<b>Endpoint: </b>")
    doc.p("Content-Type application/json, signed with our outbound key.")
    fields = WIRE.value("WIRE.CALLBACK.FIELDS")
    doc.claim_prose("WIRE.CALLBACK.FIELDS", "Required body fields: <b>" + escape(", ".join(fields)) + "</b>.")
    doc.claim_prose(
        "WIRE.CALLBACK.DECISIONS",
        "<b>decision is one of: </b>"
        + escape(", ".join(WIRE.value("WIRE.CALLBACK.DECISIONS"))) + ".")
    doc.claim_prose(
        "WIRE.CALLBACK.GATES",
        "<b>gates carries these booleans: </b>"
        + escape(", ".join(WIRE.value("WIRE.CALLBACK.GATES"))) + ".")
    optional = WIRE["WIRE.CALLBACK.OPTIONAL_FIELDS"]
    doc.claim_prose(
        "WIRE.CALLBACK.OPTIONAL_FIELDS",
        "Optional fields to tolerate and preserve: <b>"
        + escape(", ".join(optional.value))
        + "</b>. "
        + escape(optional.note),
    )

    doc.h2("Delivery: what actually reaches you")
    doc.claim_bullets("WIRE.CALLBACK.DELIVERY")

    doc.h2("Validate before you classify")
    doc.claim_note("WIRE.CALLBACK.VALIDATION")
    doc.claim_table("WIRE.CALLBACK.VALIDATION", [3.35 * INCH, 3.6 * INCH])
    doc.keep_last_together(3)

    doc.h2("Your endpoint must commit before it answers")
    doc.claim_steps("WIRE.CALLBACK.RECEIVER_TXN")
    doc.claim_note("WIRE.CALLBACK.RECEIVER_TXN")

    doc.h2("Recording a callback is not the same as acting on it")
    doc.claim_note("WIRE.CALLBACK.EFFECTIVENESS")
    # the outcome booleans are the machine-checkable mirror of the printed `record`/`effective`
    # cells; both derive from the same OutcomeKind record, and a test asserts the halves agree
    doc.claim_table(
        "WIRE.CALLBACK.EFFECTIVENESS",
        [0.6 * INCH, 1.85 * INCH, 1.2 * INCH, 1.15 * INCH, 2.15 * INCH],
    )
    # heading + note + table travel as one unit: the heading must never sit alone at a page
    # bottom with the table starting overleaf (gate finding 15)
    doc.keep_last_together(3)
    doc.claim_table("WIRE.CALLBACK.LEGEND", [1.55 * INCH, 5.15 * INCH], code_columns=(0,))
    doc.claim_note("WIRE.CALLBACK.LEGEND")

    doc.h2("While a release is pending (post-024 only)")
    doc.claim_note("WIRE.CALLBACK.RELEASE")
    doc.claim_table(
        "WIRE.CALLBACK.RELEASE",
        [2.1 * INCH, 1.25 * INCH, 1.5 * INCH, 1.85 * INCH],
    )

    doc.h2("Timing")
    doc.claim_paragraph("WIRE.CALLBACK.WAIT_BOUND")
    retry = WIRE.value("WIRE.CALLBACK.RETRY")
    doc.claim_prose(
        "WIRE.CALLBACK.RETRY",
        "Retry schedule: <b>" + ", ".join(f"{d}s" for d in retry["delays"]) + "</b> across "
        f"<b>{retry['attempts']} attempts</b>, no jitter. Backoff alone totals "
        f"{retry['backoff_total_seconds']}s (about {retry['backoff_total_minutes']} minutes); "
        f"counting every attempt's hard wall the worst case is {retry['worst_case_seconds']}s "
        f"(about {retry['worst_case_minutes']} minutes). An outage longer than that exhausts "
        "the schedule and the row dead-letters; tell us and we requeue.",
    )
    doc.claim_paragraph("WIRE.CALLBACK.COMPLETION")

    # ── 4. signing ─────────────────────────────────────────────────────────────────────────────
    doc.section("signing", "4. Request signing (HMAC v2)")
    canonical = WIRE.value("WIRE.SIGN.CANONICAL")
    # Numbered by the REGISTRY projection, not by an f-string here: the generator numbering its
    # own lines meant the generator authored the very content the page was checked against
    # (re-audit `4f23f23..122cc67` finding 3).
    doc.claim_code(
        "WIRE.SIGN.CANONICAL",
        numbered=True,
        lead=f"Signature = hex HMAC-SHA256 over these {len(canonical)} lines, LF-joined, in "
             "this order:",
    )
    directions = WIRE["WIRE.SIGN.DIRECTIONS"]
    doc.claim_prose(
        "WIRE.SIGN.DIRECTIONS",
        "The direction tokens are literals: <b>"
        + escape(directions.value["platform_to_tool"])
        + "</b> for your calls to us and <b>"
        + escape(directions.value["tool_to_platform"])
        + "</b> for ours to you. "
        + escape(directions.note)
        + " slot is the Idempotency-Key on "
        "event POSTs and empty otherwise; path?query is the raw request target.",
    )
    doc.claim_paragraph("WIRE.SIGN.SKEW_SECONDS", prefix="<b>Skew window (seconds): </b>")
    doc.why(
        "v1 signed only timestamp and body, so a captured signature could replay against a "
        "different path. v2 binds method, exact path, and the idempotency slot. Sending any "
        "v2 header disables the v1 fallback for that request."
    )
    doc.claim_paragraph("WIRE.SIGN.V1_SUNSET", prefix="<b>On the v1 sunset dates: </b>")
    doc.h2("Key rotation: the two directions are not symmetric")
    doc.claim_bullets("WIRE.SIGN.ROTATION")
    doc.claim_table(
        "WIRE.SIGN.ROTATION_RETIREMENT",
        # "Blocked step" must fit the longer gated clause wrapped; the two prose columns share
        # the rest of the frame.
        [0.85 * INCH, 1.45 * INCH, 2.3 * INCH, 2.1 * INCH],
        # `refuse`/`must_reject` are the executable gate and its red specimens; checked, not shown.
        row_fields=("direction", "transition", "why_blocked", "unblocked_by"),
    )

    doc.h2("The runnable signer is a file, not the page")
    doc.claim_mixed("WIRE.SIGN.COMPANION", [
        ("p", escape(WIRE.value("WIRE.SIGN.COMPANION"))),
        ("code", f"{ARTIFACT_NAME}\nsha256  {artifact_digest()}"),
    ])
    doc.claim_note("WIRE.SIGN.COMPANION")

    doc.h2("Test vector: verify against this before writing anything else")
    v = WIRE.value("WIRE.SIGN.VECTOR")
    doc.claim_mixed(
        "WIRE.SIGN.VECTOR",
        [
            (
                "p",
                f"Secret <b>{escape(v['secret'])}</b>, key id <b>{escape(v['key_id'])}</b>, "
                f"timestamp <b>{v['timestamp']}</b>, Idempotency-Key <b>{escape(v['slot'])}</b>, "
                f"<b>{escape(v['method'])} {escape(v['path_qs'])}</b>. The body is one line, "
                f"<b>{v['body_bytes']} bytes</b>, UTF-8, no whitespace and no trailing newline. It "
                "wraps in print below; a newline you add changes the digest.",
            ),
            ("wrap", v["body"].decode()),
            ("p", "Canonical string:"),
            ("code", "\n".join(v["canonical_lines"])),
            ("p", "Expected X-KYC-Signature-V2:"),
            ("code", v["signature"]),
            (
                "p",
                "Reference implementation. This is the exact source our tests execute against the "
                "shipped signer to produce the digest above:",
            ),
            ("atomic_code", published_snippet(delimited=True)),
        ],
    )

    # ── 5. ordering ────────────────────────────────────────────────────────────────────────────
    doc.section("ordering", "5. Ordering, and one field you must not sort by")
    doc.claim_paragraph("WIRE.ORDERING.NO_DECIDED_AT")
    doc.h2("Two per-case ordinals, and only one of them orders decisions")
    doc.claim_bullets("WIRE.ORDERING.SEQUENCE_DOMAINS")
    doc.claim_note("WIRE.ORDERING.SEQUENCE_DOMAINS")
    doc.claim_paragraph("WIRE.ORDERING.INTERIM", prefix="<b>Until activation: </b>")
    doc.claim_paragraph("WIRE.ORDERING.INTEGRITY_MISMATCH", prefix="<b>Note: </b>")

    # ── 6. retention ───────────────────────────────────────────────────────────────────────────
    doc.section("retention", "6. Retention")
    doc.claim_paragraph("WIRE.RETENTION.WINDOW_DAYS", prefix="<b>Compliance window (days): </b>")
    doc.claim_table("WIRE.RETENTION.BY_KIND", [1.5 * INCH, 5.2 * INCH])

    # ── 7. checklist ───────────────────────────────────────────────────────────────────────────
    doc.section("checklist", "7. Go-live checklist")
    doc.table(
        ("#", "Item", "Owner"),
        (
            ("1", "Callback base URLs, staging and production (HTTPS, no query or fragment)", "TechCraft"),
            ("2", "Key ids and secrets both directions, both environments, encrypted channel", "both"),
            ("3", "Object store bucket and IAM split (uploads/ write for you, read for us)", "both"),
            ("4", "POC verify page URL pattern for token links", "TechCraft"),
            ("5", "Expected event volume and burst profile", "TechCraft"),
            ("6", "Dedupe on (case_id, run_id) confirmed in your handler", "TechCraft"),
            ("7", "Receiver commits before returning 2xx (section 3)", "TechCraft"),
            ("7b", "Acknowledge-versus-apply implemented: a callback arriving after a manual "
                   "approval is recorded but not made effective (section 3)", "TechCraft"),
            ("8", "Ordering: no sorting by decided_at; conflict path agreed (section 5)", "TechCraft"),
            ("9", "Accepted-run ledger and signer identified for future activation (1.1)", "TechCraft"),
            ("10", "Escalation contact for dead-letter alerts", "TechCraft"),
            ("11", "Staging end-to-end: test vector passes, event in, callback out, verified", "both"),
            ("12", "Sender retries on 502/503/504 and re-signs with a fresh timestamp", "both"),
        ),
        [0.35 * INCH, 4.85 * INCH, 1.5 * INCH],
    )
    return doc


def main(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--integration-contact", required=True, help="address or URL TechCraft sends the section 1 answers to"
    )
    parser.add_argument(
        "--response-due-date", required=True, help="ISO-8601 YYYY-MM-DD date those answers are due"
    )
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args(argv)
    doc = build(contact=args.integration_contact, due_date=args.response_due_date)
    return doc.build(args.out)


if __name__ == "__main__":
    print(main())
