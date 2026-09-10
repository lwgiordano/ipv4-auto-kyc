---
target: Every KYC console screen, beginning with Overview cards, pill legends and reviewer alignment
total_score: 25
max_score: 40
na_heuristics:
p0_count: 0
p1_count: 1
target_identity: "file:/Users/lgiordano/ipv4-auto-kyc/src/kyc_tool/ui/console.html"
target_fingerprint: "sha256:9890fdd146f217b015d9fcd41a5cdd1b6bf7d896ac49f97c3618dc6e60d50c1a"
target_path: /Users/lgiordano/ipv4-auto-kyc/src/kyc_tool/ui/console.html
timestamp: 2026-09-10T15-35-13Z
slug: src-kyc-tool-ui-console-html
---
Method: dual-agent (A: /root/visual_review; B: /root/technical_review), with parent measurements of Overview, header/legend spacing, reviewer alignment, company identity, and search.

# KYC console design audit — 10 September 2026

Target: `src/kyc_tool/ui/console.html`, commit `2fe524f`, branch `claude/project-setup-standing-rules-5w1fwh`; live app `http://127.0.0.1:55717/ui`.

The interface has a coherent compliance-workbench identity. Its main weakness is composition: correct tokens are combined through shared selectors and browser defaults that produce incorrect relationships. Keep the restrained navy, typography, status vocabulary, and explicit distinction between the recorded decision and current evidence. Repair the layout rules before adding visual embellishment.

Reviewed all seven screen types: Overview, Companies, company detail, Data Sources, Salesforce Fields, Decision Rules, and Send Message, plus the approval dialog, a help bubble, and empty search. Visual checks used 390/768/1440 CSS-pixel widths; Overview also used 1024. Independent technical coverage used 390/767/1440 because its native browser's zoom prevented an exact 768 setting. Representative dark screens and dialog were rendered. Only one current Acme company with five rounds was available. No messages, approvals, retries, or connection probes were submitted.

## Start with Overview and the shared shell

### 1. [P2] The lower Overview cards start on different lines

At 1440px, Decisions to Date starts at y=360 and External Services at y=376: exactly 16px out. The global `.card+.card` rule adds a stacked-card margin to the second child of `.split`, although the grid already supplies its own gap. On a stacked phone layout this becomes a 32px gap. Send Message repeats the same defect: Response ends at y=307 and Example begins at y=339, although `.stack` declares 16px.

Location: `console.html:263`, `:281–283`, `:1279–1288`, `:1876–1883`.

Fix: make the layout container the sole owner of spacing. Grid/stack children carry no sibling margin; genuine block-flow stacks get an explicit container. Use existing `--card-gap`/`--s4` (16px). Do not stretch the 99px decision summary to match the 593px service table merely to match bottoms. Their content differs. For a clearer Overview composition, propose placing the compact decision summary above a full-width service table rather than reserving half the screen for one count.

The six top KPI boxes are already equal at desktop: 166×132px, with aligned numbers. At tablet widths, content makes one row 116px and another 132px; a uniform row-size policy can improve that rhythm. This is different from the lower-panel top-edge defect.

### 2. [P2] Title, legend, and content have inconsistent ownership of space

The Data Sources legend is 24px below the introduction at desktop, but 40px below it at 390px. `tbar()` always creates an empty `.actions` element. When the title block fills the row, that zero-size child wraps and adds a real 16px flex row gap. The first useful Data Sources card consequently begins at y=320 on the phone. The legend itself takes 120px there.

Location: `console.html:247–253`, `:657–659`, `:1227–1228`, `:1737–1740`.

Fix: omit empty action slots; define one page-header component with explicit title, description, optional status/legend, and action slots. Proposed role mapping uses existing tokens: title→description 8px (`--s2`); description→legend 12px (`--s3`); header group→content 24px (`--s5`); pill→gloss 8px. Use a deliberate stacked legend pattern on narrow screens rather than whichever word happens to wrap next.

The five legend pills themselves are consistent at 24px high. Their current pill/gloss gap is 8px; group gap 24px. Company-specific statuses sit 8px below their title and are facts about that company; the Data Sources legend explains the vocabulary. Give those two roles different named components, not one indistinguishable pill strip. Preserve the existing distinction between rounded status pills, outlined machine tags, and checklist cells. Shorten redundant legend glosses only after approving the revised wording.

### 3. [P3] The blue reviewer circle aligns to the label, not the input

The circle is exactly 40×40px. Its center is y=108; the input's center is y=126. The 18px difference comes from `.avatar{grid-row:span 2;align-self:start}`. It looks detached because the eye pairs the avatar with the field, while CSS pairs its top with the small label.

Location: `console.html:183–194`.

Fix: give label, input, and hint separate grid rows and place the avatar on the input row, vertically centered. Preserve the 40px avatar, field identity, and reviewer binding. Do not add an arbitrary translateY correction. The avatar is a decorative identity placeholder; it should not imply authenticated identity.

### 4. [P2] Company Details repeats the unequal-baseline problem

