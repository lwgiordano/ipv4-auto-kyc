"""Floqer enrichment adapter — DISCOVERY ONLY (non-negotiable #8).

Its output seeds registry candidate lookups, LinkedIn matching, and website
review context. It can never by itself satisfy email, ORG-ID, POC, or
registry points — enforced by the intent builder, which awards nothing from
Floqer except linkedin_company_match after a full deterministic match.

The contract is the Floqer SHORTCUT API (AUDIT:C4): a shortcut wraps one
published workflow behind a typed input/output schema, so a case is one
`POST /shortcuts/{id}/run` plus polls of `GET /shortcuts/{id}/runs/{run_id}`
until a terminal status, then one read of `output_data`. WHICH `reference`
and `name` keys exist is read from `GET /shortcuts/{id}` at first use: only
references the live input_schema declares are sent, and only output names it
declares are read (see `_OUTPUT_NAMES`).
"""

import json
import time
from typing import Protocol

import httpx

from kyc_tool.adapters import retry
from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.domain.models import AdapterStatus
from kyc_tool.validators.normalize import domain_of

FLOQER_BASE_URL = "https://api.floqer.com/api/v1"

# The canonical shortcut schema this tool is built against. Only references the LIVE
# input_schema declares are sent; only outputs the live output_schema declares are read.
_INPUT_REFERENCES = ("company_name", "website_domain", "contact_full_name", "contact_title")
# Optional references the shortcut MAY declare. Sent only when the case actually carries the
# value and the live schema declares the key — anything else is dropped, never guessed onto the
# wire (an unknown key rejects the whole run with a 400).
_OPTIONAL_REFERENCES = ("contact_email", "contact_first_name", "contact_last_name")
# Canonical key -> the LIVE output name. Those names are the selected action outputs' LABELS:
# Floqer keys `output_data` by label and does not let an output be renamed. "Formatted Data" is
# the ONE JS-formatter output selected (the LinkedIn Source step) — selecting a second formatter
# output would make Floqer suffix the duplicates (`Formatted Data_2`, ...) in an order this client
# cannot know, so the shortcut must keep exactly one.
_OUTPUT_NAMES = {
    "linkedin_url": "Person LinkedIn URL",
    "first_name": "First Name",
    "last_name": "Last Name",
    "title": "Person Current Job Title",
    "company": "Current Company Name",
    "company_domain": "Current Company Domain",
    "website": "Website",
    "linkedin_source": "Formatted Data",
    "web_verification": "profile_matches",
}
# How the shortcut found the profile. `web_search` is the only one that is a GUESS: it is the
# verification agent, not the lookup, that makes such a profile usable as evidence.
_LINKEDIN_SOURCES = ("email", "apollo", "web_search")
_LINKEDIN_FIELDS = ("person_name", "first_name", "last_name", "company", "title", "company_domain")
# A configuration fault, never a transient one: retrying cannot make it right.
_CONFIG_STATUS_CODES = (400, 401, 403, 404)
_MAX_RETRY_AFTER_SECONDS = 60.0


class FloqerError(RuntimeError):
    """Base for every Floqer failure. The pipeline converts any of these into
    AdapterStatus.UPSTREAM_ERROR (partial run) — no message ever carries a request header or the
    API key."""


class FloqerConfigurationError(FloqerError):
    """Wrong id/key/scope, an unpublished shortcut, or a payload outside the documented
    contract. NOT retryable: fix the configuration or the shortcut."""


class FloqerRunFailed(FloqerError):
    """A terminal `failed`/`error` run, or a run that could not be started. The pipeline's own
    job retry re-runs the adapter later."""


class FloqerOutOfCredits(FloqerError):
    """Terminal `outOfCredits` — a billing stop, not a fault: do not retry, top up the account."""


class FloqerTimeout(FloqerError):
    """The client deadline passed with the run still going. Carries the run and data ids so an
    operator can inspect the run without paying for a second one."""


class FloqerClient(Protocol):
    def enrich(
        self,
        company_name: str,
        domain: str,
        *,
        contact_name: str = "",
        contact_title: str = "",
        contact_email: str = "",
        contact_first_name: str = "",
        contact_last_name: str = "",
    ) -> dict:
        """→ {company_domain, website, linkedin: {person_name, first_name, last_name,
        company, title, company_domain, url}, linkedin_source, web_verified, aliases: [..],
        provenance: {...}} — keys optional."""
        ...


