# IPv4.Global KYC/KYB Tool

Release `__VERSION__` · source commit `__COMMIT__`

Supported environment: closed staging. Production use is not supported.
Production provider wiring and callback ordering remain incomplete, and
automatic approval must remain off in production.

## Start here

1. Product and integration leads: Part 1 of `docs/TECHCRAFT_HANDOFF.md` defines
   two required provider decisions and the numbered responsibilities in §8.
2. Developers: use Part 2 for the event and callback contracts, including
   signatures and read APIs.
   The platform must commit a callback and its deduplication record before
   returning 2xx. Until ordered delivery is activated, follow §4's rules for
   conflicting decisions and manual approvals.
3. Operators: use Part 3 and `docs/RUNBOOK.md` for a closed-staging installation.
   Existing-database upgrades require the relevant maintenance procedures;
   do not treat every migration as a rolling update.
4. Before production: complete `docs/PRODUCTION_READINESS.md`. The staging
   demo and test results do not replace that checklist.

## Files to use

`docs/TECHCRAFT_HANDOFF.pdf` and `docs/TECHCRAFT_HANDOFF.html` contain the
combined guide. The plain-text files contain the same instructions and support
direct command copying.

| File or folder | Purpose |
|---|---|
| `docs/TECHCRAFT_HANDOFF.md` | Combined briefing, integration guide and deployment instructions |
| `docs/PLATFORM_BRIEFING.md` | Product overview, team responsibilities and required provider decisions |
| `docs/PLATFORM_INTEGRATION.md` | Technical integration contract and staging test commands |
| `docs/DEPLOYMENT.md` | Installation and upgrade procedures |
| `docs/RUNBOOK.md` and `docs/ALERTS.md` | Recovery procedures and monitoring |
| `docs/SALESFORCE_MAPPING.md` | Fields the platform reads and writes into Salesforce |
| `docs/PRODUCTION_READINESS.md` | Remaining work and launch requirements |
| `docs/artifacts/kyc-signer-example.py` | Runnable signing example |
| `src/` and `alembic/` | Application source and database migrations |
| `KYC_Tool_Build_Package/machine_readable/` | Runtime policy files, preserved from the release |
| `Dockerfile`, `requirements.lock`, `.env.example` | Image build, pinned runtime dependencies and configuration template |
| `tests/` and `manage.sh` | Product tests and local setup commands |
| `MANIFEST.json` | Release identification, source provenance and file checksums |

## Try it locally

Install Python 3.11 or later and PostgreSQL 16, then run these commands from
the extracted package directory:

```sh
./manage.sh setup
./manage.sh doctor
./manage.sh test
bash scripts/dev.sh
```

The demo uses a throwaway database. It starts the API, console, workers and a
sample callback receiver. Open `http://127.0.0.1:8080/ui`. The demo's configured
data sources include development stand-ins; it is not proof that production
providers are connected. Stop it with Ctrl-C.

To build the deployment image:

```sh
docker build -t kyc-tool:__VERSION__ .
```

Keep credentials out of this folder when sharing it. Exchange signing keys
through the agreed secret manager, never email, chat or a ticket. The included
`.env.example` contains configuration examples, not production credentials.
