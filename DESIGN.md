# Design system — KYC Tool console

The console at `/ui` is the back room of the IPv4.Global marketplace. A compliance reviewer opens
a company, reads the decision the platform was sent, and acts under their own name. That is the
whole job, and every rule below exists to make it faster or safer.

Everything ships in one file, `src/kyc_tool/ui/console.html`: tokens, components, views and the
embedded brand face. There is no build step and no second source of truth. Static pins in
`tests/unit/test_console_static.py` hold the parts a refactor could silently break.

**Mode: Operate.** Scanability, consistency and native expectations outrank expression. The brand
lives in precise details, not in flourishes.

---

## 1. Where the look comes from

The values are read off the marketplace's own rendered stylesheet (`auctions.ipv4.global`), not
re-imagined. A reviewer moving between the two should not feel a change of building.

| | |
|---|---|
| App bar | `#003e72` navy, IPv4.GLOBAL wordmark, `#02b5ea` accent in the mark |
| Side nav | white, 300px, grouped sections, current item blue-on-tint with a 3px left rule |
| Page | `#f8fafa` |
| Cards | white, 4px radius, one-step elevation, **no border** |
| Accent | `#0062ff`, one filled primary per screen; everything else outlined |
| Chips | pastel ground, dark ink of the same hue |
| Numbers | small uppercase label over a bold value |
| Face | Proxima Nova where the machine has it; embedded Mulish subset otherwise |

Proxima Nova is licensed and is **not** shipped from here. It is named first in the stack and
falls back to the embedded subset, which carries 291 Latin glyphs — no ✓ ✕ ⏳ → among them, which
is why every status glyph in the console is a drawn SVG rather than a character.

---

## 2. Type ladder

Six roles. **Size carries rank and nothing else**, so a reader can tell where they are from size
alone. One line-height per size, so a 14px body line and a 14px table cell share a baseline. One
tracking value for every uppercase run, so labels near each other match in colour.

| Token | Value | Role |
|---|---|---|
| `--t-page` | 700 22/28 | page title; the two display numbers |
| `--t-title` | 700 16/24 | card title — **rank B** |
| `--t-subhd` | 700 14/20 | heading inside a card body — **rank C** |
| `--t-body` | 400 14/20 | body, table cells, list rows |
| `--t-strong` | 700 14/20 | the same body line, emphasised |
| `--t-chip` | 600 13/18 | status pills and buttons — a button adds uppercase + `--track-caps` |
| `--t-label` | 600 12/16 | uppercase labels — **rank A**, `th`, KPI label |
| `--t-meta` | 400 12/16 | muted secondary, raw identifiers, hints |
| `--track-caps` | `.05em` | every uppercase run, no exceptions |

**Heading ranks.** Rank A (`h2.sec`) names a *group of cards* and is the only asymmetric heading
margin in the file — 32px above, 12px below, because the space belongs with the group it opens.
Rank B is a card's own title. Rank C is a heading inside a card body.

Two rules: **never nest rank B under rank A on the same subject** (that is one heading said
twice), and a card under a rank-A label does not repeat the label's name.

Never write a raw `font-size`/`line-height` pair in a component. Use a token.

---

## 3. Spacing

One scale: **0 · 2 · 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64** (`--s1`…`--s8`). Nothing off it.

Being on the scale is not enough — the *same role takes the same value everywhere*:

| Role | Value |
|---|---|
| Page gutter | 24/32 desktop, 12 below 720 |
| Card → card | `--card-gap` (16px), owned by `.split`/`.stack` gaps or scoped `.page > .card + .card` |
| Section label → its group | 32 above / 12 below |
| Card header | 12 / 16 |
| Card body | 16 |
| Table cell | 12 / 16 |
| Chip → chip | 8 |
| Icon → its label | 4 |

Layout does the spacing: siblings are laid out with flex/grid `gap`, not per-element margins. The
UA `<p>` margin is reset globally, because 1em of the element's *own* size means a 12px paragraph
and a 14px paragraph indent by different amounts.

---

## 4. The four chip families

The console has four kinds of "true" and they must look **deliberately** different, not
accidentally different by a few px of radius.

| | Shape | Type | Fill | Icon | Means |
|---|---|---|---|---|---|
| `.pill` | 24px, fully rounded (16px) | `--t-chip` | pastel, by status | **always** | a status a person acts on |
| `.tag` | 20px, square (4px), **outlined** | `--t-meta` mono | none, unless severity | rarely | a machine value quoted verbatim |
| `.pipe .st` | 20px, square, **joined** | `--t-meta` | track greys | never | one step in a fixed ten-step track |
| `.gate` | 66px grid cell, square, **two rows** | subject `--t-body`, result `--t-label` | pastel, by result | always | one row of a five-item checklist |

**The rule that matters: anything a reviewer reads as a status is a `.pill`, in every context** —
the list cell, the company header, the decision table, the legend, the task result. No exceptions.
A status rendered as bare text in one table and as a pill in another teaches the eye that the
chip grammar is unreliable, and then every cell has to be read.

Two projections carry their own vocabularies and get their own status→chip maps, so the same value
never renders two ways: `CASE_PILL` (case status) and `BUY_PILL` (buying status). The raw enum
always survives on the element's `title` for whoever is matching this against a log line.

**One exception, deliberate:** the Salesforce Fields table renders values as plain text. Its job is
to show exactly what the platform will write, verbatim — chipping it would misrepresent the payload.

Chip labels are **Title Case**, matching the marketplace.

---

## 5. Icons

