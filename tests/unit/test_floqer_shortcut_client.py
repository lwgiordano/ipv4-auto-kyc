"""The live Floqer client over the Shortcut API: bootstrap, run, poll, map (T5 / AUDIT:C4).

Everything runs against httpx.MockTransport with an injected clock and sleep, so the whole poll
schedule executes instantly and NO wire call and NO credential ever leaves the process. The rules
under test are the documented ones: only published shortcuts run; input keys come from the live
`input_schema`; a per-field status other than `completed` is "no data", not a value; terminal
`failed`/`error`/`outOfCredits` are distinct outcomes; and the paid run POST is never replayed.
"""

import json as _json

import httpx
import pytest

from kyc_tool.adapters.floqer import (
    FloqerAdapter,
    FloqerConfigurationError,
    FloqerOutOfCredits,
    FloqerRunFailed,
    FloqerTimeout,
    ShortcutFloqerClient,
)

SHORTCUT_ID = "shortcut-1"
RUN_ID = "run-9"
DATA_ID = "row-42"
REFERENCES = ("company_name", "website_domain", "contact_full_name", "contact_title")
# Floqer keys `output_data` by the selected action output's LABEL and cannot rename it, so these
# nine strings are the wire contract; the record keeps the client's canonical keys.
OUTPUTS = ("Person LinkedIn URL", "First Name", "Last Name", "Person Current Job Title",
           "Current Company Name", "Current Company Domain", "Website", "Formatted Data",
           "profile_matches")
URL, FIRST, LAST, TITLE, COMPANY, COMPANY_DOMAIN, WEBSITE, SOURCE, MATCHES = OUTPUTS


def _shortcut(*, is_published=True, references=REFERENCES, outputs=OUTPUTS):
    return {
        "status": 200,
        "data": {
            "id": SHORTCUT_ID,
            "is_published": is_published,
            "input_schema": [{"reference": r, "name": r, "type": "text"} for r in references],
            "output_schema": [{"name": n} for n in outputs],
        },
    }


def _field(value, status="completed"):
    return {"value": value, "status": status}


def _handler(polls, *, shortcut=None, post=None):
    """GET the shortcut → its schema; POST → a started run; each poll returns the next scripted
    run payload (the last one repeats forever). `post`/`shortcut` override a stage wholesale."""
    remaining = list(polls)

    def handle(request):
        path = request.url.path
        if request.method == "POST":
            if post is not None:
                return post(request)
            return httpx.Response(
                201,
                json={"status": 201, "message": "Shortcut run started",
                      "data": {"id": RUN_ID, "data_id": DATA_ID, "status": "pending"}},
            )
        if path.endswith(f"/shortcuts/{SHORTCUT_ID}"):
            return shortcut(request) if callable(shortcut) else httpx.Response(
                200, json=shortcut if shortcut is not None else _shortcut()
            )
        state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(
            200, json={"status": 200, "data": {"id": RUN_ID, "data_id": DATA_ID, **state}}
        )

    return handle


def _make(handler, **kwargs):
    """→ (client, requests, sleeps). The clock advances by exactly what the client sleeps."""
    requests, sleeps, now = [], [], {"t": 0.0}

    def transport(request):
        requests.append(request)
        return handler(request)

    def sleep(seconds):
        sleeps.append(seconds)
        now["t"] += seconds

    client = ShortcutFloqerClient(
        httpx.Client(
            transport=httpx.MockTransport(transport), base_url="https://floqer.test/api/v1"
        ),
        SHORTCUT_ID,
        sleep=sleep,
        clock=lambda: now["t"],
        **kwargs,
    )
    return client, requests, sleeps


def _completed(values):
    return {"status": "completed", "output_data": values}


_FULL_OUTPUT = _completed({
    URL: _field("https://www.linkedin.com/in/jane-doe"),
    FIRST: _field("Jane"),
    LAST: _field("Doe"),
    TITLE: _field("Director"),
    COMPANY: _field("Acme Networks"),
    COMPANY_DOMAIN: _field("acme.example"),
    WEBSITE: _field("https://www.acme.example/about"),
    SOURCE: _field("email"),
    MATCHES: _field(""),
})


