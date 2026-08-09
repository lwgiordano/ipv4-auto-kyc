"""Generate the TechCraft platform integration contract PDF."""

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT = "techcraft-integration-contract.pdf"

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1x", parent=styles["Heading1"], fontSize=15, spaceBefore=16, spaceAfter=6,
                    textColor=colors.HexColor("#1a1a2e"))
H2 = ParagraphStyle("H2x", parent=styles["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4,
                    textColor=colors.HexColor("#1a1a2e"))
BODY = ParagraphStyle("Bodyx", parent=styles["Normal"], fontSize=9.5, leading=13, spaceAfter=5)
WHY = ParagraphStyle("Why", parent=BODY, leftIndent=10, textColor=colors.HexColor("#444444"),
                     fontSize=9, leading=12)
CODE = ParagraphStyle("Code", parent=styles["Code"], fontSize=8, leading=10.5, leftIndent=8,
                      spaceAfter=5, backColor=colors.HexColor("#f4f4f4"))
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.5, leading=11, spaceAfter=0)
CELLB = ParagraphStyle("CellB", parent=CELL, fontName="Helvetica-Bold")

TBL = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8f0")),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
])


def tbl(rows, widths):
    data = [[Paragraph(c, CELLB) for c in rows[0]]] + [
        [Paragraph(c, CELL) for c in r] for r in rows[1:]
    ]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TBL)
    return t


def p(text, style=BODY):
    return Paragraph(text, style)


story = []

story.append(Paragraph("KYC Tool — Platform Integration Contract", styles["Title"]))
story.append(p("Prepared 2026-08-04, from the shipped code. <b>Audience: TechCraft integration "
               "developers.</b> This covers YOUR side of the wire: what to POST, what callbacks "
               "to receive and verify, what to stand up (the /kyc/decision endpoint, the token "
               "verify page, uploads/ bucket writes). Section 5 describes our runtime only where "
               "it affects you; hosting and operating the tool is covered separately in the "
               "Deployment Guide."))
story.append(Paragraph("How it works", H2))
story.append(p("You POST signed case events to us; events for one case are processed strictly "
               "serially, in your send order. Each automated run ends in exactly one signed "
               "decision callback to your /kyc/decision endpoint; manual approvals by your "
               "reviewers produce no callback. Both directions use the same HMAC-v2 scheme with "
               "separate key pairs, and callback delivery is at-least-once, so you dedupe on "
               "(case_id, run_id)."))
story.append(Paragraph("Send answers to: [integration contact - fill in] by <b>2026-08-18</b>. "
                       "Both the v1 signature sunset and our ordered-delivery milestone sit "
                       "behind these answers.", WHY))
story.append(Spacer(1, 6))

# ---------------------------------------------------------------- section 1
story.append(Paragraph("1. Answers we need from you", H1))
story.append(p("<b>1.1 Bootstrap envelope for ordered decisions (blocks ordered delivery).</b> "
               "Before we put a per-case decision sequence on the wire, we seed our per-case "
               "high-water marks from your accepted-run ledger, because your ledger is the "
               "authority on what you actually applied — seeding from our own outbox would "
               "replay anything you reverted manually. Draft schema below; <b>confirm or "
               "amend</b> rather than designing from scratch."))
story.append(p("We send (signed with our outbound key):"))
story.append(Paragraph(
    '{"schema_version": 1, "kind": "ordering_bootstrap_manifest",<br/>'
    '&nbsp;"generated_at": "2026-09-01T00:00:00Z",<br/>'
    '&nbsp;"cases": [{"case_id": "...", "latest_run_id": "...", "decided_at": "..."}],<br/>'
    '&nbsp;"manifest_sha256": "&lt;hex of the canonical cases array&gt;"}', CODE))