Every icon is drawn in a `0 0 16 16` box at 1.25 stroke, in `currentColor`, so it inherits its
chip's ink. Scaling the box scales the stroke, so a larger icon is re-thinned to hold one apparent
weight: 18px nav icons at `stroke-width:1.11`, the 20px tile icon at `1`.

Alignment is flex's job. `display:inline-flex; align-items:center; gap:var(--s1)` centres an icon
against its label exactly. Where an icon must sit inline in running text, `vertical-align:middle`
aligns it to baseline + half the x-height, **not** the centre of the line box, so a 16px icon on
14/20 text lands 1.5px low; that is corrected explicitly — and **only there**. The same
correction applied through a descendant selector reaches icons inside flex parents, which are
already centred, and moves them off centre (§8, R2).

Status is never carried by colour alone — the glyph is the redundant cue, which is also what the
colour-blind reader and the contrast gates rely on.

---

## 6. Colour and contrast

Every colour resolves through one token layer. Dark keeps the navy bar and moves the page into the
same navy family; chips keep their own ground and ink in both themes so a status reads identically
wherever it is printed.

Floor: **4.5:1 for text, 3:1 for meaningful graphics, in both themes.** Measured chip pairs sit
between 7.5:1 and 8.7:1. Semantic colour (good / warning / critical) is separate from the accent
and does not count as the accent.

Never encode a state in `opacity`. It multiplies against whatever is behind it and lands wherever
it lands — that is how "not evaluated", the state an auditor most needs, ended up at ~2.9:1.

---

## 7. What the first audit found, why it broke, and what fixed it

A dual-agent critique (design review + deterministic detector with browser measurement) on
`e5aa116`. Scored 28/40 against Nielsen's heuristics, up from 17/40 before the restyle.

The headline: **the token layer was already disciplined — every padding, margin and gap was on the
scale, all 57 pills were identical to the pixel, all 141 icons shared one stroke, and text contrast
did not fail once.** What was inconsistent was *which correct value got chosen*. That distinction
set the fix: not a token cleanup, but component-level conventions saying which token a role uses.

| # | Found | Why it wasn't working | Fixed by |
|---|---|---|---|
| 1 | Same status, different component: Decision and Broker were pills in the list; Status and Buying were plain text. Method was 1-of-8 chipped. | The eye learns "pastel rounded thing = status." When half the status columns opt out, the grammar stops being trustworthy and every cell must be read — the largest tax on the primary task. | `CASE_PILL` / `BUY_PILL` maps and a `statePill()` helper; every status column in the list, header and decision table now renders a pill. |
| 2 | The company header's four chips: two carried an icon, two did not. Every other pill context was 100% iconed (52/52). | The icon is the non-colour status cue. Half a row having it makes it decoration rather than signal. | The two bare `p-neutral` spans became real `statePill()` calls. All four now carry an icon. |
| 3 | Five chip geometries with three radii, all pastel, within 800px of each other. | Four different vocabularies for one concept ("did this pass"), differing by 12px of border-radius — which reads as sloppiness, not meaning. | Each family given a job and a distinct form: `.tag` outlined and unfilled (it quotes machine data), `.pipe .st` joined into one track, `.pill` and `.gate` unchanged. Documented in §4. |
| 4 | Chip labels mixed Title Case and sentence case in the same row. | Two registers in one glance reads as two systems. | All chip vocabularies moved to Title Case in `SAY{}`. |
| 5 | 20 distinct type triples; 8 different styles at 14px; four of five size steps below a 1.2 ratio. | Size stopped predicting rank, so nothing could be skimmed. Three line-heights at one size meant adjacent lines never shared a baseline. | The eight-token ladder in §2, applied everywhere. **20 triples → 8.** |
| 6 | `<h2>` rendered two ways on one page (16px black card title; 12px muted uppercase section label), and `h3` rendered *larger and darker* than the `h2` above it. | On a 4520px page with eight identically-weighted sections there was no "new section" signal at all. | Three named ranks with a rule against nesting B under A; the duplicated Salesforce Fields heading removed. |
| 7 | Card-to-card gap was 0px on the company page, 16px on Data Sources, 32px after a section label. At 0px the two cards touched. | It merged "the decision the platform got" with "what the evidence says now" into one slab — the exact distinction the page exists to draw. | One `--card-gap`, applied by `.card + .card`. |
| 8 | Six equal-height KPI tiles; one label wrapped to two lines, so its number sat **16px below** the other five and the footnotes ended at four different heights. | A broken baseline on the first thing anyone sees on login. | Tile is a flex column; label reserves two lines (`min-height:32px`); footnote pushed to the bottom with `margin-top:auto`. **All six numbers and all six footnotes now align exactly.** |
| 9 | `.crow` measured 41px against a declared `--row-h: 36px`. | The 24px status dot plus 8+8 padding plus the rule overflowed the token that was supposed to govern it. | Row padding 8 → 4. Height is now 36px, as declared. |
| 10 | Every bare `<p>` carried the UA 1em margin. | 1em of the element's *own* size: a 12px and a 14px paragraph indented differently, off any scale. | `p{margin:0}` in the reset; margins declared where wanted. |
| 11 | The warning icon on Data Sources touched its label — icon right edge 412.0, text left edge 412.0 — and sat 3.5px above its optical centre. | `.irow .todo` used inline flow, where a 16px icon in a 12/16 line box cannot centre and no gap exists. | `display:flex; align-items:flex-start; gap:var(--s1)`. Measured after: **4px gap, 0.5px offset.** |
| 12 | Feed icons sat 1.5px below their label's centre. | `vertical-align:middle` targets baseline + half x-height, not the line-box centre. | An explicit −1.5px optical correction on inline `.ic`. |
| 13 | 18px and 20px icons were the same 16-unit artwork scaled up, thickening their stroke to an effective 1.41 and 1.56. | One drawn weight rendering as three. | `stroke-width` compensated per rendered size. |
| 14 | Uppercase tracking took five values (0.6 / 0.39 / 0.36 / 0.32 / 0.3 px). | Uppercase labels sitting near each other had visibly different colour. | One `--track-caps: .05em` everywhere. Em-relative, so it scales with size by design. |
| 15 | The Companies table held 1180px of content in a 1076px card — **104px hidden**, including "Last Updated" and the row-open chevron, with no cue. | The only ordering signal a reviewer has, invisible at the default desktop width. | Two score columns merged into one (`105`, with `now 115` beneath only when they differ), header shortened, chevron cell unpadded, plus scroll shadows painted on `.bd.flush`. **104px hidden → 8px, with a visible cue beyond that.** |
| 16 | `.gate.na` — "this rule was bypassed by a manual approval" — rendered at `opacity:.55`, computing to ~2.9:1. | The most audit-relevant state on the page was the least readable thing on it. | Full-strength muted ink on the card surface with a dashed edge: quieter than a result, still legible. |
| 17 | `.note` was defined only as `.toolbar .note`. Four of seven uses fell through to body type — including **both hints in the approval dialog**. | On the highest-stakes screen in the product, guidance was typographically identical to narration. | `.note` promoted to a global rule; `.flab` used for every form label. |
| 18 | The approval dialog's required fields carried no required marker, and never said the action was irreversible. | Highest stakes, lowest guardrail. | `*` markers, `aria-describedby` on both fields, and one line: the approval is appended permanently and cannot be withdrawn. |
| 19 | The dialog title was 22px — the same rank as the page `h1` behind it — over a `.55` backdrop that left the callout and gate strip fully legible. | The modal never took the top of the hierarchy it was interrupting. | Title dropped to rank B; backdrop to `.72`. |
| 20 | The Data Sources legend was a run-on sentence spaced by both an 8px flex gap and literal `·` separators, stranding one at every wrap. | A key that has to be parsed as prose. | Each pill and its gloss wrapped as one `.item`; separation by space alone. |
| 21 | "Buying not yet applicable" (chip) sat 130px above "Buying enabled" (decision line). | Both are correct — they are different fields with different lifetimes — but nothing on screen said so, so it reads as a bug and gets escalated as one. | The decision line now says "this decision: buying enabled". |
| 22 | `td.sub` declared `margin-top:2px` on 59 cells. | `margin` is inert on `display:table-cell` — a dead declaration. | Scoped to `div.sub, p.sub`, where it applies. |

