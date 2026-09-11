# Console Live Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax. Implementers never commit, push, or dispatch subagents; the parent performs git/bus work and dispatches independent reviews.

**Goal:** Shared, audited live point/broker/mapping edits for future reviews, with the requested console refinements.

**Architecture:** Immutable configuration revisions reference the existing content-addressed policy store and include complete broker snapshots and destination maps. A transactional active pointer selects a revision at run admission; workers consume that creation revision. The console uses authenticated server saves and never promotes a stored browser preview automatically.

**Tech Stack:** Existing Python, PostgreSQL 16, SQLAlchemy, FastAPI/Pydantic, embedded HTML/CSS/JavaScript, pytest, existing Playwright check scripts. No new service or runtime dependency.

**Spec:** `.agents/superpowers/specs/2026-09-11-console-live-configuration-design.md` (approved including section 8 reorder).

## Global Constraints

- Work in `/Users/lgiordano/ipv4-auto-kyc`, current shared branch; parent owns all commits/pushes/bus writes.
- Preserve `KYC_Tool_Build_Package/`, every migration 013-023 byte, M2 behavior, callback wire schema, manual record-only semantics, and existing uncertain-send handling.
- New 024 is configuration; pending 024-028 become 025-029. Platform activation remains unbuilt/fail-closed; move only its current reservation references, not historical records.
- Saving does not recalculate or rewrite old decisions. Newly created runs, including new runs for existing companies, use the saved revision; existing runs and event replays keep their creation version.
- Salesforce mappings edit destination names only. The service does not write Salesforce.
- Exact integers 0-1000 for points, fixed threshold/gates/evidence rules. Mapping names 1-80 ASCII identifier characters, case-insensitively unique.
- Broker limits: 200 entries; names 1-200 characters; 100 entries per identifier class; identifiers 1-256 characters; notes <=2000 characters. Aggregate request bound 32 MiB. Blocked precedence, exact matching, no list truncation.
- New configuration mutations require a nonempty configured admin token in every environment. No credential logging or mutation of app settings to pass a constructor.
- Schema alone does not enable configuration. Activation is explicit and refuses legacy runnable/dead work. No invented historical snapshots.
- Read/keep current UI identity and tokens. No legends; primary solid, secondary outlined/ghost; shared header baseline and spacing; no preview claims after server activation.
- No global format pass. Format only changed/new code, then lint. Parent re-pins engine source hash in every source commit, never ENGINE_BUILD_ID.
- Tests must exercise real behavior and prove RED before changes. DB tests use isolated PostgreSQL; browser event sends are intercepted and admin-save tests use disposable fixtures.
- Test database for this execution: `KYC_TEST_DATABASE_URL=postgresql+psycopg://kyc@127.0.0.1:55699/kyc_test`; use `no_proxy='*'`. Only one test suite using shared reset fixtures runs at once.
- Node dependencies: `NODE_PATH=/Users/lgiordano/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules`.
- No UI captures while `console.html` is being edited: devproxy reloads on changes. Do not stop the displayed local server until controlled final activation.

## Task 1: Schema and migration ownership

**Files:** create `alembic/versions/024_live_configuration.py`; modify `src/kyc_tool/db/tables.py`, `.agents/ROADMAP.md`, current pending `.agents/superpowers/specs/*` and plans, `docs/contracts/wire.py`, `tests/roadmap.py`, affected migration/document contract tests, `tests/conftest.py`; add `tests/integration/test_migration_024.py`. Current operator docs referenced by the moved reservations may be amended; historical records and frozen files are not.

**Interfaces produced:**

