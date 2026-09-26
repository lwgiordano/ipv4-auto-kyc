---
target: "the individual case page: spacing, text and inconsistencies"
total_score: 24
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 5
timestamp: 2026-09-21T18-41-38Z
slug: src-kyc-tool-ui-console-html-case-demo-case-001
---
Method: dual-agent (A: isolated design review, source-only · B: detector + Chromium measurement —
4 combos, light/dark × 1440/390, 780 DOM nodes, one synthesised fixture). Target `9d91e25`.
Parent verified A's three load-bearing claims and one persona claim against the source directly;
one of A's claims was wrong and is corrected below.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | `viewCase` awaits its fetch before writing anything (1850–53), so case→case shows the *previous* case until the response lands. No skeleton, no dim. |
| 2 | Match System / Real World | 3 | "the database has prohibited this state since migration 022" (2020); "today's weights are not substituted" (2033). |
| 3 | User Control and Freedom | 2 | Still no Reject for a case. "Approve Website" commits a named person's verdict on one click; approving a case needs a modal and two fields. |
| 4 | Consistency and Standards | 2 | Measured: 3 cards titled at 700 16px/24px inside a bordered header, 8 titled by an outer 600 12px/16px uppercase label. Both are `h2`. |
| 5 | Error Prevention | 2 | `button.warn` (424) declares exactly `button.primary`'s four properties (410) while two comments above it specify peach. |
| 6 | Recognition Rather Than Recall | 3 | Strong `title`/enum discipline, 12 explainers — but the threshold is stated three ways in one card and there are no in-page anchors on a 4760px page. |
| 7 | Flexibility and Efficiency | 2 | The 5s refresh rebuilds `page.innerHTML` wholesale: typed input, open `<details>` and the expanded feed all die. `needReviewer()` fires after the click. |
| 8 | Aesthetic and Minimalist Design | 2 | 11 cards, 6 tables, 23 `<summary>` elements, two raw-JSON `<pre>` dumps. The reviewer's decision surface is the first 1000px of 4760. |
| 9 | Error Recovery | 2 | The 404 branch (1855) drops the breadcrumb and offers no way back; Salesforce Fields has no empty state at all (2054). |
| 10 | Help and Documentation | 4 | Twelve real `<button>` explainers, keyboard-reachable, Escape-dismissible, and they follow their trigger on scroll instead of closing. |
| **Total** | | **24/40** | Same score as the last run, different composition |

## Design Specificity Verdict

**LLM assessment — specific in language, generic in shape.** The vocabulary is unmistakably this
product: `SAY{}` (1169–1235) turns engine enums into "Blocked Broker List", "RIR contact verified",
"Buying locked pending Org ID verification"; the Org ID panel (1896–1910) is a real ARIN/RIPE/
APNIC/LACNIC/AFRINIC workflow. The page's central move — the published decision held apart from
the live evidence score, with the meter deliberately grey so two authority eras never merge into
one read (1866–71) — is a domain problem solved in the layout, and the comment defending it will
survive the next editor. But the *shape* is stock admin: title bar, chip row, eleven stacked cards,
six tables, a feed. Seven of the eleven blocks are the engine explaining itself. Swap `SAY{}` and
this is any job-runner console.

**Deterministic scan.** `detect.mjs` exit 2, **one finding**: `flat-type-hierarchy` at "line 7".
The evidence is an artifact — jsdom cannot resolve `font: var(--t-page)`, so the detector never saw
the real ladder and sampled six literal `font-size:` declarations, five of which belong to selectors
that never render on this page. But the finding survives on corrected numbers: the true rendered
ladder inside `#page` is 12/13/14/16/22px = 1.83:1 top-to-bottom, still under the rule's 2.0, with
three of four steps (1.08, 1.08, 1.14) below its 1.25 target. Every other one of the 59 registry
rules is clean, including `low-contrast`, `cramped-padding`, `tiny-text` and `text-overflow`.