**Detector false positives, disproved by measurement, not waved off:** `cramped-padding` on flush
cards (real 16px inset, carried by cell padding); `cramped-padding` on a button (breathing room from
`min-height`, which the rule doesn't read); `single-font` (two families render on every view);
`line-length` on a flex pill row and on cells measuring 40 and 49.8 chars. The `ai-color-palette`
hits are the marketplace's own `#02b5ea` in the wordmark — pinned by the brief.

---

## 8. The case page, and the three regressions the first pass caused

A second critique, scoped to `#/case/:id` — the screen the whole product exists to serve — on
`f97502f`, the commit that wrote §1–§7. Scored 25/40. The finding that matters most:

**Three of the defects were introduced by the fixes that wrote this document.** Each was a rule
written correctly for the component in front of me and applied through a selector that reached
further than that component. A design system is not safe once it is written down; the act of
applying it is itself a place things break, and the only proof is measuring the page after.

### The three regressions

| # | Introduced | What it did | Fixed by |
|---|---|---|---|
| R1 | `.who{text-transform:uppercase; letter-spacing:var(--track-caps)}` for the sidebar's reviewer label | `.fitem .who` — the actor on every activity-feed line — sets `font` but not case or tracking, so it inherited both. A reviewer's id printed `J.OKONJO`, at a 14/20/700-uppercase tuple that exists in no token. | Scoped to `.who-block .who`. The feed actor is back to `--t-strong`. |
| R2 | `td .ic{position:relative; top:-1.5px}`, the inline optical correction from finding 12 | `td .ic` matches every icon inside a `.pill`, `.iw` or `button.sm` in a table cell. Those parents are `inline-flex; align-items:center`, where `vertical-align` is inert and the icon is already centred — so the correction **displaced 12 icons to fix 4**. | The correction now names inline contexts only: `.demo-journey>.ic, .fitem .obj .ic, .probe-out>.ic`. Measured after: every table-cell icon at 0.00px. |
| R3 | `.tag{padding:1px var(--s2)}` | 1px is not on the spacing scale, and it was there to hit a 20px height. It set the height by accident, off-scale, in the one file that says every value is on the scale. | `padding:0 var(--s2); min-height:20px`. Same 20px, declared. |

### What the case page itself was doing wrong

| # | Found | Why it wasn't working | Fixed by |
|---|---|---|---|
| 23 | The five Decision Rules cells carried their result in the icon and the ground colour; the label named the *subject* ("Score threshold met") in both states. | Read aloud, a passed rule and a failed rule are the same sentence. §5 says status is never colour alone, and this was the one place it was. | A `.vh` visually-hidden utility. A met rule appends a hidden `— met`; a failed one prints `— not met` on screen, where the extra words also stop the two states looking like one another at a glance. |
| 24 | The evidence rows had the same defect: `.crow`'s status was a coloured dot, and the dot's `·` and `!` glyphs are text, so a screen reader announced "·Not Yet Available: Company email verified". | Half a status, plus a punctuation mark read as content. | The status word is a `.vh` prefix on every row; the dot and the bypassed gate's `·` are `aria-hidden`. Accessible text now reads "Passed: Company email verified +25 / 25". |
| 25 | `.gate.na` — fixed once already, in finding 16 — had been moved to `background:var(--surface)`, byte-identical to the card behind it. Its only boundary was a dashed `--line-strong` edge at **1.51:1**. | The state an auditor most needs to see has now been unreadable in two different ways. Removing an opacity is not the same as giving something a form. | A real ground (`--surface-muted`) and a border at the 3:1 control token (`--border-input`, measured 3.69:1). |
| 26 | `.sub-hd` declared `margin-bottom` only, so "Decision Rules" sat **0px** below the decision word above it. | Invisible on the two states anyone demos, because the enforcement-hold callout in between carried its own top margin. Every other state showed the collision. | `margin:var(--s5) 0 var(--s3)` — its own space on both sides, not borrowed from whatever precedes it. |
| 27 | The company header printed up to five chips, and on most states two pairs of them said one thing twice: "On Hold" beside "Awaiting Further Evidence" (same enum, two vocabularies), and "Manual Approval on Record" beside "Approved Manually". | Five chips is a summary; five chips where two are synonyms is a puzzle. The reader has to work out whether the difference is meaningful before they can ignore it. | The case-status chip renders only when it differs from the published decision; the manual-provenance chip renders only for the states the status cannot express (a record conflict, an unresolved order, a manual decision on a case not marked manually approved). **5 chips → 3–4.** |
| 28 | Decision History printed relative times only ("5h ago", four rounds deep). | It is the legally load-bearing part of the record and is read months later, when "5h ago" means nothing and every row says the same thing. | Absolute time, with the relative form kept underneath for the reader who is here now. |
| 29 | Three of the header's five chips carried no `title`, so the raw enum was unavailable on exactly the chips a reviewer would be matching against a log line. Rule 12 says it always survives. | The rule was held by the callers that happened to pass a label, not by the component. | `pill()` sets `title` itself. Now unconditional. |
| 30 | Two buttons in the top bar both read as primary: "Approve Manually" (peach, flat) and "Send Message" (filled accent, raised). §1 allows one filled primary per screen. | The screen's own primary action is approving; sending a test message is a utility. The elevation said the opposite. | `#sendbtn` becomes an ordinary outlined button. `button.warn` takes the raised elevation, and the dialog it opens confirms in the same colour, so the action keeps one identity from trigger to commit. |
| 31 | The threshold tick's label sat above the track, on the same line as the right-aligned verdict. At any threshold past ~70% of the scale they printed on top of each other — "Threshold 100" through "100 POINTS BELOW THRESHOLD". | Two absolutely-positioned things in one band with no reservation between them. | The label moved under the track into its own 24px lane. It now reads as the axis mark it is, and cannot collide with the line above at any threshold. |
| 32 | Off-token type: `.pipe .st.cur` and `.st.fail` at 700 (no token exists at 12/16/700), and `.verdict` on `--t-label` without the uppercase and tracking every other `--t-label` run carries. | Two ways to be emphatic at one size, and one uppercase family printing in two colours. | Both moved onto the ladder: the pipe steps to 600, `.verdict` to uppercase + `--track-caps`. The page's type inventory is **eight tuples, all of them tokens.** |
| 33 | The manual-review Details cell truncates JSON at 280px with an ellipsis and no `title`; the full value was unreachable. | The cell exists to be read. | Pretty-printed JSON on the `title`. |
| 34 | The Salesforce Fields table rendered `false` as "—" for every field except `Hard_Conflict__c`, which printed the word. | Two readings of one falsy value in one table, decided by a hard-coded field name. "—" means *not set*; it must not also mean *set to false*. | Only `null` and `undefined` render "—". Every real value prints, including `false`. |
| 35 | Every case tab's `document.title` read "KYC Tool · Console". | A reviewer works several companies at once. | The company's name, per case. |

**Detector false positives, again disproved by measurement:** `cramped-padding` on the flush cards
(the 16px inset is real, carried by cell padding); `#feedmore` (breathing room comes from
`min-height`, which the rule does not read); `single-font` (two families render); `line-length` on
flex pill rows and 40-char cells. The `ai-color-palette` hits are the marketplace's `#02b5ea`,
pinned by the brief — and not on this page at all.

---

## 9. The geometry pass

A third critique, scoped by the brief to alignment, box geometry, cramping and overlap, measured on
three live states at 1440/1100/1024/768/390 in both themes. Scored 28/40.

**The structure measured clean and the components did not.** 18 top-level blocks with 0.00px edge
deltas across 24 runs; 17 adjacent gaps all on the scale with no same-role divergence; zero document
overflow; zero real overlaps; no contrast failure in either theme. What was broken was four
components' internal geometry — and every one of them was a **layout default**, not a token choice.
§7 and §8 were both looking at tokens, which is why this class survived them.

| # | Found | Why it wasn't working | Fixed by |
|---|---|---|---|
| 36 | The ten-step round track wrapped to **4 rows at 1440** (7 at 1100, 9 at 1024) in a 266.8px column, **row gap 0.00px**, right edges ragged by 103.7px. Only steps 1 and 10 carried a radius, so every wrap point showed a hard square edge with a 1px border hanging in space. | §4 defines the track as "joined… never free-floating," precisely so it cannot read as a row of independent chips. Wrapped, that is exactly what it was, on every case and every round. | The track keeps its job as an 8px ten-segment meter that cannot wrap, and the step a reviewer acts on is named underneath it in words ("Decide · step 8 of 10"). The ten state names stay on the segments' own titles. |
| 37 | `.gates{repeat(auto-fit,minmax(140px,1fr))}` rendered **4 + 1 orphan** from ~1024 to ~1130px, with 485px of dead ground beside the orphan. | The copy one line above says "all five must be true." `auto-fit` cannot express five; it expresses "as many as fit", and the one width it lands on there is four. | `repeat(5,1fr)` above 1180px, `repeat(1,1fr)` below. The count is named, not inferred. |
| 38 | Tag runs and the approve/reject button pair were spaced **3.66px across and 1.00px on wrap** — the rendered width of a space at 14px, from `.join(" ")`. Every `gap`-spaced sibling on the page measured exactly 8.00. | Anisotropic, so no value on the scale can produce it. §3 says layout does the spacing. The same component was spaced three ways in one file (one call site already used `.join("")` → 0px). Two 32px commit buttons of opposite consequence 3.66px apart is also a mis-tap. | One `.chiprow` container at `gap:var(--s2)`, and the joins emptied. Six runs moved onto it. |
| 39 | `.gate` measured **44 / 56 / 58 / 74px** across states, differing by 12px and 16px **within a single five-item strip**. | The "— met" / "— not met" suffixes from finding 23 wrap the label, and `min-height:44px` has no counterpart. The em-dash clause was also not a sentence anyone would say: "Score threshold met — not met". | Two declared rows: subject on top at `--t-body`, result underneath at the whole `--t-label`. Uniform 66px in every state at every width, and the result reads. |
| 40 | `.gate.na`'s marker was a **2.89px** text middot against the **16.00px** box every sibling carries, shifting its label origin **12.11px** in the same grid slot. It also carried a 1px border no sibling had. | §5: every icon is a 16px box, alignment is flex's job. This state had now been wrong three times. | The drawn `I.absent` glyph, and a transparent 1px border on the base `.gate` so all three variants share one content box. |
| 41 | A pill in a table cell started its glyph **3px below** every other cell in its row (Decision History row 1: pill 2916.00, everything else 2912–2913). | `td{vertical-align:top}`: a 14/20 line box starts at +1, a pill — an 18px line centred in a 24px box — at +3. §2's stated reason for one line-height per size is that a body line and a table cell share a baseline. This is the table read months later. | `td>.pill{top:-3px}` for bare cells; `td>.chiprow.haspill{margin-top:-3px}` for a chip run, so the pill and its points move as one group. Measured after: **1px spread** across the row. |
| 42 | The score bar's empty channel measured **1.13:1** light / 1.17:1 dark against the card. | §6's floor for a meaningful graphic is 3:1. Past the fill there was nothing to say where the scale ends, so "45" had no denominator on screen. | An inset ring at the 3:1 control token, which draws the edge without changing the 16px box the fill and the tick are positioned against. |
| 43 | A **ninth type tuple**: all 22 `.gate` cells rendered `12/16/600/normal/none`, while `--t-label` renders elsewhere as uppercase + `.05em` on 98 elements. | `.gate{font:var(--t-label)}` sets the shorthand and inherits neither case nor tracking — **rule 14's failure mode, on the component §4 assigns that token to.** §8 finding 32's claim of "eight tuples, all tokens" was wrong; it counted by token name, not by rendered tuple. | The subject moved to `--t-body` (which also fixes the rank inversion below), the result to the whole `--t-label`. Measured after: **eight tuples, every one a token.** |
| 44 | The Evidence History header was a **51-character uppercase sentence**: "History (oldest first; faded entries were replaced)". Caught by the detector, missed by both eyes. | The token was applied correctly; the string was prose. Uppercase is for labels. | The `th` reads "History"; the sentence became a `.gloss` on the section heading — which then needed `text-transform:none` of its own, or it inherited the heading's caps and became a tenth tuple. |
| 45 | `opacity:.55` on superseded evidence, at `console.html:1363`. | Rule 9, surviving three audits because **no seeded state renders a replaced check**. | A `.was` class: retired evidence keeps its shape and loses its hue, its chip going to the neutral ground. |

Also fixed, all measured: the feed dot sat **2.00px** below its line (`top:14px` → `12px`); the title-to-chips
gap was **4.00px**, clearance on cap height alone, now 8; `.bullet .val` wraps at every width, not only
below 720; `.contrib` flows by column so the DOM order matches the two bordered lists the eye reads
down; the Salesforce card lost the title-less 41px header band its six siblings do not have; and
`.bmeasure`'s colour moved out of an inline `style` into CSS.

**Detector verdicts.** Six of seven findings were false positives, each disproved by measurement:
`ai-color-palette` is the marketplace wordmark in the top bar, outside `.page` entirely;
four `cramped-padding` hits are the flush cards, whose real 16px inset comes from cell padding, and a
button whose room comes from `min-height`; `line-length` measures 62–75 chars against an 80 floor;
`em-dash-overuse` counts 15, of which 5 are `.vh` accessibility text and 9 the null-value marker.
Two were real: `flat-type-hierarchy` (three of four adjacent steps under 1.25) and `all-caps-body`
(finding 44). A seventh claim — that hash-only navigation between cases does not re-render — was
also a false positive, disproved by re-measuring.

---

## 10. Type, purpose, and the surface the recipe was not measuring

A fourth critique, scoped to type consistency and to whether each block earns its place on the
screen a reviewer uses to approve a company. Scored 24/40 against §9's 28 — not a regression but a
wider net: this run scored the dialog, the tooltips, the disabled state, the dark theme and the
purpose of each section, where §9 scored `.page` at rest.

**The finding is about this document, not the page.** §12 says to measure "the computed type
tuples on `.page *`". The approval dialog and the explainer bubbles are both mounted **outside**
`.page`. All three off-ladder tuples and two of the three worst contrast failures lived in exactly
that region. Three audits measured a smaller surface than the reviewer uses, and the detector pass
reproduced the blind spot one level down by measuring with the dialog closed and no tooltip open.

### Defects the previous commit wrote

| # | Found | Why it broke | Fixed by |
|---|---|---|---|
| 46 | `.pipe-lab.done` at **1.25:1** in dark; `.fail` 1.59:1, latent | The meter's fill took `--green` (overridden per theme) and its label took `--green-ink` (one dark value in BOTH themes — the ink tokens exist to sit on a pastel `*-soft` ground). Light was 12.28:1, which is why the screenshots passed. `.cur` never failed because `--accent-ink` *is* overridden. | `color:var(--green)` / `var(--red)` — the tokens the segments above already use. |
| 47 | `.contrib`'s right column lost its last two dividers | `:nth-last-child(-n+2)` counts DOM position, and the same commit moved `.contrib` to `grid-auto-flow:column`. `:last-child` was no better — it spares one column's final row and not the other's. | No exception. All eight rows are ruled; the card's padding closes the list. |
| 48 | §9 finding 39's "uniform 66px in every state at every width" | A 25-width sweep found **86px from 1420 to 1181** — a band containing 1280 and 1366 — where one to four of the five subjects wrap. §9 measured 1440/1100/1024/768/390 and stepped over it. | The shorter subject names in finding 50 fit the 1181–1440 columns, so the strip holds 66px across the band. |
| 49 | §8 finding 27's chip suppression never fired on the state it was written for | The test was `c.status===decision` — raw enum equality, and `"approve"` never equals `"approved_manual"`. | `SAME_AS_DECISION`, keyed by decision. When the status is a *more specific* form of the decision the status wins and the decision chip goes; when they are the same fact the decision stays. Measured: three chips on every state, each saying something different. |

### The page

| # | Found | Why it wasn't working | Fixed by |
|---|---|---|---|
| 50 | The five rules asserted an outcome in their labels, so a failed rule printed "Score threshold met / NOT MET" | The label and the result contradicting each other on two lines. "No conflicting evidence / NOT MET" was worse — a double negative meaning the opposite of what it looks like. §9 finding 39 named this sentence, moved the result to its own row, and left the subject: half the fix. The accessible name also ran together as `"Score threshold metMet"`. | Subjects, not verdicts: Score threshold · Legal entity proof · Control proof · Broker screening · Evidence consistency, with Pass / Fail / Not evaluated. Two of the five names were already the console's own vocabulary. A literal space now separates the two spans. |
| 51 | **The page a reviewer opens to approve a company did not contain the company.** | Legal name, registration number, jurisdiction, address, director, RIR handle — the identity being vouched for — was a raw JSON dump inside a `<details>` at the foot of a 4200px page. The first 1000px was the engine's working. All three demo companies share a name and nothing visible told them apart. | A Company Details card, first, above the decision. Named column counts (rule 20), only the fields actually submitted, a website linked only when it is `http(s)`. The verbatim payload stays at the foot for whoever needs it exactly as received. |
| 52 | The commit button rendered at **2.08:1** for the whole duration of the POST | `button:disabled{opacity:.45}` — rule 9's own failure mode, at a worse ratio than the state the rule was written about, on the control that commits an irreversible approval. Findings 16, 25 and 45 each removed an opacity from a *component*; none re-read the base rule where the pattern was written first. | A declared ground and ink: 4.70:1 light, 6.29:1 dark, opacity 1. (Measured through the button's own `background-color` transition — an immediate read catches the old value mid-fade.) |
| 53 | The dialog's designed error was unreachable | With `required` alone the browser's validation bubble preempts the submit handler, lands on the field's hint text and vanishes on the next click — so the written message and its `role="alert"` live region only ever fired for whitespace-only input. And `#ap-err` was the form's **last** child, so the error rendered below the button that caused it. | `novalidate` on the form (the fields keep `required` for the accessibility tree); `#ap-err` moved above the action row. |
| 54 | The reviewer's reason was required, load-bearing, and never displayed | `humanizeAudit` returned `Approved manually` and dropped `detail_json.note`. The approver's name was absent from the decision card too. | The reason is appended to the feed line; the decision card prints "Approved by ⟨name⟩" when the pointed row is manual. |
| 55 | The clip cue measured **1.30:1 light / 1.05:1 dark** while 251–545px of table was hidden | Two units of blue, and it is the only thing satisfying rule 11. A black scrim on a navy card is nothing. | A `--scrim` token per theme — dark inverts it to white — strong enough to clear 3:1 against its own card. |
| 56 | The modal had no boundary in dark: **1.18:1** | `rgba(0,22,45,.72)` composites to almost exactly the dark page ground. §8 finding 19 tuned the backdrop in light only (7.5:1). | `border:1px solid var(--border-input)` on the dialog. |
| 57 | Three off-ladder tuples, all outside `.page` | `.tip dt` printed five English phrases in mono 12/16/700 from a raw triple; `.req` set `font-weight:700` onto a 12px `p.note`; `.banner` was a raw 14/20/600. | `--t-strong` on the tip term, colour-only on `.req`, and the banner tokenised. Measured after: **eight tuples on the page and five in the dialog, every one a token.** |
| 58 | `.sdot.n` was a **3.23px** text middot and `.sdot.a` an exclamation mark | Against the 16px box every sibling carries — §9 finding 40 again, in the component 350px below the one it was fixed in. | `I.absent` and `I.warn`. |
| 59 | Round and task ids were cut to 8 characters with no `title` | Rule 12; finding 33 fixed this class for the Details cell and left these. An auditor could not match a console row to a log line. | `title` with the full id on both. |
| 60 | Cancel was the only accent-coloured control in the dialog | Colour convention made Cancel read as the affirmative, beside a peach confirm. | Neutral outline; the only coloured control is the one that commits. |
| 61 | The irreversibility warning was the smallest, lightest text in the dialog | Computed-identical to the field hint beside it — two sentences of completely different consequence in one treatment. | The page's own tinted callout, in the same peach as the action it describes. |