At desktop, Legal Name / Registration Number / Jurisdiction values start at y=294, while RIR Org ID starts at y=286. Contact has the same 8px mismatch in the next row. The fields with sublabels are taller; nested `.idf` grids stretch their implicit tracks differently.

Location: `console.html:473–480`.

Fix: use content-sized label rows and `align-content:start` for each field, with the existing 4px (`--s1`) label/value gap. All primary values within a row should begin on one line, regardless of optional secondary text.

## Other verified findings

| ID | Severity | Finding and user impact | Concrete direction |
|---|---|---|---|
| 5 | P1 | Companies search loses focus after the 250ms refresh. Type `Ac`, pause, type `me`: value remains `Ac`, focus is BODY. Normal typing is interrupted. `console.html:1330–1341`. | Preserve the input while replacing results, or restore focus/selection only when that input owned focus. Interaction-code repair; outside styling-only implementation. |
| 6 | P2 | Tables fit by scrolling, but important review information moves off-screen. Companies is 1048px in a 366px phone container; Salesforce Fields 512/366px; scoring rules 817/366px. At tablet, Pass Condition is still clipped. `:275`, `:1336`, `:1795`, `:1830`. | Define responsive column priorities. Keep company/decision/score together. Prefer labeled stacked records for mapping/rules; retain every raw value. Expand/collapse behavior needs a separately approved interaction change. |
| 7 | P2 | Touch controls retain desktop-sized targets: connection/earlier-entry buttons 32px; Menu/reviewer input 36px; navigation/actions 40px. `:176–201`, `:367–399`. | Proposed `--control-touch-min:44px`, applied to effective hit regions at coarse-pointer/narrow layouts. Help buttons already have a 44px pseudo-element hit area; do not enlarge them on the assumption that their 24px drawing is the whole target. This is a touch-usability recommendation, not a blanket WCAG failure claim. |
| 8 | P2 | Leaving Acme for Overview or Send Message retains Acme in the browser title. Reviewers switching tabs can misidentify the screen. `:1506`, `:1915–1940`. | Set the title on every route; preserve the local Full stack prefix. Interaction-code repair. |
| 9 | P2 | Manual Review Tasks hides a roughly 1851px line behind a 353px ellipsis and a `title` attribute, with no visual keyboard/touch disclosure. `:1564–1565`. | An accessible Details disclosure should expose the full wrapped content. The accessibility tree already retains the text; do not claim screen readers lose it. New disclosure behavior requires build-agent implementation. |
| 10 | P2 | The company record is long enough to obscure the review task. Desktop main content is 4810px. On phone the decision begins near y=834, current evidence at 1727, and review tasks at 4234. | Preserve company identity near the top, make current decision and pending review work easier to reach, and keep provenance/history secondary. Propose a compact identity summary and section anchors. Anchors/disclosures are interaction changes; gate layout and spacing are visual. Do not remove any of the five gates or manufacture a reject workflow. |
| 11 | P3 | Send Message's initial Response is only a dash, which can read as missing or broken data. `:1878–1880`. | Use explicit empty-state copy such as “No message sent yet.” Review the Example's approval assumption against this installation's visible enforcement hold before changing its factual wording. |

Count: 11 consolidated findings — 0 P0, 1 P1, 8 P2, 2 P3. Related observations are grouped instead of counted repeatedly per screen.

## Screen-by-screen critique

| Screen | Keep | Improve |
|---|---|---|
| Overview | Six legible metrics; clear failure/throughput intent | Align lower-card tops; avoid the half-page void; stack the service table when available content width is insufficient; stabilize KPI row rhythm |
| Companies | Company name leads; useful ID subtitle; clear no-match recovery | Repair focus loss; prioritize key columns on narrow screens |
| Company detail | Recorded decision distinct from live evidence; identity visible; clear hold explanation | Align identity values; shorten the navigation distance to review tasks; make long task details inspectable |
| Data Sources | Honest Live/Test/Manual distinctions and meaningful warnings | Tighten header/legend relationship; give title/status rows deliberate wrapping; reduce repetitive explanatory text while preserving rule order |
| Salesforce Fields | Raw field names and values appropriately remain verbatim | Avoid narrow value columns and off-screen sources; visually distinguish implementation notes from the value itself |
| Decision Rules | Scoring, ordered decisions, broker list, and definitions are separated | Prioritize readable Pass Condition; compact lower-priority reference content without rewriting authoritative rules |
| Send Message | Labeled inputs, working example, distinct response area | Fix the doubled card gap and vague response empty state; retain its technical-utility positioning |
| Approval dialog | Company named, consequence explicit, required fields, Cancel/Escape and focus return work; fits phone in light/dark | Increase effective touch targets; preserve the warning hierarchy and clear confirmation wording |

## Scores and interpretation

These are heuristic planning scores, not certification and not directly comparable with earlier audits that used different states or scopes.

