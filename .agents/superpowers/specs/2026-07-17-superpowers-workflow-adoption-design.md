# Design — Adopt the superpowers development workflow for this project

- **Date:** 2026-07-17
- **Status:** Draft **rev 2** — self-audited (5-lens workflow) + independently
  audited by Codex (8 findings, 3 P1, all accepted); every finding folded in and
  tagged `[F#]`. Pending Codex `AUDIT-CLEAN` on this revision, then human review.
- **Author:** Claude (fable-5), for lwgiordano/ipv4-auto-kyc
- **Scope:** process only — no product code, no change to the M2 hard stop, no
  change to the normative `KYC_Tool_Build_Package/`.

## 1. Goal

Run the remaining remediation backlog (all pending numbered units — PR 1.1, 5a,
5b, 6, 6b, 7a, 7b, 8, 9a/b/c, 10, and M2) through the
[superpowers](https://github.com/obra/superpowers) skill workflow, enforced by
the Claude⇄Codex audit loop, so the discipline survives across sessions and
agents. Shape (from brainstorming): the **full cycle**, artifacts under
`.agents/superpowers/`, enforced self-referentially via the audit loop.

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
  `docs/superpowers/`; the override to `.agents/superpowers/` MUST be recorded in
  `AGENTS.md` (referenced from `CLAUDE.md`) in the bootstrap commit — the only
  place the skills read for path overrides.

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
- Enforcement is intentionally human + adversarial-loop, not machine-gated
  (an optional CI/pre-commit artifact check is deferred — §8 Q2).

## 5. Persistence hook (part d) — tested reference implementation

`.agents/superpowers/hooks/session-start.sh` is the **tested** reference
implementation (committed here for audit; wired into `.claude/settings.json`
only at bootstrap, conditional on §8 Q1). Proven mechanics:

- **[F1] Fetch by URL, not `origin`.** `git init` creates no `origin` remote
  (`git fetch origin <SHA>` → exit 128, reproduced). Correct, tested form:
  `git init <tmp> && git -C <tmp> fetch --depth 1 https://github.com/obra/superpowers.git <SHA> && git -C <tmp> checkout -q FETCH_HEAD`.
  Pin = `d884ae04edebef577e82ff7c4e143debd0bbec99` (v6.1.1). Pinning is
  deliberate; **bumping it is a reviewed action** (§8 Q1), never automatic.
- **[F4] SHA-aware, atomic-by-marker replacement.** A marker file
  (`~/.claude/skills/.superpowers-sha`) records the installed SHA. Install runs
  iff the marker is absent or ≠ pin — so a **reviewed SHA bump replaces** the
  tree (resolving the idempotence/pin-governance contradiction). Each skill dir
  is staged then `mv`-swapped, and the marker is written **last** as the commit
  point, so a partial/failed copy is retried next session rather than becoming
  permanently "installed."
- **[F2] `reloadSkills: true`.** On a successful install the hook returns
  `reloadSkills: true` in its SessionStart JSON, so skills are usable in the
  **same** session (runtime ≥ 2.1.152; live runtime is 2.1.212). Without it a
  clean-home session that just installed the skills could not use them, and if
  the rebuild wipes `~/.claude` the loop never converges. **Bootstrap acceptance
  test:** confirm same-session availability on a real clean-home remote session
  (the exact JSON key must be re-confirmed against the runtime changelog then).
- **Remote-gated + fail-soft.** Runs only when `CLAUDE_CODE_REMOTE=true`; the
  network step is not under `set -e`, so a clone failure warns and continues
  (degraded, never blocks the session). Touches only superpowers' own skill dirs
  (other user skills untouched). Keeps the ~40 skill files OUT of the KYC source
  tree.
- **Test evidence (this revision, clean `$HOME` sandbox):** T1 clean install →
  `reloadSkills:true`, 14 skills + marker present; T2 re-run → `reloadSkills:false`,
  no work; T3 stale marker → reinstall; T4 non-remote → skip; T5 unrelated user
  skill survives a reinstall; `shellcheck` clean.

## 6. Scope & the autonomous-turn exemption (part e) — soft-only, verified

**[F3] The exemption is SOFT, and the spec says so plainly.** The
`using-superpowers` skill applies "when starting any conversation" and upstream's
skills are described as triggering automatically; omitting upstream's own
SessionStart injector (this hook does) **reduces the pressure toward automatic
activation but does not mechanically guarantee non-activation** on an autonomous
turn. The actual guarantee is **instruction priority**: a rule in `AGENTS.md`
that the autonomous maintenance turns — scheduled bus check-ins and PR-webhook
events — are OUT of scope for the superpowers cycle, and that no skill's
"before ANY response" / HARD-GATE directive applies there. `AGENTS.md`
instructions outrank skills (per `using-superpowers` itself), so this holds
behaviorally; it is not a mechanical entrypoint gate.

**Behavioral acceptance test [F3]:** after bootstrap, one scheduled bus
check-in turn AND one PR-webhook turn must each complete **without an approval
prompt and without stalling**. (Empirical pre-evidence: every scheduled check-in
and CI-webhook turn in the current session has already completed cleanly this
way — superpowers is not injected on those turns.) If either stalls, the soft
exemption is insufficient and a real entrypoint gate must be built before the
cycle is adopted.

Bootstrap sequencing: the workflow change ships as ONE self-describing process
commit. PR 5a is then the first numbered unit through the cycle; its spec
CONFIRMS its approach against ROADMAP §G (migration 010, versioned canonical
signed value, dual-accept window) rather than re-opening it — the only open item
is the v1-sunset timing vs TechCraft integration, which dual-accept makes safe
either way.

## 7. Invariants preserved (verified clean by both audits)

- **M2 HARD STOP** untouched; **`KYC_Tool_Build_Package/` unmodified**; **human
  approval gates** intact.
- **[F8] Governance register corrected.** `AUDIT_FINDINGS.md` records
  defects/deviations of the **normative KYC package** only; adopting an agent
  workflow neither changes nor deviates from that package. Record the adoption as
  an **ADR in `docs/architecture-decisions.md`**, not an `AUDIT:<id>` entry.

## 8. Open questions → bootstrap acceptance conditions

1. **Does the ephemeral rebuild wipe `~/.claude`?** **[bootstrap acceptance
   condition]** On the next fresh remote session, check whether a correct
   superpowers install is preserved. **If preserved → remove the persistence
   hook as unnecessary** (the platform re-bakes skills; the hook is dead code).
   **If wiped → retain the fully tested hook** (and complete the F2 same-session
   reload confirmation). Do not wire the hook into `.claude/settings.json` until
   this is decided.
2. **Optional CI/pre-commit artifact floor** (§4) — adopt later or never;
   deferred.

## 9. Audit provenance

- Rev 1 self-audit: workflow `wf_d5e5b727-679`, 5 lenses → per-finding
  adversarial verification → synthesis (36 raised / ~15 survived). A script
  accounting bug initially reported "0 findings"; corrected by reading the run
  journal directly.
- Rev 1 → Rev 2: independent Codex audit `AUDIT [CODEX] 287b086..HEAD` raised 8
  findings (F1/F2/F3 P1, F4/F5/F6 P2, F7/F8 P3), **all accepted**. Notably F1
  caught that Rev 1's own git fix was still invalid. All 8 folded in above; F1/F4
  proven by the clean-home hook tests (§5); F2/F3/§8-Q1 carry explicit bootstrap
  acceptance tests. Codex is asked to re-audit THIS revision to `AUDIT-CLEAN`
  before any implementation (`writing-plans`) begins.