# ── bootstrap ─────────────────────────────────────────────────────────────────────────────────────
def test_bootstrap_reads_the_schema_once_and_caches_it():
    client, requests, _ = _make(_handler([_FULL_OUTPUT]))
    client.enrich("Acme Networks Ltd", "acme.example", contact_name="Jane Doe")
    client.enrich("Acme Networks Ltd", "acme.example", contact_name="Jane Doe")
    schema_reads = [r for r in requests if r.url.path.endswith(f"/shortcuts/{SHORTCUT_ID}")]
    assert len(schema_reads) == 1 and schema_reads[0].method == "GET"


def test_unpublished_shortcut_and_403_are_configuration_errors():
    client, requests, _ = _make(_handler([_FULL_OUTPUT], shortcut=_shortcut(is_published=False)))
    with pytest.raises(FloqerConfigurationError, match="not published"):
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert [r.method for r in requests] == ["GET"]  # no run was ever started

    forbidden, requests, _ = _make(
        _handler([_FULL_OUTPUT], shortcut=lambda r: httpx.Response(403, json={"status": 403}))
    )
    with pytest.raises(FloqerConfigurationError, match="403"):
        forbidden.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert [r.method for r in requests] == ["GET"]


def test_a_shortcut_without_the_contact_reference_is_a_configuration_error():
    client, _, _ = _make(
        _handler([_FULL_OUTPUT], shortcut=_shortcut(references=("company_name",)))
    )
    with pytest.raises(FloqerConfigurationError, match="contact_full_name"):
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")


# ── the credit guard ──────────────────────────────────────────────────────────────────────────────
def test_a_contactless_case_places_no_http_call_at_all():
    """A run without a person to resolve would spend credits finding the WRONG person. Every way
    the platform can name one — full name, email, split names — has to be blank first."""
    client, requests, sleeps = _make(_handler([_FULL_OUTPUT]))
    assert client.enrich("Acme Networks Ltd", "acme.example") == {}
    assert client.enrich("Acme Networks Ltd", "acme.example", contact_name="  ", contact_email="",
                         contact_first_name="", contact_last_name=" ") == {}
    assert requests == [] and sleeps == []


def test_an_email_only_contact_still_runs():
    client, requests, _ = _make(
        _handler([_FULL_OUTPUT], shortcut=_shortcut(references=(*REFERENCES, "contact_email")))
    )
    client.enrich("Acme", "acme.example", contact_email="jane@acme.example")
    posted = [r for r in requests if r.method == "POST"]
    assert len(posted) == 1
    body = _json.loads(posted[0].read())["input_data"]
    assert body["contact_email"] == "jane@acme.example" and body["contact_full_name"] == ""


def test_optional_references_are_sent_only_when_the_schema_declares_them():
    """An unknown key rejects the whole run with a 400, so a reference the live schema does not
    declare is dropped rather than guessed onto the wire."""
    split = ("contact_first_name", "contact_last_name")
    for references, expected in (
        (REFERENCES, set(REFERENCES)),
        ((*REFERENCES, *split), set(REFERENCES) | set(split)),
    ):
        client, requests, _ = _make(
            _handler([_FULL_OUTPUT], shortcut=_shortcut(references=references))
        )
        client.enrich("Acme", "acme.example", contact_name="Jane Doe",
                      contact_first_name="Jane", contact_last_name="Doe")
        posted = next(r for r in requests if r.method == "POST")
        assert set(_json.loads(posted.read())["input_data"]) == expected


# ── the run request ───────────────────────────────────────────────────────────────────────────────
def test_input_data_is_filtered_to_the_live_references():
    client, requests, _ = _make(
        _handler([_FULL_OUTPUT], shortcut=_shortcut(references=("company_name", "contact_full_name")))
    )
    client.enrich("Acme Networks Ltd", "acme.example", contact_name="Jane Doe",
                  contact_title="Director")
    posted = next(r for r in requests if r.method == "POST")
    body = _json.loads(posted.read())
    assert body == {"input_data": {"company_name": "Acme Networks Ltd",
                                   "contact_full_name": "Jane Doe"}}
    assert posted.url.path.endswith(f"/shortcuts/{SHORTCUT_ID}/run")


