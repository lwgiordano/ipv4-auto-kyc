"""Conformance kit — check a platform integration against the published wire contract.

    python -m kyc_tool.conformance vector
    python -m kyc_tool.conformance send --tool <base-url> --case <case-id>
    python -m kyc_tool.conformance receive --port <n>

Secrets come from the ENVIRONMENT only — never a command-line argument, never printed, never
logged. `send` reads KYC_CONFORMANCE_V1_SECRET, KYC_CONFORMANCE_INBOUND_SECRET and
KYC_CONFORMANCE_INBOUND_KEY_ID; `receive` reads KYC_CONFORMANCE_OUTBOUND_SECRET,
KYC_CONFORMANCE_OUTBOUND_KEY_ID and (while callbacks still dual-emit v1) KYC_CONFORMANCE_V1_SECRET.

Every event, key, header, and status below is DERIVED from the same executable authority the
published contract registry is verified against — the payload models, the callback model, the
signer, the auth header names — never from a reading of the document. Runtime may not import the
document layer (`tests/unit/test_contract_registry_authority.py`), so the handful of values that
live only in the registry (the worked signature vector, the ingest path, the statuses, the read
keys) are pinned to it from `tests/unit/test_conformance_parity.py` instead: if a published claim
moves and this file does not, that test fails.

The HTTP client is an argument, so the same `send` that runs against a staging URL runs in-process
against an ASGI transport (tests/integration/test_conformance_kit.py).
"""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import NamedTuple, get_args

import httpx
from pydantic import ValidationError

from kyc_tool import security
from kyc_tool.api.auth import (
    IDEMPOTENCY_KEY_HEADER,
    KEY_ID_HEADER,
    TIMESTAMP_HEADER,
    V1_SIGNATURE_HEADER,
    V2_SIGNATURE_HEADER,
)
from kyc_tool.api.schemas import (
    PAYLOAD_MODELS,
    CheckSummary,
    DecisionCallback,
    EnforcementHeld,
    GatesBody,
)

# ── the contract, read off the authorities the published claims are verified against ───────────
SKEW_SECONDS = security.MAX_HMAC_SKEW_SECONDS  # WIRE.SIGN.SKEW_SECONDS
INGEST_PATH = "/v1/cases/{case_id}/events"  # WIRE.INGEST.PATH
# The §3 event table: every accepted event type with its required payload fields, straight off the
# models WIRE.EVENT.TABLE is held against (kyc_tool.api.schemas.PAYLOAD_MODELS).
EVENT_REQUIRED = {
    event_type: tuple(name for name, field in model.model_fields.items() if field.is_required())
    for event_type, model in PAYLOAD_MODELS.items()
}
# The §4 callback body, off the model the publisher encodes through.
CALLBACK_FIELDS = tuple(
    name for name, field in DecisionCallback.model_fields.items() if field.is_required())
CALLBACK_GATES = tuple(GatesBody.model_fields)
CALLBACK_DECISIONS = get_args(DecisionCallback.model_fields["decision"].annotation)
BUY_ENABLEMENT = get_args(DecisionCallback.model_fields["buy_enablement"].annotation)
CHECK_KEYS = tuple(CheckSummary.model_fields)
HELD_KEYS = tuple(EnforcementHeld.model_fields)

# The worked v2 vector §2 prints, published in the registry as SIGNATURE_VECTOR and pinned to it
# field by field by the parity test. Its "secret" is the document's own example value — an
# illustration of the recipe, never a credential; real secrets reach this module only through the
# environment.
PUBLISHED_VECTOR = {
    "secret": "integration-test-secret-0123456789ab",
    "key_id": "techcraft-inbound-1",
    "direction": security.DIRECTION_INBOUND,
    "method": "POST",
    "path_qs": "/v1/cases/case-42/events",
    "timestamp": "1754400000",
    "slot": "evt-0001",
    "body": (b'{"event_type":"kyb.run_requested","occurred_at":"2026-08-04T12:00:00Z",'
             b'"actor":{"type":"user","id":"acct-42"},'
             b'"payload":{"company_legal_name":"ACME NETWORKS LTD"}}'),
    "body_sha256": "9beb5e66987b7818d9a0e24cd141d2f94048b1c8785e8d25b02a17303a5fe024",
    "signature": "0dc9ff66f1a9e096b0dcae311efe15799b88f9f7475f3999cee10aaebcbacdba",
}

