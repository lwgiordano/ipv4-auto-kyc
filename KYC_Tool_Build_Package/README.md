# KYC Tool Build Package (v2 — Platform-Driven)

This package instructs an AI coding agent (Claude Code) to design and build the IPv4.Global KYC/KYB Tool. It is the single source of truth for the build. It reflects the **final v2 architecture**: the platform is the hub, the KYC Tool is a scoring engine the platform invokes, and Salesforce is a one-way system of record.

> **Supersedes** any earlier handoff material that describes Salesforce fields as the control plane or manual approval starting in Salesforce. Both are obsolete. All entry points — including manual approve — live on the platform.

## How to use with Claude Code (plan mode)

1. Copy this folder into the root of the repository where the tool will be built.
2. Start Claude Code, enter plan mode (Shift+Tab).
3. Prompt:

   ```
   Read KYC_Tool_Build_Package/00_AGENT_BRIEF.md and follow it. Read every
   referenced document and machine_readable file before planning. Then produce
   the implementation plan it requires.
   ```

4. Review the plan, iterate, approve, and let it execute phase by phase (Phase gates are defined in `06_IMPLEMENTATION_PHASES.md`).

## Reading order

| # | File | What it defines |
|---|------|-----------------|
| 0 | `00_AGENT_BRIEF.md` | Mission, non-negotiables, deliverables, planning instructions |
| 1 | `01_ARCHITECTURE.md` | v2 platform-driven architecture, components, sequence flows |
| 2 | `02_SCORING_AND_DECISIONS.md` | Scoring rubric, hard gates, outcomes, supersession, dynamic loop |
| 3 | `03_ADAPTERS_AND_EVIDENCE.md` | The 8 evidence adapters, pass rules, tiers, manual website review |
| 4 | `04_API_AND_DATA_MODEL.md` | Platform↔Tool API contract, events, data model, idempotency |
| 5 | `05_SALESFORCE_SYNC.md` | One-way sync, field mapping, no reverse triggers |
| 6 | `06_IMPLEMENTATION_PHASES.md` | Build phases with acceptance criteria |
| 7 | `07_TEST_PLAN.md` | Fixtures, golden cases, scenario matrix |

`machine_readable/` contains the same rules as JSON for direct consumption: scoring rubric, decision policy, platform events, adapter catalog, state machine, broker policy, Salesforce sync fields.

## The one-paragraph process

A user acts on the IPv4.Global platform (registers, verifies email, adds an ORG-ID, uploads a document, or requests KYB). The platform invokes the KYC Tool. The tool resolves inputs, runs the eight evidence adapters, validates evidence deterministically, creates or supersedes immutable KYC checks, computes the score against a 100-point threshold and four hard gates, and returns a decision. The platform enforces that decision — Approve, Approve with buying locked, Manual Review (insufficient score), or Reject/Suspend — and syncs the full result one way into Salesforce as the system of record. Reviewers manually approve directly on the platform, which bypasses score and gates. Every new piece of evidence re-enters through the platform and re-runs the loop until the case resolves.
