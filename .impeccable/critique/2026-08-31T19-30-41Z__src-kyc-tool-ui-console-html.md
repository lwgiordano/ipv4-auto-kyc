---
target: the ops console (src/kyc_tool/ui/console.html)
total_score: 17
max_score: 40
na_heuristics: 
p0_count: 3
p1_count: 2
timestamp: 2026-08-31T19-30-41Z
slug: src-kyc-tool-ui-console-html
---
Method: dual-agent (A: design review · B: detector + browser evidence), run in isolation; synthesis and disputed-claim verification by the parent.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | Route changes hold stale content until the fetch resolves (`page.innerHTML` assigned only after `await j()`), with no pending indicator. After clicking **Pass** on a review task the page re-renders at 700ms while the worker is still rescoring, so the action appears to do nothing. |
| 2 | Match System / Real World | 1 | The largest type on the case page (32px/800) is the raw enum `manual_review_insufficient` — and it is factually wrong on a case scoring 115/100 with all five gates green. The human label ("Manual Review - Insufficient Score") exists in the payload, 3,000px down in the Salesforce projection. |
| 3 | User Control and Freedom | 2 | No undo on the review-task Pass/Fail (irreversible, writes an audit row). No confirm or cancel on any action. No clear-search control. Changing the Composer's event type silently discards a hand-edited payload. |
| 4 | Consistency and Standards | 3 | Visual layer is genuinely disciplined — measured: exactly 5 type sizes on the 12/14/16/24/32 register, 1 non-inset shadow, 3 radii, no off-token strays. Interaction layer diverges: three different "pick a case" controls (search / free text / select), `<h4>` with no CSS rule at all on the most important card, one table missing `<thead>` beside one that has it. |
| 5 | Error Prevention | 1 | `reviewer.manual_approve` — which the policy file itself declares `bypasses: ["score","hard_gates"]` — is one item in a flat 9-item alphabetical dropdown plus one click of a blue button. No confirmation. Review-task Pass/Fail: one click, audited, irreversible, no confirm. Requeue on dead letters: one click, no confirm. |
| 6 | Recognition Rather Than Recall | 2 | Threshold 100 appears on the case page but nowhere on the Cases list, so triaging the queue runs on memory. The rubric `category` that maps onto the hard gates is in the payload and shown in neither the score breakdown nor the gate strip. |
| 7 | Flexibility and Efficiency | 1 | Zero keyboard shortcuts, no skip-to-content, no sort, no filter, no saved views, no bulk actions. Case rows are not links, so no middle-click-to-tab. The only accelerator offered — auto-refresh — destroys form input. |
| 8 | Aesthetic and Minimalist Design | 2 | The Runs table redraws all ten state-machine chips for every completed run (~60 near-identical chips carrying one bit of information). 36 `<details>` "raw" toggles on one page; `submitted_json` is `<details open>` by default. A permanent "Demo journey" card about dev-stack fixtures ships on the Composer. |
| 9 | Error Recovery | 2 | The error boundary preserves the shell and offers Retry, but prints a raw status and JSON body (`500: {"detail":"boom"}`). "Payload is not valid JSON" is a 5s toast at bottom-left, ~700px from the textarea, `role="status"` rather than `alert`, discarding the parser's position information. No inline field validation. |
| 10 | Help and Documentation | 1 | No contextual help at any decision point. The Policy page is genuinely excellent documentation — and is actively contradicted by the case page it should explain, with no link between them. |
| **Total** | | **17/40** | **Poor — major UX overhaul required** |

Read that score correctly: it is not a verdict on the restyle. The paint is the best part of this surface. The score is low because the heuristics measure whether a reviewer can do their job, and the workflow the console exists to support is largely unimplemented beneath a well-made skin.

## Design Specificity Verdict

**LLM assessment.** Roughly a third of this surface could only be this product; the rest is competent admin-panel furniture; and the one thing that makes this product what it is — a decision held against its own rules pending a human — has no representation in the composition at all.

