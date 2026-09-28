# Public Salesforce Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide TechCraft a stable, authenticated, read-only `GET /v1/cases/{case_id}/salesforce-projection` contract that exposes the current Salesforce mirror projection without depending on `/ui/api` or reading or writing Salesforce.

**Architecture:** Keep `project_salesforce_fields` as the sole value projector. Add a public-API snapshot assembler which opens one read-only repeatable-read PostgreSQL transaction, resolves the current configuration mapping and all case inputs from that same snapshot, and wraps every destination-keyed value in a typed source-metadata envelope. The route is mounted with the existing read router and imports the pure projector module only; it must not import the UI router or require `ui_enabled`.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL, pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-production-readiness-design.md` (Unit 2); `KYC_Tool_Build_Package/05_SALESFORCE_SYNC.md`; `KYC_Tool_Build_Package/machine_readable/salesforce_sync_fields.json`.

## Global Constraints

- The platform remains the hub; it enforces decisions and owns the accepted-run ledger.
- Salesforce is a one-way platform-owned mirror. The KYC tool exposes a projection and never reads or writes Salesforce records.
- The endpoint is read-only. It does not change decisions, callback bodies, old runs, configuration, checks, or Salesforce records.
- Checks remain immutable and supersedable; `KYC_Check__c` represents every current projector check row, including superseded rows.
- Existing runs remain bound to the configuration and policy bundle that created them. A later destination-mapping save affects only later projection reads.
- Existing authentication, authorization, evidence-containment, and audit controls remain in force. The public route uses `require_read_access` before opening a database session.
- Every source-touching commit includes the finished-tree `EXPECTED_ENGINE_SOURCE_HASH` update in `tests/policy_driven/test_engine_build_id_guard.py`; do not change `ENGINE_BUILD_ID`. Run scoped `.venv/bin/ruff format` on only the task's touched source/test files before each such re-pin. Never run `./manage.sh fmt`; the parent is the sole committer.
- The frozen normative package and frozen migration revisions remain unmodified.
- Do not infer a platform-enforced decision from a tool decision row. Migration 025 platform ordering/accepted-run reconciliation is not part of this endpoint.
- Do not add a Salesforce client, child-object mapping editor, child-field mapping settings, UI dependency, migration, or configuration schema change. Current settings map only the existing top-level source keys, including the single `KYC_Check__c` collection key.

---

## Contract fixed by this plan

The successful response is a typed JSON object with this shape. Revisions are decimal strings to match the existing configuration API; `null` means the corresponding authority does not exist or cannot honestly be resolved.

```json
{
  "case_id": "platform-case-123",
  "mapping_revision": "24",
  "configuration_revision": "21",
  "decision_authority": {
    "provenance": "latest_decision_row",
    "decision_row_id": "decision-123",
    "run_id": "run-123",
    "decision_kind": "automatic",
    "decision": "approve",
    "run_provenance": "resolved"
  },
  "manual_approval_authority": {
    "provenance": "no_manual_decisions",
    "decision_row_id": null,
    "reviewer_id": null,
    "decided_at": null
  },
  "fields": {
    "KYC_Status__c": {
      "source_field": "KYC_Status__c",
      "source_identity": "case.status",
      "value_type": "enum",
      "nullable": true,
      "value": "Account Approved"
    }
  },
  "projection_timestamp": "2026-09-12T12:00:00+00:00"
}
```

`mapping_revision` is the active immutable configuration snapshot from which the effective destination map was read. It is not a history lookup for the most recent row whose `change_kind` happened to be `mappings`; the current storage model has one active configuration pointer and each revision contains a complete mapping snapshot. When there is no activated configuration, return `null` and use identity destinations exactly as the current console does.

`configuration_revision` is deliberately different: it is the pointed automatic decision row's own `runs.configuration_revision`, not the current mapping revision. It is `null` for a manual decision, no decision, unresolved decision authority, an unpinned legacy run, or an unresolved run. The response must never substitute the active configuration revision for it.

`decision_authority` names only the tool-local, trigger-maintained decision pointer. Its complete provenance/run matrix is:

| Decision provenance | Decision kind and identity | Run provenance | `configuration_revision` |
| --- | --- | --- | --- |
| `latest_decision_row` with `manual=false` | `automatic`; row id, decision, and run id are present | `resolved` when the same-case run resolves; otherwise `unresolved` | resolved run's pinned revision, or `null` when the run is unpinned/unresolved |
| `latest_decision_row` with `manual=true` | `manual`; row id and decision are present; run id is `null` | `not_applicable` | `null` |
| `unresolved_pointer_drift`, `unresolved_legacy_order`, or `no_decisions` | all identity and decision fields are `null`, including `decision_kind` | `not_applicable` | `null` |

This is not a statement that TechCraft accepted, ordered, or enforced that decision.

`manual_approval_authority` independently preserves the existing sticky-manual pointer semantics needed by the projector. Its provenance is exactly `latest_manual_row`, `unresolved_pointer_drift`, `unresolved_legacy_order`, or `no_manual_decisions`. It never guesses by `decided_at` or UUID sorting. A later automatic decision may move `decision_authority` while this object continues to identify the manual approval that supplies `Manual_Approved_By__c` and `Manual_Approved_At__c`.

Each `fields` dictionary key is the saved destination name. Each field body's `source_field` is the unchanged canonical source key, so changing a destination never changes its source identity. `source_identity`, `value_type`, and `nullable` are fixed contract metadata, not UI prose. `KYC_Check__c` is a typed list at its one mapped top-level destination; its child properties retain the existing canonical names and are not independently configurable.

| Source field | Source identity | Value type and actual current value domain | Nullable |
| --- | --- | --- | --- |
| `KYC_Status__c` | `case.status` | enum: `Registered`, `Email Verification Pending`, `Email Verified`, `Enrichment Running`, `KYC Pending`, `Manual Review - Insufficient Score`, `Account Approved`, `Rejected` | yes |
| `KYC_Score__c` | `decision.score` | integer | yes |
| `Buy_Enablement_Status__c` | `case.buy_status` | enum: `Not Applicable`, `Buy Locked - ORG-ID Required`, `ORG-ID Validation Pending`, `ORG-ID Failed`, `Buy Enabled`, `Buy Suspended` | yes |
| `Platform_Action_Taken__c` | `tool.decision_or_manual_approval` | enum: `Approve Account`, `Approve Account - Buy Locked`, `Reject`, `Manual Approve` | yes |
| `ORG_ID__c` | `org_id_match.handle_or_submission` | text | yes |
| `ORG_ID_Status__c` | `org_id_match.status` | enum: `Pending`, `Pass`, `Fail`, `Superseded` | no |
| `POC_Handle__c` | `poc_verified.handle_or_submission` | text | yes |
| `POC_Verification_Status__c` | `poc_verified.status_or_token` | enum: `Pending`, `Token Sent`, `Verified`, `Failed`, `Superseded` | no |
| `Business_Document_Status__c` | `business_document_verified.status_or_submission` | enum: `None`, `Uploaded`, `Verified`, `Failed` | no |
| `Website_Review_Status__c` | `website.review_task_or_check` | enum: `Open`, `Pass`, `Fail` | yes |
| `Broker_Status__c` | `case.broker_status` | enum: `Clear`, `Allowed Broker`, `Blocked` | yes |
| `Hard_Conflict__c` | `decision.gates_json.no_hard_conflict` | boolean, negated by the projector | yes |
| `Review_Reason_Codes__c` | `checks.live.reason_codes` | semicolon-delimited text | yes |
| `Manual_Approved_By__c` | `manual_decision.reviewer_id` | text | yes |
| `Manual_Approved_At__c` | `manual_decision.decided_at` | RFC 3339 datetime | yes |
| `KYC_Check__c` | `checks.all` | list of typed `KYC_Check__c` records | no |

Each `KYC_Check__c` record is `{Check_Type__c: str, Status__c: "pass" | "fail" | "needs_review", Points__c: int, Category__c: str, Source__c: str, Superseded__c: bool, Reason_Codes__c: str, Created_At__c: datetime}`. These fields are non-null because migration `003_checks_decisions.py` makes `check_type`, `status`, `points_awarded`, `category`, `source`, `reason_codes`, and `created_at` non-null; the assembler must read persisted check rows rather than invent compatibility nulls. This does not manufacture an unsupported child-field mapping surface.

`projection_timestamp` is PostgreSQL's `transaction_timestamp()` for the repeatable-read snapshot, serialized as RFC 3339 UTC. This first endpoint slice always returns the complete authenticated `200` representation; it does not emit an `ETag`, interpret `If-None-Match`, or return `304`. Conditional caching is a separate follow-on design after this endpoint has its projection/snapshot contract in use.

For every required re-pin, compute the digest after scoped formatting with:

```bash
.venv/bin/python - <<'PY'
import hashlib
from pathlib import Path