story.append(p("You return (signed with a key you nominate):"))
story.append(Paragraph(
    '{"schema_version": 1, "kind": "ordering_bootstrap_response",<br/>'
    '&nbsp;"manifest_sha256": "&lt;echo&gt;", "ledger_source": "&lt;system of record id&gt;",<br/>'
    '&nbsp;"generated_at": "...",<br/>'
    '&nbsp;"cases": [{"case_id": "...", "high_water_run_id": "...",<br/>'
    '&nbsp;&nbsp;"state": "applied | reverted | manual_override"}]}', CODE))
story.append(p("Open for you to amend: the signing key/algorithm, the ledger_source identifier, "
               "and whether reverted cases carry the reverted-to run.", WHY))
story.append(p("<b>1.2 Dedupe commitment.</b> Confirm you dedupe callbacks on (case_id, run_id) "
               "and drop a late duplicate that arrives after a newer decision. Why: delivery is "
               "at-least-once; a retried callback is indistinguishable on the wire from a fresh "
               "one, and that pair is unique per decision and stable across retries."))
story.append(p("<b>1.3 Callback URL and key exchange.</b> Production and staging HTTPS base "
               "URLs (we append /kyc/decision), plus key ids and secrets both directions "
               "(section 4). Why: our production boot refuses HTTP, localhost, and any query or "
               "fragment in the base URL, so a wrong value fails at deploy time, not at first "
               "delivery. <b>Secrets move over an encrypted channel</b> — a shared vault entry "
               "or an age/GPG file to a published key. Never email, chat, or ticket text."))
story.append(p("<b>1.4 Document upload path.</b> document.uploaded events carry an object_ref "
               "your side wrote to the shared object store. Confirm the bucket, the uploads/ "
               "key convention, and the IAM split: you write uploads/…, we read them; our "
               "staging namespace adapter-raw/… is written and swept only by us. Separate write "
               "scopes keep either side from clobbering the other's evidence."))

# ---------------------------------------------------------------- section 2
story.append(Paragraph("2. Events you send us", H1))
story.append(p("<b>POST https://&lt;kyc-tool&gt;/v1/cases/{case_id}/events</b> — the only way "
               "work enters the tool. The first event for a case creates it; there is no "
               "registration call. <b>No inbound rate limit:</b> bursts queue on our side (we "
               "throttle our own registry calls, not you). We return no 429 today; if a limit "
               "is ever added it will be 429 + Retry-After, announced in advance."))
story.append(tbl(
    [["Header", "Value", "Why"],
     ["Idempotency-Key", "unique per logical event",
      "a replay returns the stored response verbatim (200); same key with a different payload "
      "is a 409, so retries are always safe"],
     ["X-KYC-Timestamp", "unix seconds", "replay window is 300s either side"],
     ["X-KYC-Key-Id", "your inbound key id", "supports zero-downtime key rotation"],
     ["X-KYC-Signature-V2", "hex HMAC-SHA256", "section 4; binds method, path, idempotency "
      "key, and body"]],
    [1.35 * inch, 1.75 * inch, 3.6 * inch]))
story.append(Spacer(1, 4))
story.append(p("Envelope:"))
story.append(Paragraph(
    '{"event_type": "kyb.run_requested", "occurred_at": "2026-08-04T12:00:00Z",<br/>'
    '&nbsp;"actor": {"type": "user", "id": "acct-123"}, "payload": { ... }}', CODE))
story.append(p("<b>Compatibility policy, both directions:</b> unknown fields are must-ignore. "
               "We preserve extra fields you put in payloads; you must ignore fields we add to "
               "callbacks. We add fields without notice; we never remove or repurpose one "
               "without a version bump agreed with you."))