Product character is real and specific in places. The **Policy page** is the best screen in the build: the rubric with points, category, mode and the deterministic pass rule verbatim; a priority-ordered decision table rendered with the same pills the cases use; broker policy as identifier chips; the bundle hash pinned in both the title bar and the sidebar. Nothing else could use that page. The **checks-and-supersession chain** — superseded checks at 55% opacity with an arrow to the live one — is a data shape that belongs to evidence scoring and nowhere else. The five named hard gates as a strip, the deliberate split between "Published decision" and "Live evidence", and the Integrations live/stub/needs-config taxonomy are all authored.

Then it defaults hard. The **Overview is an SRE dashboard, not a reviewer's dashboard** — runs completed, queue depth, dead letters, p95 event-to-decision. Not one tile answers "what needs me." The **Cases table is a stock nine-column admin grid** whose two score columns are bare integers with the threshold, 100, appearing nowhere on screen. The nav reads Overview / **Investigate** (one item) / **Wiring** (three) / **Act** (one) — group labels as IA cosplay. The case page is ten equally-weighted stacked cards ending in a raw `submitted_json` dump: the default shape of every generated admin detail page in existence.

The decisive failure: **demo-acme-1 renders `manual_review_insufficient` at 32px beside "score 115 · buy enabled", directly above five green checkmarks, and the interface contains no word acknowledging the contradiction.** No hold vocabulary, no owner, no reason, no next step. The design is faithful to the data model and blind to the workflow the data model exists to serve.

**Deterministic scan.** The bundled detector returned **0 findings, exit 0** on the markup. That result is genuine, not a silent no-op — Assessment B ran a control file (Inter, a purple zero-offset glow, 13px radius, 61px h1) and the same detector returned 2 findings and exit 2. But the clean result has a coverage caveat that matters: the entire content area is JS-rendered, so the static scan only ever saw the shell and the `<style>` block, never a rendered view.

Browser injection carried the real coverage — **12 findings across 11 element groups on 5 views** (Overview 0, Cases 1, case detail 8, Integrations 2, Composer 1). Dispositions:

- **`cramped-padding` ×6 — false positives, all six.** Five sit on flush table cards where `padding:0` is deliberate (`.bd.flush`) and the inset comes from the cells; measured text inset is 17px left / 9px top, above the rule's own 8px threshold. The sixth is a button with `min-height:44px` and centred content — 14px above, 15px below the text. The rule reads the wrapper's `padding` property and never accounts for child padding or flex centring.
- **`em-dash-overuse` ×2 — false positive.** Of 12 em-dashes on the case page, 9 are standalone `—` cells used as the null placeholder in data tables; only 2 sit in running prose.
- **`line-length` ×2 — true positive, one with a wrong magnitude.** `td.sub` on case detail measures exactly 108 characters per line, as reported. The Integrations `p.sub` genuinely exceeds 80 characters, but the reported "183" matches no measured count (actual: 141 total, longest run 105).
- **`skipped-heading` ×2 — true positive, and undercounted.** Case detail jumps h2 → h4; Composer jumps h1 → h3. B's own independent DOM scan found a third the injection missed: Policy also jumps h1 → h3.

**Where the detector caught what the review missed:** the Composer and Policy heading skips (the review only named the case page), and the 108-character measure on `td.sub`. **Where the review caught what no detector could:** every P0 below. The mechanical scan is clean precisely because the failures are not in the paint.

**Visual overlays:** injection succeeded and ran on five views, but the live server has since been stopped, so there is no overlay tab still open for you to inspect. Nothing in the repo was modified — the server was stopped with `--keep-inject` specifically so it could not rewrite `console.html`, confirmed by a clean `git status` and an unchanged mtime.

## Overall Impression

This is an excellent design-system skin over an unfinished workflow. The token layer, both themes, the measured contrast, the focus rings and the glyph discipline are better than most shipped products — and Assessment B's measurements back that up without qualification: five type sizes, one shadow, three radii, zero contrast failures on text, zero page-level overflow at any width, zero unlabelled form controls. Then you open the one case that matters and the console shows you a verdict that contradicts its own policy page, offers no way to act on it, and cannot be opened with a keyboard at all.

The single biggest opportunity: **design the hold.** One state — "held, all gates green, awaiting a named approver" — is the product's whole reason to exist, and it is currently rendered as a database enum with no affordance attached. Everything else on this list is ordinary product work; that one is the difference between a viewer and a tool.