```python
# ORM models, integer DB keys serialized to decimal strings at HTTP boundaries.
ConfigurationRevision  # id, schema_version=1, parent_revision nullable FK,
                       # policy_bundle_hash FK, brokers_json list, mappings_json dict,
                       # created_at, created_by, change_kind
ConfigurationState     # id=1, active_revision FK
ConfigurationRequest   # request_id UUID PK, request_digest, result_revision FK,
                       # changed bool, created_at
# Run additions:
Run.configuration_revision  # nullable BIGINT for pre-activation history
Run.matched_broker_entity_id # nullable text, belongs to pinned JSON snapshot
Run.matched_identifier_class # nullable closed match-class text
```

- [ ] Write a migration regression that upgrades 023 -> head and inspects all new columns/FKs/CHECKs/immutability triggers, and asserts pending activation has not been created.

```python
def test_configuration_revision_is_immutable(migrated_session):
    revision = insert_valid_configuration_revision(migrated_session)
    with pytest.raises(DBAPIError):
        migrated_session.execute(text(
            "UPDATE configuration_revisions SET brokers_json='[]' WHERE id=:id"
        ), {"id": revision})
```

Use a local test fixture that inserts a real stored policy bundle and valid configuration; no production test helper. Prove UPDATE and DELETE refuse, an invalid active reference refuses, and a mismatched run configuration/bundle pair refuses.

- [ ] Run the targeted new test and capture the missing-schema RED, plus the existing lineage guard.
- [ ] Implement the migration and ORM. Use BIGINT generated identities for revisions; exact CHECKs on schema version, change kind, JSON types, digest format, positive IDs, singleton ID, and match-class/null pairing. Add UNIQUE `(id, policy_bundle_hash)` and a composite FK from run `(configuration_revision, policy_bundle_hash)`; nullable old rows remain honest. DB triggers enforce revision/request immutability and run configuration-pin immutability. An unchanged run update must remain legal. No active row seeded by schema installation.

```sql
CREATE TABLE configuration_state (
 id integer PRIMARY KEY CHECK (id=1),
 active_revision bigint NOT NULL REFERENCES configuration_revisions(id)
);
-- Revisions are append-only; state is the only mutable pointer.
-- Downgrade locks state/revision/run authorities against concurrent writers,
-- refuses after any active baseline/edit/versioned run, otherwise removes unused additions.
```

- [ ] Reorder current reservations: new owner `PR Console Configuration` shipped 024; activation 025, revalidation 026, queue 027, evidence 028, remaining PR10 029. Preserve shipped owners 008-023; append 024. Remove broker snapshot responsibility from PR10 detail and name the new owner, keeping all other debts there.
- [ ] Rebind current activation-number guards to its new owner/025, keeping wire-field absence and all missing-capability barriers intact. Update meaningful renumber-dependent tests rather than blanket replacing every 024 string. Current frozen-023 tests still target 023; head tests target head dynamically.
- [ ] Add the new tables to test isolation resets. Run migration tests, lineage/static/document authority selectors, then report RED/GREEN and exact files. Parent verifies, reviews, re-pins, and commits this task; do not publish/edit 024 again after its source commit.

## Task 2: Configuration service, validation, and activation operations

**Files:** create `src/kyc_tool/configuration/{__init__,models,repo}.py`, `src/kyc_tool/ops/activate_live_configuration.py`, `tests/unit/test_configuration_models.py`, `tests/integration/test_configuration_repo.py`, `tests/integration/test_configuration_activation.py`; modify mapping projection only for a pure rename helper if needed. Consume Task1 tables; do not change console or event pipeline yet.

**Interfaces:**

```python
@dataclass(frozen=True)
class ResolvedConfiguration:
    revision: int
    bundle: PolicyBundle
    brokers: tuple  # immutable normalized records, not live ORM rows
    mappings: dict  # returned copies / immutable owned data

get_active(session, *, lock=False) -> ResolvedConfiguration | None
get_revision(session, revision: int) -> ResolvedConfiguration
save_section(session, *, section: str, expected_revision: int,
             request_id: UUID, value: dict | list, actor: str) -> dict
bootstrap(session, *, policy: PolicyBundle, actor: str) -> ResolvedConfiguration
# save result keys: revision(str), current_revision(str), changed(bool), replayed(bool).
# Errors: ConfigurationUnavailable, ConfigurationConflict,
#         ConfigurationRequestConflict, ConfigurationInvalid.
```