**Still open, and deliberately not fixed here:** there is no Reject, so the record cannot
distinguish "nobody looked" from "somebody looked and declined"; the five tables still size their
columns by content mass rather than by role (first-column edges spread 256.5px and move 54.2px
with the data); and `--t-subhd` and `--t-strong` are byte-identical declarations, so rank C has no
render of its own.

---

## 11. Rules to hold

1. Anything a reviewer reads as a status is a `.pill` — every table, header, list and legend.
   Running prose is the one exemption: a status named inside a sentence stays a word.
2. Every pill carries an icon.
3. Chip labels are Title Case; one vocabulary per value, in `SAY{}`.
4. Size carries rank. Use a `--t-*` token; never write a raw size/line-height pair.
5. One line-height per size. One tracking value for uppercase.
6. Never nest a card title under a section label of the same name.
7. Spacing comes from `gap`, from the scale, and the same role uses the same value on every page.
8. Icon alignment is flex's job; inline icons get the explicit optical correction.
9. Never encode a state in `opacity`.
10. 4.5:1 for text and 3:1 for graphics, in both themes, or it does not ship.
11. A table that clips says so.
12. The raw enum survives next to the readable name — a mono subtitle or a `title`.
13. Only explicit authenticated actions mutate the service. Configuration saves create revisions
    for new runs; existing runs and decisions keep their recorded version. Salesforce is not written.