## What's Working

**1. The token system and both themes.** Every colour resolves through one named layer, and the four status pairs derive from mix recipes whose mixers flip under dark, so the dark theme re-resolves without a second hand-picked palette. Measured: amber pill 8.11:1 light / 9.95:1 dark, green 7.62 / 10.23, red 6.85 / 9.73, body copy 4.94–5.30. Zero text elements below threshold across seven views in both themes. This matters because an ops console is read all day in both lighting conditions, and a reviewer who misreads a status tint makes a wrong call on a real company.

**2. The glyph discipline.** The embedded Mulish subset carries 291 Latin glyphs; ✓ ✕ ⏳ → are absent from it. Rather than accept a mid-word font fallback, every status symbol is drawn as a 16-box, 1.25-stroke, `currentColor` SVG. Assessment A verified the premise independently: `✓` measures identically in Mulish and serif (genuinely missing), while `›` `·` `—` `…` `'` all render at Mulish-specific widths. The discipline is invisible when right and glaring when wrong, and it doubles as the non-colour status cue colour-blind reviewers need.

**3. The humanized audit feed.** Thirteen event shapes translated into readable lines ("check **org_id_match** → pass · +25"), a timeline rail, accent dots on the four consequential actions, routine stage noise filtered out but retained under a per-item raw disclosure. The audit trail is the deliverable in a compliance tool, and this is the one place the console translates the machine's language into the reviewer's without losing the machine's version.

## Priority Issues

### [P0] The enforcement hold is rendered as a bare contradiction
**What:** demo-acme-1 shows `manual_review_insufficient` at 32px/800 in amber, with "score 115 · buy enabled", directly above five green gate cells. The Policy page's decision table states that this exact condition produces `approve`. No element on either page names the hold, its cause, its owner, or how it clears.
**Why it matters:** this is the moment the product exists for. A reviewer either escalates a non-bug ("the engine is broken") or overrides a hold they do not understand. Both are compliance incidents; the second is the expensive kind.
**Fix:** stop printing the enum. Use a true label — "Held — awaiting manual approval", not "Insufficient", which is false here. Put a tinted callout inside the Published decision card: *"All five hard gates passed and the score is 15 points over threshold. This case is held by enforcement policy and needs a named reviewer's approval before the platform enables the account."* Make the decision word link to the exact Policy row that produced it, and mark the hold as a visible exception to that row.
**Suggested command:** `/impeccable clarify`

### [P0] There is no approval action, and the approval path launders reviewer identity
**What:** the case header's only button is "Send event", which navigates to the Composer. Approving requires selecting `reviewer.manual_approve` from a flat 9-item dropdown and sending a template that hardcodes `"reviewer_id": "console"` (verified at `src/kyc_tool/ui/routes.py:61`). No confirmation, no reason field, no named approver.
**Why it matters:** the audit row will name "console" as the party who bypassed the hard gates on a real company. That is the one field a regulator asks about. And the highest-stakes action in the product is visually indistinguishable from sending a test event.
**Fix:** an "Approve manually" button in the case header, behind a confirmation that names the company, states exactly what is being bypassed, requires a reviewer identity and a reason, and previews the resulting buy-enablement. Remove `reviewer.manual_approve` from the Composer's ordinary event list, or give it destructive treatment there.
**Suggested command:** `/impeccable harden`

### [P0] The Cases table cannot be opened with a keyboard
**What:** rows are `<tr class="click" data-id="…">` with a JS `onclick`, no `href`, no `tabindex`, no `role` (verified at `console.html:564`). The entire tab order on `#/cases` is eight stops — six nav links, the auto-refresh checkbox, the search input. Opening a case, the primary action of the console, is mouse-only. Assessment B corroborates from the other side: interactive elements number only 7–11 per view precisely because rows are not links. The `caseLink()` helper that emits proper anchors already exists in the file and is used elsewhere.
**Why it matters:** task-blocking for keyboard and screen-reader users, and it also kills middle-click-to-new-tab, which is how ops staff actually triage a queue.
**Fix:** wrap the company-name cell in the existing `caseLink()` anchor; keep the row click as a convenience layered on a real link, not as the only path.
**Suggested command:** `/impeccable audit`

