# Console Preview Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax. Parent alone commits, pushes and writes the bus; workers do not delegate.

**Goal:** Implement the approved UI refinements, browser-local point/mapping/broker editors, decision detail access and a guided message composer.

**Architecture:** Keep the existing single-file console and read-only APIs. Configuration editors stage and save labelled browser-local previews, never change live policy or broker data. The composer retains the existing send endpoint with explicit review/confirmation and schema-shaped inputs.

**Tech Stack:** Embedded HTML/CSS/JavaScript, native controls, Playwright Node scripts, existing pytest static pins. No new dependencies.

**Spec:** The approved design is recorded below, incorporating the user's "okay go" after preview-first was recommended for all configuration editors. This is the implementation authority; no separate live-activation subsystem is approved.

## Execution disposition — 2026-09-10

Tasks 1 and 2 are implemented and independently reviewed. Task 3's guided form,
immutable review/send, company retention, and conservative outcome handling are implemented.
The original robust retry/replay requirement below is **not complete**: the unchanged console
endpoint creates a fresh `occurred_at` on each attempt, while ingestion hashes that timestamp.
A committed request whose response is lost can therefore return 409 on retry rather than replay.
A backend fix requires the user's separate scope approval; no backend change was authorized here.
The safe UI interim behavior blocks blind resubmission of an ambiguous message, retains its
identity, and directs the operator to verify the company record. It does not certify retry safety
across browser sessions or repair the backend contract. The unchecked retry items remain open.

Whole-unit W1/W2 and visual F1–F4 were fixed in `d32a340`; both scoped re-reviews passed.
The visual confirmation is closed: one fix batch and one confirmation capture, no further polish.

## Global Constraints

- Preserve DESIGN.md's incumbent typography, flat palette, spacing tokens and icon family, including both themes.
- No backend Python, scoring engine, M2, provider, migrations, normative package or external Salesforce changes.
- Preview save/reset/add/edit/remove must issue zero non-GET requests. Browser-local storage is not an audit log or server publication.
- Source values and strings are untrusted: escape HTML; validate stored preview shape before using it; never evaluate user text.
- Preserve historical/pointer decision semantics. An allowed broker still needs other checks; blocked matches take precedence. Never imply the preview changes those outcomes.
- New forms survive auto-refresh and preserve keyboard focus. Stale route responses cannot replace a different page. Unsaved and saved preview states are distinguishable.
- Parent is sole git/bus writer. All implementation must use apply_patch. No global formatter or unrelated changes. Existing tests may be updated only for intentionally changed UI contracts.
- Runtime URL: http://127.0.0.1:55717/ui (isolated test browser only). Node dependencies: NODE_PATH=/Users/lgiordano/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules. Tests: node scripts/check_console_NAME.cjs. PostgreSQL full gate is parent-owned.

## Approved design and acceptance

Shared layout: 12px clear space after section/subheaders, owned by containers rather than doubled margins; card headers retain their border and gain appropriate following-content space. Appearance browser-local helper sits in a separated lower footer, not directly under radios. Reviewer note exactly "Recorded on every review action." The progress fill and threshold share an inset coordinate lane with side padding and retain correct score/threshold geometry. Table rows use a consistent density and middle alignment; remove old compensating pill offsets where they contradict centering. Controls remain equal height. Dropdowns have explicit 12px right inset for a drawn chevron and sufficient text reservation. Legends become responsive aligned groups (pill plus separate description), not a loose flex sentence. Dark navigation has visible selected, hover and keyboard-focus states.

Header: replace "Signed Messages" with an accurately scoped message-authentication label; do not imply health or successful delivery. Remove opaque fingerprint from global chrome; retain labelled full fingerprint and copy affordance on Decision Rules. Header metadata must work on direct routes, not only after visiting Overview.

Preview editors: Points edit only numeric evidence weights, not threshold or gates. Salesforce edits destination names only, retains source/value preview, never company values. Broker editor supports search/filter, add/edit/remove, Allowed/Blocked, name and typed identifier lists plus notes. All edits say "Preview only — saved in this browser; live rules are unchanged." Provide Save Preview, Reset to Live, unsaved/saved/error states, and stable base identity. Points base uses bundle_hash; mapping base includes field/source set; broker base includes sorted live list because DB entries are not in bundle_hash. A changed base invalidates the saved draft and presents recovery instead of silently overlaying it. Malformed storage fails safely. Writes denied by browser storage produce visible errors, not a false saved claim. No secrets in storage. No backend or external requests from preview operations.