**Browser measurement.** Chromium did complete renders in this container this time — the earlier
failure did not reproduce. Four combos, zero page errors. **No user-visible overlay was injected**;
this was a read-only measurement pass, so there is nothing to look at in a browser tab.

## Overall Impression

This page knows exactly what it is saying and has not decided how to say it. The copy discipline is
genuinely good and the one hard domain idea — published fact versus live evidence — is carried by
the layout rather than by a caption. What has not converged is the *grammar*: one page carries three
heading patterns, two card gaps, two renderings of one type token, and one button class that
contradicts the two comments written above it. The single biggest opportunity is to pick one card
grammar and let the rhythm fall out of it; roughly half the spacing findings below are downstream
of that one choice.

## What's Working

1. **The published/live split, and the grey meter** (1866–71, 2001, 2024, `.btrack` 573–87).
   Colouring the bar by the published decision would have collapsed two facts with different
   lifetimes into one read. The code refuses and records why.
2. **The explainer system** (`.tipbtn` 753–79, `TIPS` 1240–1416). Twelve on this page, all real
   `<button>`s, Escape-dismissible, and they follow their trigger on scroll rather than closing
   when you scroll to reach one. Each body is a sentence a reviewer would say.
3. **The measured hygiene is real, and it improved.** Zero text-contrast failures across all four
   combos (67–68 pairs each). Every truncation site carries a `title` — three for three. Zero
   colour literals on any case-page selector, including the four JS-computed ones. No page-level
   horizontal scroll at 390px. Several of these were open findings in earlier runs.

## Priority Issues

**[P0] `button.warn` renders as a second filled primary, in the accent colour, on the two most
consequential controls.** `console.html:424` vs `410`.
Both declare `background:var(--accent);border-color:var(--accent);color:var(--on-accent);
box-shadow:var(--e2)` — byte-identical. The two comments immediately above say *"Peach for the
action that bypasses the rulebook … deliberately NOT the filled blue"*. It is the filled blue.
`.warn` is used three times in this flow: the header's "Approve Manually" (1997), every row's
"Approve Website" (1947), and the dialog's commit (`#ap-go`, 969). So the page renders 1+N raised
accent-blue primaries against DESIGN.md §1's "one filled primary per screen" — the same defect §8/30
fixed for `#sendbtn`. Worse, "Reject Website" (1948) is the default outlined accent-blue button
sitting 8px away, so two opposite-consequence commits are the same hue with no confirmation step
(2102–2110).
*Why it matters:* a mis-tap records a named reviewer's verdict on a real company, permanently.
*Fix:* give `.warn` the amber family it documents (`--amber-soft` ground, `--amber-ink` ink, keep
`--e2`); make "Reject Website" neutral-outlined the way `#ap-cancel` already is (848–49); add an
inline confirm to the website pair.
*Suggested command:* `/impeccable harden`

**[P1] One page, three heading patterns — and the rendered hierarchy runs opposite to the outline.**
`1846, 2001, 2024` vs `2036–2073` vs `2009`.
Measured: `.card > .hd > h2` ×3 at **700 16px/24px**, sentence weight, no tracking. `h2.sec` ×8 at
**600 12px/16px uppercase, 0.6px tracking**. `h3.sub-hd` ×1 at **700 14px/20px**. The outer label
that names a *group* renders 4px smaller than the inner title it contains, and the `h3` nested two
levels deeper renders between them. Every one of the eight rank-A labels names a group of exactly
one card, which is not what DESIGN.md §2 defines rank A for. The prior run raised this; removing the
gloss from `sec()` made it more visible, not less.
*Fix:* pick one. Either give all eleven cards a `.hd` with a rank-B title and drop `h2.sec` from
this page, or drop the three headers and label all eleven.
*Suggested command:* `/impeccable layout`