root = Path("src/kyc_tool")
h = hashlib.sha256()
for path in sorted(root.rglob("*.py")):
    if "__pycache__" not in path.parts:
        data = path.read_bytes()
        rel = path.relative_to(root).as_posix().encode()
        h.update(rel + b"\x00" + str(len(data)).encode() + b"\x00" + data)
print(h.hexdigest())
PY
```

Paste that digest into `EXPECTED_ENGINE_SOURCE_HASH`, run `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v`, and stage the guard file in the same source commit.

### Task 1: Define the public response models and canonical field metadata

**Files:**
- Modify: `src/kyc_tool/api/schemas.py`
- Modify: `src/kyc_tool/ui/salesforce_projection.py`
- Modify: `tests/unit/test_salesforce_projection.py`
- Modify: `tests/policy_driven/test_engine_build_id_guard.py`
- Create: `tests/unit/test_salesforce_projection_contract.py`

**Interfaces:**
- Consumes: the existing `project_salesforce_fields` return dictionary and canonical source-key set from `FIELD_SOURCES`.
- Produces: one `SALESFORCE_FIELD_CONTRACT` authority plus `SalesforceProjectionResponse`, `SalesforceProjectionField`, `KycCheckChildRecord`, `ToolDecisionAuthority`, and `ManualApprovalAuthority` for the public route.

- [ ] **Step 1: Write failing metadata and schema tests**

Add tests that make the response contract executable rather than a prose-only description.

```python
def test_public_field_contract_covers_the_existing_projector_keys():
    assert set(SALESFORCE_FIELD_CONTRACT) == set(FIELD_SOURCES)
    assert SALESFORCE_FIELD_CONTRACT["KYC_Score__c"].source_identity == "decision.score"
    assert SALESFORCE_FIELD_CONTRACT["Hard_Conflict__c"].nullable is True
    assert SALESFORCE_FIELD_CONTRACT["KYC_Check__c"].value_type == "check_records"


