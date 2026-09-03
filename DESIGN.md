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
| `--t-chip` | 600 13/18 | status pills only |
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
| Card → card | `--card-gap` (16px), via `.card + .card` |
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
| `.gate` | 44px grid cell, square | `--t-label` | pastel, by result | always | one row of a five-item checklist |

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
14/20 text lands 1.5px low; that is corrected explicitly.

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

## 7. What the audit found, why it broke, and what fixed it

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

## 8. Rules to hold

1. Anything a reviewer reads as a status is a `.pill`. Every context. No exceptions.
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
13. Presentation only: nothing in this file changes what the engine computes, stores or sends.

## 9. Verifying a change

```bash
bash scripts/dev.sh                    # stack + console on :8080/ui
./manage.sh lint
.venv/bin/python -m pytest tests/unit/test_console_static.py tests/integration/test_ui.py
```

Then look at it: seven routes, light and dark, at 1440 and 390. Check the document does not scroll
sideways at 1440 / 1024 / 768 / 390, and that the type inventory has not grown.
