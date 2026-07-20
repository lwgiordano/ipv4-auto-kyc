# PR 5b — Review-record binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Parent session is the SOLE committer/pusher/bus-writer (Claude⇄Codex bus discipline); subagents implement + hand diffs back.

**Goal:** Bind the review-completion / manual-approve paths to the signed platform-asserted reviewer actor, and make the `website.review_completed` task-close + `website_verified` check atomic and duplicate-safe under a `FOR UPDATE` lock.

**Architecture:** Two enforcement points. (1) An **ingest floor** (fast-fail, advisory) rejects a bad reviewer actor with 422 and no orphan rows. (2) An **authoritative decide-txn guard** locks the `ReviewTask FOR UPDATE`, re-validates the *persisted* actor + task state, and produces ONE guard decision that gates both the check emission and the close. Rollout is a brief full maintenance window (no old/new overlap); a new `ops.requeue_interrupted_jobs` one-shot recovers interrupted jobs during it.

**Tech Stack:** Python 3.11, FastAPI (sync + threadpool), SQLAlchemy 2 (sync psycopg), Postgres, pytest against ephemeral Postgres.

**Spec:** `.agents/superpowers/specs/2026-07-19-pr5b-review-record-binding-design.md` (Codex AUDIT-CLEAN at rev 8).

## Global Constraints

- **No migration** — no schema change, no new task states (`ReviewTask.reviewer_id`, `Event.actor_json` already exist).
- **`KYC_Tool_Build_Package/` is immutable** — never edit; deviations go in `AUDIT_FINDINGS.md`.
- **M2 untouched** — do not touch `KYC_ENFORCE_POSITIVE_DECISIONS` / the enforcement kill switch.
- **Never** put the model identifier in any repository artifact (commit messages, code, docs).
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK
  ```
- **Actor rules are scoped to the two review events** — do NOT add a global nonblank constraint to `Actor.id` in `api/schemas.py`.
- Reviewer identity comparison: nonblank after `.strip()`, **exact case-sensitive** equality of the stripped values.
- Reject codes: bad actor / inconsistent = **422** (authenticated but inconsistent; not 401/403).
- Tests run against real Postgres. `./manage.sh test` runs the WHOLE suite (ignores path args); for targeted red-green use `.venv/bin/pytest <selector>`. Never run `./manage.sh fmt` (it reformats the whole tree — the PR 4/5a noise trap); `ruff check` is the gate.
- Reviewer actors are passed **explicitly** in tests; do NOT teach the shared `post_event`/`envelope`/`sign_headers` fixtures to infer them.

---

## File Structure

- **Create** `src/kyc_tool/events/review_guard.py` — pure reviewer-actor rule + the decide-txn lock+evaluate guard (scalar view + locked ORM task). One responsibility: "who may complete/approve, and is this the authoritative completion".
- **Create** `src/kyc_tool/ops/requeue_interrupted_jobs.py` — one-shot cutover recovery command (mirrors `ops/activate_hmac_v1_observation.py`).
- **Modify** `src/kyc_tool/events/ingest.py` — website floor gains the actor check + `actor` arg; new manual-approve actor floor.
- **Modify** `src/kyc_tool/orchestration/pipeline.py` — evaluate the guard once in `_decide_txn` (both VALIDATE and DECIDE short-circuit paths), thread the scalar into `_validation_extras`, pass the locked task to `on_event`.
- **Modify** `src/kyc_tool/orchestration/side_effects.py` — `on_event` closes via the locked task from the guard; actor-derived persistence; skip-audit on ineligible.
- **Modify** `src/kyc_tool/validators/build.py` + `src/kyc_tool/validators/website.py` — gate `website_intent` on the guard; take the trusted reviewer id.
- **Modify** `src/kyc_tool/ui/routes.py` — production composer bar (403) for the two sensitive types; dev reviewer actor.
- **Modify** docs: `PLATFORM_INTEGRATION.md`, `RUNBOOK.md`, `OVERVIEW.md`, `DEPLOYMENT.md`, `architecture-decisions.md` (ADR-004), `.agents/ROADMAP.md`.
- **Test** files: `tests/unit/test_review_guard.py`, `tests/unit/test_requeue_interrupted_jobs.py` (or integration), extend `tests/integration/test_review_completed_event.py`, `tests/integration/test_manual_approve.py` (or the phase-4 suite), `tests/integration/test_ui.py`.

---

## Task 1: Reviewer-actor rule + ingest floors

**Files:**
- Create: `src/kyc_tool/events/review_guard.py`
- Modify: `src/kyc_tool/events/ingest.py:53-67` (website floor) and `:195-199` (manual-approve dispatch)
- Test: `tests/unit/test_review_guard.py`, `tests/integration/test_review_completed_event.py`

**Interfaces:**
- Produces: `review_guard.REVIEWER_ACTOR_INVALID: str = "actor_invalid"`; `review_guard.reviewer_actor_reason(actor: dict, payload: dict) -> str | None` (returns `REVIEWER_ACTOR_INVALID` or `None`).
- Consumes: `ingest.IngestOutcome`, `ingest._FloorReject`.

- [ ] **Step 1: Write the failing unit test for the pure rule**

```python
# tests/unit/test_review_guard.py
import pytest
from kyc_tool.events.review_guard import REVIEWER_ACTOR_INVALID, reviewer_actor_reason