def test_public_response_rejects_unknown_field_metadata_and_bad_domain(valid_response_dict):
    candidate = valid_response_dict()
    candidate["fields"]["KYC_Status__c"]["value"] = "Approved Somehow"
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)
```

Define `valid_response_dict` in this test file as one complete valid response fixture covering every source key. Also assert the exact hyphenated current strings (`Manual Review - Insufficient Score` and `Approve Account - Buy Locked`), that `Hard_Conflict__c` permits `null`, that `Manual_Approved_At__c` accepts the projector's `datetime`, that an integer value refuses `True`, and that an unrecognized metadata member is rejected by `extra="forbid"`.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/unit/test_salesforce_projection_contract.py tests/unit/test_salesforce_projection.py -v`

Expected: the selector initially fails because the field contract and public response models do not yet exist.

- [ ] **Step 3: Add one canonical metadata authority and strict response models**

Keep the human-facing `FIELD_SOURCES` unchanged for the console. In `ui/salesforce_projection.py`, add one immutable `SALESFORCE_FIELD_CONTRACT` declaration keyed by the existing canonical source fields. Each entry contains `source_identity`, `value_type`, `nullable`, and the allowed enum values where applicable. This is the sole authority for the table above and for response validation; do not restate those domains in a parallel set of Pydantic `Literal` classes.

```python
class SalesforceProjectionField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_field: str
    source_identity: str
    value_type: Literal["enum", "integer", "text", "boolean", "datetime", "check_records"]
    nullable: bool
    value: StrictStr | StrictInt | StrictBool | datetime | list[KycCheckChildRecord] | None


class KycCheckChildRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    Check_Type__c: StrictStr
    Status__c: Literal["pass", "fail", "needs_review"]
    Points__c: StrictInt
    Category__c: StrictStr
    Source__c: StrictStr
    Superseded__c: bool
    Reason_Codes__c: str
    Created_At__c: datetime
```