| Nielsen heuristic | /4 | Reason |
|---|---:|---|
| System status | 3 | Recorded/live distinction works; initial response is vague |
| Match to real world | 3 | Reviewer language is mostly clear; implementation jargon remains |
| Control and freedom | 3 | Cancel, Escape, breadcrumbs, and Clear Search work |
| Consistency | 2 | Repeated container/spacing and baseline failures |
| Error prevention | 3 | Consequence and required-field guidance are explicit |
| Recognition over recall | 2 | Long case requires users to remember where sections are |
| Flexibility and efficiency | 1 | Search interrupts typing; long review navigation |
| Aesthetic/minimalist design | 2 | Strong visual identity, excessive record density |
| Error recovery | 3 | Observed no-match recovery works; broader errors untested |
| Help/documentation | 3 | Contextual help exists; some terms assume engineering knowledge |
| Total | 25/40 | Sound foundation with workflow and composition work remaining |

Technical assessment after combining both reviews: Accessibility 2/4 (including verified focus loss); Performance 3/4 (source inspection, no production profiling); Responsive 2/4 (task-critical content needs lateral scrolling); Theming 3/4; Implementation integrity 2/4. Total 12/20. The standalone detector returned `[]`: zero findings. It missed the contextual layout failures.

No confirmed contrast failure in measured light-theme direct-text pairs. Representative dark screens and dialog remained readable; settled dark navigation measured 5.64:1. No document-wide sideways overflow in the independent 21 route/width observations. Local table scrolling is deliberate and was not mislabeled as document overflow. Keyboard-accessible help and the dialog worked. Full screen-reader testing, software-keyboard occlusion, provider failures, in-flight network states, and all case variants were not covered.

## Phased implementation plan

### Phase 1 — Critical and shared consistency

Visual scope: fix the common card-spacing ownership (1), conditional page-header slots and legend proximity (2), reviewer grid (3), and company-field baselines (4). Start at Overview and apply the same rules to every affected context. Preserve current colors, icon set, content, and status semantics. Treat Companies search (5) and route-title reset (8) as a separate interaction-code patch, not CSS work.

Acceptance: same-row card top edges differ by no more than 1 CSS pixel; stacked gaps equal 16px rather than 32; empty header actions consume no row/gap; reviewer avatar/input centers differ by no more than 1px; company primary-value baselines align within 1px. Search retains focus and selection across a refresh; every route title matches its current heading. Verify light/dark and 390/768/1024/1440 widths, including the sidebar breakpoint.

Suggested command: `$impeccable layout`; separately `$impeccable harden` for interaction defects.

### Phase 2 — Refinement and task hierarchy

Make the Overview composition deliberate. Proposed default: compact decision summary above a full-width service table. If keeping two columns, use an available-content-width threshold derived from the service table's minimum rather than the current shared 980px viewport threshold. Define a uniform KPI row policy. Define narrow-screen table layouts (6), touch targets (7), and stable Data Sources title/status placement. Propose company section navigation and inspectable task details (9–10) for build-agent approval; preserve every decision, gate, history record, and raw mapping value.

Acceptance: primary review information is readable without lateral scanning on phone; no data silently disappears; all effective touch targets reach the proposed 44px minimum; the active decision and pending review tasks are easy to locate from the case header. Any interaction additions require their own behavior checks.

Suggested commands: `$impeccable adapt`, `$impeccable layout`, and `$impeccable distill` with protected provenance content.

### Phase 3 — Polish

Improve response empty-state wording (11), review redundant legend glosses, test loading/error/disabled states using controlled fixtures, and recheck focus/tooltip placement, dark mode, and reduced-motion behavior. Final verification is one batched inspection followed by at most one confirmation pass; do not reopen an unbounded specimen-by-specimen polishing loop.

Suggested commands: `$impeccable clarify`, then `$impeccable polish`.

## Design-system updates proposed

Keep existing spacing tokens. Add role aliases/rules instead of more arbitrary values: page-header internal gap 8/12px, header-to-content 24px, card layout gap 16px, and a proposed touch-minimum token of 44px. Document four explicit component contracts: page-header slots, card-layout containers, reviewer field grid, and label/value field grid. Add responsive table and legend patterns only after their presentation is approved.

The governing lesson: test relationships, not merely whether each element uses a valid token. An approved 16px margin plus an approved 16px gap still produces the wrong 32px relationship. A 40px circle can be perfectly drawn and positioned against the wrong row. An empty element can add space. A default grid can misalign labels even while every padding value is correct.

## Cognitive load and personas

Moderate extraneous load, especially on company detail: weak single focus, excessive simultaneous record detail, and limited progressive disclosure. The five gates and evidence categories are real domain complexity and should remain intact.

Power users lose time to search interruptions and scrolling back to actions. New reviewers must distinguish decision, case status, buying status, and mapping values; clear context labels help more than additional colors. Keyboard/touch users need full details beyond hover titles and larger action targets. The hold explanation and approval dialog provide reassurance; the long middle of the record is where orientation weakens.

## Next choices

Recommend Phase 1 first, with visual changes and interaction defects reviewed as separate patches. For mobile tables, select a deliberate stacked-record presentation or retain scrolling with stronger column prioritization; this choice should precede implementation. For legends, keep the meaning visible while eliminating redundant glosses only after copy approval.

No application source or behavior has been changed by this audit. The requested output is this critique and plan.