# Ingest statuses, each with the meaning the published table gives it (WIRE.INGEST.STATUS).
ACCEPTED = 202  # "accepted and queued — the normal result for a new event"
STORED_REPLAY = 200  # "replay of a seen Idempotency-Key (stored response verbatim), or an inline
#                       reviewer.manual_approve, which runs without a queued job"
BAD_SIGNATURE = 401  # "signature invalid"
TASK_NOT_FOUND = 404  # "review task not found"
INVALID_PAYLOAD = 422  # "malformed envelope, malformed payload, or an invalid reviewer actor"

# Two walk events answer with something other than 202, and both answers are published:
# reviewer.manual_approve is handled inline and "returns 200 rather than 202" (its event-table
# note), and a website.review_completed naming a task no case has is the documented 404.
EXPECTED_STATUS = {"reviewer.manual_approve": STORED_REPLAY, "website.review_completed": TASK_NOT_FOUND}

# §3: these two events are reviewer-sensitive — actor.type must be 'reviewer' and actor.id must
# equal payload.reviewer_id. They are exactly the events whose payload names a reviewer, so the
# walk derives the actor from the payload rather than keeping a second list
# (kyc_tool.events.review_guard.reviewer_actor_reason is the rule's authority).
REVIEWER_FIELD = "reviewer_id"
REVIEWER_ACTOR_TYPE = "reviewer"

# §7: GET /v1/cases/{id} carries status, score, the latest decision, the live checks, and
# information_requested — under these key names (kyc_tool.api.routes_read.get_case).
READ_KEYS = ("status", "score", "latest_decision", "live_checks", "information_requested")

# One minimal VALID value per required payload field, so a walk body is derived from the event's
# own required fields instead of being a second, hand-written event table.
FIELD_VALUES = {
    "company_legal_name": "Conformance Kit Ltd",
    "contact": {"name": "Robin Vale", "email": "robin.vale@conformance.test"},
    "platform_account_id": "acct-conformance-1",
    "email": "robin.vale@conformance.test",
    "domain": "conformance.test",
    "verified_at": "2026-01-01T00:00:00Z",
    "rir": "arin",
    "org_handle": "ORG-CONFORMANCE-1",
    "poc_handle": "POC-CONFORMANCE-1",
    "token_id": "conformance-token-1",
    "token": "conformance-token-value",
    "object_ref": "uploads/conformance-kit.pdf",
    "doc_type": "certificate_of_incorporation",
    "task_id": "conformance-kit-no-such-task",
    "result": "pass",
    "reviewer_id": "reviewer-conformance-1",
}
ACTOR = {"type": "system", "id": "conformance-kit"}


class Row(NamedTuple):
    check: str
    expected: str
    got: str
    ok: bool


def _row(check: str, expected, got, ok: bool | None = None) -> Row:
    return Row(check, str(expected), str(got), (expected == got) if ok is None else ok)


