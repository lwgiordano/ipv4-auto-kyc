---
version: 1
slug: "src-kyc-tool-ui-console-html"
primary_target: "src/kyc_tool/ui/console.html"
related_targets: []
---

# Console refinement and live configuration

Scope: approved extensions inside the established console. Mode: Operate. Audience: reviewers
reading decisions, preparing company actions, and editing shared configuration. Appearance stays
browser-local; authenticated point, broker and destination-mapping saves create server revisions.
Existing DESIGN.md and all earlier authority contracts remain authoritative.

## Direction contract

THESIS: keep recorded facts, unsent edits and deliberate actions visibly separate while giving
every shared component consistent alignment and spacing.

OWN-WORLD: inherit the marketplace navy, current light/dark tokens, Mulish fallback, compact
4px controls and currentColor line icons. No palette or typography replacement.

STORY: read the recorded decision and its context, save a reviewed configuration for new runs,
or prepare a company action and review it before sending. Choose System / Light / Dark in
Options without losing the preference on reload.

FIRST VIEWPORT: each page starts with a clear title, short purpose and no standalone status legend.
Decision Rules and Salesforce Fields expose right-aligned Edit/Add controls replaced by Save/Cancel.
Company Actions leads with company, action and relevant fields; review and send are separate.
Overview separates historical decision counts from latest-company drill-down. Options retains
its routed Appearance form. Tables center controls and text; mobile uses the existing Menu drawer.

FORM: local extension of the approved sidebar grammar; no concept tournament or seed applies.
Selected/hover/focus states work in both themes. Server responses confirm saves; conflicts retain
drafts and uncertain outcomes retain the identical request. Operator credentials stay only in page
memory. Broker forms sit before the full table. Salesforce values remain read-only; the service
does not write Salesforce. Browser tests intercept mutations; backend and final round-trip gates
independently verify durable saves.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

Ordinary extension: DESIGN.md is inspected for consistency, not rewritten. No new raster assets.