Keep `ui/salesforce_projection.py` one-way and pure: it defines only raw `SALESFORCE_FIELD_CONTRACT` metadata made of standard-library values, and it never imports `kyc_tool.api.schemas` or a Pydantic child model. In `api/schemas.py`, implement `validate_projected_value(source_field, value)` against that metadata and the model-owned `KycCheckChildRecord` type before response-model construction. It checks exact scalar shape: text is exact `str`, integer is exact `int` excluding `bool`, boolean is exact `bool`, datetime is `datetime`, and check records are `list[KycCheckChildRecord]`. The `SalesforceProjectionField` model validator consumes the same metadata to require identity/type/nullability, enum values, non-nullability, and list/scalar shape. `fields` remains `dict[str, SalesforceProjectionField]`; a response-level validator requires unique source fields and exact coverage of the contract key set. This prevents an API-to-UI import cycle and prevents malformed destination mappings from silently dropping a source.

Define the authority objects with literal provenance domains from `domain.provenance`, an explicit `decision_kind`, and no `enforced`, `accepted`, or ordering field. `configuration_revision` and `mapping_revision` are `str | None`; timestamp-bearing values are `datetime | None` so FastAPI generates RFC 3339 OpenAPI schemas.

- [ ] **Step 4: Run the focused model and projector tests**

Run: `pytest tests/unit/test_salesforce_projection_contract.py tests/unit/test_salesforce_projection.py -v`

Expected: the new contract tests and existing projector tests are green after the implementation.

- [ ] **Step 5: Commit the contract-model slice**

```bash
git add src/kyc_tool/api/schemas.py src/kyc_tool/ui/salesforce_projection.py \
  tests/unit/test_salesforce_projection.py tests/unit/test_salesforce_projection_contract.py \
  tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat: define public salesforce projection contract"
```

Before this source commit, run scoped formatting only (`.venv/bin/ruff format` on the four files above), then re-pin `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py` from the finished `src/kyc_tool` tree and run `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v`. Do not run `./manage.sh fmt`; the parent remains the sole committer.

### Task 2: Build the UI-independent, consistent projection snapshot

**Files:**
- Create: `src/kyc_tool/api/salesforce_projection.py`
- Modify: `src/kyc_tool/api/routes_read.py`
- Create: `tests/integration/test_salesforce_projection_api.py`
- Modify: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Consumes: `SALESFORCE_FIELD_CONTRACT`, `SalesforceProjectionResponse`, `project_salesforce_fields`, `configuration.repo.get_active`, `Case`, `DecisionRow`, `Run`, `Check`, `ReviewTask`, `PocToken`, and `provenance.classify`.
- Produces: `build_salesforce_projection(session_factory, case_id) -> SalesforceProjectionResponse` and `GET /v1/cases/{case_id}/salesforce-projection` with status `200` or `404`/`503`.

- [ ] **Step 1: Write failing integration tests for the core read contract**

Create `tests/integration/test_salesforce_projection_api.py`, marked `pytest.mark.usefixtures("clean_db")`. Import the existing `baseline` helper from `tests.integration.test_configuration_repo`, the existing `save_section(sf, section, value)` helper from `tests.integration.test_configuration_runtime`, and the existing `make_client` helper from `tests.integration.test_configuration_api`.

Import `create_app`, `TestClient`, `json`, `uuid4`, `Pipeline`, `Worker`, `FsStore`, `ProcessRole`, `process_context`, `envelope`, and `sign_headers` in the new integration test. Define a local `_pinned_stack(settings, session_factory, policy, tmp_path)` helper which makes one `pinned_settings = settings.model_copy(update={"enforce_bundle_pinning": True})`, then constructs the app/client, `Pipeline`, and `Worker` from that same settings object. It also returns a local signed `post` function bound to that same client:

```python
def _pinned_stack(settings, session_factory, policy, tmp_path):
    pinned_settings = settings.model_copy(update={"enforce_bundle_pinning": True})
    client = TestClient(create_app(pinned_settings, session_factory=session_factory, policy=policy))
    pipeline = Pipeline(
        session_factory, policy, FsStore(tmp_path / "evidence"), pinned_settings, adapters={}
    )
    worker = Worker(
        session_factory, {"run_transition": pipeline.handle_job},
        lease_seconds=pinned_settings.job_lease_seconds, backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
        process_role=process_context(ProcessRole.PIPELINE_WORKER, pinned_settings),
        settings=pinned_settings,
    )
    def post(case_id, event_type, payload, actor=None):
        body = json.dumps(envelope(event_type, payload, actor)).encode()
        return client.post(
            f"/v1/cases/{case_id}/events", content=body,
            headers=sign_headers(body, key=uuid4().hex),
        )
    return client, worker, post
```

