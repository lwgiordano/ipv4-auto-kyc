# KYC Tool — release __VERSION__ (source handoff)

This package is the source for the KYC/KYB tool, release `__VERSION__`, cut from commit
`__COMMIT__`. It is a **staging** release: the production kill switch refuses to boot in production
mode by design (the OCR engine, email provider, and adapter profile are stubs pending later
work), so use this to stand up staging and to build production readiness — not to launch.

## What's in the box

- `techcraft-deployment-guide.pdf` — the ops manual: one image, six commands, Postgres 14+, one
  S3 bucket, configuration, health checks, cutovers.
- The **Staging Handoff** document accompanying this package is the single read-first reference;
  the **Platform Integration Contract PDF** and the runnable signer `kyc-signer-example.py`
  accompany it too (both also ship inside this tree, under `docs/artifacts/` for the signer).
- `docs/PLATFORM_BRIEFING.md` — orientation: what the tool is and how the staging plan runs.
- `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, `docs/ALERTS.md` — full procedures, failure
  playbooks, the complete configuration table, alerting.
- `src/`, `alembic/`, `KYC_Tool_Build_Package/` — the application, its migrations, and the
  normative policy bundle the image ships with.
- `Dockerfile`, `requirements.lock` — the image builds reproducibly:
  `docker build -t kyc-tool:__VERSION__ .`
- `tests/` and `./manage.sh` (`setup`, `doctor`, `test`, `lint`) to run the suite yourself.
- `scripts/dev.sh` — a one-command local demo stack (throwaway Postgres, the API with its ops
  console on `http://127.0.0.1:8080/ui`, a worker running canned data-source fixtures, and a
  fake platform receiver) if you want to see a case flow end to end before touching staging.

## First steps

1. Ops: read the deployment guide end to end — especially "Read this before provisioning
   anything" — then stand up staging per sections 1–4. Configuration starts from
   `.env.example`; a misconfigured boot prints every violation at once.
2. Integration developers: build the event sender and the `/kyc/decision` callback receiver
   against the contract. Verify your signing against its published test vector first.
3. Secrets (HMAC keys both directions) move over an encrypted channel only — never email,
   chat, or a ticket.

## Provenance

Cut from commit `__COMMIT__` (`__VERSION__`) by `scripts/package_handoff.sh` in the source
repository. Relative to that commit, this package omits internal development, audit, and CI
working files that are not needed to build, run, test, or operate the tool, replaces the
repository's kit-backed `manage.sh` with the self-contained equivalent you have here (same
commands, no kit), and adds this note plus the deployment guide PDF, which is generated from
the same commit — its page footers say `source __COMMIT__`.
