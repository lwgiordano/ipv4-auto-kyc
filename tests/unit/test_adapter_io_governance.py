"""Re-audit `750630c..ca85355` F4/F7 REDs: the governed authority is transitive — the object
store + OCR engine (document_ocr) and provider-protocol delegates (floqer) prove the claim and
deadline before ANY external call, document reads are byte-capped BEFORE materialization, and an
unprovable claim fails closed instead of becoming upstream evidence."""

import pytest

from kyc_tool.adapters import retry
from kyc_tool.adapters.document_ocr import DocumentOcrAdapter
from kyc_tool.adapters.floqer import FixtureFloqerClient, FloqerAdapter
from kyc_tool.queue import jobs
from kyc_tool.storage.object_store import ObjectTooLarge

_EVENT = {
    "event_type": "document.uploaded",
    "payload": {"object_ref": "fs://uploads/doc.json", "doc_type": "certificate"},
}


class _RecordingStore:
    def __init__(self, data=b'{"fields": {}}'):
        self.data = data
        self.calls = []

    def get_bounded(self, ref, *, max_bytes):
        self.calls.append((ref, max_bytes))
        if max_bytes is not None and len(self.data) > max_bytes:
            raise ObjectTooLarge(f"{ref} over {max_bytes}")
        return self.data


class _RecordingEngine:
    def __init__(self):
        self.calls = []

    def extract(self, data, doc_type):
        self.calls.append(doc_type)
        return {"fields": {}}


def _lost_claim_budget():
    job = jobs.ClaimedJob(id=3, kind="k", case_id=None, payload={}, attempts=1, max_attempts=5,
                          claim_nonce="w:n")
    ctx = jobs.ClaimContext(job=job)
    ctx.lost.set()
    budget = retry.RetryBudget(
        deadline_monotonic=1e9, clock=lambda: 0.0, prove_live=jobs.check_claim_live,
        max_response_bytes=1000,
    )
    return ctx, budget


def test_lost_claim_places_zero_object_store_and_ocr_calls():
    store, engine = _RecordingStore(), _RecordingEngine()
    adapter = DocumentOcrAdapter(store, engine)
    ctx, budget = _lost_claim_budget()
    with jobs.claim_scope(ctx), retry.budget_scope(budget), pytest.raises(jobs.StaleJobClaim):
        adapter.run({}, _EVENT)
    assert store.calls == [] and engine.calls == []  # revoked BEFORE any external call


def test_lost_claim_places_zero_floqer_calls():
    calls = []

    class _SpyClient(FixtureFloqerClient):
        def enrich(self, company_name, domain):
            calls.append(company_name)
            return {}

    adapter = FloqerAdapter(_SpyClient({}))
    ctx, budget = _lost_claim_budget()
    with jobs.claim_scope(ctx), retry.budget_scope(budget), pytest.raises(jobs.StaleJobClaim):
        adapter.run({"company_legal_name": "ACME"}, {"event_type": "kyb.run_requested"})
    assert calls == []


def test_document_read_is_byte_capped_before_the_engine_sees_it():
    store, engine = _RecordingStore(data=b"x" * 5000), _RecordingEngine()
    adapter = DocumentOcrAdapter(store, engine)
    budget = retry.RetryBudget(deadline_monotonic=1e9, clock=lambda: 0.0, max_response_bytes=1000)
    with retry.budget_scope(budget), pytest.raises(ObjectTooLarge):
        adapter.run({}, _EVENT)
    assert store.calls == [("fs://uploads/doc.json", 1000)]  # the governed cap reached the read
    assert engine.calls == []  # an oversized document never reaches OCR


def test_spent_budget_places_zero_document_calls():
    store, engine = _RecordingStore(), _RecordingEngine()
    adapter = DocumentOcrAdapter(store, engine)
    spent = retry.RetryBudget(deadline_monotonic=0.0, clock=lambda: 1.0)
    with retry.budget_scope(spent), pytest.raises(retry.BudgetExhausted):
        adapter.run({}, _EVENT)
    assert store.calls == [] and engine.calls == []


def test_ungoverned_direct_call_still_works():
    """No ambient budget (direct/unit/dev callers): the adapter behaves as before — governance
    activates with the pipeline's budget, it does not break standalone use."""
    store, engine = _RecordingStore(), _RecordingEngine()
    adapter = DocumentOcrAdapter(store, engine)
    output = adapter.run({}, _EVENT)
    assert output.status.value == "ok"
    assert store.calls == [("fs://uploads/doc.json", None)]  # uncapped, but same bounded surface


def test_prove_live_for_send_fails_closed_on_proof_error():
    """R10-F4 unit witness: the proof's own DB error sets lost and raises StaleJobClaim — it must
    never escape as a generic exception for the pipeline's UPSTREAM_ERROR branch to swallow."""
    job = jobs.ClaimedJob(id=4, kind="k", case_id=None, payload={}, attempts=1, max_attempts=5,
                          claim_nonce="w:n")
    ctx = jobs.ClaimContext(job=job)

    def broken_factory():
        raise ConnectionError("claim DB unreachable")

    with jobs.claim_scope(ctx), pytest.raises(jobs.StaleJobClaim) as exc:
        jobs.prove_live_for_send(broken_factory)
    assert ctx.lost.is_set()
    assert "UNAVAILABLE" in str(exc.value)
    jobs.prove_live_for_send(broken_factory)  # outside any claim scope: no-op, no raise