### [P1] A global setting silently destroys the reviewer's work
**What:** ticking "auto-refresh 5s" in the persistent sidebar re-runs `route()` every five seconds, rebuilding `#page` from scratch. Measured: a hand-typed payload was replaced by the event template after one tick, and a typed Case ID reverted to the hardcoded default. No warning, no recovery.
**Why it matters:** the checkbox is always-visible chrome with no indication it will wipe the form you are about to fill. Someone will lose a hand-built payload mid-incident.
**Fix:** skip the re-render on routes holding unsaved input, or diff rather than replacing `innerHTML`. At minimum suppress auto-refresh on the Composer and surface "paused while editing".
**Suggested command:** `/impeccable harden`

### [P1] The frame has no responsive behaviour, and the failure hides from page-level metrics
**What:** `aside` is a fixed 240px with `flex:none` and there is no media query for the shell. The two media queries in the file govern inner grids (`.split` at 980px, `.contrib` at 820px), not the frame that causes the problem. Measured on the case page: `main` falls to 660px at a 900px viewport, 528px at 768px, 360px at 600px and **150px at 390px**, while the content inside stays fixed at 733px.
This is where the two assessments appeared to conflict, and both were right about different things. Page-level horizontal overflow is genuinely zero at every width — `scrollWidth === clientWidth`, 28/28 clean. The failure is contained *inside* `main`, which acquires its own horizontal scroll because `overflow-y:auto` forces the other axis to `auto`. So nothing is lost, but at 390px the sidebar consumes 62% of the viewport and the case page becomes a 150px-wide column that must be scrolled sideways to read a decision.
**Why it matters:** ops staff check queues on undocked laptops and on phones during a call. This is also a caution about the metric: page-level overflow checks will keep reporting clean while the surface is unusable.
**Fix:** collapse the sidebar to icons or an off-canvas drawer below ~1024px; wrap flush table bodies in their own `overflow-x:auto` containers so the scroll is a deliberate affordance rather than a side effect.
**Suggested command:** `/impeccable adapt`

## Persona Red Flags

**Alex (Impatient Power User)** — *triage the queue, clear a held case*
- No keyboard shortcuts at all: no `/` to focus search, no `j`/`k` on the table, no `g c`.
- The Cases table offers no sort, no filter, no saved views — he cannot ask "held cases with all gates green", which is precisely his job on this screen.
- No bulk actions. Three cases in identical states, no way to act on them together.
- Rows are not links, so no middle-click to open three cases in tabs.
- The approval path is five steps across two screens, and the Composer forgets his case ID if auto-refresh ticks.
- He must scroll past roughly 60 redundant pipeline chips in the Runs table to reach the Review tasks card.

**Sam (Accessibility-Dependent)** — *same action*
- **Cannot open a case at all.** Tab order on `#/cases` is eight stops and none is a case row.
- No skip-to-content link: every route change costs seven tab presses through the sidebar.
- Heading order on the case page runs h1 → h2 → **h4** → h2 → h3 (detector-confirmed), and two visually distinct treatments share the `<h2>` level, so the screen-reader outline flattens "Published decision" and "Submitted snapshot" to equal rank. Composer and Policy each jump h1 → h3.
- The error toast uses `role="status"` (polite) for a hard failure and is not associated with the field: no `aria-invalid`, no `aria-describedby`.
- The active nav item carries a `.active` class but no `aria-current`.
- Route changes swap `#page` wholesale with no live region and no focus management; nothing is announced, focus is stranded.
- Six sidebar SVG icons lack `aria-hidden` (detector-confirmed) — harmless, since each link carries its own visible text, but untidy.
- Credit where due: focus rings are excellent, status never relies on colour alone, contrast holds in both themes, and every one of 18 form controls has an accessible name. Sam is blocked by structure, not by paint.