def test_400_on_the_run_is_a_configuration_error_with_no_retry():
    posts = []

    def post(request):
        posts.append(request)
        return httpx.Response(400, json={"status": 400, "error": "Validation Error"})

    client, _, sleeps = _make(_handler([_FULL_OUTPUT], post=post))
    with pytest.raises(FloqerConfigurationError, match="400"):
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert len(posts) == 1 and sleeps == []


def test_429_on_the_run_sleeps_the_documented_retry_after_then_succeeds_once():
    posts = []

    def post(request):
        posts.append(request)
        if len(posts) == 1:
            return httpx.Response(
                429, json={"status": 429, "error": "Too Many Requests", "retryAfter": 7}
            )
        return httpx.Response(
            201, json={"data": {"id": RUN_ID, "data_id": DATA_ID, "status": "pending"}}
        )

    client, _, sleeps = _make(_handler([_FULL_OUTPUT], post=post))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert len(posts) == 2
    assert sleeps == [7.0, 5.0]  # the documented retryAfter, then the first poll delay
    assert record["linkedin"]["person_name"] == "Jane Doe"


def test_a_5xx_run_start_fails_after_the_single_attempt():
    posts = []

    def post(request):
        posts.append(request)
        return httpx.Response(503)

    client, _, _ = _make(_handler([_FULL_OUTPUT], post=post))
    with pytest.raises(FloqerRunFailed, match="503"):
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert len(posts) == 1  # a replayed POST would start a SECOND paid run


# ── polling to a terminal status ──────────────────────────────────────────────────────────────────
def test_happy_path_maps_every_output_and_records_provenance():
    """All nine LIVE output labels in, the canonical record out — including `person_name`, which
    is no longer an output at all but the two halves joined."""
    client, requests, sleeps = _make(
        _handler([{"status": "pending"}, {"status": "inProgress"}, _FULL_OUTPUT])
    )
    record = client.enrich("Acme Networks Ltd", "acme.example", contact_name="Jane Doe",
                           contact_title="Director")
    assert record == {
        "aliases": [],
        "provenance": {"shortcut_id": SHORTCUT_ID, "run_id": RUN_ID, "data_id": DATA_ID},
        "website": "https://www.acme.example/about",
        "company_domain": "acme.example",
        "linkedin_source": "email",
        "linkedin": {
            "person_name": "Jane Doe",
            "first_name": "Jane",
            "last_name": "Doe",
            "company": "Acme Networks",
            "title": "Director",
            "company_domain": "acme.example",
            "url": "https://www.linkedin.com/in/jane-doe",
        },
    }
    assert sleeps == [5.0, 3.0, 3.0]  # first poll at 5s, then the 3s cadence
    assert len([r for r in requests if r.method == "POST"]) == 1


def test_a_failed_field_is_no_data_not_a_value():
    output = _completed({
        URL: _field("https://www.linkedin.com/in/jane-doe"),
        FIRST: _field("Jane"),
        LAST: _field("Doe"),
        TITLE: _field("No data found", status="failed"),
        COMPANY: _field("Acme Networks"),
        COMPANY_DOMAIN: _field("acme.example"),
        WEBSITE: _field(""),
    })
    client, _, _ = _make(_handler([output]))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert "title" not in record["linkedin"]
    assert record["linkedin"]["person_name"] == "Jane Doe"
    assert "website" not in record  # a completed-but-empty value is also "no data"
    assert record["company_domain"] == "acme.example"


def test_no_person_resolved_omits_linkedin_but_keeps_the_company_fields():
    """The validator records NO check when `linkedin` is absent; a half-filled dict would be a
    mismatch and cost the case points it never had."""
    output = _completed({
        URL: _field(""),
        FIRST: _field("", status="failed"),
        LAST: _field("", status="failed"),
        WEBSITE: _field("https://acme.example/contact"),
    })
    client, _, _ = _make(_handler([output]))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert "linkedin" not in record
    assert record["website"] == "https://acme.example/contact"
    assert record["company_domain"] == "acme.example"  # derived from the website


