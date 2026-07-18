# Design — Adopt the superpowers development workflow for this project

- **Date:** 2026-07-17 (rev 3: 2026-07-18)
- **Status:** Draft **rev 3** — self-audited (5-lens workflow) + two independent
  Codex audits. Rev 2's 8 findings folded; rev 2's SessionStart hook then drew 5
  more Codex findings, all sharing one root cause. **Rev 3 deletes the hook and
  vendors the skills as a namespaced in-repo plugin.** Pending Codex
  `AUDIT-CLEAN` on rev 3, then human review.
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
skills themselves are **vendored into the repo** (§5), so "survives across
sessions" is a property of the checkout, not of any runtime installer.

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

**rev 3 replaces the SessionStart installer with in-repo vendoring.** Codex's
round-2 audit raised five findings on `session-start.sh`; they share **one root
cause** — the hook was a *network package manager that ran at session startup and
mutated global `~/.claude` state.* Correcting its JSON nesting, timeout,
ownership check, marker, and cleanup would leave that architecture fragile.
Delete it. Commit the pinned payload as a project-local plugin, reviewed like
source code.

**Layout** (`.claude/skills/ipv4-superpowers/`):

```text
.claude/skills/ipv4-superpowers/
├── .claude-plugin/
│   └── plugin.json
├── LICENSE.upstream            # upstream MIT notice, retained per license
├── UPSTREAM.lock.json          # provenance: repo, commit, expected tree, hashes
├── patches/
│   └── project-namespace.patch # deterministic superpowers: -> ipv4-superpowers:
└── skills/
    ├── brainstorming/
    ├── systematic-debugging/
    ├── test-driven-development/
    └── … all 14 pinned skills
```

