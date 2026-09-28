---
target: the ops console case page (#/case/:id)
total_score: 25
max_score: 40
na_heuristics: 
p0_count: 2
p1_count: 5
timestamp: 2026-09-03T16-05-00Z
slug: src-kyc-tool-ui-console-html-case
---
Method: dual-agent (A: design review · B: detector + browser evidence). Target f97502f — the commit
that wrote DESIGN.md §1–§7. Measured across all five seeded states.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Five header chips, two pairs of them synonyms for one enum. |
| 2 | Match System / Real World | 4 | Consistent everyday register; Decision History still spoke in "5h ago". |
| 3 | User Control and Freedom | 3 | Two buttons reading as primary; the screen's own primary was the flat one. |
| 4 | Consistency and Standards | 2 | Three rules from DESIGN.md broken by the pass that wrote DESIGN.md. |
| 5 | Error Prevention | 3 | Dialog is sound; its trigger and its confirm were different colours. |
| 6 | Recognition Rather Than Recall | 2 | Truncated Details cell with no title; three header chips with no raw enum. |
| 7 | Flexibility and Efficiency | 2 | Every case tab titled "KYC Tool · Console". |
| 8 | Aesthetic and Minimalist Design | 2 | 0px heading collision; tick label printing through the verdict. |
| 9 | Error Recovery | 2 | Gate results were colour-only; `.gate.na` boundary at 1.51:1. |
| 10 | Help and Documentation | 2 | Explainers good; `false` rendering as "—" for all fields but one. |
| **Total** | | **25/40** | **A written system, measurably broken in named places** |

## Design Specificity Verdict

Authored for this product. Nothing here is a template: the published-vs-live authority split, the
five-gate strip with its bypassed state, the supersession chain and the enforcement hold are
particular to this engine. The failures are application failures, not identity failures.

Deterministic scan plus browser injection across the five seeded states. Four detector findings
were disproved by measurement (`cramped-padding` on flush cards and on `#feedmore`, `single-font`,
`line-length` on flex pill rows); `ai-color-palette` is the marketplace's pinned `#02b5ea`, which
does not appear on this page.

## Overall Impression

The page has a written design system now, and it breaks that system in places the system names.
Three of the defects were introduced by the previous fix pass: each was a correct rule applied
through a selector that reached past the component it was written for. `.who`'s uppercase leaked
into the activity feed. `td .ic`'s optical correction displaced twelve already-centred icons to
fix four. `.tag`'s 1px padding set a height off the scale in the file that says every value is on
the scale.

The two P0s are both about a result that never reaches a reader who cannot see colour: the five
Decision Rules cells named their subject in both states and carried the outcome in the icon and
the ground; `.gate.na`, already fixed once, had been given a ground byte-identical to the card
behind it, leaving a 1.51:1 dashed edge as the only thing that made it exist.

Full findings, causes and fixes: DESIGN.md §8, items R1–R3 and 23–35.
