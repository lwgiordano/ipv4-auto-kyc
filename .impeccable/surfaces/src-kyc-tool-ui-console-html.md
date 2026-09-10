---
version: 1
slug: "src-kyc-tool-ui-console-html"
primary_target: "src/kyc_tool/ui/console.html"
related_targets: []
---

# Console appearance settings

Scope: bounded addition inside the established console. Mode: Operate. Audience: reviewers
working in different ambient light; appearance is their browser-local preference, not a service
configuration. Existing DESIGN.md and all earlier page contracts remain authoritative.

## Direction contract

THESIS: make appearance discoverable in the console while leaving the active review in place.

OWN-WORLD: inherit the marketplace navy, current light/dark tokens, Mulish fallback, compact
4px controls and currentColor line icons. No palette or typography replacement.

STORY: open Settings, choose System / Light / Dark, see the result immediately; reload without
losing the choice. System follows the browser's operating-system preference.

FIRST VIEWPORT: a labelled Settings control sits in the sidebar footer above refresh. A compact
non-modal panel opens beside it, with Appearance and three labelled choices; mobile uses the
existing Menu drawer and keeps the panel within the screen.

FORM: local extension of the approved sidebar grammar; no concept tournament or seed applies.
Selected state is visible and keyboard reachable; unavailable storage is disclosed without
breaking the current page. No backend configuration or case data is touched.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

Ordinary extension: DESIGN.md is inspected for consistency, not rewritten. No new raster assets.
