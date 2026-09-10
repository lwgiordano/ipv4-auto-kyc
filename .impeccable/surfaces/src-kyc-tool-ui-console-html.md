---
version: 1
slug: "src-kyc-tool-ui-console-html"
primary_target: "src/kyc_tool/ui/console.html"
related_targets: []
---

# Console refinement and configuration previews

Scope: approved extensions inside the established console. Mode: Operate. Audience: reviewers
reading decisions, preparing messages, and exploring configuration. Appearance and configuration
previews are browser-local; point, broker and mapping edits do not publish server configuration.
Existing DESIGN.md and all earlier authority contracts remain authoritative.

## Direction contract

THESIS: keep live facts, editable previews and deliberate actions visibly separate while giving
every shared component consistent alignment and spacing.

OWN-WORLD: inherit the marketplace navy, current light/dark tokens, Mulish fallback, compact
4px controls and currentColor line icons. No palette or typography replacement.

STORY: read the live decision and its context, compare local configuration drafts with live values,
or prepare a structured message and review it before sending. Choose System / Light / Dark in
Options without losing the preference on reload.

FIRST VIEWPORT: each page starts with a clear title, short purpose and aligned optional legend.
Decision Rules and Salesforce Fields expose explicitly labelled preview controls beside live
values. The composer leads with company, action and relevant fields; review and send are separate.
Overview separates historical decision counts from latest-company drill-down. Options retains
its routed Appearance form. Tables center controls and text; mobile uses the existing Menu drawer.

FORM: local extension of the approved sidebar grammar; no concept tournament or seed applies.
Selected/hover/focus states work in both themes; unavailable storage is disclosed without
claiming a save. Preview controls send no writes. The existing message action remains a deliberate
server operation, with no new backend capabilities. Browser tests intercept its sends.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

Ordinary extension: DESIGN.md is inspected for consistency, not rewritten. No new raster assets.