class FixtureFloqerClient:
    def __init__(self, records: dict[str, dict]) -> None:
        self.records = records  # key: normalized company name

    def enrich(self, company_name: str, domain: str, **contact) -> dict:
        return self.records.get(company_name.strip().lower(), {})  # contact_* ignored


class ShortcutFloqerClient:
    """Live discovery through one published Floqer shortcut.

    The injected `httpx.Client` carries the base URL and the `Authorization` header — this class
    never sees the key, so it cannot leak it into a message. Every wire call goes through the
    governed transport (`retry.request_with_retry`), and the run POST is NOT retried by default:
    a shortcut run is billed, and a replayed POST starts a second paid run.
    """

    def __init__(
        self,
        client: httpx.Client,
        shortcut_id: str,
        *,
        first_poll_seconds: float = 5.0,
        poll_seconds: float = 3.0,
        slow_after_seconds: float = 60.0,
        slow_poll_seconds: float = 10.0,
        deadline_seconds: float = 180.0,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.client = client
        self.shortcut_id = shortcut_id
        self.first_poll_seconds = first_poll_seconds
        self.poll_seconds = poll_seconds
        self.slow_after_seconds = slow_after_seconds
        self.slow_poll_seconds = slow_poll_seconds
        self.deadline_seconds = deadline_seconds
        self.sleep = sleep
        self.clock = clock
        self._schema: tuple[set[str], set[str]] | None = None

    # ── wire helpers ──────────────────────────────────────────────────────────────────────────
    def _body(self, response: httpx.Response, what: str) -> dict:
        try:
            body = response.json()
        except ValueError as exc:
            raise FloqerConfigurationError(
                f"{what} returned a non-JSON body (HTTP {response.status_code}) — outside the "
                "documented contract"
            ) from exc
        if not isinstance(body, dict):
            raise FloqerConfigurationError(
                f"{what} returned a {type(body).__name__} body — outside the documented contract"
            )
        return body

    def _retry_after(self, response: httpx.Response) -> float:
        """429 carries `retryAfter` in the JSON BODY (there is no documented header)."""
        try:
            value = float(response.json().get("retryAfter"))
        except (ValueError, TypeError, AttributeError):
            return self.poll_seconds
        return value if value > 0 else self.poll_seconds

    def _nap(self, seconds: float, deadline: float, run_id: str = "", data_id: str = "") -> None:
        """Sleep, refusing any wait that would land past the client deadline."""
        seconds = min(seconds, _MAX_RETRY_AFTER_SECONDS)
        if self.clock() + seconds > deadline:
            raise FloqerTimeout(
                f"shortcut {self.shortcut_id} run {run_id or '(not started)'} "
                f"(data_id {data_id or '-'}) still running at the {self.deadline_seconds}s client "
                "deadline — inspect the run in Floqer rather than re-running it"
            )
        self.sleep(seconds)

    # ── schema bootstrap (lazy, cached per instance) ──────────────────────────────────────────
    def _schema_sets(self) -> tuple[set[str], set[str]]:
        if self._schema is None:
            response = retry.request_with_retry(
                self.client, "GET", f"/shortcuts/{self.shortcut_id}"
            )
            if response.status_code != 200:
                raise FloqerConfigurationError(
                    f"reading shortcut {self.shortcut_id} failed with HTTP {response.status_code}"
                )
            data = self._body(response, "get shortcut").get("data") or {}
            if not data.get("is_published"):
                raise FloqerConfigurationError(
                    f"shortcut {self.shortcut_id} is not published — only published shortcuts run"
                )
            self._schema = (
                {str(f.get("reference", "")) for f in data.get("input_schema") or []},
                {str(f.get("name", "")) for f in data.get("output_schema") or []},
            )
        return self._schema

    # ── one case ──────────────────────────────────────────────────────────────────────────────
    def enrich(
        self,
        company_name: str,
        domain: str,
        *,
        contact_name: str = "",
        contact_title: str = "",
        contact_email: str = "",
        contact_first_name: str = "",
        contact_last_name: str = "",
    ) -> dict:
        person = (contact_name, contact_email, contact_first_name, contact_last_name)
        if not any(str(v or "").strip() for v in person):
            # No person to resolve: the run would spend credits finding the wrong one.
            return {}
        references, outputs = self._schema_sets()
        if "contact_full_name" not in references:
            raise FloqerConfigurationError(
                f"shortcut {self.shortcut_id} has no contact_full_name input reference — its "
                "input_schema drifted from the chain this client was built against"
            )
        domain = domain_of(domain) or domain  # `website_domain` wants a bare host, not a URL
        supplied = dict(
            zip(
                _INPUT_REFERENCES + _OPTIONAL_REFERENCES,
                (company_name, domain, contact_name, contact_title,
                 contact_email, contact_first_name, contact_last_name),
                strict=True,
            )
        )
        input_data = {
            k: str(v or "")
            for k, v in supplied.items()
            if k in references and (k in _INPUT_REFERENCES or str(v or "").strip())
        }
        deadline = self.clock() + self.deadline_seconds
        run = self._start_run(input_data, deadline)
        run_id, data_id = str(run.get("id") or ""), str(run.get("data_id") or "")
        if not run_id:
            raise FloqerConfigurationError(
                f"shortcut {self.shortcut_id} started a run without a data.id to poll"
            )
        return self._poll(run_id, data_id, deadline, outputs)

    def _start_run(self, input_data: dict, deadline: float) -> dict:
        """POST the run. One attempt, plus exactly one more after a documented 429 back-off."""
        for attempt in (1, 2):
            response = retry.request_with_retry(
                self.client,
                "POST",
                f"/shortcuts/{self.shortcut_id}/run",
                json={"input_data": input_data},
            )
            if response.status_code == 201:
                return self._body(response, "run shortcut").get("data") or {}
            if response.status_code in _CONFIG_STATUS_CODES:
                raise FloqerConfigurationError(
                    f"starting a run of shortcut {self.shortcut_id} was refused with HTTP "
                    f"{response.status_code} — check the shortcut id, the key's scopes, and that "
                    "input_data matches the live input_schema"
                )
            if response.status_code == 429 and attempt == 1:
                self._nap(self._retry_after(response), deadline)
                continue
            raise FloqerRunFailed(
                f"starting a run of shortcut {self.shortcut_id} failed with HTTP "
                f"{response.status_code} after {attempt} attempt(s)"
            )

    def _poll(self, run_id: str, data_id: str, deadline: float, outputs: set[str]) -> dict:
        started = self.clock()
        self._nap(self.first_poll_seconds, deadline, run_id, data_id)
        while True:
            # attempts=1: THIS loop is the retry schedule. A transient retry inside the transport
            # would sleep on the real clock and poll off-cadence.
            response = retry.request_with_retry(
                self.client, "GET", f"/shortcuts/{self.shortcut_id}/runs/{run_id}", attempts=1
            )
            if response.status_code in _CONFIG_STATUS_CODES:
                raise FloqerConfigurationError(
                    f"polling run {run_id} of shortcut {self.shortcut_id} was refused with HTTP "
                    f"{response.status_code}"
                )
            if response.status_code == 429:
                self._nap(self._retry_after(response), deadline, run_id, data_id)
                continue
            if response.status_code == 200:
                data = self._body(response, "get shortcut run").get("data") or {}
                status = str(data.get("status") or "")
                if status == "completed":
                    return self._record(data, outputs, run_id, data_id)
                if status == "outOfCredits":
                    raise FloqerOutOfCredits(
                        f"run {run_id} (data_id {data_id}) stopped out of credits — this is a "
                        "billing stop, do NOT retry: top the account up first"
                    )
                if status in ("failed", "error"):
                    raise FloqerRunFailed(
                        f"run {run_id} (data_id {data_id}) ended {status}: "
                        f"{data.get('error_message') or 'no error_message'}"
                    )
                # any other value is in-progress (the docs enumerate only "include pending,
                # inProgress"), as is a 5xx: wait the scheduled interval and poll again
            elapsed = self.clock() - started
            interval = (
                self.slow_poll_seconds if elapsed >= self.slow_after_seconds else self.poll_seconds
            )
            self._nap(interval, deadline, run_id, data_id)

    def _record(self, data: dict, outputs: set[str], run_id: str, data_id: str) -> dict:
        output_data = data.get("output_data")
        if not isinstance(output_data, dict):
            raise FloqerConfigurationError(
                f"run {run_id} completed with output_data of type "
                f"{type(output_data).__name__} — outside the documented contract"
            )
        got = {
            key: _output_value(output_data.get(name))
            for key, name in _OUTPUT_NAMES.items()
            if name in outputs
        }
        if got.get("first_name") and got.get("last_name"):
            # Not a shortcut output: the shortcut returns the halves, so the whole name exists
            # only when BOTH are present — never inferred from one of them.
            got["person_name"] = f"{got['first_name']} {got['last_name']}"
        record: dict = {
            "aliases": [],
            # Audit trail only — the adapter keeps this in `raw` and never normalises it.
            "provenance": {
                "shortcut_id": self.shortcut_id, "run_id": run_id, "data_id": data_id
            },
        }
        if got.get("website"):
            record["website"] = got["website"]
        company_domain = got.get("company_domain") or domain_of(got.get("website", ""))
        if company_domain:
            record["company_domain"] = company_domain
        if got.get("linkedin_source") in _LINKEDIN_SOURCES:
            record["linkedin_source"] = got["linkedin_source"]
        if record.get("linkedin_source") == "web_search":
            # Only an explicit "yes" from the verification agent counts; anything else — "no",
            # blank, a field that failed — leaves the profile unverified.
            record["web_verified"] = got.get("web_verification", "").lower() == "yes"
        if got.get("linkedin_url"):
            # Only a RESOLVED person gets a `linkedin` key: without one the validator must record
            # no check at all rather than a mismatch.
            linkedin = {key: got[key] for key in _LINKEDIN_FIELDS if got.get(key)}
            linkedin["url"] = got["linkedin_url"]
            if record.get("web_verified") is False:
                # An unverified web-found profile is a guess about WHICH person this is: it must
                # never reach the validator. The URL stays for audit, outside the match inputs.
                record["provenance"]["unverified_linkedin_url"] = linkedin["url"]
            else:
                record["linkedin"] = linkedin
        return record


def _output_value(entry: object) -> str:
    """One `output_data` entry → its value, or "" for "no data".

    A value counts only when its OWN status is exactly "completed" (a run completes with some
    fields failed — "No data found") and it is non-blank. Declared types are hints, not
    contracts, so everything is coerced to str.
    """
    if not isinstance(entry, dict) or str(entry.get("status") or "") != "completed":
        return ""
    value = entry.get("value")
    text = "" if value is None else str(value).strip()
    # A `run_if`-skipped step completes with the literal two-character string `""` (seen live
    # on `Website` and `profile_matches`): that is no data, not a value.
    return "" if text in ('""', "''", "null") else text


def make_floqer_client(settings, records: dict[str, dict] | None = None) -> FloqerClient:
    """The live client when a shortcut id is configured, else the fixture client."""
    if not settings.floqer_shortcut_id:
        return FixtureFloqerClient(records or {})
    if not settings.floqer_api_key:
        raise ValueError("floqer_shortcut_id is set but floqer_api_key is empty")
    return ShortcutFloqerClient(
        httpx.Client(
            base_url=FLOQER_BASE_URL,
            headers={"Authorization": "Bearer " + settings.floqer_api_key},
            timeout=15.0,
        ),
        settings.floqer_shortcut_id,
    )


# `contact` is a free-form dict on the platform payload (api.schemas.KybRunPayload); today's
# submissions carry name/title, and the rest are read when present and blank when not.
_CONTACT_KEYS = ("name", "title", "email", "first_name", "last_name")


class FloqerAdapter:
    adapter_id = "floqer_company_enrichment"

    def __init__(self, client: FloqerClient) -> None:
        self.client = client

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        contact = case_snapshot.get("contact") or {}
        return hash_inputs(
            self.adapter_id,
            case_snapshot.get("company_legal_name"),
            case_snapshot.get("website"),
            *(contact.get(k) for k in _CONTACT_KEYS),
        )

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        name = case_snapshot.get("company_legal_name")
        if not name:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)
        contact = case_snapshot.get("contact") or {}
        # The provider-protocol delegate is external I/O (re-audit `750630c..ca85355` F7): the
        # governed delegate proves the claim + deadline immediately before invoking it — a lost
        # claim places ZERO Floqer calls. The live client's own wire calls additionally route
        # through the governed transport (retry.request_with_retry).
        record = retry.governed_delegate(
            self.client.enrich,
            name,
            case_snapshot.get("website", ""),
            **{f"contact_{k}": contact.get(k) or "" for k in _CONTACT_KEYS},
        )
        if not record:
            return AdapterOutput(
                self.adapter_id, AdapterStatus.OK, raw=b"{}", normalized={"discovered": False}
            )
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=json.dumps(record, default=str).encode(),
            normalized={
                "discovered": True,
                "company_domain": record.get("company_domain", ""),
                "website": record.get("website", ""),
                "linkedin": record.get("linkedin") or {},
                "aliases": record.get("aliases", []),
                "registry_candidates": record.get("registry_candidates", []),
                "broker_context": record.get("broker_context", {}),
                # Absent when the shortcut did not report them — the validator carries whichever
                # of the two is present into source_detail and never defaults the missing one.
                **{k: record[k] for k in ("linkedin_source", "web_verified") if k in record},
            },
        )
