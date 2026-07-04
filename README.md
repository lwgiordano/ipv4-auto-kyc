# ipv4-auto-kyc — IPv4.Global KYC/KYB Tool

A platform-invoked KYC/KYB scoring engine: the IPv4.Global platform POSTs
customer lifecycle events, the tool gathers evidence through nine adapters,
validates it **deterministically**, records immutable supersedable checks,
scores against a 100-point threshold with five hard gates, and returns one of
four decisions (`approve`, `approve_buy_locked`, `manual_review_insufficient`,
`reject`) for the platform to enforce. Salesforce is a one-way mirror owned by
the platform.

**Normative spec**: [`KYC_Tool_Build_Package/`](KYC_Tool_Build_Package/)
(committed unmodified; the `machine_readable/*.json` files win over prose).
**Spec audit**: [`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) — every known defect
and its resolution; corrections live in code/fixtures tagged `AUDIT:<id>`.

## Quick start (dev)

```sh
./manage.sh setup                    # venv + editable install
.venv/bin/pip install -e '.[dev]'    # test/lint toolchain
./manage.sh doctor                   # verify wiring
./manage.sh test                     # full suite (spins an ephemeral Postgres)
```

Run the service (needs Postgres + env, see `.env.example`):

```sh
.venv/bin/alembic upgrade head
.venv/bin/uvicorn kyc_tool.api.app:create_app --factory --port 8000
.venv/bin/python -m kyc_tool.workers.pipeline_worker   # run orchestration
.venv/bin/python -m kyc_tool.workers.outbox_worker     # decision callbacks + POC emails
```

## Architecture (one service, background workers, Postgres)

```
platform ──POST /v1/cases/{id}/events──► api/ ──TXN-1──► events + runs + jobs
                                                            │ (pg queue, SKIP LOCKED + leases,
                                                            │  per-case FIFO)
        workers/pipeline_worker: QUEUED → RESOLVE_INPUTS → BROKER_GATE ─blocked→ DECIDE(reject)
                                   → RUN_ADAPTERS (fetch outside txn, raw → object store)
                                   → [VALIDATE → WRITE_CHECKS → SCORE → DECIDE] one commit
                                   → PUBLISH_DECISION ──outbox──► platform callback → COMPLETE
```

- `domain/` + `validators/` + `policy/` are **pure** (enforced by
  import-linter); adapters fetch only and never touch the DB.
- Policy (rubric, decision rules, broker list, events, state machine) loads
  from the spec's JSONs at startup; each file's sha256 is stamped onto every
  run and decision for audit provenance, and tests are generated from the
  same loader (a drift-guard pins the hashes).
- Checks are append-only; supersession chains + a partial unique index keep
  exactly one live check per type; the ORG-ID→POC cascade is automatic.
- The audit log reconstructs any decision: event → adapters (with raw
  evidence refs) → checks → score → gates → decision → callback.

## Operations

| Command | Purpose |
|---|---|
| `GET /healthz` | liveness + policy bundle hash |
| `GET /v1/metrics` | run/decision/queue/outbox/review-queue counters |
| `GET /v1/review-tasks?status=open` | human queues: website review, POC email unavailable |
| `python -m kyc_tool.workers.retention` | prune audit/evidence past retention (default 7y) |

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for failure playbooks and
[`docs/SALESFORCE_MAPPING.md`](docs/SALESFORCE_MAPPING.md) for the
platform-team sync mapping.

## Integration status

Stubbed behind interfaces, marked `TODO(integration)` (AUDIT_FINDINGS §C):
platform callback URL + HMAC secret exchange, Floqer API contract, platform
email-verification fetch, outbound email provider, production OCR engine,
POC token link hosting (platform forwards the raw token, `AUDIT:C2`).

## Repo layout

`src/kyc_tool/` service · `alembic/` migrations · `tests/` (policy-driven,
golden cases as data, integration suites per phase) ·
`ai-kits/agent-substrate-kit/` the project-substrate kit that provisions
`manage.sh`/`.substrate/` · `KYC_Tool_Build_Package/` the spec.