- [ ] Tests first: strict bool/float/string/extra-key rejection; legal zero/max weights; mapping uniqueness; broker limits, stable ID/notes preservation, bad policy refusal. Validate a complete candidate against the previous bundle rather than accepting arbitrary policy metadata.

```python
@pytest.mark.parametrize('bad', [True, 1.5, '25', -1, 1001])
def test_points_refuse_non_domain_values(policy, bad):
    points = {item.check_type: item.points for item in policy.rubric.items}
    points[policy.rubric.items[0].check_type] = bad
    with pytest.raises(ConfigurationInvalid):
        validate_points(points, policy.rubric)
```

- [ ] Repository REDs: two sessions saving same base yield one success/one conflict; snapshot integrity and restart load; request replay exactly once (including no-op); same UUID/different payload refuses; transaction rollback leaves no request/version/pointer change; save preserves untouched sections.
- [ ] Build strict closed input models and immutable resolution. Reuse `policy_store` validation; preserve unknown normative-file keys in derived bytes but permit changes only to requested point fields and their dependent cap/text/version metadata. Read raw stored bundle files, never depend on a policy directory for a DB-resolved bundle. Recompute hashes through `build_bundle`, store/read-back through existing repo.

```python
# Transaction order inside save_section:
# 1. Validate/canonicalize request and calculate digest including section/base/value.
# 2. Lock configuration_state row FOR UPDATE, bounded wait.
# 3. Existing request ID -> compare digest, return its original result + current pointer.
# 4. expected_revision mismatch -> conflict, no write.
# 5. Build/store verified candidate; INSERT revision unless semantic no-op.
# 6. UPDATE pointer; INSERT immutable request receipt + audit; return for caller commit.
```

The first caller that commits wins; never raise after committing and then claim no change. Request replay lookup must occur before stale-base refusal. Save is one caller-owned transaction. Use local `lock_timeout=5s` and `statement_timeout=30s` on ops/save transactions, with stable translated failures.

- [ ] Bootstrap CLI supports read-only preflight default and explicit `--apply --attest-writers-stopped`, requires pinning enabled and nonempty admin configuration, locks relevant admission/job/broker state and refuses pending/running/dead jobs or other retryable legacy runs. It reads complete legacy broker rows including notes, and initial FIELD_SOURCES identity mappings. Read-back integrity before commit. Existing activation returns verified existing config and never resets it.
- [ ] Dry-run does not acquire an unbounded lock or write; apply reruns preflight authoritatively. Startup is not bootstrap. No backfill onto historical runs. Record audit identity as administrative mechanism plus operator label, not proved personal identity.
- [ ] Run new model/repo/activation selectors and existing policy-store tests. Parent review/commit gate.

## Task 3: Live consumers, authenticated routes, and company filtering

**Files:** create `src/kyc_tool/ui/configuration_routes.py`, `tests/integration/test_configuration_api.py`, `tests/integration/test_configuration_runtime.py`, `tests/integration/test_company_filters.py`; modify `events/ingest.py`, `orchestration/{pipeline,broker_gate}.py`, `api/app.py`, `ui/{routes,salesforce_projection}.py`, worker entry points only if necessary, ops requeue integration for unversioned-work refusal. Consume Task2 APIs; do not change M2 or wire schema.

**HTTP contract for Task4:**

```json
{"active":true,"revision":"1","bundle_hash":"sha256","threshold":100,
 "points":{"website_verified":10},"brokers":[],
 "mappings":{"KYC_Status__c":"KYC_Status__c"},"can_edit":true}
```