def test_person_name_is_derived_only_from_both_halves():
    """`person_name` is not a shortcut output any more: a whole name exists only when the run
    returned BOTH halves — half a name is not a name the validator may match on."""
    for half in ({FIRST: _field("Jane")}, {LAST: _field("Doe")}):
        output = _completed({URL: _field("https://www.linkedin.com/in/jane-doe"), **half})
        client, _, _ = _make(_handler([output]))
        record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
        assert "person_name" not in record["linkedin"]


def test_snake_case_output_keys_read_as_absent():
    """Outputs are read by LIVE LABEL only: a run whose output_data still carries the old
    snake_case keys has nothing this client can read, and claims no profile from it."""
    output = _completed({
        "linkedin_url": _field("https://www.linkedin.com/in/jane-doe"),
        "first_name": _field("Jane"),
        "last_name": _field("Doe"),
        "website": _field("https://www.acme.example/about"),
    })
    client, _, _ = _make(_handler([output]))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert record == {
        "aliases": [],
        "provenance": {"shortcut_id": SHORTCUT_ID, "run_id": RUN_ID, "data_id": DATA_ID},
    }


# ── how the profile was found ─────────────────────────────────────────────────────────────────────
def _found(source, verification=""):
    return _completed({
        URL: _field("https://www.linkedin.com/in/jane-doe"),
        FIRST: _field("Jane"),
        LAST: _field("Doe"),
        COMPANY: _field("Acme Networks"),
        SOURCE: _field(source),
        MATCHES: _field(verification),
    })


@pytest.mark.parametrize("source", ["email", "apollo"])
def test_a_directly_found_profile_keeps_its_split_names_and_ignores_web_verification(source):
    """email/apollo resolve a KNOWN person, so `web_verification` says nothing about them: no
    `web_verified` flag is invented for a source that cannot have one."""
    client, _, _ = _make(_handler([_found(source, verification="no")]))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert record["linkedin_source"] == source and "web_verified" not in record
    assert record["linkedin"]["first_name"] == "Jane"
    assert record["linkedin"]["last_name"] == "Doe"


def test_an_unverified_web_found_profile_never_reaches_the_validator():
    client, _, _ = _make(_handler([_found("web_search", verification="No")]))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert "linkedin" not in record  # a guess about WHICH person is not matchable evidence
    assert record["linkedin_source"] == "web_search" and record["web_verified"] is False
    assert record["provenance"]["unverified_linkedin_url"] == (
        "https://www.linkedin.com/in/jane-doe"  # still auditable, just outside the match inputs
    )


def test_only_an_explicit_yes_verifies_a_web_found_profile():
    verified, _, _ = _make(_handler([_found("web_search", verification=" YES ")]))
    record = verified.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert record["web_verified"] is True
    assert record["linkedin"]["url"] == "https://www.linkedin.com/in/jane-doe"
    for verification in ("", "maybe", "not verified"):
        client, _, _ = _make(_handler([_found("web_search", verification=verification)]))
        run = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
        assert run["web_verified"] is False and "linkedin" not in run


def test_a_source_outside_the_documented_set_is_dropped_not_recorded():
    """An unknown value is provenance the tool cannot interpret — and it is not `web_search`, so
    it must not silently acquire a verification verdict either."""
    client, _, _ = _make(_handler([_found("guesswork", verification="no")]))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert "linkedin_source" not in record and "web_verified" not in record
    assert record["linkedin"]["url"] == "https://www.linkedin.com/in/jane-doe"


def test_values_are_coerced_from_whatever_the_schema_actually_returns():
    """Declared types are a hint, not a contract: ints must not reach norm_equal/domain_of, and a
    null value is no data rather than the string "None"."""
    output = _completed({
        URL: _field(12345),
        FIRST: _field(None),
        COMPANY_DOMAIN: _field(" ACME.example "),
        WEBSITE: _field(None),
    })
    client, _, _ = _make(_handler([output]))
    record = client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert record["linkedin"] == {"company_domain": "ACME.example", "url": "12345"}
    assert record["company_domain"] == "ACME.example" and "website" not in record