14. A rule that sets `text-transform` or `letter-spacing` is scoped to the block it was written
    for. Component rules that set only `font` inherit the rest from whatever else matched.
15. Optical corrections are for inline flow only. Never write one through a selector that can
    reach an icon inside a flex parent — it is already centred, and the shift breaks it.
16. Where a glyph carries the result and the label only names the subject, the result reaches
    the accessibility tree as text. Decorative glyphs are `aria-hidden`.
17. A record read after the fact carries absolute time. Relative time is for "now" only.
18. Two chips saying one thing is worse than one chip. A derived chip renders only when it
    differs from the chip it derives from.
19. Measure the page after applying a rule from this file. Three of the defects in §8 were
    written by the pass that wrote §7.
20. A fixed-cardinality set names its column count. `auto-fit` expresses "as many as fit", never
    "five" — and the width where it lands on four is a width people use.
21. A run of siblings is spaced by `gap` on a real container, never by a space in a template
    string. A space is a font metric: 3.66px across, 1.00px down, on no scale in either axis.
22. A component that cannot fit its column changes form; it does not wrap. Wrapping is the browser
    choosing a layout you did not.
23. A token is what RENDERS, not what is named. `font:var(--t-label)` without the case and tracking
    that token carries elsewhere is a second tuple, and counting by name will not catch it.