`points`/`brokers`/`mappings` are complete sets; example is abbreviated. GET unactivated returns `active:false, revision:null, can_edit:false, reason:"Live configuration has not been activated."` plus read-only current values. No secrets. PUT body `{expected_revision:"1", request_id:"uuid", value:<section data>}`; optional operator label is separately audit-labelled. PUT success returns Task2 result and complete refreshed configuration. 409 retains both known revisions; 401/403 unauthorized; 422 validation; 503 unavailable/timeout. JSON error fields `{error:<stable code>, detail:<safe text>}`.

- [ ] Reproduce boundary REDs through real TestClient routes: empty development admin, wrong token, foreign Origin, malformed/oversize body, conflict, lost-response replay; test a separately constructed app reads the committed version.
- [ ] Mount configuration routes only under existing UI enabled gate; preserve existing read auth. Live writes demand exact nonempty admin token then `require_admin`. Check present Origin against request origin; require JSON, stream/read body with pre-decode bound; do not add permissive CORS. Authenticate before reading a large body.
- [ ] At the beginning of ingest's transaction, read active configuration with shared pointer lock before case lock. No active baseline -> existing legacy path. Active -> require pinning mode, override process-supplied policy with resolved configuration, attach run revision/bundle together; cover every caller through central ingest, not per-router conventions. Replay remains bound to its original event/run. Manual record-only path uses active policy provenance without creating a run.
- [ ] Worker `resolve_bundle` validates/uses versioned run config; versioned run + pinning disabled refuses. An active system processing an old unversioned job refuses before side effects. Requeue refuses such legacy jobs too. No active mutable cache.
- [ ] BrokerGate accepts a pinned snapshot as an explicit keyword and returns match provenance through a shared pure match helper; preserve its legacy callable behavior for unactivated runs. Pipeline records matched ID/class and uses the pinned list at every relevant gate site. Snapshot normalization and overlap detection share the same matching semantics.
- [ ] Real runtime proof: admit A under old points/broker list, save, admit B, complete both via genuine Pipeline. Assert A's old rubric/list, B's new ones, and actual decision/callback check summary under unchanged safety hold. Test process reconstruction; don't assert only the settings object changed.
- [ ] Apply active destination mappings to backend projection keys and field-source metadata, preserving canonical sources and values; return mapping revision. Update console policy/overview reads to use current config, and individual company rubric to use the relevant pinned run/decision. Keep published score distinct from current evidence.
- [ ] Companies API supports `filter=all|new|review|approved|buy_locked|rejected`, existing q, validated limit 1-200 and offset >=0. Use one bound SQL predicate shared by count and results. New means no decision records; review uses held/manual-review/pending workflow state, approved includes effective manual approval, not merely raw positive verdict. Return `{cases,total,limit,offset}`. Test beyond-page and empty populations, holds, manual and broken pointer states.
- [ ] Run runtime/API/filter selectors and relevant ingest/UI/queue/broker suites, lint and source pin, independent task review, parent commit.

## Task 4: Live editors and complete console refinement

**Files:** `src/kyc_tool/ui/console.html`, `tests/unit/test_console_static.py`, `scripts/check_console_{layout,previews,composer,first_pass,settings}.cjs`; create `scripts/check_console_live_configuration.cjs`. Update surface brief and DESIGN only after approved visible changes. Backend Task3 is the data authority.

**Interfaces:** use Task3 JSON contract. The hash route `#/composer` remains; all visible names become Company Actions. Existing `#/cases` gains filters/counts, not client-only filtering over a truncated dataset.

- [ ] Read approved spec section6 and current DESIGN/surface. Read Impeccable craft-floor immediately before editing; do not invent a replacement visual identity.
- [ ] Write browser REDs for undefined broker icon, visible legends, header action placement, save/cancel replacing edit/add, actual PUT (not storage write), stale conflicts, and mapped backend values. Use intercepted complete API fixtures for visual checks and disposable actual API/DB fixtures for end-to-end saves.