story.append(tbl(
    [["event_type", "Required payload", "Notes"],
     ["kyb.run_requested", "company_legal_name",
      "optional: address, registration_number, jurisdiction, website, contact, "
      "platform_account_id. Incomplete submissions ingest fine; missing evidence simply never "
      "passes a check."],
     ["email.verified", "email, domain, verified_at", "you verify the mailbox, we score it"],
     ["org_id.submitted", "rir, org_handle", "rir is one of arin, ripe, apnic, lacnic, afrinic"],
     ["poc.submitted", "rir, poc_handle",
      "optional org_handle, resource. Triggers our verification email to the RIR-listed "
      "address."],
     ["poc.token_verified", "token_id, token, verified_at",
      "you host the verify page and echo the raw token from the email link; we store only its "
      "digest. Tokens expire after 72 hours."],
     ["document.uploaded", "object_ref, doc_type", "ref to the object you wrote (1.4)"],
     ["website.review_completed", "task_id, result, reviewer_id",
      "result is pass or fail; the signed actor must be that reviewer, and the task must be an "
      "open website task on the same case"],
     ["reviewer.manual_approve", "reviewer_id", "handled inline: no run, no callback"],
     ["recalculate.requested", "(none)", "re-scores from stored evidence, no new fetches"]],
    [1.68 * inch, 1.62 * inch, 3.4 * inch]))
story.append(Spacer(1, 4))
story.append(p("Responses: 200 accepted or replayed; 400 missing Idempotency-Key; 401 bad "
               "signature; 409 payload mismatch on a reused key, or a task-state conflict; 422 "
               "malformed envelope or payload; 404 unknown review task."))

# ---------------------------------------------------------------- section 3
story.append(Paragraph("3. Callbacks we send you", H1))
story.append(p("<b>POST &lt;your-base&gt;/kyc/decision</b>, Content-Type application/json, "
               "signed with our outbound key. One callback per automated decision."))
story.append(Paragraph(
    '{"case_id": "...", "run_id": "...", "event_id": "...",<br/>'
    '&nbsp;"decision": "approve | approve_buy_locked | manual_review_insufficient | reject",<br/>'
    '&nbsp;"score": 120,<br/>'
    '&nbsp;"gates": {"score_met": true, "legal_proof": true, "control_proof": true, '
    '"broker_ok": true, "no_hard_conflict": true},<br/>'
    '&nbsp;"buy_enablement": "enabled | locked_org_id_required",<br/>'
    '&nbsp;"checks": [{"type": "...", "status": "...", "points": 10, "source": "...", '
    '"reason_codes": []}],<br/>'
    '&nbsp;"decided_at": "2026-08-04T12:00:05Z"}', CODE))
story.append(p("Optional fields to tolerate and preserve (per the section 2 compatibility "
               "policy): <b>enforcement_held</b> (present while positive-decision enforcement "
               "is held for manual review; the outer decision stays authoritative) and "
               "<b>event_sequence</b> (per-case ordinal of the triggering event, behind a "
               "rollout flag). After the 1.1 bootstrap, decision_sequence joins the wire and "
               "your high-water mark becomes the ordering authority."))
story.append(Paragraph("What your endpoint must do", H2))
story.append(tbl(
    [["Requirement", "Value", "Why"],
     ["Ack", "any 2xx", "anything else is retried"],
     ["Respond within", "10 seconds",
      "each attempt has a 40s hard wall (4 × the 10s HTTP phase timeout); past it we cancel and "
      "count a retryable failure, but the cancelled request may still land — dedupe covers it"],
     ["Retry schedule", "10s base, doubling, no jitter: 10, 20, 40, 80, 160, 320, 640s",
      "8 attempts total; dead-letter lands about 21 minutes after the first failure, or about "
      "27 minutes counting the per-attempt walls. An outage longer than that exhausts the "
      "schedule; tell us and we requeue"],
     ["Duplicates", "possible", "at-least-once delivery; dedupe on (case_id, run_id)"],
     ["Ordering", "not guaranteed until the 1.1 bootstrap lands",
      "until then, apply the newest decided_at per case or hold for decision_sequence"]],
    [1.3 * inch, 2.35 * inch, 3.05 * inch]))

# ---------------------------------------------------------------- section 4
story.append(Paragraph("4. Request signing (HMAC v2), with a worked example", H1))
story.append(p("Signature = hex HMAC-SHA256 of a newline-joined string. The direction tokens "
               "are literal: <b>platform-&gt;tool</b> for your calls to us, "
               "<b>tool-&gt;platform</b> for ours to you. slot is the Idempotency-Key on event "
               "POSTs, empty otherwise. path?query is the raw request target. Skew window: 300 "
               "seconds."))
