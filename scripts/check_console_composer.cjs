const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:55717/ui";
const results = [];
const posts = [];
let reply = { status: 202, body: { status: "queued", run_id: "run-test" } };
let deferReply = null;

async function launch() {
  try { return await chromium.launch({ headless: true }); }
  catch (error) {
    if (!/Executable doesn't exist|browserType\.launch/.test(error.message)) throw error;
    return chromium.launch({ headless: true, channel: "chrome" });
  }
}
async function check(name, fn) {
  try { await fn(); results.push(true); console.log(`PASS ${name}`); }
  catch (error) { results.push(false); console.log(`FAIL ${name}: ${error.message}`); }
}
async function rect(locator) {
  const box = await locator.boundingBox();
  assert.ok(box, `element is not rendered: ${await locator.evaluate(el => el.outerHTML)}`);
  return box;
}
function near(actual, expected, message, tolerance = 1) {
  assert.ok(Math.abs(actual - expected) <= tolerance,
    `${message}: expected ${expected} +/- ${tolerance}px, got ${actual}`);
}
async function ready(page) {
  await page.goto(`${baseUrl}#/composer`);
  await page.waitForFunction(() => location.hash === "#/composer" &&
    document.querySelector("#page h1")?.textContent.trim() === "Send Message");
  await page.locator("#composer-form").waitFor();
}
async function choose(page, eventType) {
  await page.locator("#c-type").selectOption(eventType);
  assert.match(await page.locator("#c-technical").textContent(), new RegExp(eventType.replace(".", "\\.")));
}
async function review(page) {
  await page.getByRole("button", { name: "Review Message" }).click();
  await page.locator("#c-review-summary:not([hidden])").waitFor();
}
async function confirm(page) {
  const before = posts.length;
  await page.getByRole("button", { name: "Confirm Send" }).click();
  await page.waitForFunction(n => window.__composerPostCount >= n, before + 1);
  await page.waitForFunction(() => !document.querySelector("#c-status")?.textContent.includes("Sending the reviewed message"));
  return posts.at(-1);
}
async function advanced(page) {
  const details = page.locator(".composer-advanced");
  if (!(await details.evaluate(el => el.open))) await details.locator("summary").click();
  return page.getByLabel("Advanced JSON payload");
}
async function validCompany(page) {
  const existing = page.locator("#c-case-list option:not([value=''])");
  if (await existing.count()) await page.locator("#c-case-list").selectOption(await existing.first().getAttribute("value"));
  else {
    await page.getByLabel("New company with ID").check();
    await page.getByLabel("New company ID").fill("composer-test-company");
  }
}

const cases = [
  ["kyb.run_requested", {
    "Company legal name": "Composer Co", Address: "1 Main St", "Registration number": "REG-1",
    Jurisdiction: "US", Website: "https://composer.example", "Contact name": "Alex Example",
    "Contact title": "Director", "Platform account ID": "acct-1",
  }, { company_legal_name: "Composer Co", address: "1 Main St", registration_number: "REG-1",
    jurisdiction: "US", website: "https://composer.example",
    contact: { name: "Alex Example", title: "Director" }, platform_account_id: "acct-1" }],
  ["email.verified", { Email: "ops@composer.example", Domain: "composer.example",
    "Verified at": "2026-09-10T12:00:00Z" }, { email: "ops@composer.example", domain: "composer.example",
    verified_at: "2026-09-10T12:00:00Z" }],
  ["org_id.submitted", { RIR: "ripe", "Org handle": "ORG-COMPOSER" },
    { rir: "ripe", org_handle: "ORG-COMPOSER" }],
  ["poc.submitted", { RIR: "apnic", "POC handle": "POC-COMPOSER", "Org handle": "ORG-COMPOSER",
    Resource: "203.0.113.0/24" }, { rir: "apnic", poc_handle: "POC-COMPOSER",
    org_handle: "ORG-COMPOSER", resource: "203.0.113.0/24" }],
  ["poc.token_verified", { "Token ID": "tok-composer", "Verified at": "2026-09-10T12:00:00Z",
    Token: "secret-token" }, { token_id: "tok-composer", verified_at: "2026-09-10T12:00:00Z", token: "secret-token" }],
  ["document.uploaded", { "Object reference": "s3://bucket/doc-1", "Document type": "certificate" },
    { object_ref: "s3://bucket/doc-1", doc_type: "certificate" }],
  ["website.review_completed", { "Task ID": "task-1", Result: "fail", "Reviewer ID": "reviewer-1",
    "Reason codes": "site_unavailable, ownership_unclear" }, { task_id: "task-1", result: "fail",
    reviewer_id: "reviewer-1", reason_codes: ["site_unavailable", "ownership_unclear"] }],
  ["reviewer.manual_approve", { "Reviewer ID": "reviewer-2", Note: "Verified offline evidence" },
    { reviewer_id: "reviewer-2", note: "Verified offline evidence" }],
  ["recalculate.requested", {}, {}],
];

(async () => {
  let browser;
  try {
    browser = await launch();
    const disappearing = await browser.newContext();
    let casesRequest = 0;
    await disappearing.route("**/ui/api/send-event", route => route.fulfill({
      status: 500, contentType: "application/json", body: JSON.stringify({ detail: "test forbids sends" }),
    }));
    await disappearing.route("**/ui/api/cases?limit=100", route => {
      casesRequest += 1;
      const company = casesRequest === 1
        ? { id: "case-beta", company_name: "Beta Networks", jurisdiction: "US", status: "registered",
          buy_status: "not_applicable", broker_status: "clear", current_evidence_score: 0,
          latest_decision: null, decision_score: null, updated_at: "2026-09-10T12:00:00Z",
          enforcement_held: false, decision_provenance: "no_decisions" }
        : { id: "case-gamma", company_name: "Gamma Networks", jurisdiction: "GB", status: "registered",
          buy_status: "not_applicable", broker_status: "clear", current_evidence_score: 0,
          latest_decision: null, decision_score: null, updated_at: "2026-09-10T12:01:00Z",
          enforcement_held: false, decision_provenance: "no_decisions" };
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ cases: [company] }) });
    });
    const disappearingPage = await disappearing.newPage(); disappearingPage.setDefaultTimeout(9000);
    await ready(disappearingPage);
    await check("a listed company that leaves the refreshed top-100 is retained without retargeting", async () => {
      await disappearingPage.locator("#c-case-list").selectOption("case-beta");
      await disappearingPage.locator('[data-r="overview"]').click();
      await disappearingPage.getByRole("heading", { name: "Overview", level: 1 }).waitFor();
      await disappearingPage.locator('[data-r="composer"]').click();
      await disappearingPage.locator("#composer-form").waitFor();
      assert.equal(await disappearingPage.getByLabel("Use an existing company ID beyond this list").isChecked(), true);
      assert.equal(await disappearingPage.getByLabel("Existing company ID beyond this list", { exact: true }).inputValue(), "case-beta");
      assert.match(await disappearingPage.locator('[data-company-control="existing-id"] .note').textContent(), /not in the current .*list|no longer in/i);
      await disappearingPage.getByRole("button", { name: "Review Message" }).click();
      assert.equal(await disappearingPage.locator("#c-review-company").textContent(), "case-beta",
        "the replacement list entry must not become the message target");
    });
    await disappearing.close();

    const context = await browser.newContext({ colorScheme: "light" });
    await context.route("**/ui/api/send-event", async route => {
      const request = route.request();
      if (request.method() !== "POST") return route.continue();
      const body = request.postDataJSON(); posts.push(body);
      for (const page of context.pages()) await page.evaluate(() => { window.__composerPostCount = (window.__composerPostCount || 0) + 1; });
      if (deferReply) await deferReply;
      if (reply.abort) return route.abort(reply.abort);
      await route.fulfill({ status: reply.status, contentType: "application/json", body: JSON.stringify(reply.body) });
    });
    const page = await context.newPage(); page.setDefaultTimeout(9000);
    await page.addInitScript(() => { window.__composerPostCount = 0; });
    await ready(page); await validCompany(page);

    await check("company choice is bounded, explicit, and does not default to creating an unknown company", async () => {
      assert.equal(await page.getByRole("group", { name: "Company" }).count(), 1);
      assert.equal(await page.getByLabel("Existing company from this list").isChecked(), true);
      assert.ok(await page.locator("#c-case-list option").count() >= 1);
      assert.match(await page.locator("#c-company-note").textContent(), /up to 100|first 100/i);
      assert.equal(await page.getByLabel("Existing company ID beyond this list", { exact: true }).count(), 1);
      assert.equal(await page.getByLabel("New company with ID", { exact: true }).count(), 1);
      assert.equal(await page.getByLabel("New company ID").inputValue(), "");
      assert.match(await page.locator("#c-status").textContent(), /no message sent/i);
    });

    await check("company draft survives navigation without entering local storage", async () => {
      await page.getByLabel("Use an existing company ID beyond this list").check();
      await page.getByLabel("Existing company ID beyond this list", { exact: true }).fill("private-company-reference");
      await page.locator('[data-r="overview"]').click();
      await page.getByRole("heading", { name: "Overview", level: 1 }).waitFor();
      await page.locator('[data-r="composer"]').click(); await page.locator("#composer-form").waitFor();
      assert.equal(await page.getByLabel("Use an existing company ID beyond this list").isChecked(), true);
      assert.equal(await page.getByLabel("Existing company ID beyond this list", { exact: true }).inputValue(), "private-company-reference");
      const stored = await page.evaluate(() => Object.keys(localStorage).map(key => `${key}:${localStorage.getItem(key)}`).join("\n"));
      assert.doesNotMatch(stored, /private-company-reference|composer/i);
      await page.getByLabel("Existing company from this list").check();
    });

    await check("all nine accepted actions have labelled guided fields and produce the intended payload", async () => {
      for (const [eventType, fields, expected] of cases) {
        await choose(page, eventType);
        for (const [label, value] of Object.entries(fields)) {
          const field = page.getByLabel(label, { exact: true });
          if (label === "RIR" || label === "Result") await field.selectOption(value);
          else await field.fill(value);
        }
        await review(page); reply = { status: 202, body: { status: "queued", run_id: `run-${posts.length}` } };
        const sent = await confirm(page);
        assert.equal(sent.event_type, eventType);
        assert.deepEqual(sent.payload, expected);
        assert.ok(sent.case_id);
        assert.match(sent.idempotency_key, /^[a-z0-9-]+$/i);
      }
    });

    await check("sensitive guided actions point to the selected company context", async () => {
      await choose(page, "website.review_completed");
      const selected = await page.locator("#c-case-list").inputValue();
      const link = page.locator("#c-warn a");
      assert.equal(await link.getAttribute("href"), `#/case/${encodeURIComponent(selected)}`);
      assert.match(await page.locator("#c-warn").textContent(), /sensitive action/i);
      await choose(page, "reviewer.manual_approve");
      assert.match(await page.locator("#c-warn").textContent(), /bypasses all decision rules/i);
    });

    await check("required guided fields, ISO timestamps, invalid JSON, and non-object roots block review", async () => {
      await choose(page, "email.verified");
      await page.getByLabel("Email", { exact: true }).fill("");
      await page.getByRole("button", { name: "Review Message" }).click();
      assert.match(await page.locator("#c-email-error").textContent(), /required/i);
      await page.getByLabel("Email", { exact: true }).fill("ops@example.test");
      await page.getByLabel("Verified at", { exact: true }).fill("not-a-time");
      await page.getByRole("button", { name: "Review Message" }).click();
      assert.match(await page.locator("#c-verified-at-error").textContent(), /ISO/i);
      const advancedInput = await advanced(page);
      await advancedInput.fill("{");
      await page.getByLabel("Email", { exact: true }).fill("still-editable@example.test");
      assert.equal(await advancedInput.inputValue(), "{", "guided edits must not erase invalid advanced text");
      await page.getByRole("button", { name: "Review Message" }).click();
      assert.match(await page.locator("#c-json-error").textContent(), /valid JSON/i);
      assert.equal(await advancedInput.inputValue(), "{");
      await advancedInput.fill("[]");
      await page.getByRole("button", { name: "Review Message" }).click();
      assert.match(await page.locator("#c-json-error").textContent(), /object/i);
    });

    await check("advanced extras survive guided round-trip including a nested contact extra", async () => {
      await choose(page, "kyb.run_requested");
      const advancedInput = await advanced(page);
      await advancedInput.fill(JSON.stringify({ company_legal_name: "Extra Co", top_extra: "keep",
        contact: { name: "Old", title: "Lead", channel: "signal" } }, null, 2));
      await advancedInput.blur();
      assert.equal(await page.getByLabel("Contact name", { exact: true }).inputValue(), "Old");
      await page.getByLabel("Contact name", { exact: true }).fill("New");
      const payload = JSON.parse(await advancedInput.inputValue());
      assert.equal(payload.top_extra, "keep"); assert.equal(payload.contact.channel, "signal");
      assert.equal(payload.contact.name, "New");
    });

    await check("event drafts survive type switches and reset is the only example overwrite", async () => {
      await page.getByLabel("Company legal name", { exact: true }).fill("Edited draft");
      await choose(page, "org_id.submitted");
      await page.getByLabel("Org handle", { exact: true }).fill("ORG-EDITED");
      await choose(page, "kyb.run_requested");
      assert.equal(await page.getByLabel("Company legal name", { exact: true }).inputValue(), "Edited draft");
      await page.getByRole("button", { name: "Reset example" }).click();
      assert.notEqual(await page.getByLabel("Company legal name", { exact: true }).inputValue(), "Edited draft");
      await choose(page, "email.verified"); await page.getByRole("button", { name: "Reset example" }).click();
      const fresh = Date.parse(await page.getByLabel("Verified at", { exact: true }).inputValue());
      assert.ok(Math.abs(Date.now() - fresh) < 60_000, "example timestamp must be generated when reset, not fixed in the past");
      await choose(page, "kyb.run_requested");
    });

    await check("automatic refresh preserves the mounted composer, draft, and focus", async () => {
      const name = page.getByLabel("Company legal name", { exact: true });
      await name.fill("Focus survives"); await page.getByLabel("Refresh every 5 seconds").check();
      await name.focus();
      await name.evaluate(el => { window.__composerField = el; window.__composerForm = document.querySelector("#composer-form"); });
      await page.waitForTimeout(5250);
      assert.equal(await name.inputValue(), "Focus survives");
      assert.equal(await name.evaluate(el => document.activeElement === el && window.__composerField === el), true);
      assert.equal(await page.evaluate(() => window.__composerForm === document.querySelector("#composer-form")), true);
      await page.getByLabel("Refresh every 5 seconds").uncheck();
    });

    await check("review is immutable and editing invalidates it", async () => {
      await page.getByLabel("Company legal name", { exact: true }).fill("Snapshot Co"); await review(page);
      assert.match(await page.locator("#c-review-summary").textContent(), /Snapshot Co/);
      const advancedInput = await advanced(page);
      await advancedInput.evaluate(el => { el.value = '{"company_legal_name":"Tampered"}'; });
      const sent = await confirm(page);
      assert.equal(sent.payload.company_legal_name, "Snapshot Co");
      await page.getByLabel("Company legal name", { exact: true }).fill("Edited after review");
      assert.equal(await page.locator("#c-review-summary").isHidden(), true);
      assert.equal(await page.getByRole("button", { name: "Review Message" }).count(), 1);
    });

    await check("double click submits once and successful confirm cannot repeat", async () => {
      await review(page); const before = posts.length;
      await page.getByRole("button", { name: "Confirm Send" }).dblclick();
      await page.waitForFunction(n => window.__composerPostCount >= n, before + 1);
      await page.waitForTimeout(100);
      assert.equal(posts.length, before + 1);
      assert.equal(await page.locator("#c-send").isDisabled(), true);
    });

    await check("ambiguous 503 retains semantic identity and blocks blind resubmission", async () => {
      await choose(page, "kyb.run_requested");
      const advancedInput = await advanced(page);
      await advancedInput.fill('{"company_legal_name":"Retry Co","contact":{"name":"A","title":"B"}}'); await advancedInput.blur();
      await review(page); reply = { status: 503, body: { detail: "temporary failure" } }; await confirm(page);
      const first = posts.at(-1).idempotency_key, sentCount = posts.length;
      assert.match(await page.locator("#c-status").textContent(), /outcome unknown/i);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /message not sent|retry/i);
      assert.equal(await page.locator("#c-status a").getAttribute("href"),
        `#/case/${encodeURIComponent(posts.at(-1).case_id)}`);
      assert.equal(await page.getByRole("button", { name: /confirmation blocked/i }).isDisabled(), true);
      await advancedInput.fill('{"contact":{"title":"B","name":"A"},"company_legal_name":"Retry Co"}'); await advancedInput.blur();
      await review(page);
      assert.equal(await page.locator("#c-review-key").textContent(), first);
      assert.equal(await page.getByRole("button", { name: /confirmation blocked/i }).isDisabled(), true);
      assert.equal(posts.length, sentCount);
      await page.locator('[data-r="overview"]').click(); await page.getByRole("heading", { name: "Overview", level: 1 }).waitFor();
      await page.locator('[data-r="composer"]').click(); await page.locator("#composer-form").waitFor();
      assert.equal(await page.getByRole("button", { name: /confirmation blocked/i }).isDisabled(), true);
      await page.getByLabel("Company legal name", { exact: true }).fill("Changed Co"); await review(page);
      assert.notEqual(await page.locator("#c-review-key").textContent(), first);
      assert.equal(await page.getByRole("button", { name: "Confirm Send" }).isEnabled(), true);
    });

    await check("202 and actual 200 stored response shapes use conservative acceptance copy", async () => {
      reply = { status: 202, body: { status: "queued", run_id: "run-new", jobs_queued: 99 } }; await confirm(page);
      assert.match(await page.locator("#c-status").textContent(), /accepted.*queued|queued.*processing/i);
      assert.match(await page.locator("#c-status").textContent(), /run-new/);
      assert.match(await page.locator("#c-status").textContent(), /does not mean verification is complete/i);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /jobs started|jobs_queued/i);
      await choose(page, "recalculate.requested"); await review(page);
      reply = { status: 200, body: { status: "queued", run_id: "run-stored", jobs_queued: 99 } };
      await confirm(page);
      assert.match(await page.locator("#c-status").textContent(), /stored.*queued|queued.*stored/i);
      assert.match(await page.locator("#c-status").textContent(), /run-stored/);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /status.*replayed/i);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /99 jobs|jobs started/i);
      assert.equal(await page.locator("#c-raw").evaluate(el => el.open), false);
    });

    await check("actual manual 200 shape reports recording without inventing replay status", async () => {
      await choose(page, "reviewer.manual_approve");
      await page.getByLabel("Reviewer ID", { exact: true }).fill("reviewer-safety"); await review(page);
      reply = { status: 200, body: { case_id: "manual-case", case_status: "approved_manual",
        buy_status: "buy_enabled", recorded: true } };
      await confirm(page);
      assert.match(await page.locator("#c-status").textContent(), /manual action recorded/i);
      assert.match(await page.locator("#c-status").textContent(), /does not establish|whether this call/i);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /replay response/i);
    });

    await check("403 and 422 are explicit editable refusals without a retry invitation", async () => {
      await choose(page, "website.review_completed"); await review(page);
      reply = { status: 403, body: { detail: "composer cannot submit reviewer events in production" } }; await confirm(page);
      assert.match(await page.locator("#c-status").textContent(), /attempt rejected.*403/i);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /outcome unknown|retry send/i);
      assert.equal(await page.locator("#c-send").isDisabled(), true);
      await page.getByLabel("Task ID", { exact: true }).fill("task-corrected"); await review(page);
      reply = { status: 422, body: { detail: "payload refused" } }; await confirm(page);
      assert.match(await page.locator("#c-status").textContent(), /attempt rejected.*422/i);
      assert.match(await page.locator("#c-status").textContent(), /payload refused/);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /retry send/i);
      assert.equal(await page.locator("#c-send").isDisabled(), true);
    });

    await check("409 identifies the existing event and blocks key evasion", async () => {
      await choose(page, "recalculate.requested"); await review(page);
      const conflictKey = await page.locator("#c-review-key").textContent();
      reply = { status: 409, body: { error: "idempotency key reuse with different payload", event_id: "evt-existing" } };
      const before = posts.length; await confirm(page);
      assert.match(await page.locator("#c-status").textContent(), /attempt rejected.*409/i);
      assert.match(await page.locator("#c-status").textContent(), /evt-existing/);
      assert.match(await page.locator("#c-status").textContent(), /verify/i);
      assert.equal(await page.getByRole("button", { name: /confirmation blocked/i }).isDisabled(), true);
      assert.equal(await page.locator("#c-review-key").textContent(), conflictKey);
      assert.equal(posts.length, before + 1);
    });

    await check("network abort after capture is unknown and never invites blind retry", async () => {
      await choose(page, "org_id.submitted"); await page.getByLabel("Org handle", { exact: true }).fill("ORG-NETWORK-ABORT"); await review(page);
      reply = { abort: "connectionreset" }; const before = posts.length; await confirm(page);
      assert.equal(posts.length, before + 1);
      assert.match(await page.locator("#c-status").textContent(), /outcome unknown/i);
      assert.doesNotMatch(await page.locator("#c-status").textContent(), /message not sent|retry send/i);
      assert.match(await page.locator("#c-status").textContent(), /verify/i);
      assert.equal(await page.getByRole("button", { name: /confirmation blocked/i }).isDisabled(), true);
    });

    await check("raw refusal response remains collapsed", async () => {
      await choose(page, "document.uploaded"); await review(page);
      reply = { status: 422, body: { detail: "payload refused" } }; await confirm(page);
      assert.equal(await page.locator("#c-raw").evaluate(el => el.open), false);
    });

    await check("pending send survives navigation and cannot be duplicated", async () => {
      await choose(page, "poc.submitted"); await page.getByLabel("POC handle", { exact: true }).fill("POC-LATE"); await review(page);
      let release; deferReply = new Promise(resolve => { release = resolve; }); reply = { status: 202, body: { status: "queued", run_id: "run-late" } };
      const before = posts.length; await page.getByRole("button", { name: "Confirm Send" }).click();
      await page.waitForFunction(n => window.__composerPostCount >= n, before + 1);
      assert.equal(await page.locator("#composer-form :is(input,select,textarea,button):not([disabled])").count(), 0);
      await page.locator('[data-r="overview"]').click(); await page.getByRole("heading", { name: "Overview", level: 1 }).waitFor();
      await page.locator('[data-r="composer"]').click(); await page.locator("#composer-form").waitFor();
      assert.match(await page.locator("#c-status").textContent(), /sending/i);
      assert.equal(await page.locator("#c-send").isDisabled(), true);
      release(); deferReply = null;
      await page.waitForFunction(() => document.querySelector("#c-status")?.textContent.includes("run-late"));
      assert.equal(posts.length, before + 1);
    });

    await check("demo fixture is three ordered safety-held steps without an approval guarantee", async () => {
      const steps = page.locator(".demo-step"); assert.equal(await steps.count(), 3);
      const text = await steps.allTextContents();
      assert.match(text[0], /request.*review|kyb\.run_requested/i);
      assert.match(text[1], /verify.*email|email\.verified/i);
      assert.match(text[2], /submit.*org|org_id\.submitted/i);
      assert.match(await page.locator("#c-demo").textContent(), /fixture|demo/i);
      assert.match(await page.locator("#c-demo").textContent(), /hold|does not guarantee approval/i);
    });

    await choose(page, "kyb.run_requested");
    for (const theme of ["light", "dark"]) for (const width of [390, 768, 1147, 1440]) {
      await page.setViewportSize({ width, height: 1137 });
      await page.emulateMedia({ colorScheme: theme });
      await page.evaluate(() => KYCTheme.current = "system");
      await check(`${width}px ${theme} composer fits and keeps labelled controls`, async () => {
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        const select = page.locator("#c-type"), wrap = select.locator(".."), arrow = wrap.locator(":scope > svg");
        assert.equal(await select.evaluate(el => el.parentElement.classList.contains("select-wrap")), true);
        assert.equal(await arrow.count(), 1);
        const sb = await rect(select), wb = await rect(wrap), ab = await rect(arrow);
        near(sb.x, wb.x, "composer select and wrapper left edge");
        near(sb.width, wb.width, "composer select fills wrapper width");
        near(sb.x + sb.width - (ab.x + ab.width), 12, "composer select arrow right inset");
        await select.selectOption("kyb.run_requested");
        assert.equal(await select.inputValue(), "kyb.run_requested");
        assert.equal(await page.locator("#composer-form label").count() >= 4, true);
        assert.equal(await page.locator(".split[data-composer] > .stack > .card").count(), 2,
          "both composer support panels remain available");
        if (width === 768 || width === 1147) {
          const primary = await rect(page.locator(".split[data-composer] > .card").first());
          assert.ok(primary.width >= 600, `composer primary card is only ${primary.width}px wide`);
          const optionFit = await select.evaluate(el => {
            const style = getComputedStyle(el), canvas = document.createElement("canvas");
            const context = canvas.getContext("2d"); context.font = style.font;
            const text = el.selectedOptions[0].textContent.trim();
            return { text, textWidth: context.measureText(text).width,
              contentWidth: el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) };
          });
          assert.ok(optionFit.contentWidth >= optionFit.textWidth,
            `${optionFit.text} needs ${optionFit.textWidth}px but has ${optionFit.contentWidth}px`);
          const labels = await page.locator(".composer-fields .flab").evaluateAll(nodes => nodes.map(node => {
            const style = getComputedStyle(node); return { text: node.textContent.trim(),
              height: node.getBoundingClientRect().height, lineHeight: parseFloat(style.lineHeight) };
          }));
          for (const label of labels) assert.ok(label.height <= label.lineHeight + 1,
            `${label.text} wrapped to ${label.height}px at ${width}px`);
        }
      });
    }
  } finally {
    if (browser) await browser.close();
    const passed = results.filter(Boolean).length;
    console.log(`RESULT ${passed}/${results.length} checks passed`);
    if (passed !== results.length) process.exitCode = 1;
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
