---
target: the case page, aesthetic/geometry pass
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 2
timestamp: 2026-09-03T19-53-13Z
slug: src-kyc-tool-ui-console-html-case-demo-case-001
---
Method: dual-agent (A: isolated design review · B: detector + browser evidence). Target the case page
at `f97502f` + working tree, measured on three live states (held / approved-by-hand / thin evidence)
at 1440/1100/1024/768/390, light and dark.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The ten-step round track breaks into 4 flush rows; the score bar's empty channel is 1.13:1. |
| 2 | Match System / Real World | 3 | "Score threshold met — not met"; "Approved — not evaluated" ×5. |
| 3 | User Control and Freedom | 2 | "Show N Earlier Entries" is one-way; nothing collapses a 747px table on a 4300px page. |
| 4 | Consistency and Standards | 2 | Six measured breaks of the page's own written system, including a ninth type tuple. |
| 5 | Error Prevention | 4 | The approval dialog is the best-built thing on the page. |
| 6 | Recognition Rather Than Recall | 3 | Details cell truncates at 283px, recoverable only by hover. |
| 7 | Flexibility and Efficiency | 2 | No shortcuts; reviewer ID retyped per tab; no jump between eight sections. |
| 8 | Aesthetic and Minimalist Design | 2 | Four visible geometry failures, all measured. |
| 9 | Error Recovery | 3 | 403/401/409 each get specific copy. |
| 10 | Help and Documentation | 4 | Every card explains itself; the hold callout teaches. |
| **Total** | | **28/40** | **Exact macro rhythm, broken micro-geometry** |

## Design Specificity Verdict

Authored for this product. The published-vs-live authority split, the five-gate strip with its bypassed
state, the peach button reserved for the action that bypasses the rulebook, the "(this decision)"
qualifier, and the four-sentence hold callout are decisions about THIS product's failure modes. Where
it lapses is not the look but the mechanics of four components — the wrapped `.pipe`, the `auto-fit`
gate grid, the whitespace-joined tag runs, the `vertical-align:top` pill. Those are the parts nobody
drew; they are the parts CSS defaulted into.

## What the brief asked, answered

- **Edge alignment: clean.** 18 top-level blocks × 3 states × 4 widths × 2 themes = 24 runs, every
  left and right delta 0.00px. Column 332→1408 at 1440.
- **Vertical rhythm: clean.** 17 adjacent gaps per state; crumb→bar 4, bar→card 24, card→card 16,
  card→section 32, section→card 12. Zero off-scale, zero same-role divergence.
- **Overlap: none real.** ~2,000 intersecting pairs, every one a collapsed `<details> > pre`;
  `elementsFromPoint()` never returns it. The threshold-tick label overhangs `.verdict` by 65–80px
  horizontally but sits 384–710px below — no collision at any width.
- **Overflow: clean.** `scrollWidth == clientWidth` in all 24 runs.
- **Contrast: clean.** Every text pair 4.70–9.6:1 both themes; `.gate.na` border 3.69 / 5.15.

## Priority Issues

| # | Sev | Found | Measurement | Why it fails | Fix |
|---|---|---|---|---|---|
| 36 | P0 | `.pipe` wraps into flush rows | Column 266.8px at 1440; 4 rows per round (7 at 1100, 9 at 1024). **Row gap 0.00px.** Right edges rag 103.7px. Only steps 1 and 10 carry a radius. | §4 defines the track as "never free-floating," precisely so it cannot read as independent chips. Wrapped, it is four grey-green slabs. Every case, every round. | Collapse to a rule + current step, ten-step form on `title`. Or `row-gap:var(--s1)`, radius on all steps, `min-width:340px` on the column. |
| 37 | P1 | `.gates` renders 4 + 1 orphan | `repeat(auto-fit,minmax(140px,1fr))`: 5 cells at 1440/1280/1180; **4+1 at 1130→1024**, ~485px dead ground beside the orphan. | The copy one line above says "— all five must be true." `auto-fit` cannot express five. A 1280×1024 monitor with a sidebar shows a broken checklist. | `repeat(5,1fr)` above 1180px; `repeat(1,1fr)` below. Name the cardinality. |
| 38 | P1 | Chip runs spaced by a literal space | `.tag`→`.tag` **3.66px** across, **1.00px** on wrap; `button.sm.good`→`.danger` 3.66px; `.pill`→`b.strong` 3.65px. `.pill`, `.gate`, `.tbar button` all measure exactly 8.00. | 3.66px is the rendered width of a space at 14px — anisotropic, so no scale value can produce it. §3 says layout does the spacing via `gap`. The same component is spaced three ways in one file (one call site uses `.join("")` → 0px). Two 32px commit buttons of opposite consequence 3.66px apart is a mis-tap. | Flex container with `gap:var(--s2)` around both runs; `.join(" ")` → `.join("")` inside them. |
| 39 | P2 | `.gate` height varies within one strip | 44 / 56 / 58 / 74 across states and widths; **within one five-item strip two rows differ by 12px (-001 @768/390) and 16px (-002 @1024)**. | The "— met" / "— not met" suffixes (finding 23) wrap the label, and `min-height:44px` has no counterpart. §4 declares a 44px cell. | Shorten the label (see Q2) or give `.gate` a fixed two-line box. |
| 40 | P2 | `.gate.na`'s marker is a text glyph | `·` measures **2.89px** against the **16.00px** SVG box every other gate carries; label origin shifts **12.11px** in the same grid slot. `.na` also carries a 1px border no sibling has, so its box is 2px taller. | §5: every icon is a 16px box, alignment is flex's job. §4: families differing by a few px "reads as sloppiness, not meaning." This state has now been wrong three times. | Use the existing `I.absent` glyph; `.gate{border:1px solid transparent}` so all three variants share one content box. |
| 41 | P2 | A pill in a table cell prints 3px low | Decision History row 1 text-node tops: pill label **2916.00**, all five other cells 2912–2913. **5px baseline spread in one row.** Repeats in Evidence History. | `td{vertical-align:top}`: a `.pill` centres its 18px line in a 24px box. §2's stated reason for one line-height per size is that a body line and a table cell share a baseline. This is the table read months later. | `td > .pill{position:relative;top:-3px}`, following the inline-icon precedent. |
| 42 | P2 | The score bar's empty channel is invisible | `.btrack` `#f0f1f2` on card `#ffffff` = **1.13:1**; dark 1.17:1. §6 floor for a meaningful graphic is 3:1. | On a 45-point case the fill ends at 45% and the tick stands at 76.9%; between and beyond, nothing is visible. Without a right end, "45" has no denominator on screen. | `border:1px solid var(--border-input)` — the 3.69/5.15 token `.tag` and `.gate.na` already use. |
| 43 | P2 | A ninth type tuple, off the ladder | All 22 `.gate` cells render `12px/16px/600/normal/none`. `--t-label` renders elsewhere as uppercase + 0.6px (98 elements). | `.gate{font:var(--t-label)}` sets the shorthand but inherits neither case nor tracking — rule 14's failure mode, on the component §4 assigns that token to. **§8 finding 32's claim of "eight tuples, all tokens" is wrong; measured it is nine.** | Add `text-transform:uppercase;letter-spacing:var(--track-caps)` to `.gate`, or move it to `--t-meta`+600. Correct §8 #32. |
| 44 | P3 | A 51-character uppercase sentence in a `th` | "History (oldest first; faded entries were replaced)" — 51 chars, uppercase, 0.6px tracking, 511px cell. Caught by the detector, missed by eye. | The token is applied correctly; the string is prose. Uppercase is for labels. | Shorten to "History" and move the gloss to the card hint. |

