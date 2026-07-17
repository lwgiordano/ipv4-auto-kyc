# Design — Adopt the superpowers development workflow for this project

- **Date:** 2026-07-17
- **Status:** Draft, self-audited (5-lens adversarial workflow), pending independent Codex audit + human approval
- **Author:** Claude (fable-5), for lwgiordano/ipv4-auto-kyc
- **Scope:** process only — no product code, no change to the M2 hard stop, no
  change to the normative `KYC_Tool_Build_Package/`.

## 1. Goal

Run the remaining remediation backlog (PR 5a → PR 10, PR 1.1, M2) through the
[superpowers](https://github.com/obra/superpowers) skill workflow, enforced by
the machinery we already have (the Claude⇄Codex audit loop), so the discipline
survives across sessions and agents. The chosen shape (from brainstorming) is
the **full cycle**, with artifacts under `.agents/superpowers/`, enforced
**self-referentially via the audit loop** (no new CI infrastructure required).

This design was produced by brainstorming, then audited by a 5-lens adversarial
workflow (safety/governance, enforcement-soundness, environment-fit,
scope/YAGNI, contract-consistency). 36 findings were raised; ~15 survived
verification. The material ones are folded in below and flagged **[audit-fix]**.

## 2. The per-PR cycle (part a)

Every remaining numbered PR runs five stages, each with a superpowers skill and
a gate. **This cycle governs interactive PR development only** — see §6 for the
explicit exemption of autonomous turns.

1. **Spec — `brainstorming`.** Input is the PR's `.agents/ROADMAP.md` §G entry,
   which is already a twice-audited mini-spec. **[audit-fix, scope]** The per-PR
   spec is therefore a **thin delta pointer**: it restates only what the ROADMAP
   leaves genuinely open (resolving it with the human) and links the ROADMAP
   entry for everything already settled. It is NOT a fresh design and MUST NOT
   re-litigate locked decisions (§B/§D of the ROADMAP). If the ROADMAP entry has
   no open questions, the spec is a one-paragraph pointer.
2. **Plan — `writing-plans`.** A stepwise implementation plan with explicit
   verification steps. Human approves before any code.
3. **Build — `executing-plans` + `test-driven-development`.** Failing test
   first, then code. **[audit-fix, TDD]** DB-backed tests are run **red locally**
   against the ephemeral dev-stack Postgres (`bash scripts/dev.sh` /
   `tests/pg.py`), the same path already used to verify PR 4's finding #2 — not
   "trust CI." Only where a red run is genuinely impossible locally is CI the
   witness, and that is called out per-test. **[audit-fix, worktree]** Work
   happens **directly on the shared branch** `claude/project-setup-verify-kpfgjs`
   — NO git worktree — because the Codex bus depends on that single branch.
   Where a skill assumes worktree isolation, ignore that step. Bus claim-first
   before touching files.
4. **Review — `requesting-code-review` / `receiving-code-review`.** The existing
   Codex audit loop IS the review leg (this is a deliberate substitution, not a
   second reviewer). `receiving-code-review` governs how Claude handles findings:
   verify against code, rebut with evidence, no reflexive agreement. The human
   approval gate between audit and fix is unchanged.
5. **Verify & finish — `verification-before-completion` + `finishing-a-development-branch`.**
   No bus RELEASE without evidence (lint, import-linter, tests, adversarial repro
   where security-relevant). **[audit-fix, skill-fit]** `finishing-a-development-branch`
   is used in its **"keep as-is"** mode only — this is a persistent shared branch
   with one long-lived draft PR; the skill MUST NOT force a merge/delete, and its
   local-full-test gate is satisfied by the offline suite + CI. Item done when CI
   is green AND Codex posts `AUDIT-CLEAN` for the range → ROADMAP entry marked
   complete.

## 3. Artifacts (part b)

- Location: `.agents/superpowers/specs/` and `.agents/superpowers/plans/`,
  named `YYYY-MM-DD-pr<NN>-<slug>-design.md` / `-plan.md`.
- **[audit-fix, split-brain]** The skills default to `docs/superpowers/`. The
  override to `.agents/superpowers/` MUST be recorded in `AGENTS.md` (and via
  reference in `CLAUDE.md`) in the bootstrap commit, because that is the only
  place the skills read for path overrides. Otherwise a fresh session writes to
  `docs/` while Codex audits `.agents/` — a split-brain.
- Every bus RELEASE entry for a numbered PR links its spec, plan, and
  verification evidence.

## 4. Enforcement wiring (part c)

- `AGENT_BUS.md` gains a "Superpowers cycle" section (the five stages + the
  artifact-link requirement).
- Codex's standing prompt is **extended**: for a numbered backlog PR, verify the
  spec/plan/verification artifacts exist and match the shipped code; a missing
  artifact, or code that deviates from its plan without a noted reason, is a
  **process finding**. **[audit-fix, read-vs-edit]** This is a READ of committed
  files — it does NOT relax the "Codex edits only `AGENT_BUS.md` in an audit
  round" rule; the prompt wording must say so explicitly.
- **[audit-fix, self-reference]** The artifact-triple requirement applies to
  **numbered backlog PRs (5a onward) only**. The bootstrap/process commit that
  introduces this workflow, and any doc/bus-only or hotfix commit, are exempt
  (they carry a spec but no plan/red-green by nature).
- **[audit-fix, soft-floor — deferred]** Enforcement is intentionally
  human + adversarial-loop, not machine-gated. That is the same soft mechanism
  that let claim-first slip twice (Codex's F7). A cheap belt-and-suspenders CI or
  pre-commit check ("a feature commit has a matching spec+plan") is noted as an
  OPTIONAL later hardening, not part of this adoption. Open question §8.

## 5. Persistence hook (part d)

A committed `.claude/hooks/session-start.sh` + `.claude/settings.json` that, on
**remote** session start, installs the superpowers skills into `~/.claude/skills/`
if absent. Properties:

- **[audit-fix, invalid-git]** Install by **fetch-by-SHA**, not
  `clone --branch <sha>` (which exits 128 — you cannot clone by SHA). Exact
  incantation:
  ```
  git init "$tmp" && git -C "$tmp" fetch --depth 1 origin <PINNED_SHA> \
    && git -C "$tmp" checkout -q FETCH_HEAD
  ```
  Pinned SHA = `d884ae04edebef577e82ff7c4e143debd0bbec99` (superpowers v6.1.1).
  Pinning is deliberate: `main` must not silently change our agent's directives.
- **[audit-fix, remote-gate + fail-soft]** Guard the whole clone behind
  `[ "${CLAUDE_CODE_REMOTE:-}" = "true" ]`, and do NOT wrap the network step in a
  bare `set -euo pipefail` that aborts the session on a clone failure — the hook
  warns and continues (a session without superpowers is degraded, not broken).
- **Idempotent:** if the target skills already exist, no-op. This also makes the
  hook harmless if the platform turns out NOT to wipe `~/.claude` on rebuild
  (open question §8).
- Copies **only** `skills/` (14 dirs) into `~/.claude/skills/`. It deliberately
  does NOT install superpowers' own SessionStart auto-injector hook — see §6.
- Keeps the ~40 skill files OUT of the KYC source tree (`.claude/skills/` is
  git-tracked, so the hook targets `~/.claude`, never the repo). The repo carries
  only the ~30-line hook + settings entry.
- **[audit-fix, pin-governance]** Bumping the pinned SHA is a **reviewed action**
  (it adopts whatever new agent-directing prose upstream wrote), never automatic.

## 6. Scope & the autonomous-turn exemption (part e) — the load-bearing fix

**[audit-fix, approval-gate-erosion — BLOCKER].** The `using-superpowers` skill
demands a skill invocation "before ANY response"; `brainstorming` has a HARD-GATE
("no action until the human approves"). This session also runs **non-interactive
turns with no human present**: the hourly bus self-check-in and PR-webhook
autofix. Treating those directives as authoritative there would force the
autonomous loop to stall or rationalize past its own gate.

Rule: **the superpowers cycle governs interactive PR development only. The
autonomous maintenance turns (scheduled bus check-ins, PR-webhook events) are
explicitly out of scope** and keep their current lightweight behavior. This is
why §5 deliberately omits superpowers' auto-injector hook — the assertive
behavior fires only when Claude explicitly invokes a skill during interactive
work, never automatically on an autonomous turn.

Bootstrap sequencing: the workflow change ships as ONE self-describing process
commit (this spec, the bus "Superpowers cycle" section, the `AGENTS.md` path +
autonomous-exemption notes, the persistence hook). PR 5a is then the first
numbered PR through the cycle. **[audit-fix, locked-decision]** PR 5a's spec must
CONFIRM its approach against ROADMAP §G (which already specifies migration 010,
the versioned canonical signed value, and the dual-accept window) rather than
re-opening it; the only genuinely open item is the v1-sunset timing relative to
TechCraft's integration, which dual-accept is designed to make safe either way.

## 7. Invariants preserved (verified clean by the audit)

- **M2 HARD STOP** untouched: `KYC_ENFORCE_POSITIVE_DECISIONS` stays false in
  production; this is process-only.
- **`KYC_Tool_Build_Package/` unmodified** governance untouched.
- **Human approval gates** between stages, and between Codex audit and fix, stay.
- **[audit-fix, governance-record]** Adopting this workflow is itself a process
  deviation under `AGENTS.md`; record it in `AUDIT_FINDINGS.md` (or a short ADR)
  in the bootstrap commit, same as every other deviation.

## 8. Open questions (resolve before / during bootstrap)

1. **Does the ephemeral rebuild actually wipe `~/.claude`?** The persistence
   premise is unverified — superpowers is already present user-level this
   session. The idempotent hook is harmless either way, but if the platform
   re-bakes skills the hook is dead code. Verify on the next fresh session.
2. **Optional CI/pre-commit artifact floor** (§4) — adopt now or defer?
3. **Intent:** is the goal "ship the remaining ~dozen backlog units safely" or
   "institutionalize this workflow"? The scope lens (partly unverified — several
   verifiers died on API overload) argues stages 1/4/5 are largely relabels and
   TDD (3) + verification (5) are the load-bearing additions. Full-cycle was
   chosen deliberately; this question only calibrates how thin the §2.1 spec
   pointer should be.

## 9. Audit provenance

Self-audit: workflow `wf_d5e5b727-679`, 5 review lenses → per-finding adversarial
verification → synthesis. 36 raised / ~15 survived / 15 refuted / 6 unverified
(verifier API overload). A script accounting bug initially reported "0 findings";
corrected by reading the run journal directly. Codex is asked to independently
audit THIS spec before implementation.