Do not use the default `worker` or `post_event` fixtures for activated configuration tests: both are bound to the ordinary settings/client, while the pinned stack must use one app, event poster, pipeline, and worker configuration.

Write these concrete tests using that pinned stack: `test_public_projection_is_mounted_without_the_ui_and_uses_identity_mapping` creates `Case(id="c1")`, makes an app with `ui_enabled=False`, then will assert `200`, null `mapping_revision`, and the identity destination/source pair. `test_public_projection_uses_one_pointed_automatic_row_and_its_run_pin` runs a normal event after `baseline`, records the decision's run pin, calls the imported three-argument `save_section` to rename `KYC_Status__c`, then will assert automatic pointer provenance, the old run's configuration revision, and a different current mapping revision. `test_unresolved_decision_authority_projects_null_decision_values` seeds the existing pointer-null legacy condition used by `tests/integration/test_read_latest_decision.py` and will assert provenance, score, action, hard conflict, and `run_provenance="not_applicable"`. `test_decision_authority_matrix` will cover resolved automatic/run-resolved, resolved manual/not-applicable, null-pointer legacy/not-applicable, and set-but-unresolvable-pointer/not-applicable shapes from the matrix above; the response model test will cover the resolved-automatic/run-unresolved compatibility shape.

- [ ] **Step 2: Run the core integration tests to verify they fail**

Run: `pytest tests/integration/test_salesforce_projection_api.py -v`

Expected: the selector initially fails with `404` because the route and snapshot builder do not exist.

- [ ] **Step 3: Implement one snapshot builder and delegate the route to it**

In `src/kyc_tool/api/salesforce_projection.py`, implement one small `build_salesforce_projection(session_factory, case_id) -> SalesforceProjectionResponse` function. It begins a session transaction with `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`, takes `transaction_timestamp()`, and completes every configuration, case, decision, run, check, review-task, and POC-token read before returning. It must call `project_salesforce_fields` once with those values; it must not copy projector value rules, import `kyc_tool.ui.routes`, read `/ui/api`, or call a Salesforce client.

Use `select(DecisionRow).where(DecisionRow.id == pointer, DecisionRow.case_id == case_id)` for the main pointer. If the pointer is null, query only whether a same-case decision row exists and pass that result to `provenance.classify`; never select a row by `decided_at`, `id`, or display order. Resolve the manual pointer with `DecisionRow.manual.is_(True)` in the same hardened manner. Resolve an automatic decision's `Run` by both `id` and `case_id`; if that lookup fails, keep the decision-row authority but return `run_provenance="unresolved"` and `configuration_revision=None`.

Resolve the active configuration through `configuration.repo.get_active(session)`. If it returns `None`, use `mappings=None` (identity map) and `mapping_revision=None`. If it raises `ConfigurationUnavailable`, translate it to the same `503` safe-retry response used for unavailable configuration elsewhere; do not fall back to a partial or stale mapping.

Collect all checks in deterministic `created_at, id` order; collect open task types; determine an outstanding POC token using the exact existing rule (`verified_at is None` and `expired_at is None or expired_at > projection_timestamp`); then call:

```python
project_salesforce_fields(
    case=case_values,
    checks=check_values,
    open_task_types=open_task_types,
    poc_token_outstanding=poc_token_outstanding,
    latest_decision=pointed_decision_values,
    latest_manual_decision=pointed_manual_values,
    mappings=active.mappings if active else None,
)
```

Wrap each returned destination value with metadata looked up by its canonical source key. Emit the projection timestamp returned by PostgreSQL, not a second application-clock call. In `routes_read.py`, authenticate first and simply delegate:

```python
@router.get("/v1/cases/{case_id}/salesforce-projection", response_model=SalesforceProjectionResponse)
def get_salesforce_projection(case_id: str, request: Request):
    require_read_access(request.app.state.settings, request)
    return build_salesforce_projection(request.app.state.session_factory, case_id)
```

Return `HTTPException(404, "case not found")` only after successful authorization and snapshot lookup. Do not alter the existing `/v1/cases/{case_id}`, `/checks`, or UI endpoints in this task.

