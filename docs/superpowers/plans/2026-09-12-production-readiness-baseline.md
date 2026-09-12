# Production Readiness Baseline Implementation Plan

**Goal:** Restore a reproducible local and CI test baseline for the authorized
production-readiness program on PR #2.

**Spec:** `docs/superpowers/specs/2026-09-12-production-readiness-design.md`

## Task 1: PostgreSQL discovery and accurate handoff prose

Files: `tests/pg.py`, `tests/unit/test_pg_discovery.py`,
`docs/DEPLOYMENT.md`, `docs/PLATFORM_INTEGRATION.md`.

- [ ] Reproduce current deployment migration-range test failure.
- [ ] Test local PostgreSQL discovery using temporary executable trees: PATH
  installation, pg_config bindir, incomplete installation fallback, no usable
  installation, and external database URL bypass. Require initdb, pg_ctl,
  pg_isready, and createdb from the same usable directory. Bound pg_config calls.
- [ ] Implement minimal discovery with pg_config/PATH and existing fallback
  directories, including Homebrew. Quote discovered paths when constructing
  existing shell commands, including paths with spaces.
- [ ] Clarify that core 013–023 and configuration 024 are frozen independently;
  do not change either migration or weaken the ownership guard.
- [ ] Replace the unsupported per-case callback-order guarantee with the current
  interim contract: at-least-once, duplicates acknowledged, manual preserved,
  subsequent unordered automatic callbacks held for review; never infer order
  from decided_at or event_sequence. Reference activation 025.
- [ ] Run targeted discovery and document checks, then start/stop a disposable
  PostgreSQL cluster and run the full suite with no_proxy=* for the known macOS
  fork/proxy issue. Do not use or reset the user's live database.
- [ ] Separate review for compliance/quality; fix verified findings, run lint
  and import checks, commit only claimed files, and release with exact evidence.

No runtime Python changes, dependency changes, or engine repin are expected.
Existing untracked artifacts are user-owned and remain untouched.
