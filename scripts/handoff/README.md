# IPv4.Global KYC/KYB Tool

Release `__VERSION__` · source commit `__COMMIT__`

Start with [START-HERE.md](START-HERE.md). This is a source package for
TechCraft's integration planning and closed-staging tests, not production use.

The platform sends signed company-verification events. The tool gathers
evidence, records checks and returns a decision. TechCraft owns the platform
screens, applies the decision and writes the supplied values into Salesforce.
The tool does not write Salesforce or replace the platform.

The [combined handoff](docs/TECHCRAFT_HANDOFF.md) contains the product briefing,
technical integration contract and deployment procedures. The
[production checklist](docs/PRODUCTION_READINESS.md) names the unfinished work
and the evidence required before launch.

The handoff is also available as [PDF](docs/TECHCRAFT_HANDOFF.pdf) and
[HTML](docs/TECHCRAFT_HANDOFF.html).

## Local commands

```sh
./manage.sh setup
./manage.sh doctor
./manage.sh test
./manage.sh lint
bash scripts/dev.sh
```

Python 3.11 or later is required. Product integration tests and the local demo
need PostgreSQL; this release is tested with PostgreSQL 16. Docker is needed
only to build or run the deployment image.

Configuration examples are in `.env.example`. Keep real credentials outside
the distributed package. See `MANIFEST.json` for file checksums and the
source commit used to assemble this release.