OK = {"type": "reviewer", "id": "rev-1"}

@pytest.mark.parametrize("actor,payload,expected", [
    (OK, {"reviewer_id": "rev-1"}, None),                              # valid
    ({"type": "system", "id": "rev-1"}, {"reviewer_id": "rev-1"}, REVIEWER_ACTOR_INVALID),  # wrong type
    (OK, {"reviewer_id": "rev-2"}, REVIEWER_ACTOR_INVALID),            # mismatch
    ({"type": "reviewer", "id": ""}, {"reviewer_id": ""}, REVIEWER_ACTOR_INVALID),   # both blank
    ({"type": "reviewer", "id": "  "}, {"reviewer_id": "  "}, REVIEWER_ACTOR_INVALID),  # whitespace
    ({"type": "reviewer", "id": "rev-1 "}, {"reviewer_id": "rev-1"}, None),  # trailing ws stripped
    (OK, {}, REVIEWER_ACTOR_INVALID),                                 # payload missing reviewer_id
])
def test_reviewer_actor_reason(actor, payload, expected):
    assert reviewer_actor_reason(actor, payload) == expected
```

- [ ] **Step 2: Run it — expect ImportError / fail**

Run: `.venv/bin/pytest tests/unit/test_review_guard.py -q`
Expected: FAIL (`No module named kyc_tool.events.review_guard`).

- [ ] **Step 3: Implement the pure rule**

```python
# src/kyc_tool/events/review_guard.py
"""Review-record trust rules (PR 5b).

Reviewer identity is platform-asserted via the signed envelope (HMAC v2). These
helpers enforce that the asserted `actor` is a consistent, nonblank reviewer for
the two sensitive event types. The `actor.id == payload.reviewer_id` equality is
a CONSISTENCY check; the security boundary is the signature.
"""

REVIEWER_ACTOR_INVALID = "actor_invalid"


def reviewer_actor_reason(actor: dict, payload: dict) -> str | None:
    """None if the actor is a valid reviewer whose id matches the payload's
    reviewer_id (both nonblank after strip, exact case-sensitive). Else
    REVIEWER_ACTOR_INVALID."""
    if (actor or {}).get("type") != "reviewer":
        return REVIEWER_ACTOR_INVALID
    actor_id = str((actor or {}).get("id", "")).strip()
    reviewer_id = str((payload or {}).get("reviewer_id", "")).strip()
    if not actor_id or not reviewer_id or actor_id != reviewer_id:
        return REVIEWER_ACTOR_INVALID
    return None