24. Measure the whole surface, not `.page`. The dialog, the tooltips and every `::backdrop` are
    mounted outside it, and that is where three audits' worth of defects were hiding.
25. Measure the STATES, not the resting page: disabled, in-flight, empty, error, bypassed — and
    both themes. A `*-ink` token holds one value in both themes; only its `*-soft` partner and the
    reading-strength aliases flip.
26. A label names its subject, never its outcome. The result is a separate word, and the two are
    separated by something a screen reader can hear.
27. Read a transitioned property after the transition. `getComputedStyle` immediately after a
    state change returns the value being animated away from.
28. Anything written into the markup is only a default if code rewrites it later. The router sets
    `document.title` per company, so a `<title>` spliced into the source is gone by the time the
    window has one. Take over the setter, or measure `document.title` after the first route and
    find out.
29. A fixed corner is somebody's corner. Bottom-left is the sidebar footer and the refresh
    control in it; bottom-right is the toast. A badge that floats over either is a control you
    have hidden. Put it in the flow of the thing it belongs to.
30. A configuration save is confirmed only by a successful server receipt. Conflicts retain the
    unsent draft; unknown outcomes retain the immutable request and offer only identical retry.
31. A native select's drawn chevron belongs inside the select's own border, not merely inside a
    wider wrapper. Measure both boxes and reserve text space before the icon.