**Loading mechanism (doc-confirmed).** A directory under `.claude/skills/`
containing `.claude-plugin/plugin.json` loads as the plugin
`ipv4-superpowers@skills-dir` on the next session — *"discovered in place rather
than copied into the plugin cache,"* with no marketplace and no install step
([Claude Code plugins reference — skills-directory plugins](https://code.claude.com/docs/en/plugins-reference)).
There is **no** separate plugins location and **no** `settings.json` entry: the
runtime scans `.claude/skills/` for both bare `SKILL.md` skills and
`.claude-plugin/plugin.json` bundles. Project-scope content loads only after the
**standard workspace trust gate** (the same one that governs `.claude/settings.json`)
— a one-time consent on first clone, not a per-session install, write, or network
call. Plugin skills invoke as `ipv4-superpowers:<skill>`
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

Cost: ~40 files, < 0.5 MB — a small price for deterministic behavior.

**Required invariants (non-negotiable):**

1. Session startup performs **zero network calls** for workflow skills.
2. Session startup **writes nothing outside the repository**.
3. The exact skill payload is committed and reviewed **like source code**.
4. There is **no** marker file, bootstrap installer, or `reloadSkills` contract.
5. Upstream updates **never happen automatically**.
6. Every update identifies an **exact upstream commit and tree hash**.
7. The project namespace **cannot collide** with personal or marketplace skills.
8. Missing, modified, or extra vendored files **fail CI**.
9. Scheduled and webhook turns **mechanically disable skills** (§6).
10. Rollback is a normal `git revert`.

**Provenance — `UPSTREAM.lock.json`:**

```json
{
  "repository": "https://github.com/obra/superpowers.git",
  "commit": "d884ae04edebef577e82ff7c4e143debd0bbec99",
  "plugin_namespace": "ipv4-superpowers",
  "expected_skills": [
    "brainstorming", "dispatching-parallel-agents", "executing-plans",
    "finishing-a-development-branch", "receiving-code-review",
    "requesting-code-review", "subagent-driven-development",
    "systematic-debugging", "test-driven-development", "using-git-worktrees",
    "using-superpowers", "verification-before-completion", "writing-plans",
    "writing-skills"
  ],
  "payload_sha256": "…",
  "patch_sha256": "…"
}
```

**Updates are an explicit maintenance operation — never automatic:**

1. Human selects a new upstream SHA.
2. An update tool fetches it **outside session startup**.
3. It stages the complete replacement, applies the namespace patch, regenerates
   the lock, and shows the semantic diff.
4. CI verifies the resulting tree **offline**.
5. Human review + Codex audit approve the update.
6. The git commit is the **atomic activation point**.

Marketplace install is **not** an equivalent solution: project settings can
still require consent, use a user-level cache, and depend on install state — off
the deterministic path these invariants require.

**CI vendor-verification (one offline job) fails when:**

- any expected skill is absent;
- an unexpected skill remains after an upstream removal;
- any file hash differs from the lock;
- the upstream license is missing;
- an unconverted `superpowers:` reference remains;
- the plugin manifest is invalid;
- the old SessionStart installer, or any `settings.json` entry wiring it, still
  exists.

**Real-runtime acceptance tests (offline):**

- **Empty `$HOME`, network disabled:** all 14 `ipv4-superpowers:*` skills load
  from the checkout.
- **Personal `brainstorming` skill present:** its checksum is unchanged while the
  namespaced project skill is available.
- **Negative:** an intentionally deleted vendored file AND an intentionally stale
  extra file each make verification fail.

**Fallback (only if the plugin bundle ever fails the acceptance test):** 14 bare
skill dirs `.claude/skills/ipv4-superpowers-<skill>/`, each namespaced by a
manual prefix — the mechanism this repo already uses for `architecture` and
`stop-slop`, doc-confirmed collision-safe. The acceptance test decides; the
vendored plugin is primary.

## 6. Scope & the autonomous-turn exemption (part e)

**[F3] Mechanical where the launcher allows flags; honestly soft otherwise.**
Scheduled bus check-ins and PR-webhook turns are OUT of scope for the superpowers
cycle; no skill's "before ANY response" / HARD-GATE directive applies there.

- **Strong (preferred) form — mechanical.** Launch those turns with
  `--disable-slash-commands`, which disables all skill/command invocation while
  **preserving `AGENTS.md` / `CLAUDE.md`** project instructions
  ([Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)).
  (`--bare` is stronger but also drops CLAUDE.md and MCP, so the bus protocol
  would then need to be re-supplied explicitly — not worth it here.)
- **Honesty clause.** If the scheduler/webhook platform cannot control launch
  flags, the exemption is **soft**: it rests on `AGENTS.md` instruction priority
  (which outranks skills per `using-superpowers` itself) and **must not be
  described as guaranteed** until the entrypoint is tested mechanically.
  Empirical pre-evidence: every scheduled check-in and CI-webhook turn in this
  session has completed with no skill activation and no approval stall — evidence,
  not proof.

**Behavioral acceptance test:** after bootstrap, one scheduled bus turn AND one
PR-webhook turn must each complete with no approval prompt and no stall. If
either stalls under the soft form, the mechanical `--disable-slash-commands`
launch is required before the cycle is adopted.

Bootstrap sequencing: the workflow change ships as ONE self-describing process
commit (which also deletes `session-start.sh`). PR 5a is then the first numbered
unit through the cycle; its spec CONFIRMS its approach against ROADMAP §G
(migration 010, versioned canonical signed value, dual-accept window) rather than
re-opening it — the only open item is the v1-sunset timing vs TechCraft
integration, which dual-accept makes safe either way.

## 7. Invariants preserved (verified clean by both audits)

- **M2 HARD STOP** untouched; **`KYC_Tool_Build_Package/` unmodified**; **human
  approval gates** intact.
- **[F8] Governance register corrected.** `AUDIT_FINDINGS.md` records
  defects/deviations of the **normative KYC package** only; adopting an agent
  workflow neither changes nor deviates from that package. Record the adoption as
  an **ADR in `docs/architecture-decisions.md`**, not an `AUDIT:<id>` entry.

## 8. Open questions → resolved by vendoring

1. **~~Does the ephemeral rebuild wipe `~/.claude`?~~ RESOLVED — moot by design.**
   The plugin lives in the repo and is present after every checkout, independent
   of `~/.claude`. Vendoring removes the persistence question entirely: no
   marker, installer, or home-dir state to preserve, so there is nothing for a
   rebuild to wipe. This is why rev 3 supersedes rev 2's hook.
2. **Autonomous-turn flag control (the one remaining open item).** Whether the
   scheduler/webhook launcher can set `--disable-slash-commands` decides whether
   §6's exemption is mechanical or soft. Decided at bootstrap by §6's behavioral
   test; until then the exemption is documented as soft, not guaranteed.

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
  5 findings, **all on the SessionStart hook** (`reloadSkills` nesting — since
  confirmed against the official Claude Code docs; no fetch timeout; user-skill
  overwrite; marker accepts a partial install; temp-dir leak). Root cause: the
  hook was a network package manager mutating global `~/.claude` at startup.
  **Rev 3 deletes the hook** and vendors the pinned payload as the namespaced
  project plugin `ipv4-superpowers` (§5); the loading mechanism,
  `.claude/skills/` plugin discovery, namespacing, and `--disable-slash-commands`
  behavior are all doc-confirmed. The hook file is removed in the bootstrap
  implementation commit, after this design passes human + Codex review. F5/F6/F7/F8
  (workflow boundaries) are unchanged and were verified internally coherent in
  round 2. Codex is asked to re-audit THIS revision to `AUDIT-CLEAN` before any
  implementation (`writing-plans`) begins.
