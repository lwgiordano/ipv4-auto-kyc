# Adapters & Evidence

Normative JSON: `machine_readable/adapter_catalog.json`.

Every adapter implements one contract:

```
run(case_snapshot, event) → {
  adapter_id, case_id, event_id,
  raw_ref,          // object-storage pointer to the untouched upstream response
  normalized,       // adapter-specific normalized fields
  status,           // ok | upstream_error | not_applicable
  fetched_at
}
```

Validation is a separate pure layer: `validate(normalized, submitted_data) → pass | fail | needs_review + reason_codes`. Adapters fetch; validators judge; the check store records. Keep these three stages separate — it is what makes the audit trail defensible.

## The eight adapters

### 1. Platform email status — `email_verification` (approval-grade, automated)
- Source: the platform's own email verification events (delivered in the event payload or fetched from a platform API).
- Produces: `verified_email` (+10) — any inbox verified; `verified_company_email` (+25) — verified inbox on a plausible business domain.
- Pass rule (+25): inbox verified AND domain is not a free/disposable provider AND domain plausibly belongs to the submitted company (exact domain match to submitted website/company domain).

### 2. Floqer — `floqer_company_enrichment` (discovery-only, automated)
- Provides: company domain, website, LinkedIn URL, aliases, registry candidates, broker context.
- **Never awards points by itself.** Output seeds: registry candidate lookups, the LinkedIn check, and website review context.
- LinkedIn check (+20) pass rule: person name AND current company AND title AND company domain all deterministically match between Floqer's LinkedIn data and the platform submission.

### 3. Registries — `companies_house`, `gleif`, state/country registries (approval-grade, automated)
- Provides: legal name, company number, status, registered address (Companies House); LEI, legal name, address, entity status (GLEIF).
- Pass rule (+25 legal proof): company is active/current AND name + address + number match the submission exactly (normalized for case/punctuation, not fuzzy).

### 4. RIR ORG-ID — `arin_rdap`, `ripe`, `apnic_rdap`, `lacnic_rdap`, `afrinic_rdap` (approval-grade, automated)
- Provides: org/entity handle, name, address, associated POCs, ASN/IP resources.
- Pass rule (+25 control proof): exact handle exists AND RIR org name matches company AND RIR address materially matches submission AND no conflict with broker or previously rejected entity records.
- Route to `needs_review` (do not award) on: parent/subsidiary ambiguity, stale/missing address, handle belonging to a related-but-different entity, resources actually held by an ISP/technical provider, or handle found only via broad name search.

### 5. RIR POC — `rir_poc` (approval-grade, automated + user token step)
- Flow: confirm the POC handle is associated with the submitted ORG-ID/resource → extract the **RIR-listed** email → send a verification token **to that email only** → user confirms.
- Pass rule (+25 control proof): association confirmed AND token verified from the RIR-listed address.
- If the RIR hides the POC email: no points; create a manual review task for alternate proof.

### 6. Website — `website_manual_review` (**manual**, supporting)
- **There is no automated website adapter.** The tool creates a review-queue task containing the submitted/discovered domain and any Floqer context.
- A human reviews (site resolves, HTTPS, real company content that ties to the applicant, not parked, no obvious fraud) and marks pass/fail with reason codes.
- Pass awards +10 via a normal check record whose `source` is the reviewer identity.

### 7. Document OCR — `document_ocr` (legal proof, automated)
- Trigger: `document.uploaded` platform event; file fetched from object storage.
- OCR + field extraction → compare name, address, registration number, jurisdiction to the submission and to registry evidence.
- Pass rule (+25 legal proof): extracted fields match AND no conflict with an official registry result.

### 8. Broker / risk — `broker_policy` (policy gate, automated, local)
- Runs **first** on every full run. Exact-identifier match against the broker list (`machine_readable/broker_policy.json`): legal name, aliases, domains, email domains, RIR ORG-IDs, POC handles, ASNs.
- Blocked match → short-circuit: decision `reject`, skip remaining adapters (spend nothing further).
- Allowed match → tag and continue. No match → clear.

## Adapter execution order (full KYB run)

1. `broker_policy` (short-circuit gate)
2. `email_verification` (cheap, local data)
3. Free public adapters: registries
4. `floqer_company_enrichment` → validate its outputs → seed LinkedIn check + registry candidates
5. `arin_rdap`/`ripe`/… if an ORG-ID is on file
6. `rir_poc` if a POC handle is on file
7. `document_ocr` if a document is on file
8. `website_manual_review` task creation (human completes asynchronously)

Single-evidence events run only the affected adapter plus rescore (see `machine_readable/platform_events.json`).

## Operational requirements (all adapters)

- Timeouts, bounded retries with jitter, and per-source rate limiting.
- Raw upstream responses stored to object storage before normalization (audit).
- Upstream failure ≠ check failure: an `upstream_error` leaves the prior check live and flags the run as partial; it never creates a failing check.
- Every RIR/registry call must be attributable in logs: case, event, URL (sans secrets), latency, outcome.