```javascript
// Intended behavior, adapted to existing runner helpers:
await page.getByRole('button', {name:'Edit Points',exact:true}).click();
await page.locator('[data-point="website_verified"]').fill('30');
await page.getByRole('button', {name:'Save',exact:true}).click();
// Assert one PUT with base revision/request UUID/full validated values,
// wait for committed server response, then assert the read-only 30 value.
// Reload against the server fixture and assert 30 without localStorage drafts.
```

- [ ] Replace preview state with read/edit/saving/conflict/unknown/error states. Existing preview keys are ignored for activation (may be removed from UI storage only when explicit, never submitted). GET on route entry; cancellation restores last server values and sends nothing. While saving freeze fields/action identity; unknown outcomes retain immutable request and offer bounded same-ID retry/status check. HTTP response controls saved state. No silent retry with a fresh UUID.
- [ ] Points and mappings: right-aligned Edit action replaced by Save/Cancel. Broker: header Add Broker replaced by Save/Cancel during one add/edit form; no Apply Entry phase, hidden unapplied changes, Save Preview, or Reset to Live. Stable server IDs. Remove confirmation saves reviewed list with fresh request ID. Search icon exists, compact tools row, filter on complete snapshot, no vague preview wording. Inactive backend offers clear disabled read-only notice, never a fake save.
- [ ] Implement every row of specsection6: remove all legends; primary solid/secondary ghost; scoring tooltip beside heading with no permanent range sentence; quiet credentials/call counts; fingerprint in bottom section; subheader spacing/baseline; main composer actions first; labelled read-only Technical event; identical DocumentType/ObjectReference control grid; mobile-safe manual actions/title; filters/results count.
- [ ] Preserve all existing guided composer events, response handling, unresolved history and manual record-only copy. No network sender changes. Use status pills' own text/help for meaning after legends are gone.
- [ ] Browser matrix: both themes at 390,768,1440 and actual user width. Check all routes, editor states, long data, keyboard, row alignment, count/filter, no page overflow. Capture in one batch only after source is stable, open each image, apply one batch of material corrections, recapture once. Save under `.impeccable/review/console-live-configuration/`.
- [ ] Run all console browser scripts (update former preview checks to new intended behavior, retain safety tests) and static tests. Independent task review then parent commit. Fresh visual finish review is Task5.

## Task 5: Operator contract, independent final proof, and local handoff

**Files:** `docs/{DEPLOYMENT,RUNBOOK,SALESFORCE_MAPPING}.md`, `docs/architecture-decisions.md`, `AUDIT_FINDINGS.md`, `.env.example` only for needed operational guidance, `DESIGN.md`, surface brief; relevant docs-generated contracts/pins if a reviewed section changes; `scripts/devproxy.py` only if needed to restart/activate the user's isolated demo safely. Parent controls local server and bus.

- [ ] Document explicit dry-run/apply activation commands using implemented CLI, stop/attest/preflight order, required token/pinning, complete broker baseline including notes, old dead/retryable blockers, recovery via new versions, and false Salesforce/automatic-approval claims prohibited. Mark the point-cap deviation and pulled-forward broker ownership.
- [ ] Verify docs point to callable shipped command arguments, with real CLI tests that wrong flags/unsafe state refuse. Do not weaken frozen playbook source/projection gates; update typed sources and current pins together when their reviewed section changes.
- [ ] Parent runs exact whole-source gate on isolated PostgreSQL, source-hash guard, lint/import contracts, browser checks, frozen-artifact diff check. Review full unit with a fresh independent highest-capability reviewer; one fix wave and scoped re-review. Record any open findings honestly, not by test count.
- [ ] Spawn fresh Impeccable finish reviewer with approved request/spec, DESIGN/surface, all screenshots and actual-width evidence. Resolve material findings via implementer/reviewer followup. Document built visual decisions without unrelated design-format migration.
- [ ] Before local demo cutover inspect existing process, data location, pending jobs, token availability. Preserve company data. Preflight read-only first; if safe, use controlled stopped-writer activation and restart compatible local processes, then prove live save on a disposable review/company and restore configuration via a new version if a test edit was made. Never display or store admin credentials in committed artifacts. If the demo cannot safely activate, finish code and state the exact blocker instead of presenting read-only controls as live.
- [ ] Parent confirms exact-source CI on PR2, posts RELEASE with commit range, test evidence and any unresolved constraints; do not distribute PDFs or claim platform ordering/M2 completion. Existing untracked files remain untouched.

