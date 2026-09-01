# {{PROJECT_NAME}} — Agent Guide

> Seeded by agent-substrate-kit v{{KIT_VERSION}} (profile: {{PROFILE}}).
> This file belongs to the project — keep it current as the project evolves.

## What this project is

<!-- One paragraph: what {{PROJECT_NAME}} does, and for whom. -->

## Everyday commands

All routine work goes through `./manage.sh` — one entry point shared by
humans, CI, and coding agents:

| Command | Purpose |
| --- | --- |
| `./manage.sh setup` | Install dependencies and prepare the toolchain ({{LANGS}}) |
| `./manage.sh doctor` | Verify project wiring — run after setup, and whenever things look broken |
| `./manage.sh fmt` | Format the code |
| `./manage.sh lint` | Lint the code |
| `./manage.sh test` | Run the test suite |
| `./manage.sh info` | Show substrate state (profile, languages, versions) |

## Layout

<!-- Where the interesting code lives and how the pieces fit together. -->

## Conventions

- Languages: {{LANGS}} (primary: {{PRIMARY_LANG}}).
- Run `./manage.sh fmt && ./manage.sh lint` before committing. The pre-commit
  hook runs lint in warn-only mode; export `SUBSTRATE_STRICT_HOOKS=1` to make
  lint failures block commits.
- `.substrate/` and `manage.sh` are managed by agent-substrate-kit: re-run its
  `bootstrap.sh` (or `./manage.sh update`) to refresh them, and don't hand-edit
  them — changes are overwritten on the next update.
- Custom doctor checks can be dropped into `.substrate/checks.d/` (they print
  `PASS`/`WARN`/`FAIL` lines; see the existing checks for the pattern).