## 12. Console first-pass layout contract

The shared header owns title, description and optional actions. Its internal
interval is 12px from title to description; the header as a
whole owns 24px before page content. An absent slot emits no element, because an empty flex child
can wrap and create visible space.

Layout parents own card rhythm. `.split` and `.stack` use one 16px gap and their children carry no
adjacent-card margin; only direct block-flow card siblings under `.page` retain the 16px margin.
Subsections own a 12px heading-to-content gap. Card headers keep their divider; the following
non-flush body or integration row owns 12px top padding. Flush tables get that clearance from
their cells, without another spacer.

The reviewer grid names three rows and centers its avatar on the input row. Identity fields use
content-sized tracks so an optional subline cannot move a neighboring primary value. The reviewer
helper reads "Recorded on every review action."

Standalone status legends are removed. Pills retain their own text and contextual help.
Native fieldset legends remain accessible names for related form controls.

Native selects fill their wrappers. The drawn chevron has a 12px right inset and ignores pointer
events; the select reserves 48px on the right for text clearance. Regular inputs, selects and
buttons share a 40px minimum height; compact table buttons keep their 32px variant.

Table cells use middle alignment with 12px vertical and 16px horizontal padding. Pills and chip
rows no longer carry the historical negative offsets recorded in §9. Preview-editor cells are
the scoped exception: they remain top-aligned with their live-value sublines. Salesforce mapping
inputs reserve a 230px minimum width; a narrow table scrolls inside its card instead of squeezing
the destination name. The score fill and threshold tick share one inner scale inset 4px from each
end of the track, so their percentages use the same width.

