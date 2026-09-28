const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:64346/ui";

const preview = JSON.parse(fs.readFileSync("scripts/preview_data.json", "utf8"));
const caseId = Object.keys(preview.full).find(id => !preview.full[id].case.submitted_json?.org_id?.org_handle);
const fullFixture = () => structuredClone(preview.full[caseId]);

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const fixture = fullFixture();
    const page = await browser.newPage();
    let full = structuredClone(fixture);
    let releaseRefresh;
    let holdRefresh = false;
    let failNextFull = false;
    let releaseNavigation;
    let holdNavigation = false;
    let releaseIntegrations;
    let holdIntegrations = false;
    let failIntegrations = false;
    let fullRequests = 0;
    await page.route(`**/ui/api/cases/${caseId}/full`, async route => {
      fullRequests += 1;
      if (holdNavigation) await new Promise(resolve => { releaseNavigation = resolve; });
      if (holdRefresh) await new Promise(resolve => { releaseRefresh = resolve; });
      if (failNextFull) {
        failNextFull = false;
        await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"fixture failure"}' });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(full) });
    });
    await page.route("**/ui/api/integrations", async route => {
      if (failIntegrations) {
        failIntegrations = false;
        await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"fixture failure"}' });
        return;
      }
      if (holdIntegrations) await new Promise(resolve => { releaseIntegrations = resolve; });
      await route.continue();
    });

    await page.goto(`${baseUrl}#/case/${caseId}`);
    await page.locator("#org-ask-note").fill("Keep this note");
    await page.locator("h2").first().click();
    const beforeSkip = fullRequests;
    await page.locator("#autorefresh").check();
    await page.waitForTimeout(5100);
    assert.equal(fullRequests, beforeSkip, "a blurred dirty draft must prevent automatic refresh");
    assert.equal(await page.locator("#org-ask-note").inputValue(), "Keep this note");
    await page.locator("#autorefresh").uncheck();

    await page.reload();
    await page.locator("#org-ask-note").waitFor();
    holdRefresh = true;
    releaseRefresh = null;
    await page.locator("#autorefresh").check();
    for (let i = 0; i < 60 && !releaseRefresh; i += 1) await page.waitForTimeout(100);
    assert.ok(releaseRefresh, "automatic refresh must reach the controlled response barrier");
    await page.locator("#org-ask-note").fill("Keep this newer note");
    await page.locator("#org-ask-note").evaluate(el => { el.dataset.identityWitness = "same-node"; });
    releaseRefresh();
    await page.waitForTimeout(100);
    assert.equal(await page.locator("#org-ask-note").inputValue(), "Keep this newer note",
      "a draft typed after refresh starts must survive its response");
    assert.equal(await page.locator("#org-ask-note").getAttribute("data-identity-witness"), "same-node");
    assert.equal(await page.locator("#org-ask-note").evaluate(el => document.activeElement === el), true);
    await page.locator("#autorefresh").uncheck();
    holdRefresh = false;

    await page.reload();
    await page.locator("#org-ask-note").waitFor();
    holdRefresh = true;
    releaseRefresh = null;
    failNextFull = true;
    await page.locator("#autorefresh").check();
    for (let i = 0; i < 60 && !releaseRefresh; i += 1) await page.waitForTimeout(100);
    await page.locator("#org-ask-note").fill("Keep this through a failed refresh");
    await page.locator("#org-ask-note").evaluate(el => { el.dataset.identityWitness = "failed-get"; });
    releaseRefresh();
    await page.waitForTimeout(100);
    assert.equal(await page.locator("#org-ask-note").inputValue(), "Keep this through a failed refresh");
    assert.equal(await page.locator("#org-ask-note").getAttribute("data-identity-witness"), "failed-get");
    assert.equal(await page.locator("#org-ask-note").evaluate(el => document.activeElement === el), true);
    await page.locator("#autorefresh").uncheck();
    holdRefresh = false;

    full = structuredClone(fixture);
    full.case.status = "approved_manual";
    full.pointer_decision.manual = false;
    full.latest_manual_decision = {
      id: "manual-authority", manual: true, reviewer_id: "authoritative-reviewer",
      decided_at: "2026-09-21T12:00:00Z",
    };
    full.manual_decision_provenance = "latest_manual_row";
    full.decisions = [{
      id: "display-only", manual: true, reviewer_id: "wrong-display-reviewer",
      decided_at: "2026-09-21T13:00:00Z", decision: "approve", score: 100,
      buy_enablement: "enabled", published_at: null,
    }];
    await page.reload();
    await page.locator(".approved-by").waitFor();
    assert.match(await page.locator(".approved-by").innerText(), /authoritative-reviewer/);
    assert.doesNotMatch(await page.locator(".approved-by").innerText(), /wrong-display-reviewer/);

    full.pointer_decision.published_at = null;
    await page.reload();
    assert.equal(await page.locator("[data-decision-delivery]").innerText(), "Delivery not recorded");
    assert.match(await page.getByRole("heading", { level: 2 }).allTextContents().then(xs => xs.join("\n")),
      /Recorded Decision/);

    full.enforcement_hold = { computed_decision: "approve" };
    full.adapter_results = [];
    await page.reload();
    assert.doesNotMatch(await page.locator("#page").innerText(), /platform received On Hold/);
    assert.match(await page.locator("#page").innerText(), /recorded decision is On Hold/);
    assert.match(await page.locator("#page").innerText(), /No external sources were queried in this round/);
    assert.match(await page.locator("#page").innerText(), /No verification codes created/);

    let askRequests = 0;
    let releaseAsk;
    let askStatus = 500;
    await page.route("**/ui/api/cases/*/information-request", async route => {
      askRequests += 1;
      await new Promise(resolve => { releaseAsk = resolve; });
      await route.fulfill({ status: askStatus, contentType: "application/json",
        body: askStatus < 300 ? "{}" : '{"detail":"fixture failure"}' });
    });
    await page.locator("#reviewer").fill("reviewer-one");
    await page.locator("#org-ask-note").fill("Retry this request");
    await page.locator("#org-ask-form").evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
    for (let i = 0; i < 30 && !releaseAsk; i += 1) await page.waitForTimeout(50);
    assert.equal(askRequests, 1, "an in-flight information request must not submit twice");
    assert.equal(await page.locator("#org-ask-form button").first().isDisabled(), true);
    releaseAsk();
    await page.waitForTimeout(100);
    assert.equal(await page.locator("#org-ask-form button").first().isEnabled(), true,
      "a failed information request must be retryable");
    assert.equal(await page.locator("#org-ask-note").inputValue(), "Retry this request");
    askStatus = 202;
    releaseAsk = null;
    await page.locator("#org-ask-form").evaluate(form => form.requestSubmit());
    for (let i = 0; i < 30 && !releaseAsk; i += 1) await page.waitForTimeout(50);
    assert.equal(askRequests, 2, "retry must issue one new request");
    releaseAsk();
    await page.waitForTimeout(800);

    let recordRequests = 0;
    let releaseRecord;
    await page.route("**/ui/api/send-event", async route => {
      recordRequests += 1;
      await new Promise(resolve => { releaseRecord = resolve; });
      await route.fulfill({ status: 202, contentType: "application/json", body: "{}" });
    });
    await page.locator("#org-record-toggle").click();
    await page.locator("#org-handle").fill("ORG-REVIEW-1");
    await page.locator("#org-record-form").evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
    for (let i = 0; i < 30 && !releaseRecord; i += 1) await page.waitForTimeout(50);
    assert.equal(recordRequests, 1, "an in-flight Org ID record must not submit twice");
    releaseRecord();
    await page.waitForTimeout(800);

    await page.locator("#org-ask-note").fill("navigation must still leave this page");
    failIntegrations = true;
    await page.evaluate(() => { location.hash = "#/integrations"; });
    await page.getByText("This page did not load").waitFor();
    assert.equal(await page.getByText("Jane Doe · Acme Networks Ltd", { exact: true }).count(), 0,
      "a dirty prior page must not suppress a navigation error state");

    await page.evaluate(() => { location.hash = "#/cases"; });
    await page.getByRole("heading", { level: 1, name: "Cases" }).waitFor();
    holdNavigation = true;
    await page.evaluate(id => { location.hash = `#/case/${id}`; }, caseId);
    for (let i = 0; i < 30 && !releaseNavigation; i += 1) await page.waitForTimeout(50);
    holdIntegrations = true;
    await page.evaluate(() => { location.hash = "#/integrations"; });
    for (let i = 0; i < 30 && !releaseIntegrations; i += 1) await page.waitForTimeout(50);
    releaseNavigation();
    await page.waitForTimeout(50);
    releaseIntegrations();
    const integrationsTitle = page.getByRole("heading", { level: 1, name: "Data Sources" });
    await integrationsTitle.waitFor();
    assert.equal(await integrationsTitle.evaluate(el => document.activeElement === el), true,
      "a stale route must not consume the newer navigation's focus handoff");

    console.log("PASS console T23 behavioural regressions");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
