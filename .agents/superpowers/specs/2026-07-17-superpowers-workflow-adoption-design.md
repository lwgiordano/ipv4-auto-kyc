# Design — Adopt the superpowers development workflow for this project

- **Date:** 2026-07-17 (rev 3–5: 2026-07-18)
- **Status:** Draft **rev 5** — self-audited (5-lens workflow) + four independent
  Codex audits, each fully folded. Rev 2 folded 8 findings; rev 3 deleted the
  SessionStart hook and vendored the skills as an in-repo plugin (Codex validated
  the layout loads on the real runtime); rev 4 folded 5 refinements (two of them
  over-claims of mine, corrected); rev 5 folds 3 more (executable-mode in the
  integrity digest, a corrected fallback-discovery analysis, and a bus-process
  fix). Codex verified rev4-F1..F5 all closed. Pending Codex `AUDIT-CLEAN` on rev
  5, then human review.
- **Author:** Claude, for lwgiordano/ipv4-auto-kyc
- **Scope:** process only — no product code, no change to the M2 hard stop, no
  change to the normative `KYC_Tool_Build_Package/`.

## 1. Goal

Run the remaining remediation backlog (all pending numbered units — PR 1.1, 5a,
5b, 6, 6b, 7a, 7b, 8, 9a/b/c, 10, and M2) through the
[superpowers](https://github.com/obra/superpowers) skill workflow, enforced by
the Claude⇄Codex audit loop, so the discipline survives across sessions and
agents. Shape (from brainstorming): the **full cycle**, artifacts under
`.agents/superpowers/`, enforced self-referentially via the audit loop. The
skills themselves are **vendored into the repo** (§5), so the skill *files*
survive across sessions as a property of the checkout (the workspace *trust*
grant is a separate, home-scoped concern — §5, §8).

## 2. The per-PR cycle (part a)

Every remaining numbered unit runs five stages. **The cycle governs interactive
PR development only** — §6 exempts the autonomous turns.

1. **Spec — `brainstorming`.** Input is the unit's `.agents/ROADMAP.md` §G entry
   (already a twice-audited mini-spec). The per-PR spec is a **thin delta
   pointer**: it restates only what the ROADMAP leaves genuinely open (resolving
   it with the human) and links the ROADMAP entry for the rest. It MUST NOT
   re-litigate locked decisions (§B/§D of the ROADMAP).
2. **Plan — `writing-plans`.** A stepwise plan with explicit verification steps.
   Human approves before code.
3. **Build — `subagent-driven-development` + `test-driven-development`.**
   **[F7]** `subagent-driven-development`, NOT `executing-plans`:
   `executing-plans/SKILL.md` itself dispatches to `subagent-driven-development`
   when subagents are available, and Claude Code qualifies. **Lane discipline:
   subagents do bounded implementation work only; the parent agent remains the
   SOLE committer, pusher, and bus writer.** TDD: failing test first, then code;
   DB-backed tests are run **red locally** against the ephemeral dev-stack
   Postgres (`bash scripts/dev.sh` / `tests/pg.py`), not "trust CI." Work happens
   **directly on the shared branch** `claude/project-setup-verify-kpfgjs` — NO
   git worktree — because the Codex bus depends on that single branch; ignore any
   skill step that assumes worktree isolation. Bus claim-first before editing.
4. **Review — `requesting-code-review` / `receiving-code-review`.** The Codex
   audit loop IS the review leg (deliberate substitution). `receiving-code-review`
   governs handling findings: verify against code, rebut with evidence, no
   reflexive agreement. Human approval gate between audit and fix is unchanged.
5. **Verify & finish — `verification-before-completion` + `finishing-a-development-branch`
   ("keep as-is" mode only).** No bus RELEASE without the verification artifact
   (§3). `finishing-a-development-branch` MUST NOT force a merge/delete on this
   persistent shared branch. Unit done when CI is green AND Codex posts
   `AUDIT-CLEAN` for its range → ROADMAP entry marked complete.

## 3. Artifacts (part b)

Three artifacts per numbered unit, all under `.agents/superpowers/`:

- **Spec:** `specs/YYYY-MM-DD-pr<NN>-<slug>-design.md`.
- **Plan:** `plans/YYYY-MM-DD-pr<NN>-<slug>-plan.md`.
- **[F6] Verification artifact — defined.** The unit's bus RELEASE entry IS the
  verification artifact; to count, it MUST contain: (i) the commands run
  (lint, import-linter, tests) with their pass/fail results; (ii) the DB/golden
  suite witness (local red→green note or the CI run URL/SHA); (iii) an
  adversarial-repro line where the change is security-relevant; (iv) the anchor
  commit SHA. A RELEASE lacking these fields is an incomplete artifact.
- **[F5, split-brain] Location override recorded.** The skills default to
  `docs/superpowers/` for their own working notes; the override to
  `.agents/superpowers/` MUST be recorded in `AGENTS.md` (referenced from
  `CLAUDE.md`) in the bootstrap commit — the only place the skills read for path
  overrides.

## 4. Enforcement wiring (part c)

- `AGENT_BUS.md` gains a "Superpowers cycle" section (the five stages + the
  artifact requirement).
- Codex's standing prompt is **extended**: for a numbered backlog unit, verify
  the spec, plan, and verification-artifact (§3) exist and match the shipped
  code; a missing artifact, or code that deviates from its plan without a noted
  reason, is a **process finding**. This is a READ of committed files — it does
  NOT relax "Codex edits only `AGENT_BUS.md` in an audit round"; the prompt says
  so explicitly.
- **[F5, boundary] Scope of the artifact requirement:** ALL remaining pending
  numbered units — **including PR 1.1** — wherever each lands in the sequence.
  Exempt only: the bootstrap/process commit that introduces this workflow, and
  doc-only / bus-only / hotfix commits (they carry a spec but no plan/red-green
  by nature).
- Enforcement is human + adversarial-loop for the *process*; the *vendored
  payload* is machine-gated by the §5 CI job (integrity is not a judgement call).

## 5. Persistence — vendored, namespaced project plugin (part d)

**rev 3 replaced the SessionStart installer with in-repo vendoring; rev 4 fixes
the runtime-coupling details Codex found.** Codex's round-2 audit raised five
findings on `session-start.sh`; they shared **one root cause** — the hook was a
*network package manager that ran at session startup and mutated global
`~/.claude` state.* Correcting its JSON, timeout, ownership, marker, and cleanup
would leave that architecture fragile. Delete it. Commit the pinned payload as a
project-local plugin, reviewed like source code.

**Layout** (`.claude/skills/ipv4-superpowers/`):

```text
.claude/skills/ipv4-superpowers/
├── .claude-plugin/
│   └── plugin.json
├── LICENSE.upstream            # upstream MIT notice, retained per license
├── UPSTREAM.lock.json          # provenance: repo, commit, tree OIDs, per-file digest
├── patches/
│   └── project-namespace.patch # deterministic superpowers: -> ipv4-superpowers:
└── skills/
    ├── brainstorming/
    ├── systematic-debugging/
    ├── test-driven-development/
    └── … all 14 pinned skills
```

**Loading mechanism (doc-confirmed + Codex-verified on the runtime).** A directory
under `.claude/skills/` containing `.claude-plugin/plugin.json` loads as the
plugin `ipv4-superpowers@skills-dir` on the next session — *"discovered in place
rather than copied into the plugin cache,"* with no marketplace and no install
step ([Claude Code plugins reference](https://code.claude.com/docs/en/plugins-reference)).
Codex confirmed the exact layout loads on the installed runtime and reports its
namespaced skill. Two runtime couplings the design must honor:

- **[rev4-F2] Workspace trust is home-scoped.** Project-scope content (this
  plugin, and bare project skills alike) loads only after the workspace **trust
  gate**, and that decision is recorded under the user's home, not the repo.
  Codex verified `HOME=<empty> claude plugin list` returns `(suppressed)@skills-dir`
  / "workspace not trusted." So vendoring makes the *files* persistent but not the
  *trust grant*. **On this remote platform the practical risk looks low:** the
  current remote session already loads project skills (`architecture`, `stop-slop`)
  and the vendored bus at repo root **with no trust prompt** — evidence the
  launcher pre-provisions/persists workspace trust. This is confirmed at bootstrap
  (§8-Q1); if a fresh rebuild does not auto-trust, the first session per rebuild
  needs a **one-time trust acceptance** (a click, not a network install) — an
  accepted minor degradation.
- **[rev4-F3] Discovery is CWD-relative — repo-root launch required.** Project
  `@skills-dir` plugins are discovered from the session's working directory and do
  **not** walk up to the repo root. Codex repro: at the checkout root the plugin
  is detected; from `subdir/` it is not. Sessions MUST launch at the repository
  root (invariant 11); the current remote session does (`CWD = repo root`). A
  subdir-launch negative test is part of acceptance.

Plugin skills invoke as `ipv4-superpowers:<skill>`
([skills — command name](https://code.claude.com/docs/en/skills)).

**Namespace.** Use the unique `ipv4-superpowers`, not the generic `superpowers`,
so a developer who already has an upstream Superpowers plugin does not collide.
One deterministic patch (`patches/project-namespace.patch`) rewrites internal
cross-references `superpowers:<skill>` → `ipv4-superpowers:<skill>`.

**License.** Upstream is MIT, which permits vendoring and modification provided
the notice is retained → `LICENSE.upstream` (the
[pinned commit's LICENSE](https://raw.githubusercontent.com/obra/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/LICENSE)).

**Why this eliminates every round-2 finding** (root cause, not per-symptom patch):

| Round-2 hook finding | Eliminated by |
|---|---|
| P1 — wrong `reloadSkills` nesting | no runtime install; no reload signal at all |
| P2 — network fetch stalls startup | no startup network call |
| P2 — existing user skill overwritten | no writes to `~/.claude`; unique namespace |
| P2 — marker accepts incomplete install | the git checkout **is** the complete payload |
| P3 — temp dirs leak on failure | no runtime staging / temp directories |

Cost: ~40 files (Codex measured 48 files, 436 KiB at the pinned SHA) — a small
price for deterministic behavior.

**Required invariants (non-negotiable):**

1. Session startup performs **zero network calls** for workflow skills.
2. Session startup **writes nothing outside the repository**.
3. The exact skill payload is committed and reviewed **like source code**.
4. There is **no** marker file, bootstrap installer, or `reloadSkills` contract.
5. Upstream updates **never happen automatically**.
6. Every update identifies an **exact upstream commit and tree OID**.
7. The project namespace **cannot collide** with personal or marketplace skills.
8. Missing, modified, extra, or **mode-changed** vendored files **fail CI** —
   the §5 digest binds file content AND git mode (rev5-F1).
9. **[rev4-F1] Autonomous turns are held out of the cycle — soft, with accepted
   residual risk.** They SHOULD launch with `--disable-slash-commands` where the
   launcher permits (mechanical). Where it does not (we do not control the remote
   scheduler/webhook launcher's flags), the exemption is **soft** — it rests on
   `AGENTS.md` instruction priority — and the residual risk (an autonomous turn
   could auto-invoke a workflow skill) is **explicitly accepted**. This is not a
   mechanical guarantee; a behavioral sample is evidence, not a control (§6).
10. Rollback is a normal `git revert` (taking effect on the next session — see
    the update procedure below).
11. **[rev4-F3] Sessions launch at the repository root** — `@skills-dir` plugins
    are root-CWD-only and do not walk up, so a subdir launch does not load the
    workflow. **[rev5-F2]** This constraint is specific to the *plugin*; the
    bare-skill fallback (§5) *does* walk up to the repo root and would be exempt.

**Provenance & integrity — `UPSTREAM.lock.json` (rev4-F5, fully specified):**

```json
{
  "repository": "https://github.com/obra/superpowers.git",
  "commit": "d884ae04edebef577e82ff7c4e143debd0bbec99",
  "upstream_tree_oid": "795caed14920f27a1d2d152a09b4720194f64472",
  "upstream_skills_tree_oid": "2f2679ad867d88ec2286a42c5dc0509f15d53e54",
  "plugin_namespace": "ipv4-superpowers",
  "expected_skills": [
    "brainstorming", "dispatching-parallel-agents", "executing-plans",
    "finishing-a-development-branch", "receiving-code-review",
    "requesting-code-review", "subagent-driven-development",
    "systematic-debugging", "test-driven-development", "using-git-worktrees",
    "using-superpowers", "verification-before-completion", "writing-plans",
    "writing-skills"
  ],
  "digest": {
    "algorithm": "sha256",
    "path_set": "every file under .claude/skills/ipv4-superpowers/ EXCEPT UPSTREAM.lock.json itself",
    "path_normalization": "repo-relative, '/'-separated, sorted lexicographically (bytewise)",
    "scope": "file CONTENT + normalized git mode (rev5-F1: 100644 or 100755 ONLY; reject 120000 symlinks / 160000 gitlinks / any other type); mtime/ownership excluded",
    "files": { "<sorted repo-relative path>": { "sha256": "<hex>", "mode": "100644|100755" } },
    "aggregate": "sha256 over the sorted '<path>\\0<mode>\\0<sha256>\\n' records of `files`"
  },
  "patch_sha256": "<sha256 of patches/project-namespace.patch>"
}
```

Two distinct checks, deliberately separated:

- **Local-integrity (offline, every CI run):** recompute the per-file
  `{sha256, mode}` map + `aggregate` over the checkout's `path_set` and compare to
  the lock. Any missing, modified, extra, or **mode-changed** file (e.g. an
  executable helper script flipped `0755→0644`, content untouched) changes the
  aggregate → fail. Requires no network; does not consult upstream.
- **Upstream-provenance (update time only):** fetch the pinned `commit`, confirm
  `upstream_tree_oid` / `upstream_skills_tree_oid` match, then re-derive the
  vendored tree by applying `patches/project-namespace.patch` and confirm the
  result reproduces the lock's `digest`.

**Updates are an explicit maintenance operation — never automatic — and are
live-watch-safe [rev4-F4]:**

`git commit` is **NOT** the activation point: Claude Code live-watches project
skill text and applies edits in the current session, so replacing a vendored
`SKILL.md` in a trusted live workspace activates it immediately, before review.
The update therefore runs **off** the watched tree:

1. Human selects a new upstream SHA.
2. An update tool fetches + stages + applies the namespace patch + regenerates
   the lock + emits the semantic diff **entirely outside any discovered
   `.claude/skills/` tree** (a scratch dir), so nothing activates live.
3. The update runs in a **maintenance session launched with
   `--disable-slash-commands`**, so even the in-tree swap cannot auto-activate a
   skill mid-review.
4. CI runs the offline local-integrity + the upstream-provenance checks.
5. Human review + Codex audit approve the update.
6. **Activation = starting a NEW session on the reviewed commit** — not the
   `git commit` command, and not the maintenance session.

Marketplace install is **not** an equivalent solution: project settings can still
require consent, use a user-level cache, and depend on install state — off the
deterministic path these invariants require.

**CI vendor-verification (one offline job) fails when:**

- any expected skill is absent;
- an unexpected skill remains after an upstream removal;
- the local-integrity digest does not match the lock (missing/modified/extra file);
- the upstream license is missing;
- an unconverted `superpowers:` reference remains;
- the plugin manifest is invalid;
- the old SessionStart installer, or any `settings.json` entry wiring it, still
  exists.

**Real-runtime acceptance tests (offline):**

- **Empty `$HOME`, network disabled, workspace pre-trusted:** all 14
  `ipv4-superpowers:*` skills load from the checkout. (If trust cannot be
  pre-provisioned in the harness, the test asserts the single expected trust
  prompt, then load — documenting the rev4-F2 boundary.)
- **Personal `brainstorming` skill present:** its checksum is unchanged while the
  namespaced project skill is available.
- **[rev4-F3] Subdir launch negative:** a session started in `src/` does **not**
  discover the plugin — documents the repo-root requirement (invariant 11).
- **[rev5-F1] Executables run:** every shipped executable (the upstream `100755`
  helper scripts — e.g. `subagent-driven-development/scripts/review-package`,
  `.../task-brief`, `brainstorming/scripts/start-server.sh`) is present with mode
  `100755` and actually executes.
- **Negative integrity:** an intentionally deleted vendored file, an intentionally
  stale extra file, AND a **mode-only flip** (`0755→0644` on a helper script,
  content unchanged) each fail the local-integrity check.

**Fallback (only if the plugin bundle ever fails an acceptance test):** 14 bare
skill dirs `.claude/skills/ipv4-superpowers-<skill>/`, each namespaced by a manual
prefix — the mechanism this repo already uses for `architecture` and `stop-slop`.
**[rev5-F2, corrected]** Bare project skills are discovered by walking from the
launch directory **up to the repo root** (Codex runtime-verified on 2.1.179), so
they **do escape rev4-F3** — a subdir launch still finds them, unlike the
root-CWD-only plugin; if the fallback were adopted, invariant 11 would not apply.
They remain **equally trust-gated (rev4-F2 still applies)**. The tradeoff for the
acceptance-test decision: the plugin gives automatic `plugin:skill` namespacing
but requires a repo-root launch; the bare fallback is subdir-robust but uses
manual-prefix namespacing. The vendored plugin is primary.

## 6. Scope & the autonomous-turn exemption (part e)

**[F3 / rev4-F1] Mechanical where the launcher allows flags; honestly soft with
accepted residual risk otherwise.** Scheduled bus check-ins and PR-webhook turns
are OUT of scope for the superpowers cycle; no skill's "before ANY response" /
HARD-GATE directive applies there.

- **Strong (preferred) form — mechanical.** Where the launcher permits, launch
  those turns with `--disable-slash-commands`, which disables all skill/command
  invocation while **preserving `AGENTS.md` / `CLAUDE.md`** project instructions
  ([Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)).
  (`--bare` is stronger but also drops CLAUDE.md and MCP, so the bus protocol
  would then need to be re-supplied explicitly — not worth it here.)
- **Soft form — honest, with accepted residual risk.** We do **not** control the
  remote scheduler/webhook launcher's flags, so mechanical suppression may be
  unavailable. In that case the exemption rests on `AGENTS.md` instruction
  priority (which outranks skills per `using-superpowers` itself). The residual
  risk — an autonomous turn could auto-invoke a workflow skill — is **explicitly
  accepted**; it is low-consequence (extra planning ceremony, not a wrong code
  change, and human gates still sit before any fix). It is **not** described as a
  mechanical guarantee. Empirical mitigation: every scheduled check-in and
  CI-webhook turn in this session has completed with no skill activation and no
  approval stall.

**Behavioral acceptance test (evidence, not a control):** after bootstrap, one
scheduled bus turn AND one PR-webhook turn each complete with no approval prompt
and no stall. A passing sample does not upgrade the soft form to mechanical; it
only corroborates that the accepted residual risk is not manifesting. If either
stalls, the mechanical `--disable-slash-commands` launch becomes a prerequisite.

Bootstrap sequencing: the workflow change ships as ONE self-describing process
commit (which also deletes `session-start.sh`). PR 5a is then the first numbered
unit through the cycle; its spec CONFIRMS its approach against ROADMAP §G
(migration 010, versioned canonical signed value, dual-accept window) rather than
re-opening it — the only open item is the v1-sunset timing vs TechCraft
integration, which dual-accept makes safe either way.

## 7. Invariants preserved (verified clean by all audits)

- **M2 HARD STOP** untouched; **`KYC_Tool_Build_Package/` unmodified**; **human
  approval gates** intact.
- **[F8] Governance register corrected.** `AUDIT_FINDINGS.md` records
  defects/deviations of the **normative KYC package** only; adopting an agent
  workflow neither changes nor deviates from that package. Record the adoption as
  an **ADR in `docs/architecture-decisions.md`**, not an `AUDIT:<id>` entry.

## 8. Open questions → bootstrap acceptance conditions

1. **[rev4-F2] Workspace-trust durability across a rebuild.** The plugin *files*
   are in-repo (persistent); the *trust grant* that lets them load is home-scoped.
   **Bootstrap gate:** on a fresh remote session, confirm project skills load with
   no trust prompt (as they do in the current session — strong pre-evidence the
   platform pre-provisions trust). If a rebuild does not auto-trust, accept a
   one-time trust acceptance on the first session per rebuild (a click, not a
   network install). Vendoring resolved the payload-persistence half of the old
   §8-Q1; this is the remaining half, correctly scoped.
2. **Autonomous-turn flag control.** Whether the scheduler/webhook launcher can
   set `--disable-slash-commands` decides whether §6's exemption is mechanical or
   soft-with-accepted-risk. Tested at bootstrap by §6's behavioral test; until
   then the exemption is documented as soft, not guaranteed.

(The earlier "optional CI floor" question is closed: the §5 vendor-verification
job is a **required** gate, not deferred.)

## 9. Audit provenance

- Rev 1 self-audit: workflow `wf_d5e5b727-679`, 5 lenses → per-finding
  adversarial verification → synthesis (36 raised / ~15 survived). A script
  accounting bug initially reported "0 findings"; corrected by reading the run
  journal directly.
- Rev 1 → Rev 2: independent Codex audit `AUDIT [CODEX] 287b086..HEAD` raised 8
  findings (F1/F2/F3 P1, F4/F5/F6 P2, F7/F8 P3), all accepted and folded. F1
  caught that rev 1's own git fix was still invalid.
- Rev 2 → Rev 3: independent Codex audit `AUDIT [CODEX] 9b2abfe..d088f7b` raised
  5 findings, **all on the SessionStart hook** (`reloadSkills` nesting confirmed
  against the docs; no fetch timeout; user-skill overwrite; partial-install
  marker; temp-dir leak). Root cause: the hook was a network package manager
  mutating global `~/.claude`. Rev 3 deleted the hook and vendored the pinned
  payload as the namespaced plugin `ipv4-superpowers`.
- Rev 3 → Rev 4: independent Codex audit `AUDIT [CODEX] a25b486..b34ca72` raised
  5 findings and **validated the vendored layout on the runtime**
  (`ipv4-superpowers@skills-dir` loads; 14 skill dirs / 48 files / 436 KiB + MIT;
  `--disable-slash-commands` works). Folded here: rev4-F1 (invariant-9 vs §6
  contradiction → soft exemption with explicitly accepted residual risk),
  rev4-F2 (§8-Q1 not moot → trust grant is home-scoped, reframed as a bootstrap
  gate with pre-evidence the platform auto-trusts), rev4-F3 (CWD-relative
  discovery → repo-root invariant 11 + subdir negative test), rev4-F4 (git commit
  is not activation → live-watch-safe update off the watched tree, activation = a
  new session on the reviewed commit), rev4-F5 (lock schema → canonical per-file
  digest + aggregate + upstream tree OIDs, local-integrity vs upstream-provenance
  separated). Two were over-claims of mine, corrected to honest statements.
- Rev 4 → Rev 5: independent Codex audit `AUDIT [CODEX] 1bee45b..383ab6c`
  **verified rev4-F1..F5 all closed** and raised 3 smaller findings, all folded:
  rev5-F1 (P2 — the content-only digest missed executable-mode flips; confirmed
  `subagent-driven-development/scripts/review-package` is `0755` and a `0755→0644`
  flip keeps the content sha → the digest now binds a normalized git mode and
  acceptance exercises every shipped executable), rev5-F2 (P3 — my fallback
  analysis was wrong; bare project skills walk up to the repo root and DO escape
  rev4-F3, unlike the root-CWD-only plugin → §5 corrected, invariant 11 scoped to
  the plugin), rev5-F3 (P3 — rev 4 was a single commit with no pre-edit CLAIM →
  remedied by a separate CLAIM pushed before this revision). The hook file is
  removed in the bootstrap implementation commit, after this design passes human +
  Codex review. Codex is asked to re-audit rev 5 to `AUDIT-CLEAN` before any
  implementation (`writing-plans`) begins.