**Riley (Deliberate Stress Tester)** — *probe the edges*
- Search `zzzznope` with three cases present returns **"No cases yet — Send a `kyb.run_requested` from the Composer to create one"** (verified at `console.html:573`). The zero-data empty state is reused for zero results: it tells a reviewer their caseload is empty and directs them to fabricate data. No clear-search control.
- Tick auto-refresh, type in the Composer, wait five seconds: work silently gone.
- Deep-link to `#/policy` in a fresh tab and the sidebar's environment and policy-hash chips are blank — they are written only by `viewOverview()`. "Am I in prod, which rules am I looking at" is unanswerable on six of seven routes.
- `#/case/does-not-exist` gives a bare card with no title, no breadcrumb, no back link.
- The review task's Context cell truncates JSON with an ellipsis and, unlike the dead-letter error cell, carries no `title` — permanently unreadable. Measured 2,488px of content in a 397px cell even at 1440px.
- **`d.outbox` is fetched on every case load and never rendered** — verified: the API returns four outbox rows for demo-acme-1 and no view reads them. The one fact a compliance reviewer needs after a decision, whether it actually reached the platform, is retrieved and thrown away.

## Minor Observations

- **`<h4>Published decision hard gates</h4>` has no CSS rule at all.** Verified: the stylesheet styles `h1,h2,h3` only. It renders at UA default with 21.28px margins — a value that exists nowhere in the 4/8/12/16/24/32 register — on the most important card in the product.
- **The bullet chart does not work as a chart.** Verified by computation: the threshold band measures **1.21:1** against its track (1.37:1 dark) — drawn and effectively invisible — and the score bar measures **3.10:1 light / 2.79:1 dark**, at and then below the 3:1 floor for meaningful graphics. Both are mid-greys, so the band and the bar read as the same object. The bar is hardcoded `background:var(--faint)` regardless of outcome (`console.html:722`), so 0/100 and 115/100 differ only in length, never in colour. Assessment B measured text contrast only and did not cover this; it is a genuine non-text contrast failure on the primary visualization of the most important screen.
- Decision history shows 45 → 80 → 105 → 115 with an unchanged verdict. The threshold crossing between 80 and 105 is the entire story of the hold, and nothing marks it.
- Audit timestamps are relative only ("4m ago"), with exact time hover-only via `title`. For the legally load-bearing artifact in a KYC tool that is backwards, and it makes the decided-vs-published ordering the code works hard to get right invisible.
- Overview's KPI grid breaks 5 + 1 at 1440px, orphaning "p95 event to decision".
- The toast is fixed at `bottom:24px; left:24px` and lands on top of the sidebar's auto-refresh checkbox.
- Integrations places its pill legend at the very bottom, below the 900px of pills it explains.
- A green filled-dot "Live" pill labelled **"fs"** describes a local-filesystem evidence store in a compliance system.
- TODO lines print engineering markers verbatim: `! TODO(integration): outbound email provider (AUDIT:C4)`, with a bare `!` where every other status cue is a drawn SVG.
- Card subtitles are written for the codebase's author: *"every field below comes from ONE decision row — the ordering authority"*.
- The Composer's permanent "Demo journey" card describes dev-stack fixtures on what is meant to be a production ops surface.
- The website review task's domain is not a link; the reviewer must retype it out of a truncated JSON string to do the review being asked of them.
- The timeline rail centres at x≈6 while its dots centre at x≈5 — a 1px misregistration repeated fifteen times.

## Questions to Consider

1. If every case in the queue reads `manual_review_insufficient`, what is the Cases table actually sorting? What would the screen look like if it opened on *"3 cases held · 1 with every gate green and 15 points of headroom — approve, or request more evidence"*?
2. The tool computes two scores from two authority eras, and the current fix is to name a card by what it is not ("Live evidence — not the published decision"). What if the case page led with one sentence — *"115 of 100, five of five gates, held by enforcement"* — and the two-authority machinery lived in a disclosure for the person auditing the audit?
3. `reviewer_id: "console"` is the default in the manual-approve template. Who do you want named in the audit row when a regulator asks who bypassed the hard gates on a specific company?
4. The Runs table redraws ten pipeline chips for every completed run. What does a reviewer learn from the sixth identical `QUEUED › RESOLVE INPUTS › …` that the first did not teach?
5. The Policy page is the most confident, most product-specific screen here, and nothing links to it from the case that contradicts it. What if every verdict deep-linked to the exact policy row that produced it, with the enforcement hold rendered as a named exception to that row?