def print_rows(rows: list[Row]) -> int:
    """Print the check/expected/got/verdict table. Returns the process exit code."""
    width = max(len(row.check) for row in rows)
    for row in rows:
        print(f"{row.check:<{width}}  expected {row.expected:<40}  got {row.got:<40}  "
              f"{'PASS' if row.ok else 'FAIL'}")
    failed = [row.check for row in rows if not row.ok]
    print(f"{len(rows) - len(failed)}/{len(rows)} PASS"
          + (f" — FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


# ── mode 1: the published signature vector, offline ────────────────────────────────────────────
def vector_rows() -> list[Row]:
    """Recompute the published v2 vector with the shipped signer. No network, and nothing read
    from the environment: the vector's inputs are the document's own worked example."""
    vector = PUBLISHED_VECTOR
    fields = {name: vector[name]
              for name in ("key_id", "direction", "method", "path_qs", "timestamp", "slot", "body")}
    return [
        _row("vector:body_sha256", vector["body_sha256"], hashlib.sha256(vector["body"]).hexdigest()),
        _row("vector:signature", vector["signature"], security.sign_v2(vector["secret"], **fields)),
    ]


# ── signing, exactly as §2 documents it ────────────────────────────────────────────────────────
def _request(client: httpx.Client, method: str, path: str, body: bytes, slot: str) -> httpx.Request:
    headers = {"Content-Type": "application/json"} if body else {}
    if slot:
        headers[IDEMPOTENCY_KEY_HEADER] = slot
    return client.build_request(method, path, content=body, headers=headers)


def v2_request(client, method: str, path: str, body: bytes, *, secret: str, key_id: str,
               slot: str = "", timestamp: str | None = None) -> httpx.Request:
    """A v2-signed request. The canonical binds the LITERAL target the client will put on the wire
    (the publisher's own pattern), the method, the timestamp, the Idempotency-Key slot — empty on
    reads — and the body hash."""
    request = _request(client, method, path, body, slot)
    stamp = timestamp or str(time.time())
    request.headers[TIMESTAMP_HEADER] = stamp
    request.headers[KEY_ID_HEADER] = key_id
    request.headers[V2_SIGNATURE_HEADER] = security.sign_v2(
        secret,
        key_id=key_id,
        direction=security.DIRECTION_INBOUND,
        method=method,
        path_qs=request.url.raw_path.decode("ascii"),
        timestamp=stamp,
        slot=slot,
        body=request.content,
    )
    return request


def v1_request(client, method: str, path: str, body: bytes, *, secret: str,
               slot: str = "") -> httpx.Request:
    """A v1-signed request: HMAC over "{timestamp}.{raw body}", no path binding."""
    request = _request(client, method, path, body, slot)
    stamp = str(time.time())
    request.headers[TIMESTAMP_HEADER] = stamp
    request.headers[V1_SIGNATURE_HEADER] = security.sign(secret, stamp, request.content)
    return request


def _envelope(event_type: str, payload: dict, actor: dict = ACTOR) -> bytes:
    """The §3 envelope — exactly these four keys."""
    return json.dumps({
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": actor,
        "payload": payload,
    }).encode()


def walk() -> list[tuple[str, int, bytes]]:
    """(event_type, expected status, body) for every accepted event type, in contract order, each
    carrying exactly that event's required payload fields."""
    events = []
    for event_type, required in EVENT_REQUIRED.items():
        payload = {name: FIELD_VALUES[name] for name in required}
        actor = ACTOR
        if REVIEWER_FIELD in payload:  # reviewer-sensitive: the actor IS the reviewer
            actor = {"type": REVIEWER_ACTOR_TYPE, "id": payload[REVIEWER_FIELD]}
        events.append((event_type, EXPECTED_STATUS.get(event_type, ACCEPTED),
                       _envelope(event_type, payload, actor)))
    return events


# ── mode 2: the platform side, against a running tool ──────────────────────────────────────────
def send(client, case_id: str, *, inbound_secret: str, inbound_key_id: str,
         v1_secret: str = "") -> list[Row]:
    """Sign and post the documented walk, then every negative the docs promise, then a signed
    read. `client` is any httpx.Client — a real one for staging, a TestClient/ASGI transport
    in-process."""
    path = INGEST_PATH.replace("{case_id}", case_id)
    rows = []

    def post_v2(body: bytes, *, slot: str | None = None, timestamp: str | None = None) -> httpx.Response:
        return client.send(v2_request(client, "POST", path, body, secret=inbound_secret,
                                      key_id=inbound_key_id,
                                      slot=uuid.uuid4().hex if slot is None else slot,
                                      timestamp=timestamp))

    for name, expected, body in walk():
        rows.append(_row(f"walk:{name}", expected, post_v2(body).status_code))

    simple = _envelope("recalculate.requested", {})

    tampered = v2_request(client, "POST", path, simple, secret=inbound_secret,
                          key_id=inbound_key_id, slot=uuid.uuid4().hex)
    tampered.headers[V2_SIGNATURE_HEADER] = "0" * 64
    rows.append(_row("negative:wrong_signature", BAD_SIGNATURE, client.send(tampered).status_code))

    stale = str(time.time() - SKEW_SECONDS - 60)  # outside the documented replay window
    rows.append(_row("negative:stale_timestamp", BAD_SIGNATURE,
                     post_v2(simple, timestamp=stale).status_code))

    signup = next(event for event, required in EVENT_REQUIRED.items() if "contact" in required)
    incomplete = {name: FIELD_VALUES[name] for name in EVENT_REQUIRED[signup] if name != "contact"}
    rows.append(_row("negative:signup_missing_contact", INVALID_PAYLOAD,
                     post_v2(_envelope(signup, incomplete)).status_code))

    rows.append(_row("negative:unknown_event_type", INVALID_PAYLOAD,
                     post_v2(_envelope("kyb.not_a_documented_event", {})).status_code))

    if v1_secret:  # dual-accept only: after the inbound v1 sunset a valid v1 request is 401 by design
        rows.append(_row("v1:accepted", ACCEPTED, client.send(
            v1_request(client, "POST", path, simple, secret=v1_secret, slot=uuid.uuid4().hex)
        ).status_code))

    # the SAME key with the SAME bytes returns the stored response verbatim
    replay_key, replayed = uuid.uuid4().hex, _envelope("recalculate.requested", {})
    first = post_v2(replayed, slot=replay_key)
    again = post_v2(replayed, slot=replay_key)
    rows.append(_row("replay:first", ACCEPTED, first.status_code))
    rows.append(_row("replay:status", STORED_REPLAY, again.status_code))
    rows.append(_row("replay:body", first.text, again.text))

    read = client.send(v2_request(client, "GET", f"/v1/cases/{case_id}", b"",
                                  secret=inbound_secret, key_id=inbound_key_id))
    rows.append(_row("read:status", 200, read.status_code))
    served = read.json() if read.status_code == 200 else {}
    rows.append(_row("read:keys", list(READ_KEYS), [key for key in READ_KEYS if key in served]))
    return rows


# ── mode 3: the tool side — a correct decision receiver (§4) ───────────────────────────────────
def verify_callback(path: str, headers, body: bytes, *, outbound_secret: str, outbound_key_id: str,
                    v1_secret: str, seen: set) -> list[Row]:
    """Check one decision callback the way a correct receiver must. `path` is the LITERAL request
    target the v2 signature binds (prefix included), `headers` any case-insensitive mapping (both
    http.server's and httpx's are), `seen` the receiver's (case_id, run_id) ledger."""
    stamp = headers.get(TIMESTAMP_HEADER, "") or ""
    presented = headers.get(KEY_ID_HEADER, "") or ""
    rows = [
        _row("signature.key_id", outbound_key_id, presented),
        _row("signature.v2", True, security.verify_v2(
            outbound_secret, headers.get(V2_SIGNATURE_HEADER, "") or "",
            key_id=presented, direction=security.DIRECTION_OUTBOUND, method="POST",
            path_qs=path, timestamp=stamp, slot="", body=body)),
    ]

    v1 = headers.get(V1_SIGNATURE_HEADER)
    if v1 is None:
        state, ok = "absent", True  # dual-emission ends at the outbound sunset
    elif not v1_secret:
        state, ok = "unchecked (KYC_CONFORMANCE_V1_SECRET unset)", True
    else:
        ok = security.verify(v1_secret, stamp, body, v1)
        state = "valid" if ok else "invalid"
    rows.append(_row("signature.v1", "valid, or absent after the outbound sunset", state, ok=ok))

    try:
        payload = json.loads(body or b"{}")
    except (ValueError, RecursionError):  # not JSON, or nested past the decoder's depth
        payload = None
    if not isinstance(payload, dict):
        payload = {}

    # The complete schema first, through the model the publisher encodes with, before any row
    # looks inside the body. The named rows below cannot see a wrong TYPE — a string score, a
    # non-boolean gate, a malformed check, an unknown field — and a receiver that accepted one was
    # being certified. Strict JSON mode, over the raw bytes: lax mode would coerce "10", 10.0 or
    # "true" into the model, shapes the tool never emits. The named rows stay: they say WHICH
    # rule broke, this one says the body as a whole is not the contract. A schema failure holds
    # the callback out of the ledger like any other failed row.
    try:
        DecisionCallback.model_validate_json(body or b"", strict=True)
        schema, schema_ok = "valid", True
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(part) for part in errors[0]["loc"]) or "<root>"
        schema = f"{len(errors)} error(s), first at {where}: {errors[0]['msg']}"
        schema_ok = False
    rows.append(_row("body.schema", "validates as DecisionCallback", schema, ok=schema_ok))

    rows.append(_row("body.required_keys", list(CALLBACK_FIELDS),
                     [key for key in CALLBACK_FIELDS if key in payload]))
    gates = payload.get("gates")
    rows.append(_row("body.gates", sorted(CALLBACK_GATES),
                     sorted(gates) if isinstance(gates, dict) else gates))
    decision = payload.get("decision")
    rows.append(_row("body.decision", f"one of {CALLBACK_DECISIONS}", decision,
                     ok=decision in CALLBACK_DECISIONS))
    buy = payload.get("buy_enablement")
    rows.append(_row("body.buy_enablement", f"one of {BUY_ENABLEMENT}", buy, ok=buy in BUY_ENABLEMENT))
    checks = payload.get("checks")
    check_list = checks if isinstance(checks, list) else []  # a scalar here is a failed row, not a crash
    missing = sorted({key for check in check_list if isinstance(check, dict)
                      for key in CHECK_KEYS if key not in check})
    checks_ok = isinstance(checks, list) and not missing and all(
        isinstance(check, dict) for check in check_list)
    rows.append(_row("body.checks", f"each carries {', '.join(CHECK_KEYS)}",
                     "ok" if checks_ok
                     else f"missing {missing}" if missing
                     else f"not a list of objects: {type(checks).__name__}", ok=checks_ok))
    held = payload.get("enforcement_held")
    rows.append(_row("body.enforcement_held", f"absent, or {', '.join(HELD_KEYS)}",
                     "absent" if held is None else held,
                     ok=held is None or (isinstance(held, dict) and set(held) == set(HELD_KEYS))))

    identity = (payload.get("case_id"), payload.get("run_id"))
    # §4 WIRE.CALLBACK.VALIDATION_ORDER / ACK_VS_APPLY: consult the replay ledger only AFTER the
    # callback validates, and record only a VALID one — an invalid callback is HELD, never
    # recorded, or a forged body poisons the ledger against the genuine delivery.
    valid = all(row.ok for row in rows)
    duplicate = valid and identity in seen
    if valid:
        seen.add(identity)
    rows.append(_row("dedupe", "acknowledge; apply once per (case_id, run_id)",
                     "duplicate — acknowledged, not applied" if duplicate
                     else "first delivery" if valid
                     else "not recorded — an invalid callback is held, never recorded",
                     ok=True))
    return rows


def make_server(port: int, *, outbound_secret: str, outbound_key_id: str,
                v1_secret: str) -> HTTPServer:
    """The diagnostic receiver, bound to loopback and not yet serving (tests drive it directly)."""
    seen: set = set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's dispatch name
            # A malformed Content-Length is a malformed request, not a reason to drop the
            # connection: a non-numeric or negative value reads an empty body, which then fails
            # the body rows like any other invalid callback.
            try:
                length = max(0, int(self.headers.get("Content-Length") or 0))
            except ValueError:
                length = 0
            body = self.rfile.read(length)
            rows = verify_callback(self.path, self.headers, body, outbound_secret=outbound_secret,
                                   outbound_key_id=outbound_key_id, v1_secret=v1_secret, seen=seen)
            dedupe = next(row.got for row in rows if row.check == "dedupe")
            print(f"callback ← {self.path} [{dedupe}] "
                  + " ".join(f"{r.check}={'PASS' if r.ok else 'FAIL'}" for r in rows), flush=True)
            for row in (r for r in rows if not r.ok):
                print(f"    {row.check}: expected {row.expected} — got {row.got}", flush=True)
            # §4: respond 2xx to acknowledge. This receiver acknowledges even a FAILing callback so
            # a broken rule is reported once instead of retried eight times; a production receiver
            # HOLDS an invalid callback instead (WIRE.CALLBACK.VALIDATION).
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):  # quiet access log; the PASS/FAIL line is the output
            pass

    return HTTPServer(("127.0.0.1", port), Handler)