## Self-review / interface check

Task1 owns schema keys consumed by Task2; Task2 owns repository/result names used by
Task3; Task3 owns HTTP keys used by Task4. Task1 and Task3 share tables/tests only
sequentially. Task1/Task5 may both touch current operator contracts, sequentially.
Task4 edits the console once; browser checks run only after it is stable. Task5
reviews the complete range, not only its documentation diff. Every spec section
has a task above; no task changes the M2 switch or publishes a different callback.

## Implementation record — 2026-09-11

The original checklist above is the approved plan. Tasks 1–5 are implemented; the
bus RELEASE anchors the final source range. Backend commits: a891e09, 98d5332,
29ea482. The final UI/operator changes reuse existing components and add no
runtime dependency.

### Verification and review

- Exact backend 29ea482 CI succeeded on PostgreSQL, including lint/imports:
  https://github.com/lwgiordano/ipv4-auto-kyc/actions/runs/34643712689 .
- Final affected browser checks: 67 passed (live configuration 14, async 8, layout 45).
  Composer 30, settings 24 and first-pass 48 passed before the final narrow correction.
- Final parent smoke: 73 passed (console static, real-loopback proxy, cutover parity,
  engine pin). Ruff clean; import contracts 2 kept/0 broken; frozen migrations 013–024
  and normative package unchanged after 024 publication. ENGINE_BUILD_ID unchanged.
- Fresh complete-unit code/visual review inspected all 20 current captures and found
  three defects: request identity lost after an unauthorized uncertain-save retry;
  mobile broker fieldset overflow; validation focused the first rather than invalid
  control. One RED-first correction batch closed all three. Scoped independent
  re-review disposition: ship, with no demonstrated new breakage. The four affected
  phone images were recaptured in both themes; page width 390 equals viewport 390.
- Fresh documentation review accepted the ordinary DESIGN/surface update without
  format migration, new sidecars, or weakened recovery claims.
- A duplicate local backend rerun was deliberately interrupted after 339 passed;
  it is not whole-suite completion evidence. Existing exact-source CI plus the
  finished UI/proxy checks replace that duplicate work under the user's Ponytail
  direction. Final finishing-commit CI status is recorded on the bus.

### Local activation and retained boundaries

The existing database was backed up and migrated 023→024 with stopped application
writers. Read-only preflight reported no blockers; explicit activation created
revision 1. Actual API requests proved authenticated point change, same-request
replay, restoration through a new revision, mapping/broker no-op saves, and
unauthenticated refusal. An actual browser-Origin request through the local proxy
also saved successfully. Final setup revision 3 retains the original configured
values; 4 companies, 11 runs and 11 jobs were preserved. No company action was sent.

The local app uses a generated private operator credential entered through Options;
the browser retains it only in page memory. API/worker/proxy were replaced without
recreating PostgreSQL. The old destructive auto-restart watcher remains paused:
do not resume it unchanged or use its restart to preserve this temporary database.
A data-preserving operational restart is separate from launching a fresh ephemeral
demo. Local credential contents are neither embedded nor committed.

Point/broker edits apply to new review runs; old runs retain their version.
Destination-name saves change service projections, not Salesforce records.
Positive enforcement, callback wire behavior and manual record-only semantics remain
unchanged. Platform activation 025 and PDFs remain held. The pre-existing send-event
retry/replay backend contract and existing dependency warnings are not declared fixed.

