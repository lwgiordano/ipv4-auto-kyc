# ipv4-auto-kyc — Agent Guide

The **IPv4.Global KYC/KYB Tool**: a platform-invoked scoring engine that gathers
business-verification evidence through adapters, validates it deterministically,
records immutable supersedable checks, scores against a 100-point threshold with
five hard gates, and returns one of four decisions for the platform to enforce.

**Read first**: `KYC_Tool_Build_Package/` is the normative spec (committed
unmodified; the `machine_readable/*.json` files win over prose) and
`AUDIT_FINDINGS.md` records every known spec defect and the chosen resolution —
corrections live in code/fixtures tagged `AUDIT:<id>`.

## Everyday commands

All routine work goes through `./manage.sh` (installed by
`ai-kits/agent-substrate-kit/`):

| Command | Purpose |
| --- | --- |
| `./manage.sh setup` | Create `.venv`, install dependencies |
| `./manage.sh doctor` | Verify project wiring (substrate, toolchain, hooks) |
| `./manage.sh fmt` / `lint` | ruff format / ruff check |
| `./manage.sh test` | pytest (policy-driven + golden + unit + integration) |

## Layout

- `src/kyc_tool/` — the service. Strict three-stage separation:
  `adapters/` fetch (never import db) → `validators/` judge (pure) →
  `checkstore/` records. `domain/` (scoring/gates/decision) is pure and
  policy-driven; `policy/` loads the normative JSONs (sha256-stamped into every
  run for audit provenance). `queue/` is a hand-rolled Postgres jobs table
  (SKIP LOCKED, leases, dead-letter); `outbox/` delivers decision callbacks
  at-least-once. Import boundaries are enforced by import-linter — don't fight
  them.
- `tests/policy_driven/` — generated from the machine-readable JSONs (never
  hand-copy point values); `tests/golden/cases/` — the 12 golden scenarios as
  data files (G7 is split into G7a/G7b per `AUDIT:B1`).
- `alembic/` — migrations; `checks` is append-only with a partial unique index
  for live checks.

## Working alongside the other agent

Two agents share this repo — **Claude Code** (cloud) and **Codex** (local) — over
the shared branch `claude/project-setup-verify-kpfgjs`. They have no shared disk;
git is the only channel. Before starting, **read `.agents/HANDOFF.md`** (the
mailbox) and `.agents/SYNC.md` (the how-to). Pull before you work
(`./.agents/sync.sh pull`), push after you commit (`./.agents/sync.sh push`), and
respect the `turn:` baton in HANDOFF.md so you don't collide on the branch. To
hand work to or ask something of the other agent, append an entry to
`.agents/HANDOFF.md` and push.

## Conventions

- Languages: python (primary). Run `./manage.sh fmt && ./manage.sh lint` before
  committing; the pre-commit hook runs lint in warn-only mode.
- Never mutate a check row — supersede it. Never call adapters inside a DB
  transaction. Decision + checks + outbox commit atomically.
- Unknown external contracts are stubbed behind interfaces and marked
  `TODO(integration)` — see `AUDIT_FINDINGS.md §C`.
- `.substrate/` and `manage.sh` are kit-managed: refresh via
  `bash ai-kits/agent-substrate-kit/bootstrap.sh`, don't hand-edit.
