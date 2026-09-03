---
target: "the company review page: type consistency and purpose"
total_score: 24
max_score: 40
na_heuristics: 
p0_count: 4
p1_count: 6
timestamp: 2026-09-03T21-55-53Z
slug: src-kyc-tool-ui-console-html-case-demo-case-001
---
Method: dual-agent (A: isolated design review · B: detector + browser measurement — 24 page runs,
8 dialog runs, a 25-width sweep). Target `bcc57e1`, three live states, both themes, dialog and
tooltips included.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The reviewer's stored reason is never rendered anywhere. |
| 2 | Match System / Real World | 3 | "Score threshold met / NOT MET"; 635px of engineering prose in a reviewer's table. |
| 3 | User Control and Freedom | 2 | There is no Reject. A reviewer can only ever say yes. |
| 4 | Consistency and Standards | 2 | Nine cards, two titling systems; rank C has no render of its own. |
| 5 | Error Prevention | 2 | The commit button is 2.08:1 while the POST is in flight. |
| 6 | Recognition Rather Than Recall | 2 | Round and task ids cut to 8 chars with no `title`. |
| 7 | Flexibility and Efficiency | 2 | Reviewer ID typed twice; no anchors on a 4200px page. |
| 8 | Aesthetic and Minimalist Design | 2 | Three identical full-green meters whose only reading is the word beside them. |
| 9 | Error Recovery | 2 | The dialog's designed error is unreachable; the browser's bubble fires instead. |
| 10 | Help and Documentation | 4 | Ten explainers, keyboard-reachable, each a sentence a person would say. |
| **Total** | | **24/40** | **A documented design, scored on its states** |

## The structural finding

§11's verification recipe says to measure "the computed type tuples on `.page *`". The approval
dialog and the explainer bubbles are both mounted OUTSIDE `.page`. All three off-ladder type
tuples and two of the three worst contrast failures live in exactly that region. Three audits
measured a smaller surface than the reviewer uses. The detector pass reproduced the blind spot one
level down: it measured the dialog closed and no tooltip open, and so found one of the three.

**Twelve rendered tuples. Nine tokens, three not:**

| Tuple | Carrier | Cause |
|---|---|---|
| `12/16/700` mono | `.tip dt` — the five rule names in the Decision Rules explainer | `console.html:582`, a raw size/line-height/weight triple |
| `12/16/700` Proxima | `span.req` — the `*` on the irreversibility line | `.req` sets weight onto a 12px `p.note`; inside `label.flab` the same class is legal |
| `14/20/600` | `#ap-err` — the approval dialog's error message | `console.html:593`, a raw triple |

Also: `--t-subhd` and `--t-strong` are byte-identical declarations (`700 14px/20px`), so rank C has
no render of its own — `h3.sub-hd` and the hold callout's bold lead are the same rank, 24px apart.
And all seven `h2.sec` labels name exactly one card each, while cards 1-2 title themselves from
inside a bordered header: nine cards, one job, two ranks, two colours, two cases, two x-origins.

## Four defects shipped in the previous commit

| # | Found | Why it broke | Fix |
|---|---|---|---|
| 46 | `.pipe-lab.done` is **1.25:1** in dark (`.fail` 1.59:1, latent) | The meter's fill took `--green` (theme-aware, `#a9d3b5` dark) and its label took `--green-ink` (`#2c391d` in BOTH themes). Light is 12.28:1, which is why the screenshots passed. | `color:var(--green)` / `var(--red)` — the same tokens the segments above already use. |
| 47 | `.contrib` right column loses its last two dividers | `.crow:nth-last-child(-n+2)` counts DOM position; the same commit changed `.contrib` to `grid-auto-flow:column`, so DOM order stopped matching visual order. Measured: left items 0-3 all `1px`, right items 6-7 `0px`, on the same visual rows. | Wrap each column and use `:last-child`, or delete the exception and rule all eight. |
| 48 | §9 finding 39's claim "uniform 66px in every state at every width" is false | 25-width sweep: 66px at 1440, **86px from 1420 to 1181**, 66 below. In that band 1-4 of 5 labels wrap. §9 measured 1440/1100/1024/768/390 and stepped over 1280 and 1366. | Raise the `repeat(5,1fr)` breakpoint to ~1440, or shorten the subjects (see 51). |
| 49 | §8 finding 27's chip suppression never fires on `approved_manual` | The test is `decision && c.status===decision` — raw enum equality. `"approve" !== "approved_manual"`, so `demo-case-002` still prints `Approved` beside `Approved Manually`. | Compare the rendered fact, or map decision→statuses explicitly and suppress on membership. |

