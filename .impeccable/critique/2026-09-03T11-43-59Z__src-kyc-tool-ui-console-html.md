---
target: the ops console UI
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 3
timestamp: 2026-09-03T11-43-59Z
slug: src-kyc-tool-ui-console-html
---
Method: dual-agent (A: design review · B: detector + browser evidence). Target e5aa116.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Header chip "Buying not yet applicable" vs decision line "Buying enabled" 130px below. |
| 2 | Match System / Real World | 4 | Strongest thing in the file; one untranslated leak in Salesforce Fields. |
| 3 | User Control and Freedom | 3 | Manual approval irreversible; dialog never said so. |
| 4 | Consistency and Standards | 2 | Five chip geometries, two h2 ranks, three card gaps, pill/plain flip. |
| 5 | Error Prevention | 2 | Required Reason with no marker; approve button enabled immediately. |
| 6 | Recognition Rather Than Recall | 3 | Companies table hid 104px including "Last Updated". |
| 7 | Flexibility and Efficiency | 2 | No sort, filter or bulk action. |
| 8 | Aesthetic and Minimalist Design | 2 | 4520px case page, eight equal-weight sections. |
| 9 | Error Recovery | 3 | `.gate.na` at ~2.9:1. |
| 10 | Help and Documentation | 4 | Explainer on nearly every container, explaining policy not chrome. |
| **Total** | | **28/40** | **Good — disciplined foundation, unreconciled application** |

## Design Specificity Verdict

Authored for this product. The live-vs-published split, five-gate strip, supersession chain and the
enum translation layer are things only this product needs. The weakness is not genericness: each
component was authored well in isolation and never reconciled into one system.

Deterministic scan: CLI exit 2, one finding (`flat-type-hierarchy`). Browser injection across five
views raised 25 element-level findings; eight were disproved as false positives by measurement.

## Overall Impression

The token layer was disciplined — every padding/margin/gap on the 4/8/12/16/24/32 register, all 57
pills identical to the pixel, all 141 icons at one stroke, zero text contrast failures in either
theme. The inconsistency was in WHICH correct value got chosen: same role, different on-scale
number; same status, different component; same tag, different rank.

## Priority Issues

- [P0] Same status value changes component page to page (pill in one table, plain text in another).
- [P1] `<h2>` renders at two ranks; 20 type triples, 8 of them at 14px; four of five steps under 1.2.
- [P1] Same spacing role takes 0/16/32px by page; KPI numbers 16px out of alignment.
- [P1] Two buying strings contradict 130px apart.
- [P2] Icons drift where layout is not flex: 0px gap and 3.5px offset on `.irow .todo`.
- [P2] Companies table hides 104px inside its own card with no affordance.

## Resolution

All 22 findings fixed in the follow-up commit; DESIGN.md written as the standing system.
Measured after: type triples 20 → 8; KPI numbers all aligned; card gap uniform 16px; `.crow` 41 → 36px;
table hidden 104 → 8px with scroll shadows; `.todo` gap 0 → 4px and offset 3.5 → 0.5px;
all four header chips iconed; every status column a pill; zero document overflow at 1440/1024/768/390.