```

- [ ] **Step 4: Run it — expect PASS**

Run: `.venv/bin/pytest tests/unit/test_review_guard.py -q` → PASS.

- [ ] **Step 5: Write the failing integration test for the ingest floors**

```python
# tests/integration/test_review_completed_event.py  (append)
def test_wrong_actor_type_rejected_422(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-a")  # existing helper; task_type=website, open
    resp, _ = post_event(
        "case-a", "website.review_completed", _wrc(task_id, reviewer="rev-1"),
        actor={"type": "system", "id": "rev-1"},
    )
    assert resp.status_code == 422

def test_actor_id_mismatch_rejected_422(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-a")
    resp, _ = post_event(
        "case-a", "website.review_completed", _wrc(task_id, reviewer="rev-1"),
        actor={"type": "reviewer", "id": "someone-else"},
    )
    assert resp.status_code == 422

def test_valid_reviewer_actor_accepted(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-a")
    resp, _ = post_event(
        "case-a", "website.review_completed", _wrc(task_id, reviewer="rev-1"),
        actor={"type": "reviewer", "id": "rev-1"},
    )
    assert resp.status_code == 202
```

Note: `post_event` already accepts `actor=` (see `tests/conftest.py`). If a local `_seed_task` helper does not exist in this file, add one that inserts a `ReviewTask(case_id=…, task_type="website", status="open")` and returns its id (mirror the existing seeding in the file).

- [ ] **Step 6: Run it — expect the mismatch/type tests to FAIL** (today they return 202; the valid one already passes).

Run: `.venv/bin/pytest tests/integration/test_review_completed_event.py -q`

- [ ] **Step 7: Wire the actor check into the website floor**

In `ingest.py`, change the signature and prepend the actor check:

```python
def _validate_website_review_completed(
    session: Session, case_id: str, actor: dict, payload: dict
) -> IngestOutcome | None:
    if reviewer_actor_reason(actor, payload) is not None:
        return IngestOutcome(422, {"error": "invalid reviewer actor",
                                   "task_id": payload.get("task_id")})
    task_id = payload.get("task_id")
    task = session.get(ReviewTask, task_id) if task_id else None
    if task is None:
        return IngestOutcome(404, {"error": "review task not found", "task_id": task_id})
    if task.task_type != "website":
        return IngestOutcome(422, {"error": "not a website review task", "task_id": task_id})
    if task.case_id != case_id:
        return IngestOutcome(409, {"error": "task belongs to a different case", "task_id": task_id})
    if task.status != "open":
        return IngestOutcome(409, {"error": "review task not open", "task_id": task_id})
    return None
```

Update its call site (currently `reject = _validate_website_review_completed(session, case_id, payload)`) to pass `actor`:

```python
if event_type == "website.review_completed":
    reject = _validate_website_review_completed(session, case_id, actor, payload)
    if reject is not None:
        raise _FloorReject(reject)
```

Add the import at the top of `ingest.py`: `from kyc_tool.events.review_guard import reviewer_actor_reason`.

- [ ] **Step 8: Add the manual-approve actor floor**

In `ingest.py`, the `MANUAL_APPROVE` branch (currently at `:195`) runs inline. Add the actor check BEFORE `_handle_manual_approve`, raising `_FloorReject` so a bad actor rolls back with no orphan event:

```python
if event_type == MANUAL_APPROVE:
    if reviewer_actor_reason(actor, payload) is not None:
        raise _FloorReject(IngestOutcome(422, {"error": "invalid reviewer actor"}))
    body = _handle_manual_approve(session, policy, case, event, actor)
    event.response_snapshot = body
    event.processed_at = datetime.now(UTC)
    return IngestOutcome(200, body)
```

- [ ] **Step 9: Run the targeted tests — expect PASS**

Run: `.venv/bin/pytest tests/unit/test_review_guard.py tests/integration/test_review_completed_event.py -q` → PASS.

- [ ] **Step 10: Commit**

```bash
git add src/kyc_tool/events/review_guard.py src/kyc_tool/events/ingest.py \
  tests/unit/test_review_guard.py tests/integration/test_review_completed_event.py
git commit -m "feat(pr5b): reviewer-actor floor on review-completion + manual-approve"
```

---

## Task 2: Authoritative decide-txn guard (lock + scalar/ORM views + coupling)

**Files:**
- Modify: `src/kyc_tool/events/review_guard.py` (add the guard)
- Modify: `src/kyc_tool/orchestration/pipeline.py` (`_decide_txn` ~`:299`, `_validation_extras` ~`:424`, `on_event` call ~`:356`)
- Modify: `src/kyc_tool/orchestration/side_effects.py:178-200` (`on_event`)
- Modify: `src/kyc_tool/validators/build.py:47-48`, `src/kyc_tool/validators/website.py`
- Test: `tests/integration/test_review_completed_event.py`

**Interfaces:**
- Produces: `review_guard.WebsiteCompletionGuard` (frozen dataclass: `eligible: bool`, `reviewer_id: str | None`, `task_id: str | None`, `skip_reason: str | None`); `review_guard.evaluate_website_completion(session, case_id, event) -> tuple[WebsiteCompletionGuard, ReviewTask | None]` (locks the task `FOR UPDATE`; the ORM task is returned for orchestration ONLY).
- Consumes: `website_intent(event_payload: dict, reviewer_id: str) -> CheckIntent`.
- `ValidationContext.extras["website_guard"]` carries the scalar `WebsiteCompletionGuard` (never the ORM task).

- [ ] **Step 1: Write the failing test — pre-upgrade queued bad actor is skipped at decide**

```python
# tests/integration/test_review_completed_event.py  (append)
def test_pre_upgrade_bad_actor_skipped_at_decide(engine, clean_db, session_factory, policy, settings, tmp_path):
    """A website.review_completed admitted UNDER PR 5a (no actor floor) with a
    system actor must be SKIPPED by the decide-txn guard: no check, task stays
    open, completion_skipped audited, run still completes."""
    task_id = _seed_task(engine, "case-x")
    # seed the event+run directly, bypassing the floor (as a PR 5a-era API would)
    run_id = _seed_completion_run(engine, "case-x", task_id,
                                  actor={"type": "system", "id": "sys"},
                                  payload={"task_id": task_id, "result": "pass", "reviewer_id": "sys"})
    _drive_run(session_factory, policy, settings, tmp_path, run_id)  # run the worker to completion
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "open"                         # not closed
        assert _live_check(s, "case-x", "website_verified") is None   # no +10
        assert _audit_reason(s, "case-x", "review_task.completion_skipped") == "actor_invalid"
```

Helpers (`_seed_completion_run`, `_drive_run`, `_live_check`, `_audit_reason`) — add to the test module; `_drive_run` runs `pipeline.handle_job` for the run's queued job (mirror the existing run-driving pattern used elsewhere in the integration suite, e.g. `tests/integration/shared.py`). If a shared driver exists, import it.

- [ ] **Step 2: Run it — expect FAIL** (today the guard doesn't exist; the run would close the task from the payload).

Run: `.venv/bin/pytest tests/integration/test_review_completed_event.py::test_pre_upgrade_bad_actor_skipped_at_decide -q`

- [ ] **Step 3: Add the guard to `review_guard.py`**

```python
from dataclasses import dataclass

from sqlalchemy.orm import Session

from kyc_tool.db.tables import Event, ReviewTask


@dataclass(frozen=True, slots=True)
class WebsiteCompletionGuard:
    """Immutable scalar decision, safe to hand a pure validator (no ORM entity)."""
    eligible: bool
    reviewer_id: str | None   # actor-derived trusted id, only when eligible
    task_id: str | None
    skip_reason: str | None   # task_missing|wrong_type|wrong_case|task_not_open|actor_invalid


def evaluate_website_completion(
    session: Session, case_id: str, event: Event
) -> tuple[WebsiteCompletionGuard, ReviewTask | None]:
    """Lock the referenced ReviewTask FOR UPDATE and evaluate eligibility from the
    PERSISTED event (re-validated even though ingest checked it — pre-upgrade
    queued events never passed the floor). The ORM task is returned ONLY for the
    orchestration/side-effect layer; the scalar guard is what a validator sees."""
    payload = event.payload_json or {}
    actor = event.actor_json or {}
    task_id = payload.get("task_id")
    if not task_id:
        return WebsiteCompletionGuard(False, None, None, "task_missing"), None
    task = session.get(ReviewTask, task_id, with_for_update=True)
    if task is None:
        return WebsiteCompletionGuard(False, None, task_id, "task_missing"), None
    if task.task_type != "website":
        return WebsiteCompletionGuard(False, None, task_id, "wrong_type"), task
    if task.case_id != case_id:
        return WebsiteCompletionGuard(False, None, task_id, "wrong_case"), task
    if task.status != "open":
        return WebsiteCompletionGuard(False, None, task_id, "task_not_open"), task
    if reviewer_actor_reason(actor, payload) is not None:
        return WebsiteCompletionGuard(False, None, task_id, "actor_invalid"), task
    return WebsiteCompletionGuard(True, str(actor.get("id")).strip(), task_id, None), task
```

- [ ] **Step 4: Thread the guard through `_decide_txn`**

In `pipeline.py._decide_txn`, right after `run, case, event = self._load(session, run_id)` (the case is already `FOR UPDATE` there), evaluate the guard once for website completions:

```python
website_guard = None
website_task = None
if event.event_type == "website.review_completed":
    website_guard, website_task = review_guard.evaluate_website_completion(
        session, case.id, event
    )
```

Pass the scalar into validation extras — change `_validation_extras(self, session, case, event)` to also accept the guard, OR (simpler, no signature churn) set it after building extras. Recommended: pass it explicitly.

```python
# where the VALIDATE branch builds ctx:
extras=self._validation_extras(session, case, event, website_guard=website_guard),
```
```python
def _validation_extras(self, session, case, event, *, website_guard=None) -> dict:
    extras: dict = {}
    if website_guard is not None:
        extras["website_guard"] = website_guard
    if event.event_type == "poc.token_verified":
        ...  # unchanged
    return extras
```

And pass the locked task to `on_event`:

```python
self.side_effects.on_event(session, case, event, intents,
                           website_guard=website_guard, website_task=website_task)
```

Add `from kyc_tool.events import review_guard` to `pipeline.py` imports.

- [ ] **Step 5: Gate `website_intent` on the guard**

`website.py`:
```python
def website_intent(event_payload: dict, reviewer_id: str) -> CheckIntent:
    passed = event_payload.get("result") == "pass"
    return CheckIntent(
        "website_verified",
        CheckStatus.PASS if passed else CheckStatus.FAIL,
        reason_codes=tuple(event_payload.get("reason_codes", ())),
        source=f"reviewer:{reviewer_id}",
        source_detail={"task_id": event_payload.get("task_id")},
    )
```

`build.py`:
```python
if ctx.event_type == "website.review_completed":
    guard = ctx.extras.get("website_guard")
    if guard is not None and guard.eligible:
        intents.append(website_intent(ctx.event_payload, guard.reviewer_id))
```

- [ ] **Step 6: Close via the locked task + skip-audit in `on_event`**

`side_effects.py` — replace the website branch:
```python
def on_event(self, session, case, event, intents, *, website_guard=None, website_task=None):
    if event.event_type == "website.review_completed":
        payload = event.payload_json or {}
        if website_guard is not None and website_guard.eligible and website_task is not None:
            website_task.status = "done"
            website_task.result = payload.get("result")
            website_task.reviewer_id = website_guard.reviewer_id   # actor-derived, trusted
            website_task.reason_codes = list(payload.get("reason_codes", []))
            website_task.completed_at = datetime.now(UTC)
            audit(session, "review_task.completed", case_id=case.id, task_id=website_task.id,
                  result=website_task.result, actor=website_guard.reviewer_id)
        elif website_guard is not None and not website_guard.eligible:
            audit(session, "review_task.completion_skipped", case_id=case.id,
                  task_id=website_guard.task_id, reason=website_guard.skip_reason,
                  event_id=event.id)
    elif event.event_type == "poc.token_verified":
        ...  # unchanged
```

- [ ] **Step 7: Run the pre-upgrade-skip test + the existing suite for this file — expect PASS**

Run: `.venv/bin/pytest tests/integration/test_review_completed_event.py -q` → PASS.

- [ ] **Step 8: Add + run the persistence test (actor-derived id; DecisionRow untouched)**

```python
def test_persists_actor_derived_reviewer_not_payload(engine, clean_db, session_factory, policy, settings, tmp_path, post_event):
    task_id = _seed_task(engine, "case-p")
    # payload reviewer_id must equal actor.id to pass the floor; prove the STORED
    # source is the actor path by asserting task.reviewer_id == actor id and the
    # event payload is unchanged.
    post_event("case-p", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-9"},
               actor={"type": "reviewer", "id": "rev-9"})
    _drain(session_factory, policy, settings, tmp_path, "case-p")
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "done" and task.reviewer_id == "rev-9"
        chk = _live_check(s, "case-p", "website_verified")
        assert chk.source == "reviewer:rev-9"
        # automatic decision row is NOT a manual row
        dec = _latest_decision(s, "case-p")
        assert dec.manual is False and dec.reviewer_id is None
```

Run it → PASS.

- [ ] **Step 9: Run lint + import contracts**

Run: `.venv/bin/ruff check src/kyc_tool tests` and `.venv/bin/lint-imports`. Expected: clean, 2 kept / 0 broken. (The scalar `WebsiteCompletionGuard` — not the ORM task — is what enters `ValidationContext`, preserving the pure-validator contract.)

- [ ] **Step 10: Commit**

```bash
git add src/kyc_tool/events/review_guard.py src/kyc_tool/orchestration/pipeline.py \
  src/kyc_tool/orchestration/side_effects.py src/kyc_tool/validators/build.py \
  src/kyc_tool/validators/website.py tests/integration/test_review_completed_event.py
git commit -m "feat(pr5b): authoritative FOR UPDATE decide-txn guard couples close+check"
```

---

## Task 3: Concurrency, FIFO/dead-letter, and broker-blocked proofs

**Files:**
- Modify: `src/kyc_tool/orchestration/pipeline.py` (broker-blocked short-circuit — emit the website intent on the DECIDE path)
- Test: `tests/integration/test_review_completed_event.py`

**Interfaces:** consumes Task 2's guard; no new production symbols except the short-circuit wiring.

- [ ] **Step 1: Write the failing blocked-broker test**

```python
def test_blocked_broker_completion_closes_task_and_writes_check(engine, clean_db, session_factory, policy, settings, tmp_path, post_event):
    # put case on the broker blocklist so the run short-circuits to DECIDE
    _seed_blocked_broker(engine, "case-b")   # existing broker-seed helper / fixture
    task_id = _seed_task(engine, "case-b")
    post_event("case-b", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"})
    _drain(session_factory, policy, settings, tmp_path, "case-b")
    with session_factory() as s:
        assert s.get(ReviewTask, task_id).status == "done"        # closed
        assert _live_check(s, "case-b", "website_verified") is not None  # +10 written
        assert _latest_decision(s, "case-b").decision == "reject"  # still blocked
```

- [ ] **Step 2: Run it — expect FAIL** (the DECIDE short-circuit skips validator intents, so no `website_verified` is written).

- [ ] **Step 3: Emit the website intent + guard on the short-circuit path**

In `_decide_txn`, the intent-building block is guarded by `if from_state is RunState.VALIDATE:`. For `website.review_completed`, the guard + intent must also run when `from_state is RunState.DECIDE` (broker short-circuit). Add, after the VALIDATE block and before `apply_check_intents`:

```python
# website.review_completed carries the reviewer verdict in its payload (no
# adapters), so its check must be produced even on the broker-blocked DECIDE
# short-circuit — otherwise an eligible completion closes the task with no +10.
if event.event_type == "website.review_completed" and from_state is RunState.DECIDE:
    if website_guard is not None and website_guard.eligible:
        intents.append(website_intent(event.payload_json or {}, website_guard.reviewer_id))
```

Add the `website_intent` import to `pipeline.py`. (`on_event`, already called unconditionally, closes the task via the guard on both paths.)

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: Write the concurrency test (two admitted before either runs → one winner)**

```python
def test_two_completions_one_close_one_skip(engine, clean_db, session_factory, policy, settings, tmp_path, post_event):
    task_id = _seed_task(engine, "case-c")
    # admit TWO completions (distinct idempotency keys) BEFORE draining
    post_event("case-c", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"}, key="k1")
    post_event("case-c", "website.review_completed",
               {"task_id": task_id, "result": "fail", "reviewer_id": "rev-2"},
               actor={"type": "reviewer", "id": "rev-2"}, key="k2")
    _drain(session_factory, policy, settings, tmp_path, "case-c")   # FIFO: k1 then k2
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "done" and task.reviewer_id == "rev-1"   # first eligible wins
        chk = _live_check(s, "case-c", "website_verified")
        assert chk.status.name == "PASS"                                # not flipped by k2
        assert _audit_reason(s, "case-c", "review_task.completion_skipped") == "task_not_open"
```

Run → PASS. (`post_event` supports `key=`; `_drain` processes all queued jobs for the case in FIFO order.)

- [ ] **Step 6: Write the dead-letter test (winner dies → higher sequence wins)**

```python
def test_winner_deadletters_then_second_wins(engine, clean_db, session_factory, policy, settings, tmp_path, post_event):
    task_id = _seed_task(engine, "case-d")
    post_event("case-d", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"}, key="k1")
    post_event("case-d", "website.review_completed",
               {"task_id": task_id, "result": "fail", "reviewer_id": "rev-2"},
               actor={"type": "reviewer", "id": "rev-2"}, key="k2")
    _force_deadletter_first_job(engine, "case-d")   # set attempts>=max on seq-1's job
    _drain(session_factory, policy, settings, tmp_path, "case-d")
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "done" and task.result == "fail"   # seq-2 legitimately closed it
```

Run → PASS. (`_force_deadletter_first_job` bumps the earliest queued job's `attempts` to `max_attempts` and lets `fail`/reaper dead-letter it; the atomic decide-txn rollback leaves the task open for seq-2.)

- [ ] **Step 7: Commit**

```bash
git add src/kyc_tool/orchestration/pipeline.py tests/integration/test_review_completed_event.py
git commit -m "feat(pr5b): broker-blocked short-circuit closes+checks; concurrency/dead-letter proofs"
```

---

## Task 4: Production `/ui` composer bar

**Files:**
- Modify: `src/kyc_tool/ui/routes.py` (`send_event`, ~`:355`)
- Test: `tests/integration/test_ui.py`

**Interfaces:** none new; behavior gated on `settings.environment`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_ui.py  (append; follow the existing admin-token client pattern)
SENSITIVE = ["website.review_completed", "reviewer.manual_approve"]

@pytest.mark.parametrize("event_type", SENSITIVE)
def test_composer_bars_sensitive_types_in_production(prod_ui_client, event_type):
    r = prod_ui_client.post("/ui/api/send-event",
                            json={"case_id": "c1", "event_type": event_type, "payload": {}},
                            headers=_admin_headers())
    assert r.status_code == 403

def test_composer_allows_scoring_events_in_production(prod_ui_client):
    r = prod_ui_client.post("/ui/api/send-event",
                            json={"case_id": "c1", "event_type": "kyb.run_requested",
                                  "payload": {"company_legal_name": "Acme"}},
                            headers=_admin_headers())
    assert r.status_code != 403
```

`prod_ui_client`: a TestClient built with `settings.model_copy(update={"environment": "production", "ui_enabled": True, "ui_admin_token": "t"})` plus the production-required HMAC/S3/provider fields (reuse the `hardened()` helper shape from `tests/unit/test_production_config.py`, or a local fixture). Confirm the exact admin-header helper already used in `test_ui.py`.

- [ ] **Step 2: Run — expect FAIL** (composer currently ingests any type).

- [ ] **Step 3: Bar the sensitive types in production; send a reviewer actor in dev**

In `send_event` (after `require_admin`, after reading `body`/`event_type`):

```python
_SENSITIVE = {"website.review_completed", "reviewer.manual_approve"}
settings = request.app.state.settings
if settings.environment == "production" and envelope.event_type in _SENSITIVE:
    raise HTTPException(status_code=403,
                        detail="composer cannot submit reviewer events in production")
```

For the actor the composer supplies: for the two sensitive types build a reviewer actor from the payload's `reviewer_id` instead of the hardcoded `{"type": "system", "id": "ops-console"}`:

```python
if envelope.event_type in _SENSITIVE:
    rid = (payload or {}).get("reviewer_id") or "ops-console"
    actor = {"type": "reviewer", "id": rid}
else:
    actor = {"type": "system", "id": "ops-console"}
```

Use `actor` where the envelope's actor is assembled for the `ingest_event` call. (Match the exact local variable names in `send_event`; the composer currently hardcodes the system actor when composing the envelope.)

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/ui/routes.py tests/integration/test_ui.py
git commit -m "feat(pr5b): production composer bar on reviewer events; dev sends reviewer actor"
```

---

## Task 5: `ops.requeue_interrupted_jobs` cutover recovery command

**Files:**
- Create: `src/kyc_tool/ops/requeue_interrupted_jobs.py`
- Test: `tests/integration/test_requeue_interrupted_jobs.py`

**Interfaces:**
- Produces: `requeue_interrupted_jobs.requeue_interrupted(session_factory) -> int` (count requeued; raises `RuntimeError` if any `running` remains after) and `main() -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_requeue_interrupted_jobs.py
import pytest
from sqlalchemy import text
from kyc_tool.ops.requeue_interrupted_jobs import requeue_interrupted

pytestmark = pytest.mark.postgres

def _insert_job(session_factory, *, status, attempts, max_attempts=5, lease="now() - interval '1 hour'"):
    with session_factory() as s:
        jid = s.execute(text(
            f"INSERT INTO jobs (kind, payload_json, status, attempts, max_attempts, "
            f"run_after, lease_expires_at, created_at, updated_at) "
            f"VALUES ('run_transition', '{{}}'::jsonb, :st, :a, :m, now(), {lease}, now(), now()) "
            "RETURNING id"), {"st": status, "a": attempts, "m": max_attempts}).scalar_one()
        s.commit()
    return jid

def test_final_attempt_running_is_requeued_not_deadlettered(session_factory, clean_db):
    jid = _insert_job(session_factory, status="running", attempts=5, max_attempts=5)
    assert requeue_interrupted(session_factory) == 1
    with session_factory() as s:
        row = s.execute(text("SELECT status, attempts FROM jobs WHERE id=:i"), {"i": jid}).one()
        assert row.status == "queued" and row.attempts == 4   # forced-stop attempt returned

def test_unexpired_lease_running_is_requeued(session_factory, clean_db):
    jid = _insert_job(session_factory, status="running", attempts=1,
                      lease="now() + interval '1 hour'")   # STILL VALID lease
    assert requeue_interrupted(session_factory) == 1
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": jid}).scalar_one() == "queued"

def test_idempotent_and_zero_running_after(session_factory, clean_db):
    _insert_job(session_factory, status="running", attempts=1)
    requeue_interrupted(session_factory)
    assert requeue_interrupted(session_factory) == 0    # nothing running the 2nd time
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM jobs WHERE status='running'")).scalar_one() == 0
```

Confirm the `jobs` column list against `db/tables.py` before finalizing the INSERT (adjust names if needed).

- [ ] **Step 2: Run — expect FAIL** (module missing).

- [ ] **Step 3: Implement the command (mirror `ops/activate_hmac_v1_observation.py`)**

```python
# src/kyc_tool/ops/requeue_interrupted_jobs.py
"""Cutover recovery one-shot (PR 5b §10 step 3). Run ONCE after ALL pipeline
workers are confirmed stopped and none have restarted, BEFORE new workers start.

With every worker stopped, every `status='running'` row is by definition an
interrupted job (there is no worker registry to prove liveness from `locked_by`,
and none is needed). This requeues them all — regardless of lease expiry —
WITHOUT consuming the forced-stop attempt (the claim pre-incremented `attempts`;
we decrement it back so an interrupted FINAL attempt is retried rather than
dead-lettered by the passive reaper). It MUST NOT run while any worker is live.

    python -m kyc_tool.ops.requeue_interrupted_jobs
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory


def requeue_interrupted(session_factory) -> int:
    """Requeue every running job; return the count. Raises if any remains."""
    with session_factory() as session:
        count = session.execute(
            text(
                "UPDATE jobs SET status='queued', attempts = attempts - 1, "
                "locked_by=NULL, lease_expires_at=NULL, updated_at=now() "
                "WHERE status='running'"
            )
        ).rowcount
        remaining = session.execute(
            text("SELECT count(*) FROM jobs WHERE status='running'")
        ).scalar_one()
        session.commit()
    if remaining != 0:
        raise RuntimeError(f"{remaining} jobs still running after requeue — workers not stopped?")
    return count


def main() -> int:
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    n = requeue_interrupted(session_factory)
    print(f"requeued {n} interrupted job(s); 0 running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/kyc_tool/ops/requeue_interrupted_jobs.py tests/integration/test_requeue_interrupted_jobs.py
git commit -m "feat(pr5b): ops.requeue_interrupted_jobs cutover recovery one-shot"
```

---

## Task 6: Documentation, ADR-004, ROADMAP, DEPLOYMENT cutover

**Files:** `docs/PLATFORM_INTEGRATION.md`, `docs/RUNBOOK.md`, `docs/OVERVIEW.md`, `docs/DEPLOYMENT.md`, `docs/architecture-decisions.md`, `.agents/ROADMAP.md`. No tests (docs); the drift-guard suite does not read these.

- [ ] **Step 1: PLATFORM_INTEGRATION.md** — on BOTH sensitive event payload tables (`website.review_completed`, `reviewer.manual_approve`), document the reviewer actor requirement: envelope `actor.type` MUST be `"reviewer"` and `actor.id` MUST equal the payload `reviewer_id` (nonblank, exact); a mismatch/blank/wrong-type is **422**. State that the tool records the actor-derived reviewer, not the payload field.

- [ ] **Step 2: RUNBOOK.md** — add the production composer prohibition: `/ui` composer refuses `website.review_completed` and `reviewer.manual_approve` in production (403); real reviewer actions arrive as signed platform events. Note the 403 is the server-side security boundary (hiding UI controls is optional).

- [ ] **Step 3: OVERVIEW.md** — one line in the console/security note: server-side 403 for reviewer events is the boundary.

- [ ] **Step 4: DEPLOYMENT.md** — add the PR 5b **brief full maintenance-window** cutover (spec §10 verbatim in intent): build+digest the image (step 0); pause ALL event submission + composer; stop ALL old APIs+workers together (no drain); run the digest-pinned `requeue_interrupted_jobs`; deploy API first, workers at zero; direct side-effect-free probes bypassing the edge; start workers; resume. Rollback: non-mutating verification only (`/readyz`/`/healthz`/digest), sensitive mutation probes prohibited against the prior image.

- [ ] **Step 5: architecture-decisions.md** — prepend **ADR-004** (newest-first, above ADR-003): the trust model (signed platform-asserted actor; `actor.id==payload.reviewer_id` consistency check; decide-txn `FOR UPDATE` authority; maintenance-window rollout).

- [ ] **Step 6: ROADMAP.md** — mark PR 5b shipped; **move the reserved PR 10 ADR-004 to ADR-005** (§`:245-252,274-276`), since PR 5b takes ADR-004.

- [ ] **Step 7: Sanity + commit** — `grep -rn "ADR-004" docs/ .agents/` shows exactly one PR-5b ADR-004 and PR 10 now reads ADR-005; no stray `same shared secret`/reviewer-from-payload claims.

```bash
git add docs/ .agents/ROADMAP.md
git commit -m "docs(pr5b): reviewer actor contract, composer bar, maintenance-window cutover, ADR-004"
```

---

## Task 7: Full verification + bus RELEASE

- [ ] **Step 1:** `.venv/bin/ruff check .` → clean.
- [ ] **Step 2:** `.venv/bin/lint-imports` → 2 kept / 0 broken (guard's scalar view keeps validators pure).
- [ ] **Step 3:** `./manage.sh test` → full suite green on real Postgres (expect ~+15 new tests over the current count).
- [ ] **Step 4:** Revert any whole-tree `fmt` noise if `fmt` was ever run (it must not be); confirm `git status` shows only intended files.
- [ ] **Step 5:** Parent posts the bus `RELEASE [CLAUDE]` with the §3 verification artifact (commands+results, DB witness / CI run, the headline proofs — pre-upgrade-skip, concurrency one-winner, blocked-broker atomic — and the anchor SHA), pushes, and asks Codex to audit the PR 5b code range to `AUDIT-CLEAN`.

---

## Verification (whole PR)

- Per task: the task's `pytest` selector goes red → green against ephemeral Postgres, then commit.
- Whole PR: `ruff check` + `lint-imports` clean; `./manage.sh test` green.
- Security proofs (spec §8): reviewer-actor 422 matrix; **pre-upgrade queued bad-actor skipped at decide**; **two-completions-one-winner** (no result flip); dead-letter higher-sequence wins; blocked-broker atomic close+check; rollback retry (+10 once); persistence (actor-derived; DecisionRow untouched); composer 403 in prod; recovery-command final-attempt + unexpired-lease + idempotent.
- No migration; `KYC_Tool_Build_Package/` untouched; M2 untouched.
- Final: bus RELEASE → Codex audits the code range → `AUDIT-CLEAN`.

## Self-review notes (folded)

- Spec §§1-6 each map to Tasks 1-4; §10 recovery command → Task 5; §7 docs → Task 6; §8 tests distributed across Tasks 1-5 + the Task 7 gate.
- Type consistency: `WebsiteCompletionGuard(eligible, reviewer_id, task_id, skip_reason)` and `evaluate_website_completion(session, case_id, event) -> (guard, task|None)` are used identically in Tasks 2-3; `website_intent(event_payload, reviewer_id)` is the single new signature consumed by both `build.py` and the short-circuit path.
- The only new production entry point is `kyc_tool.ops.requeue_interrupted_jobs` (Task 5), matching the spec's §10 step-0/3 image-pinning requirement.
