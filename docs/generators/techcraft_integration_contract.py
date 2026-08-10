"""Renders the TechCraft Platform Integration Contract from `docs.contracts.wire`.

Run from the repo root:  python -m docs.generators.techcraft_integration_contract

Prose in this file explains and warns. Every value TechCraft builds against comes from the
registry, so a change in the code moves the document through a failing authority test rather than
through someone remembering to edit a sentence.
"""

from docs.contracts.signing_example import published_snippet
from docs.contracts.wire import WIRE
from docs.generators.render import INCH, Doc, escape

OUT = "techcraft-integration-contract.pdf"

# Claims this document is REQUIRED to display. The rendering test asserts each reaches a flowable
# exactly once and appears in the extracted PDF text.
REQUIRED_CLAIMS = (
    "WIRE.INGEST.PATH", "WIRE.INGEST.HEADERS", "WIRE.INGEST.STATUS", "WIRE.INGEST.EXTRA_FIELDS",
    "WIRE.INGEST.ORDERING", "WIRE.ACTOR.SENSITIVE", "WIRE.EVENT.TABLE",
    "WIRE.SIGN.CANONICAL", "WIRE.SIGN.DIRECTIONS", "WIRE.SIGN.SKEW_SECONDS", "WIRE.SIGN.VECTOR",
    "WIRE.SIGN.V1_SUNSET", "WIRE.SIGN.ROTATION",
    "WIRE.CALLBACK.PATH", "WIRE.CALLBACK.FIELDS", "WIRE.CALLBACK.OPTIONAL_FIELDS",
    "WIRE.CALLBACK.GATES", "WIRE.CALLBACK.DECISIONS", "WIRE.CALLBACK.DELIVERY",
    "WIRE.CALLBACK.RECEIVER_TXN", "WIRE.CALLBACK.RETRY", "WIRE.CALLBACK.WAIT_BOUND",
    "WIRE.CALLBACK.COMPLETION",
    "WIRE.ORDERING.NO_DECIDED_AT", "WIRE.ORDERING.INTERIM", "WIRE.ORDERING.BOOTSTRAP_024",
    "WIRE.ORDERING.INTEGRITY_MISMATCH",
    "WIRE.RETENTION.BY_KIND", "WIRE.RETENTION.WINDOW_DAYS",
)