**[P1] Two inter-section gaps, and the three most important cards get the smaller one.**
`.page>.card+.card` (296) vs `h2.sec` (795).
Measured on `#page`'s direct children: **16px** between Registration details → Decision Sent to
Platform → Current Evidence Score, then **32px + 12px** before every one of the eight later
sections. The reviewer's actual decision surface is the most tightly packed region on the page,
and the provenance tables get the ceremony. This is downstream of the heading issue: fix that and
the rhythm resolves with it.
*Fix:* one card gap for the whole page once the grammar is unified.
*Suggested command:* `/impeccable layout`

**[P1] The header chip row butts against the contact line at 0px — and the gap changes with the
payload.** `1980`, `.tbar .heading` 280, `.tbar .desc` 284, `.chips` 802.
`tbar()`'s `desc` slot receives `caseContactLine(c)` (a `div.sub`) immediately followed by
`<span class="chips">`, which is `display:flex` with no margin. So a row of 24px pills sits flush
against the 16px contact line. When the registrant submitted no email and no title,
`caseContactLine` returns `""` and the chips inherit the `.heading` grid's 12px gap instead — the
same row spaced two ways depending on the payload. DESIGN.md §9 fixed "title-to-chips 4.00px → 8";
this adjacency was not covered. *(Derived from the CSS; B measured `#page` children, not this
nesting, so it is not browser-confirmed.)*
*Fix:* emit the chips as a sibling of `.desc` so the `.heading` grid owns the rhythm, or
`.tbar .desc .chips{margin-top:var(--s2)}`.
*Suggested command:* `/impeccable layout`

**[P1] Engineering prose in the one branch that tells a reviewer their record is broken.** `2020`.
*"…the database has prohibited this state since migration 022"* and *"These records predate the
ordering fix."* A compliance reviewer cannot act on a migration number, and nothing on screen names
who can. Adjacent: *"today's weights are not substituted"* (2033) is engine-speak for "we will not
re-score this with current rules".
*Fix:* "No decision can be shown: this case's decision record points somewhere it should not. Raise
this with the tool's operators before acting on the case — do not approve from this page." Move
`migration 022` to the `title` or the Activity Log.
*Suggested command:* `/impeccable clarify`

**[P1] Three behaviours for "nothing here", in one page.** `2054`, `2066`, `2047`.
Salesforce Fields is the only one of six tables with no `emptyRow` fallback — an empty `<tbody>`
under a header row. Decision History passes an empty body (`emptyRow(6,"No decisions yet","")`), so
it prints a bare headline where the other five give a next step. Verification Codes deletes its
entire section when there are no codes, so a reviewer cannot tell "none were sent" from "this does
not apply to this case".
*Fix:* `emptyRow` with a real next step on all six; render Verification Codes always.
*Suggested command:* `/impeccable harden`

**[P2] One value, three vocabularies — twice, within 400px.** `1996`/`2005`/`2062`, and
`2026`/`2027`/`2030`.
Buying reads "Buying Locked Pending Org ID" (header chip, Title Case), "Buying locked pending Org ID
verification **(this decision)**" (decision line, sentence case with a trailing parenthetical
fragment), and again under a column headed "Buying". The threshold is stated three times inside one
card: "of 60 points required", "15 points below threshold", "Threshold 60".
*Fix:* one vocabulary per value; put the scope in the label ("Buying on this decision"), not in a
parenthesis. State the threshold once, on the axis.
*Suggested command:* `/impeccable clarify`

**[P2] Three time formats, and the legally load-bearing one is locale-dependent with seconds and
no timezone.** `fmtExact` 996, `when` 1001, `exact` 1005.
`fmtExact` is `toLocaleString(undefined,{hour12:false})`, so "Decided 9/21/2026, 14:03:22" renders
as `21/09/2026` for a reader in another locale, carries seconds nobody needs, and names no zone.
Decision History uses absolute-over-relative; Verification Rounds, Verification Codes and the
Activity Log are relative-only with the absolute on `title`. DESIGN.md rule 17 says a record read
after the fact carries absolute time — Rounds and Codes are records read after the fact. §8/28
fixed this for Decision History alone. Compounding it: "Decided …" sits in `.verdict`, which is
`--t-label` **uppercase with 0.6px tracking**, so a full timestamp renders as a tracked all-caps
label. `liveNote` lands there too, so "15 points below threshold" is an uppercase sentence.
*Fix:* one explicit formatter (`2026-09-21 14:03 UTC`); absolute wherever a record is read; and
take the phrase out of a label style.
*Suggested command:* `/impeccable typeset`

