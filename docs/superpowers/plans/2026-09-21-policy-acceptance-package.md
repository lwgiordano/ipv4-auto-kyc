# Policy Corrections and Integration Acceptance Package

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Parent alone commits, pushes and writes the bus.

**Goal:** Implement the two approved policy corrections and publish a staging package with explicit receiver permissions and completion criteria.

**Architecture:** Keep the existing five gates and four decision values. Authoritative inactive-company evidence fails the existing conflict gate. Only an independently verified POC check satisfies automated control proof; email and public ORG-ID matches retain supporting points. Explicit manual approval remains the existing recorded exception. Callback fields remain compatible and are documented as evidence, not standalone permission grants.

**Tech Stack:** Python, PostgreSQL 16, pytest, normative JSON policy, existing package exporter and PDF renderer.

**Spec:** Human answers on 2026-09-21: exact inactive-company registry match requires manual review rather than automatic rejection; public ORG-ID and self-declared email domain cannot establish control without independent verification or recorded human approval. This plan narrows the current independent automated proof to the existing POC verification mechanism. It does not invent a new provider or authorization credential.

## Global Constraints

- No production activation, migration changes, fabricated external test evidence or paid-provider selection.
- Preserve frozen migration bytes, wire decision values and callback ordering restrictions.
- No points change; only control eligibility and adverse-evidence precedence change.
- Existing manual approvals remain authoritative and explicitly recorded.
- Old stored categories or policy bundles must not bypass the new runtime control rule.
- Public documents contain product facts and responsibilities, no review/build narrative; archive documents are `.txt`, plus HTML/PDF.
- Engine source pin and policy version/baseline updates accompany source changes. Bump the engine build to `eng-2`: the control and conflict gates change decision semantics, so new decisions must distinguish this engine from historical `eng-1` decisions.

## Review Focus

1. Wrong-company inactive search candidates must not block the applicant: Task 1 tests identity mismatch.
2. An exact active candidate must not hide an exact inactive candidate: Task 1 tests both source/order permutations.
3. Legacy checks with category `control_proof` must not restore email/ORG-ID authorization: Task 1 tests old categories directly.
4. A manual approval must not be undone by an automatic callback: Task 2 permission precedence and acceptance cases.
5. Partial, unavailable or unordered evidence must not grant permissions even with `buy_enablement=enabled`: Task 2 receiver acceptance table.

## Task 1: Policy and scoring authority

**Files:** `src/kyc_tool/validators/registry.py`, `src/kyc_tool/domain/scoring.py`, `src/kyc_tool/domain/decision.py` (stale hold docstring only), normative `scoring_rubric.json` and `decision_policy.json`, `AUDIT_FINDINGS.md`, `AGENTS.md`, focused validator/scoring tests, golden fixtures and their related integration tests, policy/source hash baselines.

**Interface:** Preserve `registry_intent(adapter_outputs, case_snapshot)`, `evaluate_gates(...)`, and `decide(...)` signatures. Preserve all existing wire enum values.

- [ ] Write and run failing tests before implementation. The original inactive-company witness must be tested separately from the independent-control change: include a verified POC so a missing control check cannot mask the adverse-evidence failure.

```python
assert registry_intent(exact_dissolved_registry, submission).status is CheckStatus.FAIL
assert result_with_exact_inactive_registry_and_poc.decision is Decision.MANUAL_REVIEW_INSUFFICIENT
assert not result_with_exact_inactive_registry_and_poc.gates.no_hard_conflict
assert not gates_with_public_org_and_email_only.control_proof
assert not gates_with_legacy_control_categories.control_proof
assert gates_with_verified_poc.control_proof
```