Options keeps System / Light / Dark in its Appearance form. Its browser-local helper and storage
error status sit in a separate footer, 24px below the choices with a divider and 12px top padding.
Navigation uses theme tokens for distinct active, hover and keyboard-focus states. The global
header's "Message authentication: On/Off" describes configuration, not service health or delivery;
the full labelled rules fingerprint and Copy control belong on Decision Rules, not global chrome.

Configuration editors load shared server values and expose one right-aligned Edit/Add action,
replaced by solid Save and secondary Cancel. Points edit evidence weights, not threshold or gates.
Mapping edits change destination names, not company values or sources. The single broker entry
form sits directly below its header and feedback, before the full searchable table; Save commits
the reviewed list without a separate Apply phase. Removal requires confirmation. Broker IDs and
notes come from the complete server snapshot. Overlap warnings use server matcher semantics.

Read, edit, saving, refusal, conflict and unknown states remain distinct. In-flight fields freeze;
HTTP confirmation controls saved state. Conflicts retain drafts until deliberate reload. Unknown
outcomes retain request identity and payload for bounded, operator-triggered identical retries.
Other open sections are not silently rebased. Old browser-preview records are ignored.

Options includes Operator access: a password control, Use credential and Clear. The credential
stays only in page memory and is routed only to same-origin console mutations, including existing
company/review actions. Reads keep their existing access contract. Missing browser credentials
link to Options; an entered credential still requires server authorization. Inactive configuration
is honestly read-only. Fingerprint and revision live together at the bottom of Decision Rules.

The message composer leads with company, action and the relevant labelled fields. The technical
event stays beside the action; Advanced JSON is optional. Its two-column workspace stacks through
1260px so support panels do not compress the primary form. Action and field grids become one
column through 720px; the response and numbered demo fixture remain separate panels. The review
summary repeats company ID, action, technical event, unique key and exact payload before Confirm
Send. Drafts stay in page memory, not browser storage, and survive timed refresh and event changes.
Sending is an explicit server operation. Response copy must not
turn acceptance into completed verification or treat a stored response as proof of a new action.

The reusable lesson is to test the relationship at the boundary that owns it. Two individually
valid 16px rules still make a wrong 32px gap, and a correctly sized element can align to the wrong
row. Browser checks measure sibling edges, row centers, native select borders and header intervals,
including the 1147px user viewport and both sides of the legend's 1319/1320px boundary, rather than
inferring correctness from token use.

Companies keeps its toolbar and search input mounted while only the result count and rows change.
The live input is authoritative; request sequencing and node identity prevent delayed results from
overwriting a newer query or route. Every route owns its document title, while the company view
retains its company-specific title and the development proxy may continue to prepend its marker.

## 13. Verifying a change

```bash
bash scripts/dev.sh                    # stack + console on :8080/ui
./manage.sh lint
.venv/bin/python -m pytest tests/unit/test_console_static.py tests/integration/test_ui.py
```

With Playwright available to Node, run the focused browser checks against the isolated local
console. The URL argument overrides their default `http://127.0.0.1:55717/ui`:

```bash
node scripts/check_console_layout.cjs http://127.0.0.1:55717/ui
node scripts/check_console_previews.cjs http://127.0.0.1:55717/ui
node scripts/check_console_composer.cjs http://127.0.0.1:55717/ui
```

These are explicit local verification commands, not a claim that browser checks run in CI.
Browser checks intercept all configuration mutations and composer send-event requests;
verification must not send real messages or fixture credentials to the displayed service.
The focused live-configuration runner also proves server-envelope wiring and response states.

Then look at every route, including Options and the editor/review states, in light and dark at
1440 and 390. Check the document does not scroll sideways at 1440 / 1147 / 1024 / 768 / 390,
and that the type inventory has not grown. Check contained table scrolling separately from document
overflow, including whether editable destination names are readable inside their inputs.

Measure the whole surface, not `.page`: open the approval dialog and at least one explainer
bubble before you count type tuples, because both are mounted outside it and that is where three
audits' worth of defects were hiding (§10). Toggle a control to `disabled` and read it after its
transition settles. Do all of it in both themes.

Then **measure it**, on more than the state you were looking at. The case page has five seeded
states (`demo-acme-1`, `-2`, `-3`, `demo-northwind`, `demo-ipxo`) and they do not exercise the
same branches: the enforcement-hold callout, the bypassed-gate cells and the manual-provenance
chip each appear on two of the five. A collision that is invisible on the state you demo is
still shipped. What to read off the DOM: the computed type tuples across the visible console,
open dialog and explainer (they must stay within the declared tokens), `getComputedStyle().top`
on every in-flow `.ic` inside a flex parent (0px; exclude the positioned select chevron),
the accessible text of `.gate` and `.crow` with `aria-hidden` subtrees excluded, and the gap
between each pair of adjacent blocks.