§9's detector note ("5 em-dashes are inside `.vh`") is also stale: those spans were replaced by the
visible `.g-r` words in the same pass, and the doc kept the old justification.

## Priority issues

| # | Sev | Found | Measurement | Fix |
|---|---|---|---|---|
| 50 | P0 | The commit button renders at **2.08:1** while the approval is in flight | `button:disabled{opacity:.45}` (L385), pixel-sampled on `#ap-go`. Rule 9's own failure mode, at a worse ratio than the state the rule was written about, on the control that commits an irreversible legal action. Findings 16, 25 and 45 each removed an opacity state from a component; none re-read the base rule where the pattern was first written. | Declared ground and ink: `background:var(--surface-muted);color:var(--text-muted);border-color:var(--border);box-shadow:none`. |
| 51 | P0 | The dialog's designed error state is unreachable | `#ap-form` has no `novalidate` and both fields are `required`, so native constraint validation preempts the submit handler; the reviewer gets Chrome's transient bubble over the hint text. When `#ap-err` does fire it is the form's LAST child, after `.toolbar` — the error renders below the button that caused it. | `novalidate` on the form (keep `aria-required`), move `#ap-err` before `.toolbar`, `aria-invalid` on the offending field, `.banner{font:var(--t-strong)}`. |
| 52 | P0 | The reviewer's reason is required, legally load-bearing, and never displayed | `humanizeAudit` returns `Approved manually` and drops `detail_json.note`. The approver's name is absent from the decision card too — it appears only in a Decision History column ~3000px down. | Append the note to the feed line; print `Approved by <name>` under the decision word when the pointed row is manual. |
| 53 | P0 | The clip cue measures **1.30:1 light / 1.05:1 dark** while 251-545px of table is hidden | Tasks hides 251px at 1024 and 545px at 390; Decision History 163px and 457px. Clipping starts at 1240px and 1181px. The scroll shadow is the only thing satisfying rule 11 and it is two units of blue in dark. | Give the scroller a real edge at the 3:1 control token, or fix the columns (55) so it stops clipping at desktop widths. |
| 54 | P1 | The modal has no boundary in dark: **1.18:1** | `rgba(0,22,45,.72)` composites to almost exactly the dark page ground; light is 7.5:1. §8 finding 19 tuned the opacity in light only. | `dialog.modal{border:1px solid var(--border-input)}` — ~5:1 on the dark card, harmless in light. |
| 55 | P1 | Five tables, five unrelated column grids, moving with the data | First-column edges spread **256.5px** (685.5 / 429.0 / 429.6 / 559.1 / 531.6); Rounds 429.0 vs Tasks 429.6 is a 0.6px near-miss that reads as a printing error. Boundaries move by state: Decision History col 1 531.6 → 477.4, Salesforce Value 772.7 → 812.5. `auto` optimises content mass, not reading priority: Evidence History leaves **186.4px of dead ground** between a row's subject and its verdict; Salesforce gives 213.6px to the payload and **635.3px** to mapping prose. | `table-layout:fixed` + a `<colgroup>` per table, widths by ROLE: mono id 128, subject `minmax(220px,1fr)`, status 160, number 88, timestamp 168, action `width:1%`, prose the remainder and only ever last. |
| 56 | P1 | `.tag` renders **20 / 34 / 50px** inside one family | 20px to 720, 34 at 600, 50 at ≤480 — all ten tags 3 lines on a phone, against §4's declared 20px. Rule 22. | The tag row scrolls or truncates; it does not wrap to three lines. |
| 57 | P1 | `button.sm` is **34px** at every width except 1440 | The Approve/Reject pair shrinks to 108-115px and the label wraps. 34 is on no scale. | `white-space:nowrap` on the pair, and let the action column take `width:1%`. |
| 58 | P1 | Baseline spread **11px** in a Rounds row, **8px** in a Tasks row | Rounds: Round 1434.00, Sources 1436.00, Progress label **1445.00**. Tasks: five cells at 1777.00, the button label at **1784.00**. §9 finding 41 corrected pill runs; a chiprow of buttons has no counterpart. | Extend the correction to `td>.chiprow` containing a 32px control; align the pipe label to the row's text line. |
| 59 | P1 | The website Approve/Reject pair commits with no confirmation | Two opposite-consequence POSTs, 32px tall, 8.1px apart, one tap, no undo — on the page where a manual approval needs a modal and two required fields. At 1024 they sit 251px past the visible card edge. | An inline confirm state, and a 44px pointer target. |
| 60 | P2 | `.gate` subjects are predicates | "Score threshold met / NOT MET". §9 finding 39 named this sentence as the problem, moved the result to its own row, and left the subject string — half the fix. Accessible name is `'Score threshold metMet'`: no separator between `.g-s` and `.g-r`. | Subjects become noun phrases; put a space between the spans. |
| 61 | P2 | Truncated identifiers are unrecoverable | Round and Task ids are JS `.slice(0,8)+"…"` with `title: NO` on all four measured cells. Rule 12; finding 33 fixed this class for the Details cell and left these. | `title` with the full id. |
| 62 | P2 | `.pill.p-neutral`'s ground is **1.08:1** | vs 1.35-1.51 for its siblings on the same page ground. Ink is 8.4:1 and it carries an icon, so nothing is unreadable — but the chip SHAPE is absent, which is the grammar §7 finding 1 protects. Same token drives `.sdot.n` and `.gate.na`; the gate was given a 3.69:1 boundary and these were not. | Give the neutral chip the same dashed/solid boundary treatment. |
| 63 | P2 | `.sdot.n` is a **3.23px** text middot where every sibling is a 16px drawn glyph | Six of eight evidence rows carry it; `.sdot.a` is a text `!`. This is §9 finding 40 verbatim, applied to `.gate` and not to `.crow` 350px below it. | `sdot()` returns `I.absent` / `I.warn`. |
| 64 | P2 | The tooltip's ground is byte-identical to the app bar, and lands on it | `#tip` background `rgb(0,62,114)` === `.topbar` background, and the Decision Rules tip renders at y=45 over a 72px bar. `placeTip` clamps to the window, not the bar. | Clamp to `.topbar` bottom + pad; flip below when it will not fit. |
| 65 | P3 | Six raw `font-size`/`line-height` pairs written in components | L133 `code,.mono`, L219 `.topbar .tool`, L391 `textarea`, L549 `.kv`, L582 `.tip dt`, L594 `.banner`. Three produce off-ladder renders. §2 forbids the raw pair. | Token every one. |
| 66 | P3 | Two faces in one form | `#ap-who` is Proxima 14/20; `#ap-why` is `ui-monospace` 12/16 from `textarea{font-family:var(--mono)}`. The reviewer's legally load-bearing sentence is typed into a code editor. | The reason textarea takes `--t-body`. |
| 67 | P3 | A fourth, untokenised radius | `border-radius:9999px` ×18 (`.tipbtn`, `.sdot`), and `.tipbtn::after` at 44px — defensible as a WCAG target, undeclared. | Name both. |

