# agent-substrate-kit

A small, dependency-free kit that installs an **agent substrate** into any git
repository: one `./manage.sh` entry point plus the wiring that lets humans,
CI, and coding agents all drive a project the same way — and a `doctor`
command that proves the wiring is intact.

Everything is plain bash. Nothing to compile, no runtime dependencies beyond
`git` and `bash`.

## Install the kit

```sh
mkdir -p ~/ai-kits
git clone <this-repo-url> ~/ai-kits/agent-substrate-kit
```

## Quick start

```sh
cd ~/projects/my-project        # must be a git repo
bash ~/ai-kits/agent-substrate-kit/bootstrap.sh   # standard profile, lang auto-detect
./manage.sh setup
./manage.sh doctor              # verify wiring
```

`bootstrap.sh` detects the project's language(s) from its manifest files,
installs the substrate for the `standard` profile, and wires git hooks.
`setup` prepares the toolchain (venv/deps/modules), and `doctor` verifies that
everything the kit wired is still connected — exit code `0` means healthy.

## What gets installed

```
your-project/
├── manage.sh                    # single entry point (managed)
├── .substrate/
│   ├── substrate.conf           # profile, languages, kit version (managed)
│   ├── manifest                 # what the kit owns vs. seeded (managed)
│   ├── lib.sh                   # substrate runtime (managed)
│   ├── checks.d/*.sh            # doctor checks — drop in your own (managed)
│   ├── hooks/pre-commit         # soft lint hook (managed)
│   └── state/                   # local state, git-ignored
├── AGENTS.md                    # agent guide (seeded — yours to edit)
├── CLAUDE.md                    # pointer to AGENTS.md (seeded)
└── .editorconfig                # (seeded)
```

**Managed** files are refreshed every time you re-run `bootstrap.sh` (or
`./manage.sh update`). **Seeded** files are created once and never touched
again — they belong to the project. `--force` re-seeds them.

## Profiles

| Profile | Contents |
| --- | --- |
| `minimal` | `manage.sh` + `.substrate/` core only |
| `standard` *(default)* | minimal + `AGENTS.md`, `CLAUDE.md`, `.editorconfig`, git-hook wiring |
| `full` | standard + `.claude/settings.json`, `.github/workflows/substrate-ci.yml` |

```sh
bash ~/ai-kits/agent-substrate-kit/bootstrap.sh --profile full
```

## Language support

Auto-detected from project files, override with `--lang` (comma-separated —
monorepos can list several; the first is primary):

| Language | Detected by | `setup` does |
| --- | --- | --- |
| `python` | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt` | create `.venv`, install deps |
| `node` | `package.json` | `npm ci`/`npm install` (or pnpm/yarn by lockfile) |
| `go` | `go.mod` | `go mod download` |
| `rust` | `Cargo.toml` | `cargo fetch` |
| `java` | `pom.xml`, `build.gradle(.kts)` | verify JDK, note build wrapper |
| `ruby` | `Gemfile` | `bundle install` |
| `shell` | top-level `*.sh` | nothing (recommends shellcheck) |
| `generic` | fallback | nothing |

```sh
bash ~/ai-kits/agent-substrate-kit/bootstrap.sh --lang go,node
```

## `./manage.sh` commands

| Command | Purpose |
| --- | --- |
| `setup` | install dependencies / prepare the toolchain |
| `doctor` | verify wiring; exit `1` if anything is broken |
| `fmt` / `lint` / `test` | run the language-appropriate tools |
| `info` | show substrate state |
| `update` | re-run bootstrap from the recorded kit location |
| `precommit` | what the pre-commit hook runs (soft lint) |

## Doctor

`doctor` runs every script in `.substrate/checks.d/` and aggregates
`PASS`/`WARN`/`FAIL` lines. Out of the box it verifies:

- the project is a git repo rooted where the substrate expects (`10-git`)
- `substrate.conf`, the manifest, and every managed file are intact (`20-substrate`)
- `core.hooksPath` points at `.substrate/hooks` and the hook is executable (`30-hooks`)
- required toolchain binaries exist for each configured language (`40-toolchain`)
- setup has been run and local state is git-ignored (`50-state`)

Add project-specific checks by dropping a script into `.substrate/checks.d/`
that prints `PASS`/`WARN`/`FAIL <message>` lines.

## Git hooks

The `standard` and `full` profiles set `core.hooksPath` to `.substrate/hooks`.
The pre-commit hook runs lint in **warn-only** mode by default; export
`SUBSTRATE_STRICT_HOOKS=1` to make lint failures block commits. If your repo
already sets `core.hooksPath`, the kit leaves it alone (take over with
`--force`).

## Other operations

```sh
bash ~/ai-kits/agent-substrate-kit/bootstrap.sh --dry-run     # preview, write nothing
bash ~/ai-kits/agent-substrate-kit/bootstrap.sh --uninstall   # remove managed files + hook wiring
./manage.sh update                                            # refresh managed files from the kit
```

Uninstall keeps seeded files (`AGENTS.md`, etc.) — they belong to the project.

## Kit development

```sh
bash tests/run-tests.sh    # end-to-end suite: sandboxed kit install + sample projects
```

Layout: `bootstrap.sh` (installer) · `lib/` (kit-side helpers: logging,
language detection) · `profiles/` (feature flags per profile) · `templates/`
(everything installed into projects) · `tests/` (e2e suite).