- [ ] Judge candidate identity before treating inactive status as authoritative. An inactive reason may block only a fully matched name/address/registration-number candidate. Preserve incomplete and mismatched evidence as such.
- [ ] Inspect all relevant candidates before returning a PASS. Exact inactive evidence wins over an exact active record in either source/order; preserve the reason in the live registry check.
- [ ] Add the exact-only `registry_exact_company_inactive` reason to the existing conflict gate while retaining `registry_company_inactive` for diagnosis. Do not reinterpret legacy generic inactive reasons, which may describe wrong-company search candidates. An unrelated later positive check cannot clear the exact negative; a superseding registry check can resolve the evidence, while explicit human approval remains possible.
- [ ] Change email/ORG-ID policy categories to supporting, retaining points. Require a live passed `poc_verified` check for automated control; do not let legacy categories or edited point weights satisfy it. This is not retrospective validation of historical POC checks; governed evidence revalidation remains a production prerequisite.
- [ ] Update normative policy versions and intentional baselines. Record the human-approved amendment internally. Update affected golden expectations to preserve original scenario intent, adding explicit POC evidence only where a positive scenario is intended.
- [ ] Run focused policy, validator, golden and relevant pipeline tests. A separate reviewer checks identity matching, supersession and manual exception paths before the parent source commit.

## Task 2: Receiver permissions and completion criteria

**Files:** canonical/public `PLATFORM_BRIEFING.md`, `PLATFORM_INTEGRATION.md`, `PRODUCTION_READINESS.md`; public README/START-HERE where needed; document parity tests and copy manifest; combined reference generated by parent.

**Interface:** No new receiver runtime or callback fields. The current wire stays unsequenced; `buy_enablement` remains ORG-ID eligibility, not sufficient authority to change permissions.

- [ ] Add regression assertions for explicit held-buy, partial/unavailable run, unordered/conflicting callback, manual-approval precedence and durable-ack rules.
- [ ] Replace the suggested result mapping with a precedence-ordered permission table applied after receiver acceptance/effectiveness. The existing interim receiver may record the first callback as its current automatic decision; that does not authorize live permissions. For the current release, callbacks can be stored/acknowledged but cannot automatically grant live permissions. A held decision never enables account/buying; partial or unknown freshness requires review; manual decisions remain authoritative; conflicting/unordered results never use last-arrival or timestamps as authority. Do not claim that document assertions execute or certify TechCraft's receiver.
- [ ] Distinguish current staging behavior from the future production acceptance tests. Keep account approval separate from ORG-ID purchase eligibility. Do not imply the reference receiver certifies TechCraft's independently implemented receiver.
- [ ] Add a completion matrix with current implementation, responsible team and observable acceptance result for provider wiring, applicant authority, registry negatives, ordering, evidence freshness, reviewer workflows, sanctions, Salesforce reconciliation, recovery and capacity. Mark external evidence uncollected, not passed.
- [ ] Document the amended policy in plain product language and retain actual outstanding provider/migration work. No biographies, review scores, development history or statements addressed to the package requester.
- [ ] Run copy lint, document tests and independent claim-to-code review; refresh copy digests only after review.

## Task 3: Combined verification and archive

**Files:** package tests/exporter only if required by new content; internal combined reference; internal release report and bus. ZIP lives in `output/techcraft/`, not git.

- [ ] Run independent whole-unit review, full PostgreSQL suite, scoped formatting, lint, import contracts and diff check; verify policy/source hashes. Parent commits source and documentation together, pushes and checks CI.
- [ ] Build r6 from the committed source. Inspect the rendered PDF and all public documents. Verify no Markdown entries, conversation/review credits or broken references; validate manifest hashes, archive CRC and preserved command blocks.
- [ ] Extract into a clean directory, run setup/doctor and the complete packaged suite. Report skips/failures and Docker/provider/platform checks not run separately.
- [ ] Post RELEASE with exact source range, archive hash, test results and unresolved production criteria. Deliver the ZIP, clearly labelled staging, without pretending external integration acceptance was completed.

## Approval gate

This plan changes business rules, not just documentation. Confirm the scope before implementation. The existing verified POC is the only automated independent-control path in this unit; the production POC provider/delivery work stays incomplete and therefore production remains blocked.