## Purpose audit — the absences

1. **There is no Reject.** Every recorded human judgement on this surface is an approval. The
   record cannot distinguish "nobody looked" from "somebody looked and declined."
2. **The page a reviewer opens to approve a company does not contain the company.** Registration
   number, jurisdiction, address, named director, website — the identity being verified — is raw
   JSON inside a `<details>` at the bottom of a 4200px page. The first 1000px is the engine's working.
3. **The required reason is write-only** (52).
4. **`demo-case-002` shows "all five must be true" over five NOT EVALUATED cells** with the reason
   only in a `title`.
5. **Verification Codes has no empty state** while its six siblings all use `emptyRow()`, and it
   overrides `SAY{}` at the call site twice (`pill("pending","Sent")`, `pill("fail","Expired")`) —
   rule 3, in a section no seeded state renders.

## Detector verdicts

Six of eight false positives, each disproved by measurement: `ai-color-palette` (the wordmark, in
the top bar, `closest('.page')===null`); four `cramped-padding` (flush cards with a real 16.00px
inset from cell padding; `#feedmore` at exactly 32.00px from `min-height`); two `line-length`
(measured 68-75 and 54 chars against an 80 floor); `em-dash-overuse` (the `—` null marker, a symbol
rather than punctuation); and 17-19 `gray-on-color` hits that are all `<pre>` inside closed
`<details>` — `checkVisibility()===false`, never painted. Two confirmed: `flat-type-hierarchy`
(three of four adjacent steps under 1.25, a declared trade-off) and the `.req` tuple.

## What is genuinely good

The score bar is a properly engineered three-layer graphic: ring 3.69/5.15, fill 4.70/6.29, tick
18.57/13.11 — every layer clears its floor in both themes. The page geometry is exact and stays
exact: 18 blocks at 0.00px edge delta across 24 runs, every top-level gap on the scale with zero
divergence by state or width, zero document overflow, zero confirmed overlaps, zero displaced
icons, zero `opacity<1` inside `.page`. The chip grammar is 100%: all 33 pills carry both an icon
and the raw enum. And the explainer system — ten bubbles, `<button>` triggers, Escape to dismiss,
following their trigger on scroll rather than vanishing when you scroll to reach them.