def serve(port: int, *, outbound_secret: str, outbound_key_id: str, v1_secret: str) -> None:
    server = make_server(port, outbound_secret=outbound_secret, outbound_key_id=outbound_key_id,
                         v1_secret=v1_secret)
    print(f"conformance receiver on http://127.0.0.1:{server.server_port}/kyc/decision", flush=True)
    server.serve_forever()


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────
def _env(name: str) -> str:
    """One secret or key id from the environment. Only the NAME is ever printed."""
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is not set — the kit reads secrets from the environment only")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kyc_tool.conformance",
                                     description="Conformance kit for the KYC wire contract.",
                                     epilog="Secrets come from the environment only: send reads "
                                            "KYC_CONFORMANCE_V1_SECRET, KYC_CONFORMANCE_INBOUND_SECRET, "
                                            "KYC_CONFORMANCE_INBOUND_KEY_ID; receive reads "
                                            "KYC_CONFORMANCE_OUTBOUND_SECRET, "
                                            "KYC_CONFORMANCE_OUTBOUND_KEY_ID and optionally "
                                            "KYC_CONFORMANCE_V1_SECRET.")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("vector", help="reproduce the published v2 signature vector (no network)")
    sender = modes.add_parser("send", help="sign and post the documented walk at a running tool")
    sender.add_argument("--tool", required=True, help="the tool's base URL")
    sender.add_argument("--case", required=True, help="case id to walk")
    sender.add_argument("--no-v1", action="store_true",
                        help="skip the v1 row: the tool's inbound v1 sunset has passed")
    receiver = modes.add_parser("receive", help="run a correct decision receiver and check callbacks")
    receiver.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)

    if args.mode == "vector":
        return print_rows(vector_rows())
    if args.mode == "receive":
        serve(args.port,
              outbound_secret=_env("KYC_CONFORMANCE_OUTBOUND_SECRET"),
              outbound_key_id=_env("KYC_CONFORMANCE_OUTBOUND_KEY_ID"),
              v1_secret=os.environ.get("KYC_CONFORMANCE_V1_SECRET", ""))
        return 0
    with httpx.Client(base_url=args.tool) as client:
        return print_rows(send(client, args.case,
                               inbound_secret=_env("KYC_CONFORMANCE_INBOUND_SECRET"),
                               inbound_key_id=_env("KYC_CONFORMANCE_INBOUND_KEY_ID"),
                               v1_secret="" if args.no_v1 else _env("KYC_CONFORMANCE_V1_SECRET")))


if __name__ == "__main__":
    sys.exit(main())