story.append(p("Why this shape: v1 signed only timestamp and body, so a captured signature "
               "could replay against a different path. v2 binds method, exact path, and the "
               "idempotency slot. Sending any v2 header disables the v1 fallback for that "
               "request.", WHY))
story.append(p("<b>On the v1 sunset dates:</b> they are not fixed yet. Both are set with you at "
               "cutover and currently sit unset in our config. The inbound one additionally "
               "cannot take effect until an observation window records zero v1 traffic, so a "
               "date alone never cuts off a live v1 sender. None of this matters if you ship v2 "
               "from day one, which is the recommendation."))
story.append(Paragraph("Test vector (verify your implementation against this first)", H2))
story.append(p("Secret <b>integration-test-secret-0123456789ab</b>, key id "
               "<b>techcraft-inbound-1</b>, timestamp <b>1754400000</b>, Idempotency-Key "
               "<b>evt-0001</b>, POST <b>/v1/cases/case-42/events</b>. The body is <b>one line, "
               "163 bytes, UTF-8, no whitespace and no trailing newline</b> (it wraps in print "
               "below; a newline you add will change the digest):"))
story.append(Paragraph(
    '{"event_type":"kyb.run_requested","occurred_at":"2026-08-04T12:00:00Z",'
    '"actor":{"type":"user","id":"acct-42"},"payload":{"company_legal_name":'
    '"ACME NETWORKS LTD"}}', CODE))
story.append(p("Canonical string (8 lines, LF-joined):"))
story.append(Paragraph(
    "v2<br/>techcraft-inbound-1<br/>platform-&gt;tool<br/>POST<br/>"
    "/v1/cases/case-42/events<br/>1754400000<br/>evt-0001<br/>"
    "9beb5e66987b7818d9a0e24cd141d2f94048b1c8785e8d25b02a17303a5fe024", CODE))
story.append(p("Expected X-KYC-Signature-V2:"))
story.append(Paragraph(
    "0dc9ff66f1a9e096b0dcae311efe15799b88f9f7475f3999cee10aaebcbacdba", CODE))
story.append(p("Reference implementation (Python; the last line of the canonical string is "
               "sha256(body) hex):"))
story.append(Paragraph(
    "import hashlib, hmac<br/>"
    "def sign(secret, key_id, direction, method, path_qs, ts, slot, body: bytes):<br/>"
    '&nbsp;&nbsp;&nbsp;&nbsp;msg = "\\n".join(["v2", key_id, direction, method, path_qs, ts, '
    "slot,<br/>"
    "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
    "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;hashlib.sha256(body).hexdigest()])"
    ".encode()<br/>"
    "&nbsp;&nbsp;&nbsp;&nbsp;return hmac.new(secret.encode(), msg, hashlib.sha256)"
    ".hexdigest()<br/>"
    '# sign("integration-test-secret-0123456789ab", "techcraft-inbound-1",<br/>'
    '#&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"platform->tool", "POST", "/v1/cases/case-42/events",<br/>'
    '#&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"1754400000", "evt-0001", body) == the digest above', CODE))
story.append(p("Key exchange: your inbound key_id + secret (you sign, we verify) and our "
               "outbound key_id + secret (we sign, you verify). Secrets are 32+ characters; "
               "rotation = add a second key id, cut over, retire the old one. Exchange "
               "mechanism per section 1.3: encrypted channel only."))

