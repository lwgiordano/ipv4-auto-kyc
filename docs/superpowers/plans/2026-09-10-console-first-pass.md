# Console First Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Parent alone commits, pushes, and writes the bus.

**Goal:** Correct the approved shared layout defects and the two observed interaction bugs without redesigning the console.

**Architecture:** Keep the self-contained console and its current tokens. Layout parents own spacing; the search input remains mounted while results change. Route metadata follows the displayed route.

**Tech Stack:** HTML, CSS Grid/Flex, vanilla JavaScript; pytest source guards and an executable Playwright browser check.

**Spec:** `.impeccable/critique/2026-09-10T15-35-13Z__src-kyc-tool-ui-console-html.md`, approved first pass (visual and interaction), with `DESIGN.md` as the incumbent visual authority.

## Global Constraints

- Preserve existing copy, decisions, data provenance, API behavior, tokens, and visual identity.
- Do not alter backend Python, migrations, normative build package, M2, or engine identifiers.
- Do not add an in-app theme selector. Current light/dark follows system appearance.
- Do not recompose the Overview or change the mobile table strategy in this pass.
- Parent is sole committer, pusher, and bus writer. No broad formatter run.
- Use the running local console at `http://127.0.0.1:55717/ui` for non-mutating verification; do not submit events or approvals or stop the user's stack.

### Task 1: Shared layout and stable view interactions

**Files:**
- Modify: `src/kyc_tool/ui/console.html`
- Modify: `tests/unit/test_console_static.py`
- Create: `scripts/check_console_first_pass.cjs`
- Modify: `DESIGN.md` (a concise first-pass record and reusable spacing lesson)

**Interfaces:** Existing `tbar`, `viewCases`, `route`, reviewer markup, `.split`, `.stack`, `.idf`.

- [x] **Step 1: Write an executable browser regression check and confirm the observed failures.**

The Node script uses `require('playwright')`, `node:assert/strict`, and the existing app URL supplied as argv (default URL above). It opens an isolated Chromium context, never posts data, and closes the browser in `finally`. Allow `NODE_PATH` to locate the installed Playwright runtime without adding production dependencies. Report all assertions, not just the first failure. Check these measured relationships at 390, 768, 1024 and 1440px:

```js
assert.equal(secondOverviewCard.top, firstOverviewCard.top); // two columns only
assert.equal(secondCard.top - firstCard.bottom, 16); // stacked .split/.stack
assert.equal(avatar.top + avatar.height/2, input.top + input.height/2);
assert.equal(firstPrimaryValue.top, otherPrimaryValue.top); // same identity-grid row
assert.equal(legend.top - description.bottom, 12);
assert.equal(headerActions.length, 0); // pages with no actions
```

Use a 1px tolerance for browser rounding. Locate elements by their current DOM/roles; company details can use the first existing company link. Do not invent an API fixture that conceals actual DOM behavior. Interaction checks: type `Ac`, wait past the 250ms debounce, then type `me`; input must remain focused and read `Acme`. Change selection and confirm it survives result refresh. Move focus to navigation before a delayed result arrives; refresh must not steal it. Navigate away during a pending search; late results must not overwrite the new view. Case detail followed by Overview, Companies, Data Sources, Decision Rules and Send Message must yield that screen's document title rather than the company title. Run light and dark theme smoke checks without changing macOS appearance.

- [x] **Step 2: Implement layout at the parent boundaries.**

Remove the global `.card+.card` margin leak from `.split` and `.stack`; retain spacing for actual block-flow card siblings by scoping the rule to their real parent. Do not force equal card heights. Only emit `tbar`'s `.actions` when content exists. Add an optional legend slot to the shared header (or equivalently group it structurally) so title-to-description stays `var(--s2)` (8px), description-to-legend is `var(--s3)` (12px), and the full header-to-content interval stays `var(--s5)` (24px). Keep the Data Sources pill glossary intact and the company status row's different meaning intact.

Place the reviewer label, input, and hint on explicit grid rows; align the 40px avatar with the input row, not the label. Preserve `for="reviewer"`, id, accessible hint and storage/initials behavior. Set `.idf` tracks to content sizing (`align-content:start` is sufficient if verified) so optional sublines do not push neighboring primary values down.

- [x] **Step 3: Implement stable search and titles.**

Mount the Companies toolbar/input once per view and replace only result rows/count during debounced searches. Read the live input as authority, preserve selection naturally by retaining the node, and keep Clear Search working. Ignore stale responses when the input node was replaced by navigation or a newer request superseded it; for example, a monotonic request id plus node-identity check can guard result application. Invalidate pending debounce work on navigation. Retain row-link and modified-click behavior.

Set document titles at the route boundary using the route's known display label, preserving the company-specific title on its detail view. Error views must not retain the previous company's title. Keep the dev proxy's title prefix wrapper working and preserve navigation focus behavior.

- [x] **Step 4: Verify and record.**

Run the browser script against the current console (all acceptance assertions green), `.venv/bin/python -m pytest tests/unit/test_console_static.py tests/policy_driven/test_engine_build_id_guard.py -q`, and `.venv/bin/ruff check tests/unit/test_console_static.py`. Add narrowly scoped source guards for shared layout ownership/header conditional emission if useful, but browser execution is the behavioral evidence. Record the final spacing rules in DESIGN.md; do not replace its historical evidence. Report exact commands and output, including RED evidence, to the task report.

- [ ] **Step 5: Parent review and completion.**

Parent runs the broader test/lint/import gates, batches desktop/mobile/light/dark screenshots, commissions a separate task review and whole-unit review, commits only claimed files, and posts a range-anchored RELEASE. CI status must be reported separately from local results.

## Verification record

The measured pre-fix layout matrix failed 20/20 checks; the three new static guards were also
confirmed RED. The final executable browser script passed 46/46 checks against the live local
console, including distinct out-of-order search responses and company-to-route title transitions.
The parent inspected desktop/mobile and light/dark screenshots. Separate task and whole-unit
reviewers both returned PASS; the latter independently passed the 50 console/static and engine
guard tests.

The full local gate passed 2,503 tests with one inapplicable document-format skip and 526 warnings
on macOS/Python 3.13 with a disposable PostgreSQL 16 cluster. An initial run hit four native macOS
proxy-discovery crashes in unchanged forked networking tests. A process-local `no_proxy='*'`
setting made all seven focused executor tests and then the full rerun pass; no backend or
machine-wide environment setting was changed. `./manage.sh lint`, import contracts (2 kept,
0 broken), and `git diff --check` passed. Remote CI is recorded separately in the RELEASE.