- [ ] **Step 4: Run the core endpoint tests and existing decision-pointer tests**

Run: `pytest tests/integration/test_salesforce_projection_api.py tests/integration/test_read_latest_decision.py tests/unit/test_salesforce_projection.py -v`

Expected: the endpoint tests and existing decision-pointer tests are green; the legacy-case assertion rejects a timestamp-order fallback.

- [ ] **Step 5: Commit the snapshot-read slice**

```bash
git add src/kyc_tool/api/salesforce_projection.py src/kyc_tool/api/routes_read.py \
  tests/integration/test_salesforce_projection_api.py tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat: add public salesforce projection read"
```

Before this source commit, run `.venv/bin/ruff format` only on the two API modules and the integration test, re-pin `EXPECTED_ENGINE_SOURCE_HASH` in the listed guard file, then run its targeted pytest selector. Do not run `./manage.sh fmt`; the parent remains the sole committer.

### Task 3: Prove mapping adoption, manual provenance, snapshot consistency, and authentication

**Files:**
- Modify: `tests/integration/test_salesforce_projection_api.py`
- Modify: `tests/unit/test_ops_auth.py`

**Interfaces:**
- Consumes: the Task 2 snapshot response and current configuration save helper (`save_section`).
- Produces: focused acceptance evidence for the authenticated normal `200` response. Conditional caching is explicitly deferred to a separately designed follow-on.

- [ ] **Step 1: Add failing acceptance tests for every endpoint boundary**

Add these concrete tests to `tests/integration/test_salesforce_projection_api.py`:

- `test_mapping_save_changes_the_next_destination_key_not_old_decision_or_callback_bytes`: capture the `decisions` row fields and the `outbox.payload_json` value with existing SQLAlchemy `text` selects; after the imported `save_section(session_factory, "mappings", value)` succeeds, assert the destination key and `mapping_revision` changed while those captured database values are unchanged.
- `test_manual_projection_has_explicit_tool_provenance_without_platform_enforcement_claim`: create the case through the pinned stack's `post(case_id, "reviewer.manual_approve", {"reviewer_id": "rev-1"}, actor={"type": "reviewer", "id": "rev-1"})`; assert `decision_kind="manual"`, null run/configuration revision, `run_provenance="not_applicable"`, manual-pointer provenance, `Manual Approve`, and a null hard-conflict value. Assert the authority object has no `enforced`, `accepted`, or sequence key.
- `test_sticky_manual_projection_survives_a_later_automatic_decision`: using the same pinned stack, run the existing normal-event → manual-approve → later automatic-event sequence. It will assert that `decision_authority` identifies the automatic row/run while `manual_approval_authority` identifies the manual row, and that the projection still returns `Manual Approve`, the manual reviewer, and the manual timestamp.
- `test_projection_snapshot_never_mixes_a_mapping_revision_with_later_mapping_values`: use SQLAlchemy's `after_cursor_execute` event on the first `configuration_state` pointer read. The callback sets an `armed`/reentrancy boolean to `False` before calling the imported three-argument `save_section` in a second session to rename `KYC_Status__c`; it must not remove its own listener during SQLAlchemy dispatch. Wrap the GET in `try`/`finally` and remove the listener only in the `finally` block after dispatch completes. The in-flight response must have the pre-save revision and `KYC_Status__c`; a following GET must have the new revision and destination.
- `test_projection_pointer_drift_is_explicit`: follow the concrete drift setup in `tests/integration/test_read_latest_decision.py::test_read_surfaces_do_not_dereference_cross_case_pointer_if_fk_drifted`: set `engine = session_factory.kw["bind"]`, drop `fk_cases_latest_decision`, create same-case and foreign-case decision rows, then point the first case at the foreign row. Assert the public projection reports `unresolved_pointer_drift` and null decision-derived fields. In `finally`, call the existing `_restore_latest_decision_fk(engine)` helper so the named constraint is restored even on assertion failure.

In `tests/unit/test_ops_auth.py`, extend the existing `test_read_endpoints_reject_unauthenticated_when_required` selector with the new path, proving `read_auth_required=True` returns `401` before the route can invoke the database session factory. Include a signed authenticated request in the integration test so the endpoint demonstrably uses the normal public-read credential rather than UI admin credentials.

- [ ] **Step 2: Run the acceptance selectors to verify failure**