def test_terminal_failure_carries_the_run_id_and_the_error_message():
    client, _, _ = _make(
        _handler([{"status": "failed", "error_message": "Apollo rejected the request"}])
    )
    with pytest.raises(FloqerRunFailed) as exc:
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert RUN_ID in str(exc.value) and "Apollo rejected the request" in str(exc.value)


def test_out_of_credits_is_its_own_billing_stop():
    client, _, _ = _make(_handler([{"status": "outOfCredits"}]))
    with pytest.raises(FloqerOutOfCredits, match="do NOT retry"):
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")


def test_null_output_data_at_completed_is_outside_the_contract():
    client, _, _ = _make(_handler([{"status": "completed", "output_data": None}]))
    with pytest.raises(FloqerConfigurationError, match="outside the documented contract"):
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")


def test_the_deadline_bounds_the_whole_poll_schedule():
    """5s, then 3s until the slow threshold, then 10s — and the LAST sleep that would land past
    the 180s deadline is refused instead of taken, so the client never outlives its own budget."""
    client, requests, sleeps = _make(_handler([{"status": "inProgress"}]))
    with pytest.raises(FloqerTimeout) as exc:
        client.enrich("Acme", "acme.example", contact_name="Jane Doe")
    assert sleeps[0] == 5.0
    assert set(sleeps[1:20]) == {3.0} and set(sleeps[20:]) == {10.0}
    assert sum(sleeps) <= 180.0 < sum(sleeps) + 10.0  # bounded, and it used the budget it had
    assert RUN_ID in str(exc.value) and DATA_ID in str(exc.value)
    polls = [r for r in requests if r.method == "GET" and "/runs/" in r.url.path]
    assert len(polls) == len(sleeps) < 45  # under the per-case request budget (DESIGN_BRIEF C.4)


# ── the adapter around the client ─────────────────────────────────────────────────────────────────
class _SpyClient:
    def __init__(self):
        self.calls = []

    def enrich(self, company_name, domain, **contact):
        self.calls.append((company_name, domain, contact))
        return {}


_SNAPSHOT = {
    "company_legal_name": "Acme Networks Ltd",
    "website": "https://acme.example",
    "contact": {"name": "Jane Doe", "title": "Director"},
}


def test_adapter_forwards_the_contact_to_the_client():
    spy = _SpyClient()
    FloqerAdapter(spy).run(_SNAPSHOT, {})
    assert spy.calls == [
        ("Acme Networks Ltd", "https://acme.example",
         {"contact_name": "Jane Doe", "contact_title": "Director", "contact_email": "",
          "contact_first_name": "", "contact_last_name": ""}),
    ]


def test_the_adapter_passes_the_profile_provenance_into_normalized():
    """source_detail can only report how the profile was found if the adapter forwards it — and a
    shortcut that reports neither must not have the two keys defaulted into the evidence."""
    class _Client:
        def __init__(self, record):
            self.record = record

        def enrich(self, company_name, domain, **contact):
            return self.record

    found = {"linkedin": {"url": "u"}, "linkedin_source": "web_search", "web_verified": True}
    normalized = FloqerAdapter(_Client(found)).run(_SNAPSHOT, {}).normalized
    assert normalized["linkedin_source"] == "web_search" and normalized["web_verified"] is True
    bare = FloqerAdapter(_Client({"linkedin": {"url": "u"}})).run(_SNAPSHOT, {}).normalized
    assert "linkedin_source" not in bare and "web_verified" not in bare


@pytest.mark.parametrize(
    "contact",
    [
        {"name": "John Roe", "title": "Director"},
        {"name": "Jane Doe", "title": "Director", "email": "jane@acme.example"},
        {"name": "Jane Doe", "title": "Director", "first_name": "Jane", "last_name": "Doe"},
    ],
)
def test_input_hash_covers_every_contact_field_the_adapter_now_reads(contact):
    adapter = FloqerAdapter(_SpyClient())
    assert adapter.input_hash(_SNAPSHOT, {}) != adapter.input_hash(
        dict(_SNAPSHOT, contact=contact), {}
    )
    assert adapter.input_hash(_SNAPSHOT, {}) == adapter.input_hash(dict(_SNAPSHOT), {})