### Rulings I made

1. operate in the existing shared checkout — required by project bus workflow and current live UI; no new worktree. Cost if wrong: changes share the branch, mitigated by exclusive claims and parent-only commits.

2. shipped script execution permissions prevent task-brief/review-package helper use — extract equivalent files with apply_patch from the exact plan/diff instead of altering installed plugin permissions. Cost if wrong: extraction must be checked for complete task/global text.

3. accept valid nonblank broker IDs as opaque strings, including surrounding whitespace, while normalizing only matchable identifiers. Cost if wrong: display/copy must preserve an unusual ID rather than prettifying it; avoids silently severing legacy identity.

4. omit the planned new disposable browser-server helper unless an existing route proves insufficient; reuse actual backend TestClient/PostgreSQL proofs, one existing-pattern browser integration runner, and final authenticated local round-trip. Why: user requested shortest working implementation; redundant fixture infrastructure delays visible changes. Cost if wrong: fewer duplicate end-to-end environments; final live round-trip remains required before claiming activation.

5. ambiguous save recovery uses explicit operator-triggered identical-request retries, one request in flight and a bounded network timeout, without an arbitrary permanent three-click cap. GET matching values never proves receipt; retain request identity and warn before leaving. Cost if wrong: an operator can make repeated manual attempts, but each is separately bounded and server-idempotent; avoids stranding a recoverable save after a cosmetic retry count.

6. when Salesforce projected values and editor configuration report different mapping revisions, withhold the mismatched values and offer refresh while preserving draft/focus. Cost if wrong: a concurrent unrelated configuration save can temporarily hide values even if names are unchanged; no values are silently paired with the wrong destination.

7. Companies Approved includes sticky approved_manual when its same-case manual pointer resolves, even when a later automatic verdict is held; Buying locked is an overlapping subset of effective approved with current buying lock. New excludes set-but-invalid decision pointers even with no decision rows, because that state is integrity drift. Why: preserve _project_case/manual and shared provenance authority rather than classify from a single newest verdict. Cost if wrong: filter categories are intentionally not a disjoint partition; UI help must distinguish approval from buying capability.

8. add a compact Operator access credential form to Options as necessary live-editor plumbing; current console has no bearer entry or forwarding. Memory-only current-page token, same-origin console mutations only, no browser persistence or embedded secret. Cost if wrong: operators must re-enter after reload; avoids making new live saves unauthenticated or breaking existing protected console actions after a token is configured.

9. Task1 downgrade may fail fast with a stable busy sentinel on lock contention instead of waiting into a lock cycle; this satisfies bounded fail-closed maintenance and avoids expanding every application writer into a new advisory protocol. Cost if wrong: operator retries a busy maintenance step instead of waiting automatically; no history is deleted.

10. refusal inventory excludes only bare Python re-raises (Raise.exc is None), which propagate an existing error instead of authoring a refusal. Constructed exceptions inside except blocks still count and receive a regression. Cost if wrong: inherited errors are not independently listed at every propagation point; their original authored raises remain governed.

11. task1 new migration is reviewed from a complete staged diff before the first source commit. This prevents the frozen-on-publication discipline from forcing a repair migration for findings that can be fixed before publishing. Review package includes all new files and base 2323f49; review remains read-only.

12. absent active pointer with recorded configuration history is corruption/unavailable, not pre-activation legacy mode; Task2 handles it and Task3 includes readiness checks explicitly required by spec section7 but omitted from the plan's bullets. JSON configuration writes reject duplicate keys before model validation. Cost if wrong: stricter refusal requires repair instead of permissive fallback; preserves the approved no-fabricated-history contract.

Follow-through on ruling 2: the final committed-backend review package could use the
shipped helper via bash with an explicit output path; the uncommitted finishing
diff was captured separately without truncation. No installed permissions changed.