def build() -> Doc:
    doc = Doc(WIRE)
    doc.title("KYC Tool — Platform Integration Contract")
    doc.p("<b>Audience: TechCraft integration developers.</b> This covers your side of the wire: "
          "what to POST, what callbacks to receive and verify, and what to stand up (the "
          "/kyc/decision endpoint, the token verify page, uploads/ bucket writes). Hosting and "
          "operating the tool is covered separately in the Staging Integration and Production "
          "Readiness Guide.")

    doc.h2("How it works")
    doc.claim_paragraph("WIRE.INGEST.ORDERING")
    doc.p("Each automated decision enqueues one signed callback to your endpoint. Manual "
          "approvals by your reviewers send nothing at all. Both directions use the same HMAC-v2 "
          "scheme with separate key pairs, and delivery is at-least-once, so you dedupe on "
          "(case_id, run_id).")
    doc.why("Send answers to the section 1 questions to: [integration contact - fill in] by "
            "<b>2026-08-18</b>.")

    # ── 1. asks ────────────────────────────────────────────────────────────────────────────────
    doc.h1("1. Answers we need from you")
    doc.p("<b>1.1 Ordered decision delivery.</b> Today there is no wire ordering authority. The "
          "design that fixes it is accepted but unbuilt, and we are not publishing a schema you "
          "could implement against yet:")
    doc.claim_paragraph("WIRE.ORDERING.BOOTSTRAP_024")
    doc.p("What we need from you now is not code. Tell us which system holds your accepted-run "
          "ledger, whether it distinguishes an automatic decision from a manual approval as the "
          "currently effective source for a case, and who signs on your side. We will publish the "
          "schema when the activation unit is built, and you implement then.")
    doc.p("<b>1.2 Dedupe commitment.</b> Confirm you dedupe callbacks on (case_id, run_id) and "
          "drop a late duplicate that arrives after a newer decision.")
    doc.p("<b>1.3 Callback URL and key exchange.</b> Production and staging HTTPS base URLs (we "
          "append /kyc/decision), plus key ids and secrets both directions. Our production boot "
          "refuses HTTP, localhost, and any query or fragment in the base URL, so a wrong value "
          "fails at deploy time rather than at first delivery. <b>Secrets move over an encrypted "
          "channel</b>: a shared vault entry or an age/GPG file to a published key, never email, "
          "chat, or ticket text.")
    doc.p("<b>1.4 Document upload path.</b> document.uploaded events carry an object_ref your "
          "side wrote to the shared object store. Confirm the bucket, the uploads/ key "
          "convention, and the IAM split: you write uploads/, we read them, and our staging "
          "namespace adapter-raw/ is written and swept only by us.")

    # ── 2. events ──────────────────────────────────────────────────────────────────────────────
    doc.h1("2. Events you send us")
    doc.claim_paragraph("WIRE.INGEST.PATH", prefix="<b>Endpoint: </b>")
    doc.p("The first event for a case creates it; there is no registration call. <b>No inbound "
          "rate limit:</b> bursts queue on our side, since we throttle our own registry calls "
          "rather than you. We return no 429 today; if a limit is ever added it will be 429 with "
          "Retry-After, announced in advance.")
    headers = WIRE.value("WIRE.INGEST.HEADERS")
    doc._record("WIRE.INGEST.HEADERS")
    doc.table(
        ("Header", "Value", "Why"),
        (
            (headers[0], "unique per logical event",
             "a replay returns the stored response verbatim; the same key with a different "
             "payload is a 409, so retries are always safe"),
            (headers[1], "unix seconds",
             f"replay window is {WIRE.value('WIRE.SIGN.SKEW_SECONDS')}s either side"),
            (headers[2], "your inbound key id", "supports zero-downtime key rotation"),
            (headers[3], "hex HMAC-SHA256",
             "section 4; binds method, path, idempotency key, and body"),
        ),
        [1.35 * INCH, 1.75 * INCH, 3.6 * INCH],
    )
    doc.space()
    doc.p("Envelope:")
    doc.code('{"event_type": "kyb.run_requested", "occurred_at": "2026-08-04T12:00:00Z",\n'
             ' "actor": {"type": "user", "id": "acct-123"}, "payload": { ... }}')
    doc.h2("Unknown fields are not symmetric")
    extra = WIRE.value("WIRE.INGEST.EXTRA_FIELDS")
    doc._record("WIRE.INGEST.EXTRA_FIELDS")
    doc.p(escape(extra["prose"]) + f" (envelope: <b>{extra['envelope']}</b>; payload: "
          f"<b>{extra['payload']}</b>.) In the other direction, ignore fields we add to "
          "callbacks: we add without notice and never remove or repurpose one without a version "
          "bump agreed with you.")

    doc.h2("Sensitive events: the reviewer-actor rule")
    actor = WIRE.value("WIRE.ACTOR.SENSITIVE")
    doc._record("WIRE.ACTOR.SENSITIVE")
    doc.p("<b>" + escape(", ".join(actor["events"])) + "</b> both require an actor identifying "
          "the reviewer. " + escape(actor["rule"]))
    doc.why("<b>Trap:</b> " + escape(actor["trap"]))

    doc.claim_table(
        "WIRE.EVENT.TABLE",
        ("event_type", "Required payload", "Optional payload", "Notes"),
        [1.55 * INCH, 1.35 * INCH, 1.2 * INCH, 2.6 * INCH],
        rows=[(name, ", ".join(req) or "(none)", ", ".join(opt) or "(none)", note)
              for name, req, opt, note in WIRE.value("WIRE.EVENT.TABLE")],
    )
    doc.space()
    doc.h2("Response codes")
    doc.claim_table(
        "WIRE.INGEST.STATUS", ("Code", "Meaning"), [0.6 * INCH, 6.1 * INCH],
        rows=[(str(code), meaning) for code, meaning in WIRE.value("WIRE.INGEST.STATUS")],
    )

    # ── 3. callbacks ───────────────────────────────────────────────────────────────────────────
    doc.h1("3. Callbacks we send you")
    doc.claim_paragraph("WIRE.CALLBACK.PATH", prefix="<b>Endpoint: </b>")
    doc.p("Content-Type application/json, signed with our outbound key.")
    fields = WIRE.value("WIRE.CALLBACK.FIELDS")
    doc._record("WIRE.CALLBACK.FIELDS")
    doc.p("Required body fields: <b>" + escape(", ".join(fields)) + "</b>.")
    doc.claim_paragraph("WIRE.CALLBACK.DECISIONS", prefix="<b>decision is one of: </b>")
    doc.claim_paragraph("WIRE.CALLBACK.GATES", prefix="<b>gates carries: </b>")
    optional = WIRE["WIRE.CALLBACK.OPTIONAL_FIELDS"]
    doc._record("WIRE.CALLBACK.OPTIONAL_FIELDS")
    doc.p("Optional fields to tolerate and preserve: <b>" + escape(", ".join(optional.value))
          + "</b>. " + escape(optional.note))

    doc.h2("Delivery: what actually reaches you")
    doc.claim_bullets("WIRE.CALLBACK.DELIVERY")

    doc.h2("Your endpoint must commit before it answers")
    doc.claim_steps("WIRE.CALLBACK.RECEIVER_TXN")
    doc.why(escape(WIRE["WIRE.CALLBACK.RECEIVER_TXN"].note))

    retry = WIRE.value("WIRE.CALLBACK.RETRY")
    doc._record("WIRE.CALLBACK.RETRY")
    doc.h2("Timing")
    doc.claim_paragraph("WIRE.CALLBACK.WAIT_BOUND")
    doc.p("Retry schedule: <b>" + ", ".join(f"{d}s" for d in retry["delays"]) + "</b> across "
          f"<b>{retry['attempts']} attempts</b>, no jitter. Backoff alone totals "
          f"{retry['backoff_total_seconds']}s (about {retry['backoff_total_minutes']} minutes); "
          f"counting every attempt's hard wall the worst case is {retry['worst_case_seconds']}s "
          f"(about {retry['worst_case_minutes']} minutes). An outage longer than that exhausts "
          "the schedule and the row dead-letters; tell us and we requeue.")
    doc.claim_paragraph("WIRE.CALLBACK.COMPLETION")

    # ── 4. signing ─────────────────────────────────────────────────────────────────────────────
    doc.h1("4. Request signing (HMAC v2)")
    canonical = WIRE.value("WIRE.SIGN.CANONICAL")
    doc._record("WIRE.SIGN.CANONICAL")
    doc.p("Signature = hex HMAC-SHA256 over these eight lines, LF-joined, in this order:")
    doc.code("\n".join(f"{n}. {line}" for n, line in enumerate(canonical, 1)))
    directions = WIRE["WIRE.SIGN.DIRECTIONS"]
    doc._record("WIRE.SIGN.DIRECTIONS")
    doc.p("The direction tokens are literals: <b>" + escape(directions.value["platform_to_tool"])
          + "</b> for your calls to us and <b>" + escape(directions.value["tool_to_platform"])
          + "</b> for ours to you. " + escape(directions.note) + " slot is the Idempotency-Key on "
          "event POSTs and empty otherwise; path?query is the raw request target.")
    doc.claim_paragraph("WIRE.SIGN.SKEW_SECONDS", prefix="<b>Skew window (seconds): </b>")
    doc.why("v1 signed only timestamp and body, so a captured signature could replay against a "
            "different path. v2 binds method, exact path, and the idempotency slot. Sending any "
            "v2 header disables the v1 fallback for that request.")
    doc.claim_paragraph("WIRE.SIGN.V1_SUNSET", prefix="<b>On the v1 sunset dates: </b>")
    doc.claim_paragraph("WIRE.SIGN.ROTATION", prefix="<b>Key rotation: </b>")

    doc.h2("Test vector: verify against this before writing anything else")
    v = WIRE.value("WIRE.SIGN.VECTOR")
    doc._record("WIRE.SIGN.VECTOR")
    doc.p(f"Secret <b>{escape(v['secret'])}</b>, key id <b>{escape(v['key_id'])}</b>, timestamp "
          f"<b>{v['timestamp']}</b>, Idempotency-Key <b>{escape(v['slot'])}</b>, "
          f"<b>{v['method']} {escape(v['path_qs'])}</b>. The body is one line, "
          f"<b>{v['body_bytes']} bytes</b>, UTF-8, no whitespace and no trailing newline. It "
          "wraps in print below; a newline you add changes the digest.")
    doc.code(v["body"].decode())
    doc.p("Canonical string:")
    doc.code("\n".join(v["canonical_lines"]))
    doc.p("Expected X-KYC-Signature-V2:")
    doc.code(v["signature"])
    doc.p("Reference implementation. This is the exact source our tests execute against the "
          "shipped signer to produce the digest above:")
    doc.code(published_snippet())

    # ── 5. ordering ────────────────────────────────────────────────────────────────────────────
    doc.h1("5. Ordering, and one field you must not sort by")
    doc.claim_paragraph("WIRE.ORDERING.NO_DECIDED_AT")
    doc.claim_paragraph("WIRE.ORDERING.INTERIM", prefix="<b>Until activation: </b>")
    doc.claim_paragraph("WIRE.ORDERING.INTEGRITY_MISMATCH", prefix="<b>Note: </b>")

    # ── 6. retention ───────────────────────────────────────────────────────────────────────────
    doc.h1("6. Retention")
    doc.claim_paragraph("WIRE.RETENTION.WINDOW_DAYS",
                        prefix="<b>Compliance window (days): </b>")
    doc.claim_table("WIRE.RETENTION.BY_KIND", ("Kind", "What happens"),
                    [1.5 * INCH, 5.2 * INCH])

    # ── 7. checklist ───────────────────────────────────────────────────────────────────────────
    doc.h1("7. Go-live checklist")
    doc.table(
        ("#", "Item", "Owner"),
        (
            ("1", "Callback base URLs, staging and production (HTTPS, no query or fragment)",
             "TechCraft"),
            ("2", "Key ids and secrets both directions, both environments, encrypted channel",
             "both"),
            ("3", "Object store bucket and IAM split (uploads/ write for you, read for us)",
             "both"),
            ("4", "POC verify page URL pattern for token links", "TechCraft"),
            ("5", "Expected event volume and burst profile", "TechCraft"),
            ("6", "Dedupe on (case_id, run_id) confirmed in your handler", "TechCraft"),
            ("7", "Receiver commits before returning 2xx (section 3)", "TechCraft"),
            ("8", "Ordering: no sorting by decided_at; conflict path agreed (section 5)",
             "TechCraft"),
            ("9", "Accepted-run ledger and signer identified for future activation (1.1)",
             "TechCraft"),
            ("10", "Escalation contact for dead-letter alerts", "TechCraft"),
            ("11", "Staging end-to-end: test vector passes, event in, callback out, verified",
             "both"),
            ("12", "Sender retries on 502/503/504 and re-signs with a fresh timestamp", "both"),
        ),
        [0.35 * INCH, 4.85 * INCH, 1.5 * INCH],
    )
    return doc


def main() -> str:
    doc = build()
    return doc.build(OUT, "KYC Tool — Platform Integration Contract")


if __name__ == "__main__":
    print(main())