Run: `pytest tests/integration/test_salesforce_projection_api.py tests/unit/test_ops_auth.py -v`

Expected: the new endpoint assertions initially fail until the Task 2 route and snapshot builder exist.

- [ ] **Step 3: Run all targeted tests**

Run: `pytest tests/unit/test_salesforce_projection.py tests/unit/test_salesforce_projection_contract.py tests/unit/test_ops_auth.py tests/integration/test_salesforce_projection_api.py tests/integration/test_read_latest_decision.py -v`

Expected: targeted tests are green and the mapping-race assertion rejects a mixed-snapshot response.

### Task 4: Verify the complete unit without expanding Salesforce scope

**Files:**
- Modify only if verification identifies a defect in one of the files named by Tasks 1–3.

**Interfaces:**
- Consumes: the endpoint, generated FastAPI schema, and the existing projector/tests.
- Produces: evidence that the public API is schema-visible, UI-independent, auth-gated, and read-only.

- [ ] **Step 1: Add an OpenAPI contract assertion**

```python
def test_openapi_exposes_typed_public_salesforce_projection(client):
    path = client.get("/openapi.json").json()["paths"]["/v1/cases/{case_id}/salesforce-projection"]["get"]
    assert path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/SalesforceProjectionResponse"
    )
```

Do not declare `304` or cache headers in this endpoint slice.

- [ ] **Step 2: Run the new OpenAPI test to verify the schema before the full suite**

Run: `pytest tests/integration/test_salesforce_projection_api.py::test_openapi_exposes_typed_public_salesforce_projection -v`

Expected: the OpenAPI selector is green.

- [ ] **Step 3: Run quality and regression gates**

Run: `.venv/bin/ruff check src/kyc_tool/api/schemas.py src/kyc_tool/api/salesforce_projection.py src/kyc_tool/api/routes_read.py src/kyc_tool/ui/salesforce_projection.py tests/unit/test_salesforce_projection.py tests/unit/test_salesforce_projection_contract.py tests/unit/test_ops_auth.py tests/integration/test_salesforce_projection_api.py && .venv/bin/lint-imports`

Expected: scoped Ruff exits `0`; import-linter reports `2 kept, 0 broken`. Do not run `./manage.sh fmt`.

Run: `./manage.sh test`

Expected: exit `0` with PostgreSQL-backed integration tests actually executed. Do not accept a skipped PostgreSQL suite as this unit's acceptance evidence.

- [ ] **Step 4: Inspect the final diff for scope violations**

Run: `git diff --check && git diff -- src/kyc_tool/api src/kyc_tool/ui/salesforce_projection.py tests`

Expected: no modifications to `KYC_Tool_Build_Package/`, migrations, configuration schema, UI routes, Salesforce clients, callback encoders, or decision/outbox writers. The only import from `kyc_tool.ui` is the existing pure `salesforce_projection` module; no API module imports `kyc_tool.ui.routes`.

- [ ] **Step 5: Leave final committing to the parent**

Do not create a broad verification commit. If the verification exposes a defect, return it to the owning task, repeat that task's scoped formatting and guard re-pin, and let the parent make the resulting scoped commit.

## Self-review

- The planned response model will cover case id, local decision row/run identity or explicit unresolved state, current effective mapping revision, pointed run configuration revision, destination-keyed fields, fixed source identities, exact typed value metadata, and snapshot timestamp.
- The planned mapping-save test will verify that the next read changes destination keys while historical decision rows, existing run pins, and callback bytes remain unchanged.
- The planned automatic-after-manual test will verify both pointer paths and sticky manual attribution without claiming platform acceptance or enforcement.
- The planned auth, UI-disabled, 404, and configuration-unavailable tests will verify public-read boundaries independently of the console.
- The planned snapshot-race test will verify that one response cannot combine pre-save and post-save state; `transaction_timestamp()` will identify that snapshot.
- This endpoint slice will use normal authenticated `200` reads only; conditional caching remains a separately designed follow-on and adds no ordering or enforcement claim here.
- The plan will add no child-field mapping editor: the only child mapping remains the pre-existing top-level `KYC_Check__c` destination key.
- The plan names concrete types, route behavior, query rules, files, and selectors for implementation and separate review.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-12-public-salesforce-projection.md`. Execute it task-by-task with independent implementation and review passes, keeping the parent as the sole committer and bus writer.