**[P2] `.pill` is the only `--t-chip` user that skips the caps treatment — and the token says pills
own it.** `356` vs `403`; token comment at `86`.
Measured: 7 unique type tuples on the page, and `--t-chip` is the only token rendered two ways —
`600 13px/18px` sentence-case untracked on 19 `.pill`s, and uppercase + `--track-caps` on every
`button`. The token's own comment reads `/* status pills only */`, so its declared owner renders
one way and its borrower renders the other. `--t-label` is uniform across all six of its users.
*Fix:* decide which treatment `--t-chip` carries and apply it to both, or split the token.
*Suggested command:* `/impeccable typeset`

## Persona Red Flags

**Alex (impatient power user)**
- Turns on "Refresh every 5 seconds" (943, 2717–18) and `route()` replaces `page.innerHTML`
  wholesale: the note he was typing into `#org-ask-note` (1903) is gone mid-sentence, the revealed
  `#org-record-form` re-hides, every open `<details>` in the feed snaps shut, and the expanded feed
  collapses. DESIGN.md §12 protects composer drafts through the refresh tick; this page has no
  equivalent.
- Case → case renders the *previous* case until the fetch lands (1850–53). No pending cue.
- 4760px, eleven blocks, no anchors, no sticky action bar. The only Approve button is in the title
  bar, so after reading the evidence at the bottom he scrolls all the way back.
- `needReviewer()` (1126) validates *after* the click: he presses "Request from contact", gets a
  toast telling him to go type an ID, and the drawer opens under him.
- The Details cell truncates at an inline `max-width:280px` with the full JSON only on `title` —
  measured 1360px of content in a 280px box, hoverable but not copyable and not keyboard-reachable.

**Sam (keyboard + screen reader)**
- Six tables (2037, 2041, 2045, 2048, 2052, 2060) with **no `<caption>`, no `aria-label`, and no
  `scope` on `th`**. In table-navigation mode they are six unnamed tables; their only names are
  `h2.sec` elements with no programmatic association.
- `#feedmore` (1973 / handler 2077) reveals `#feedhead` and then removes its own parent, destroying
  the focused element. Focus falls to `<body>` and nothing is announced.
- The Activity Log renders up to fifteen `<details><summary>Record</summary>` — a controls list of
  fifteen identically named items.
- Both Org ID forms live inside one `<td>` (1902–10); `#org-record-toggle` has no `aria-expanded`
  and no `aria-controls`.
- `.p-neutral` (363) and `.sdot.n` (612) sit on `--surface-muted` against `--surface` with no
  boundary, so the chip *shape* is missing for "Not Yet Available" — the state where form matters
  most. (This is a boundary, not a text-contrast failure: B measured 0 text pairs below 4.5:1.)
- `.tip` is `pointer-events:none` (748), so a magnifier user cannot hover into the bubble to read it.

**Correction to A's report:** A reported that `toast()` appends a plain `<div>` with no live-region
role. It does not — `console.html:994` sets `role="alert"` for failures and `role="status"` for
successes before insertion. The narrower concern stands (a region created and inserted in the same
tick is less reliably announced than a persistent one), but the flat claim was wrong.

## Minor Observations

- **Four of eight inline `style` attributes carry no data**: `color:var(--muted)` on a missing check
  name (1885) beside `.pts.zero`, which is a class; `color:var(--red)` on a run error (1935); the
  `max-width:280px` truncation block (1944); and `border-top:1px solid var(--line)` (2055), the
  page's only hand-drawn divider. DESIGN.md §9 moved `.bmeasure`'s colour out of an inline style for
  exactly this reason.
