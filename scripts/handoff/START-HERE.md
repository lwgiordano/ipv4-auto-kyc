# KYC Tool — release __VERSION__ (source handoff)

This package is the source for the KYC/KYB tool, release `__VERSION__`, cut from commit
`__COMMIT__`. It is a **staging** release: the production kill switch refuses to boot in production
mode with the shipped provider configuration. Live registry adapters exist, but the worker
still uses an empty POC directory and development document/email providers. Use this package
to build and test the staging integration, not to launch production.

## What's in the box

- `docs/TECHCRAFT_HANDOFF.md` — start here. The briefing, the wire contract and the deployment
  instructions in one file. Read Part 1 first; Parts 2 and 3 are technical references.
- `techcraft-deployment-guide.pdf` — deployment reference generated when the package is built.
  Use the current Markdown documents above for the readiness status and corrections.
- `docs/artifacts/kyc-signer-example.py` — runnable signing example. If IPv4.Global supplies
  separate PDFs, check their source commit against this package before using them.
- `docs/PLATFORM_BRIEFING.md` — orientation: what the tool is and how the staging plan runs.
- `docs/PLATFORM_INTEGRATION.md` — the wire contract your integration developers build against.
- `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, `docs/ALERTS.md` — full procedures, failure
  playbooks, the complete configuration table, alerting.
- `src/`, `alembic/`, `KYC_Tool_Build_Package/` — the application, its migrations, and the
  normative policy bundle the image ships with.
- `Dockerfile`, `requirements.lock` — image definition and dependency constraints:
  `docker build -t kyc-tool:__VERSION__ .`
- `tests/` and `./manage.sh` (`setup`, `doctor`, `test`, `lint`) to run the suite yourself.
- `scripts/dev.sh` — a one-command local demo stack (throwaway Postgres, the API with its ops
  console on `http://127.0.0.1:8080/ui`, a worker using the demo's configured data sources, and a
  fake platform receiver) if you want to see a case flow end to end before touching staging.

## First steps

1. Product and integration leads: answer `docs/PLATFORM_BRIEFING.md` §5 and assign the
   checklist in §8. Those answers determine the remaining provider work.
2. Ops: use `docs/DEPLOYMENT.md` §1–§5 for closed staging, starting from `.env.example`.
   Development-mode readiness does not validate the production configuration.
3. Integration developers: follow `docs/PLATFORM_INTEGRATION.md`. Compare your signer's
   output with its test vector. The receiver must commit its callback and deduplication
   record before returning 2xx, and hold unordered callbacks for review (§4).
4. Secrets (HMAC keys both directions) move over an encrypted channel only — never email,
   chat, or a ticket.

## Provenance

Cut from commit `__COMMIT__` (`__VERSION__`) by `scripts/package_handoff.sh` in the source
repository. Relative to that commit, this package omits internal development, audit, and CI
working files that are not needed to build, run, test, or operate the tool, replaces the
repository's kit-backed `manage.sh` with the self-contained equivalent you have here (same
commands, no kit), and adds this note plus the deployment guide PDF, which is generated from
the same commit — its page footers say `source __COMMIT__`.