# ---------------------------------------------------------------- section 5
story.append(Paragraph("5. Our runtime, where it affects you", H1))
story.append(tbl(
    [["Item", "Value", "Why you care"],
     ["Environments", "staging + production, separate URLs, keys, and buckets",
      "staging rehearses every cutover before production; run checklist item 9 there with your "
      "staging keys"],
     ["Health probes", "GET /healthz (liveness), GET /readyz (readiness)",
      "/readyz fails on policy or schema drift, meaning we are refusing traffic on purpose — "
      "do not route around it"],
     ["Transport", "HTTPS only; callbacks originate from our egress IPs (list at key exchange)",
      "pin them in your ingress allowlist if you filter"],
     ["Decision latency", "seconds for registry checks; longer with documents or manual review",
      "the callback is the completion signal; nothing needs polling"],
     ["Verification emails", "we email the RIR-listed address; links point at your verify "
      "page; tokens live 72h",
      "you host the page and post poc.token_verified; you never touch our email infrastructure"],
     ["Data retention", "7 years (2555 days) default; delivered callback bodies and "
      "verification emails are redacted in place after the window; audit skeletons remain",
      "matches KYC record-keeping without holding raw PII forever"],
     ["Maintenance, routine", "config cutovers run drained: publishers stop briefly, change, "
      "restart",
      "callbacks pause for minutes and then resume; event ingestion continues throughout, so "
      "you do nothing"],
     ["Maintenance, full window", "a few releases require stopping the API and workers "
      "together; we schedule these with you in advance",
      "POST /v1/cases/{id}/events is unavailable for the duration, for every event type. See "
      "the requirement below — this one needs code on your side"],
     ["Failure escalation", "dead-lettered callbacks and integrity mismatches page our "
      "operators; requeue is an authenticated admin action",
      "checklist item 8 tells us who to contact on your side"]],
    [1.35 * inch, 2.55 * inch, 2.8 * inch]))
story.append(Spacer(1, 4))
story.append(Paragraph("Full maintenance windows: what your sender must do", H2))
story.append(p("During a full window every API target is down, so your load balancer returns "
               "502, 503, or 504 rather than a connection failure. Two requirements follow, and "
               "both need building before the first window:"))
story.append(p("<b>Treat 502/503/504 exactly like a transport failure:</b> retryable, same body, "
               "same Idempotency-Key. A sender that retries only on connection errors will drop "
               "events during the window."))
story.append(p("<b>Re-sign every retry with a fresh timestamp and signature</b>, keeping the "
               "original Idempotency-Key. The HMAC skew window is 300 seconds, so a buffered "
               "event replayed with its original signature after a longer window fails "
               "authentication. The idempotency key is what makes the retry safe; the signature "
               "is what makes it acceptable, and they rotate independently."))
story.append(p("Buffering events for the window and draining afterward is the cleanest "
               "approach. Either way, no event should be lost, because the tool has no way to "
               "ask for one it never received.", WHY))

# ---------------------------------------------------------------- section 6
story.append(Paragraph("6. Go-live checklist", H1))
story.append(tbl(
    [["#", "Item", "Owner"],
     ["1", "Callback base URLs, staging + production (HTTPS, no query/fragment)", "TechCraft"],
     ["2", "Key ids + secrets both directions, both environments, via encrypted channel", "both"],
     ["3", "Object store bucket + IAM split (uploads/ write for you, read for us)", "both"],
     ["4", "POC verify page URL pattern for token links", "TechCraft"],
     ["5", "Expected event volume and burst profile", "TechCraft"],
     ["6", "Dedupe on (case_id, run_id) confirmed in your handler", "TechCraft"],
     ["7", "Bootstrap envelope: confirm or amend the 1.1 draft, name the signer", "TechCraft"],
     ["8", "Escalation contact for integrity_mismatch and dead-letter alerts", "TechCraft"],
     ["9", "Staging end-to-end: signature vector passes, event in, callback out, verified",
      "both"],
     ["10", "Egress IP allowlist applied (if you filter ingress)", "TechCraft"],
     ["11", "Sender retries on 502/503/504 and re-signs with a fresh timestamp (section 5)",
      "TechCraft"]],
    [0.35 * inch, 4.85 * inch, 1.5 * inch]))

doc = SimpleDocTemplate(OUT, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                        title="KYC Tool — Platform Integration Contract")
doc.build(story)
print(OUT)