- **Off-ladder geometry, measured**: `.pill{padding:2px var(--s3)}` (356) is the only off-scale
  padding on the page; `div.sub,p.sub{margin-top:2px}` (397) and `.feed::before{left:6px}` (688) are
  the only other true spacing literals. `.card > .bd` is 12px top / 16px bottom — asymmetric.
  `280px` (1944), `44px` (607), `60px` (639) and `180px` (439) are the size literals.
- **"Registration details"** (1839, 1846) is the page's only sentence-cased card title among eleven
  headings; DESIGN.md §10/51 calls the same card "Company Details".
- **The reviewer's justification is typed into a code editor.** `textarea` (439–40) sets
  `font-family:var(--mono);font-size:12px;line-height:16px`, overriding `input,select,textarea
  {font:var(--t-body)}` (435). `#ap-why` — the written reason for bypassing every rule — is the only
  monospace 12px field in a dialog where everything else is 14px sans.
- **`.banner` is still a raw `600 14px/20px`** (781–83) in no token, and `DESIGN.md:312` (row 57)
  records it as *"the banner tokenised … every one a token."* The document and the code disagree.
- **Two buttons one word apart in one cell**: "Record handle" reveals a form (1905); "Record"
  submits it (1908). The Org ID `<td>` holds two `<form>`s and six controls for one question.
- **Up to five header chips from five vocabularies** (1980–96) — decision, manual provenance, case
  status, buy status, broker status. DESIGN.md §10/49 claims "three chips on every state"; the
  ceiling in the code is five.
- `.pts` prints "0" and "+25" in the same column (1886–87) — two grammars for one number.
- `decisionColor`'s `var(--faint)` branch (1872) is dead: the value is only read inside
  `latest.decision ? …` (2003).
- `sec()` emits `class="sec "` with a trailing space (1522).
- In-table controls at two heights: `button.sm` 32px for `#feedmore` and both Org ID buttons,
  default 40px for the website pair (1947–48), against DESIGN.md §12.
- `#approvebtn` simply vanishes once the case is approved manually (1997), with nothing in its place
  naming the approver or the date. The only confirmation was a toast that has already gone.
- The 404 state (1855) drops the breadcrumb and gives no route back to Cases; the router's own error
  state (2682–84) at least offers "Try again".

## The recurring blind spot

The prior run's structural finding was that the console's own verification recipe measures
`.page *`, while the approval dialog and the tooltip bubbles are mounted **outside** `.page` — so
three audits in a row measured a smaller surface than the reviewer uses. It happened again here:
B's measurement scope was `#page`, and its own "not taken" list names the dialog and the tooltip
bubble. Both of this round's type findings that are off the ladder — `#ap-why`'s 12px mono and
`.banner`'s untokenised `600 14px/20px` — live in exactly that unmeasured region, which is also
where `DESIGN.md:312` records a fix that is not in the code. The measurement boundary, not the
type, is the durable defect.

## Questions to Consider

1. Seven of these eleven blocks are the engine explaining itself. If Verification Rounds,
   Verification Codes, Salesforce Fields and Submitted Details moved behind one "Engine record"
   link, would any reviewer miss them in the first month — and what is the first 1000px currently
   competing with?
2. A manual approval demands a modal, a name, a written reason and an irreversibility warning. A
   website review demands one click on a blue button 8px from another blue button. Both are a named
   person's judgement on a real company; both are appended permanently. Which one is mispriced?
3. The header assembles up to five chips in five vocabularies, and DESIGN.md has twice written
   suppression rules to stop them contradicting each other. What is the one sentence a reviewer
   actually needs at the top — "Rules say approve; this installation is waiting on you" — and why is
   the page still assembling it from chips instead of writing it?
4. `DESIGN.md:312` says `.banner` was tokenised; `console.html:781` says it was not. When the
   document and the code disagree, which one is the design system?