Overview: all-time decision rows show count and percentage with an explicit total/denominator. Expandable details offer latest company decisions (company, decision, score, update time) and links to full records, labelled distinctly from all-time decision-event counts; show the existing API's bounded sample size honestly. Do not invent per-decision timestamps from case.updated_at.

Composer: choose existing company or explicitly create new company ID; select plain-language action (technical event remains visible); render relevant labelled fields for every existing accepted event, including nested contact, enums, reason codes and reviewer identity. Advanced JSON is optional, synchronized, and validates before review. Read model constraints from existing schemas while implementing; no invented fields. Draft survives refresh and event changes do not silently overwrite edited data. Review summary precedes explicit Confirm Send. Keep an idempotency key stable across retry for identical reviewed content, rotate after changed content, prevent double click. Existing endpoint remains authority; no real sends in browser tests (intercept that endpoint). Response distinguishes accepted/queued, replay and errors, with raw response under details. Do not present global jobs_queued as jobs started by this message. Example is a numbered three-step fixture walkthrough with a demo label and no guarantee of approval while the safety hold is on. Sensitive reviewer events remain refused in production by existing authority and clearly warned in UI.

### Task 1: Shared layout and header clarity

**Files:** Modify src/kyc_tool/ui/console.html; tests/unit/test_console_static.py; scripts/check_console_first_pass.cjs; scripts/check_console_settings.cjs only where approved layout changes alter assertions. Create scripts/check_console_layout.cjs.

**Interfaces:** Existing tbar(), pill(), viewOptions(), viewOverview(), route(), sidebarBadge(). Keep these names and backend response shapes. Produce shared row/legend/select/header styling used unchanged by Tasks 2–3.

- [x] Write RED browser checks against the reported defects, based on actual element bounds and computed styles. Example core assertion:
  ```js
  const row = await page.locator('[data-complete]').first().evaluate(e => {
    const tr=e.closest('tr'), box=tr.getBoundingClientRect();
    return [...tr.querySelectorAll('td > .pill,button')].map(n => {
      const b=n.getBoundingClientRect(); return Math.abs((b.y+b.height/2)-(box.y+box.height/2));
    });
  });
  assert.ok(row.every(offset => offset < 2));
  ```
- [x] Run node scripts/check_console_layout.cjs and record expected RED causes. Use the existing read-only fixture case demo-case-00131; never click its actions.
- [x] Implement the exact shared-layout/header design above. Score geometry pattern: an inset .bscale inner element owns BOTH bmeasure and btick; percentages remain relative to the same inner width. Options note gets a separate footer class. Prefer theme tokens for hover, not hardcoded black. Legend grid owns item tracks. Main header gets no raw hash. Metadata initialization must be route-independent.
- [x] Run browser matrix 390/768/1199/1440 light+dark and current static pins; assert empty/no data states remain usable, no document overflow, no non-GET API writes. Update superseded layout assertions rather than retaining opposite contracts.
- [x] Write report with RED/GREEN commands and results, changed files, concerns. Parent reviews and commits.

### Task 2: Configuration previews and decision details

**Files:** Modify src/kyc_tool/ui/console.html and tests/unit/test_console_static.py. Create scripts/check_console_previews.cjs. No Task 1 test refactors unless a new intentional contract requires them.

**Interfaces:** Consume shared styling, tbar(), j(), esc(), recall()/remember() patterns. Use independent storage prefix kyc-preview-v1 and explicit records {version:1, base:string, value:object}. Do not reuse live objects as mutable draft state. Task 3 must not depend on preview helpers.

- [x] Write RED browser tests: edit one point then reload; edit a Salesforce destination; add/edit/remove a broker with all identifier fields; assert zero writes and unchanged GET policy. Exercise malformed/stale storage, blocked storage, blank/duplicate destination, negative/nonintegral points, hostile HTML text, unsaved refresh focus, reset and duplicate broker names.
  ```js
  const writes=[]; page.on('request',r=>{if(r.method()!=='GET'&&r.url().includes('/ui/api/'))writes.push(r.url());});
  // Interact with each actual editor, save, reload and inspect its displayed draft.
  assert.deepEqual(writes,[]);
  ```