## Detector verdicts

| finding | source | verdict | decided by |
|---|---|---|---|
| `flat-type-hierarchy` | CLI + overlay | CONFIRMED, wrong numbers | CLI saw 3 sizes; 5 render. Adjacent ratios 13/12=1.083, 14/13=1.077, 16/14=1.143, 22/16=1.375 — three steps under 1.25. |
| `all-caps-body` | overlay | CONFIRMED | The 51-char `th` above. |
| `ai-color-palette` | overlay | FALSE POSITIVE | `#02b5ea` in the wordmark, in the top bar, outside `.page` entirely. Pinned by the brief. |
| `cramped-padding` ×4 (cards) | overlay | FALSE POSITIVE | Card padding is 0; range-measured inset to first glyph is top 12 / left 16, carried by `th`/`td` padding. Third audit to disprove it. |
| `cramped-padding` (`#feedmore`) | overlay | FALSE POSITIVE | `min-height:32px`; measured ink inset top 8 / bottom 9 / sides 13. The rule does not read `min-height`. |
| `line-length` | overlay | FALSE POSITIVE | Range-measured 62–75 chars/line, under the 80 floor. The 340-char hit is mono JSON in an ellipsised cell — one visual line, not prose. |
| `em-dash-overuse` | overlay | FALSE POSITIVE | 15 inside `.page`: 5 inside `.vh` (the accessibility text finding 23 added), ~9 the null-value marker, 1 prose. |
| "hash navigation does not re-render" | overlay pass | FALSE POSITIVE | Re-measured: `#/case/demo-case-001 → #/case/demo-case-003` swaps correctly. Automation timing. |

## Minor observations

- Feed dots sit **2.00px** below their line (`.fitem::before{top:14px}`; should be 12). This page spent
  two findings on a 1.5px icon offset and its own timeline is out by 2.
- `.tbar` title→chips gap is **4.00px**, the tightest on the page; it survives on cap-height clearance
  alone, and a descender in a company name will touch the chip row.
- `.bullet .val` has no wrap above 720px — at 1024 it goes 28 → 56px with three ragged columns.
- The Salesforce Fields card is the only one with a header band and no title: **41px** vs **49px**.
- The five decision rules print at **12px**, smaller than the evidence rows below them and the same
  size as muted metadata. Size is supposed to carry rank.
- **`opacity:.55` survives at `console.html:1363`** on superseded evidence — the mechanism rule 9
  forbids. No seeded state renders it, which is how three audits missed it.
- `.contrib` DOM order interleaves the two visual columns, so the eye reads down and the tab order
  reads across.
- `.bmeasure` takes its colour from an inline `style="background:var(--muted)"` — a text token used
  as a graphic fill, set in JS rather than CSS.

## The pattern

Every P0/P1 is **layout defaulting**, not a token choice: `auto-fit` picking its own column count,
`flex-wrap` picking its own row gap, a template-literal space standing in for `gap`. DESIGN.md has
nineteen rules and not one governs layout defaults — which is why this class survived three passes
that were each looking at tokens. Both P1s die to one sentence: *a fixed-cardinality set names its
column count; a run of siblings is spaced by `gap`, never by a space in a template string.*

## Questions to consider

1. Who is the ten-step pipe for? No reviewer acts on "Load inputs." The page's worst geometry renders
   a state machine with one interesting value.
2. Should "— not met" be inside the label at all? It reaches the accessibility tree, which was right,
   but the cost is a sentence nobody would say and a cell that wraps by 16px. Name the subject only
   and let ✓/✕ plus a hidden "met"/"not met" carry the result.
3. Three seeds cover held / approved / thin. None covers a superseded check, a FAILED run, or a mixed
   gate set — which is how `opacity:.55` survived. What must a fourth seed contain?