- [x] Run RED, then implement guarded browser draft helpers and inline editors. Validation: points integers 0–1000 (disclose maximum); Salesforce field names 1–80 ASCII letters/digits/underscore, starting letter, distinct; broker names 1–200 chars, canonical case/space duplicates rejected, policy exact allowed/blocked, identifier entries trimmed nonempty <=256 chars and each list <=100 entries, notes <=2000 chars, broker list <=200 entries. Reject duplicate canonical identifiers within a list; warn on cross-broker matches, explain blocked precedence. Validate restored data identically. A save should update its baseline draft only after storage succeeds.
- [x] Preserve drafts across refresh without replacing focused controls. Catch all storage get/set/remove errors and give recovery. Reset to Live requires clear acknowledgement or undo for local draft loss. Fingerprints/base comparisons use canonical live data, not editable copies. Display a stale preview notice with Reset to Live when source changes.
- [x] Add count/share to historical overview distribution and labelled expandable latest-company details from GET /ui/api/cases?limit=100. Label case.updated_at "Company updated", not decision time. Filter rows by decision with empty state and company links; never call a bounded latest-company list all historical decision records.
- [x] Run browser suite plus static pins; write report with RED/GREEN evidence. Parent reviews and commits.

### Task 3: Guided Send Message

**Files:** Modify src/kyc_tool/ui/console.html and tests/unit/test_console_static.py. Create scripts/check_console_composer.cjs.

**Interfaces:** GET /ui/api/event-templates supplies closed event set/templates; GET /ui/api/cases?limit=100 company choices; POST /ui/api/send-event accepts {case_id,event_type,payload,idempotency_key}. The reviewed immutable snapshot is the sole source for send. No backend changes.

- [ ] Write RED browser tests with intercepted send-event requests: form inputs produce the right envelope; all nine event types have labelled forms; advanced JSON round-trip; invalid JSON cannot review; missing required fields fail visibly; reviewed content exactly equals sent content; send is double-click safe; retry keeps key; edited payload rotates key; 202 is queued not completed; 200 replay differs; no false global jobs-started claim; refresh preserves input; new/existing company distinctions; mobile and both themes.
  ```js
  await page.route('**/ui/api/send-event',async route=>{
    submitted.push(route.request().postDataJSON());
    await route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({status:'queued',jobs_queued:42})});
  });
  // After a reviewed send:
  assert.equal(submitted[0].payload.company_legal_name,'Example Limited');
  assert.doesNotMatch(await page.locator('#page').innerText(),/Started 42/);
  ```
- [x] Run RED. Implement guided form using existing payload models as authority; keep all accepted enum names visible beside friendly labels. Native inputs/selects, nested field grouping; required/optional hints, inline validation linked to fields. Store drafts only in memory (not company PII in localStorage). Switching event restores its draft; explicit reset loads template. No auto sends or automatic fixture walkthrough.
- [ ] Implement review panel, Edit action, Confirm Send, in-flight disable, retry key discipline and honest response summary with expandable raw details. Keep form mounted under auto-refresh and ignore stale GET responses. Confirmation is inline rather than a popup.
- [x] Replace Example prose with ordered fixture steps and clear demo/safety-hold note. Run browser suite, static pins and earlier task suites; report RED/GREEN evidence. Parent reviews and commits.

## Final verification and handoff

- [x] Parent inspects one batch of screenshots across changed routes at desktop/tablet/mobile, both themes and user width. One batch of fixes, one confirmation capture.
- [x] Fresh whole-unit review and independent visual finish review with original screenshots and acceptance list. Documentation compares incumbent DESIGN.md; only approved spacing/legend/table changes are recorded. Scoped UI findings resolved; the backend retry limitation remains open above.
- [x] All focused scripts, pytest static/engine guard, full ./manage.sh test with local PostgreSQL 16, ./manage.sh lint and lint-imports; git diff --check. Source HTML does not alter the Python engine hash.
- [x] Parent commits exact lane files, pushes, verifies exact source CI, then RELEASE on bus anchored to source range; no unrelated untracked artifacts staged.

### Verified scoped release

Source range `0866ce7..b787355`; final UI source is `aa2c4c8`. Local browser checks
205/205 (first-pass 48, Options 24, layout 45, previews 58, composer 30), console static
58 and engine guard 3 passed. PostgreSQL 16 full suite: 2,514 passed, 1 non-applicable
document-token skip. The local full run began before the final UI-copy correction;
CI run 34555397678 verified the exact final source at `b787355` with all three jobs green.
Ruff and both import contracts passed. The final safety reviewer independently repeated
30/30 composer checks with stable source hashes. No real preview or test-send writes.

This records delivery of the safe UI scope, not completion of the original backend-dependent
retry acceptance. Its unchecked items remain visible above pending the user's scope decision.
