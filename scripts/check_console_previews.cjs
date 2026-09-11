const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:55717/ui";
const results = [];
const previewKeys = {
  points: "kyc-preview-v1:points",
  salesforce: "kyc-preview-v1:salesforce",
  brokers: "kyc-preview-v1:brokers",
};
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }

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

async function ready(page, hash, heading, selector) {
  await page.goto(`${baseUrl}${hash}`);
  await page.waitForFunction(({ hash, heading }) => location.hash === hash &&
    document.querySelector("#page h1")?.textContent.trim() === heading, { hash, heading });
  await page.locator(selector).first().waitFor();
}

async function clearPreviews(page) {
  await page.evaluate(keys => Object.values(keys).forEach(key => localStorage.removeItem(key)), previewKeys);
}

(async () => {
  let browser;
  try {
    browser = await launch();
    const context = await browser.newContext({ colorScheme: "light" });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    await page.route("**/ui/api/overview", async route => {
      const response = await route.fetch(); const body = await response.json();
      body.metrics.decisions_by_type = { approve: 1, manual_review_insufficient: 11 };
      await route.fulfill({ response, json: body });
    });
    const writes = [];
    page.on("request", request => {
      if (request.url().includes("/ui/api/") && request.method() !== "GET") {
        writes.push({ method: request.method(), url: request.url() });
      }
    });

    await ready(page, "#/policy", "Decision Rules", "#rules-hash");
    await clearPreviews(page);
    await page.reload();
    await page.getByRole("button", { name: "Edit Points" }).waitFor();

    await check("points reject invalid values and keep live gates and threshold read-only", async () => {
      assert.match(await page.locator("#points-bound").textContent(), /whole numbers from 0 to 1000/i);
      assert.equal(await page.locator("#points-editor input").count(), 0);
      await page.getByRole("button", { name: "Edit Points" }).click();
      const first = page.locator("#points-editor input[data-check]").first();
      for (const invalid of ["", "2.5", "-1", "1001", "1e309"]) {
        await first.fill(invalid);
        await page.getByRole("button", { name: "Save Preview" }).first().click();
        await page.locator("#points-status").getByText(/whole number/i).waitFor();
      }
      assert.equal(await page.locator("#points-editor input").count(), 8);
      assert.equal(await page.getByText(/threshold 100 points/i).count() > 0, true);
      assert.equal(await page.locator("[data-preview-gate]").count(), 0);
    });

    await check("points save, reload, compare with live, and reset without API writes", async () => {
      const first = page.locator("#points-editor input[data-check]").first();
      await first.fill("31");
      await page.getByRole("button", { name: "Save Preview" }).first().click();
      await page.locator("#points-status").getByText(/saved in this browser/i).waitFor();
      const stored = JSON.parse(await page.evaluate(key => localStorage.getItem(key), previewKeys.points));
      assert.deepEqual(Object.keys(stored).sort(), ["base", "value", "version"]);
      assert.equal(stored.version, 1);
      assert.equal(stored.value.verified_company_email, 31);
      await page.reload();
      await page.getByRole("button", { name: "Edit Points" }).waitFor();
      assert.match(await page.locator("#points-status").textContent(), /saved preview loaded/i);
      assert.match(await page.locator('[data-check-row="verified_company_email"]').textContent(), /Live\s*\+25/i);
      const live = await page.evaluate(() => fetch("/ui/api/policy").then(r => r.json()));
      assert.equal(live.rubric[0].points, 25);
      await page.getByRole("button", { name: "Reset to Live" }).first().click();
      assert.match(await page.locator("#points-status").textContent(), /confirm reset/i);
      await page.getByRole("button", { name: "Confirm Reset" }).first().click();
      assert.equal(await page.evaluate(key => localStorage.getItem(key), previewKeys.points), null);
      assert.match(await page.locator('[data-check-row="verified_company_email"]').textContent(), /\+25/);
    });

    await check("malformed and stale point drafts are refused with a reset path", async () => {
      await page.evaluate(key => localStorage.setItem(key, "{bad"), previewKeys.points);
      await page.reload();
      await page.getByRole("button", { name: "Edit Points" }).waitFor();
      assert.match(await page.locator("#points-status").textContent(), /cannot be used/i);
      assert.equal(await page.getByRole("button", { name: "Reset to Live" }).first().isVisible(), true);
      await page.evaluate(key => localStorage.setItem(key,
        JSON.stringify({ version: 1, base: "old-rules", value: {} })), previewKeys.points);
      await page.reload();
      await page.getByRole("button", { name: "Edit Points" }).waitFor();
      assert.match(await page.locator("#points-status").textContent(), /live rules changed/i);
      await clearPreviews(page);
    });

    await ready(page, "#/fieldmap", "Salesforce Fields", "#fmcase");
    await check("mapping editor rejects duplicate destinations and preserves original source values", async () => {
      const beforeSource = await page.locator("#mapping-editor tbody tr").first().locator(".src").textContent();
      const beforeValue = await page.locator("#mapping-editor tbody tr").first().locator("[data-map-value]").textContent();
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      const inputs = page.locator("#mapping-editor input[data-source-field]");
      const duplicate = await inputs.nth(1).inputValue();
      await inputs.first().fill(duplicate);
      await page.getByRole("button", { name: "Save Preview" }).click();
      assert.match(await page.locator("#mapping-status").textContent(), /unique/i);
      await inputs.first().fill("Preview_Status__c");
      await page.getByRole("button", { name: "Save Preview" }).click();
      await page.locator("#mapping-status").getByText(/saved in this browser/i).waitFor();
      await page.reload();
      await page.getByRole("button", { name: "Edit Mappings" }).waitFor();
      const firstRow = page.locator("#mapping-editor tbody tr").first();
      assert.match(await firstRow.textContent(), /Preview_Status__c/);
      assert.match(await firstRow.textContent(), /Original.*KYC_Status__c/i);
      assert.equal(await firstRow.locator(".src").textContent(), beforeSource);
      assert.equal(await firstRow.locator("[data-map-value]").textContent(), beforeValue);
      const live = await page.evaluate(() => fetch("/ui/api/policy").then(r => r.json()));
      assert.equal(Object.hasOwn(live.field_sources, "KYC_Status__c"), true);
    });

    await check("mapping names enforce the Salesforce identifier contract", async () => {
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      const first = page.locator("#mapping-editor input[data-source-field]").first();
      for (const bad of ["", "1Wrong", "bad-name", "x".repeat(81)]) {
        await first.fill(bad);
        await page.getByRole("button", { name: "Save Preview" }).click();
        assert.match(await page.locator("#mapping-status").textContent(), /1–80 ASCII|start with a letter/i);
      }
      await page.getByRole("button", { name: "Reset to Live" }).click();
      await page.getByRole("button", { name: "Confirm Reset" }).click();
    });

    await check("company selection preserves unsaved mapping edits and edit mode", async () => {
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      const first = page.locator("#mapping-editor input[data-source-field]").first();
      await first.fill("Unsaved_Status__c");
      const source = await page.locator("#mapping-editor tbody tr").first().locator(".src").textContent();
      const choices = page.locator("#fmcase option:not([value=''])");
      assert.ok(await choices.count() > 1);
      const next = await choices.nth(1).getAttribute("value");
      const valuesLoaded = page.waitForResponse(response => response.url().includes(`/ui/api/cases/${next}/full`));
      await page.locator("#fmcase").selectOption(next);
      await valuesLoaded;
      await page.waitForFunction(() => document.querySelector("#mapping-editor input[data-source-field]")?.value === "Unsaved_Status__c");
      const refreshed = page.locator("#mapping-editor input[data-source-field]").first();
      await refreshed.waitFor();
      assert.equal(await refreshed.inputValue(), "Unsaved_Status__c");
      assert.equal(await page.locator("#mapping-editor tbody tr").first().locator(".src").textContent(), source);
      assert.match(await page.locator("#mapping-status").textContent(), /unsaved/i);
    });

    await check("mapping company fetch changes only value cells while edits continue", async () => {
      await ready(page, "#/fieldmap", "Salesforce Fields", "#mapping-editor");
      await page.reload(); await page.getByRole("button", { name: "Edit Mappings" }).waitFor();
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      const input = page.locator("#mapping-editor input[data-source-field]").first();
      await input.fill("Before_Fetch__c");
      await input.evaluate(el => { window.__mappingInputDuringFetch = el; });
      const choices = page.locator("#fmcase option:not([value=''])");
      const current = await page.locator("#fmcase").inputValue();
      const target = await choices.evaluateAll((options, current) => options.find(option => option.value !== current)?.value, current);
      const seen = deferred(), release = deferred();
      const pattern = `**/ui/api/cases/${target}/full`;
      await page.route(pattern, async route => { seen.resolve(); await release.promise; await route.continue(); });
      await page.locator("#fmcase").selectOption(target);
      await seen.promise;
      assert.equal(await page.locator("[data-map-value]").first().getByText(/loading selected company/i).count(), 1);
      await input.fill("During_Fetch__c");
      await page.getByRole("button", { name: "Save Preview" }).click();
      assert.match(await page.locator("[data-map-value]").first().textContent(), /loading selected company/i);
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      assert.match(await page.locator("[data-map-value]").first().textContent(), /loading selected company/i);
      const currentInput = page.locator("#mapping-editor input[data-source-field]").first();
      await currentInput.fill("During_Fetch_After_Draw__c"); await currentInput.focus();
      await currentInput.evaluate(el => { window.__mappingInputDuringFetch = el; });
      const completed = page.waitForResponse(response => response.url().includes(`/ui/api/cases/${target}/full`) && response.status() === 200);
      release.resolve(); await completed;
      await page.waitForFunction(() => !document.querySelector("[data-map-value]")?.textContent.includes("Loading"));
      assert.equal(await currentInput.inputValue(), "During_Fetch_After_Draw__c");
      assert.equal(await currentInput.evaluate(el => window.__mappingInputDuringFetch === el), true);
      assert.equal(await currentInput.evaluate(el => document.activeElement === el), true);
      await page.unroute(pattern);
    });

    await check("mapping reset keeps the selected company loading state truthful", async () => {
      const choices = page.locator("#fmcase option:not([value=''])");
      const current = await page.locator("#fmcase").inputValue();
      const target = await choices.evaluateAll((options, current) => options.find(option => option.value !== current)?.value, current);
      const seen = deferred(), release = deferred(); const pattern = `**/ui/api/cases/${target}/full`;
      await page.route(pattern, async route => { seen.resolve(); await release.promise; await route.continue(); });
      await page.locator("#fmcase").selectOption(target); await seen.promise;
      await page.getByRole("button", { name: "Reset to Live" }).click();
      await page.getByRole("button", { name: "Confirm Reset" }).click();
      assert.match(await page.locator("[data-map-value]").first().textContent(), /loading selected company/i);
      const completed = page.waitForResponse(response => response.url().includes(`/ui/api/cases/${target}/full`) && response.status() === 200);
      release.resolve(); await completed;
      await page.waitForFunction(() => !document.querySelector("[data-map-value]")?.textContent.includes("Loading"));
      await page.unroute(pattern);
    });

    await check("mapping value-fetch failure is attributed without replacing destination inputs", async () => {
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      const input = page.locator("#mapping-editor input[data-source-field]").first();
      await input.fill("Error_State_Draft__c");
      await input.evaluate(el => { window.__mappingInputOnError = el; });
      const choices = page.locator("#fmcase option:not([value=''])");
      const current = await page.locator("#fmcase").inputValue();
      const target = await choices.evaluateAll((options, current) => options.find(option => option.value !== current)?.value, current);
      const pattern = `**/ui/api/cases/${target}/full`;
      await page.route(pattern, route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "offline" }) }));
      await page.locator("#fmcase").selectOption(target);
      await page.locator("#mapping-status").getByText(/values could not load/i).waitFor();
      assert.match(await page.locator("[data-map-value]").first().textContent(), /could not load/i);
      assert.equal(await input.inputValue(), "Error_State_Draft__c");
      assert.equal(await input.evaluate(el => window.__mappingInputOnError === el), true);
      await page.getByRole("button", { name: "Save Preview" }).click();
      assert.match(await page.locator("[data-map-value]").first().textContent(), /could not load/i);
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      assert.match(await page.locator("[data-map-value]").first().textContent(), /could not load/i);
      assert.equal(await page.locator("#mapping-editor input[data-source-field]").first().inputValue(), "Error_State_Draft__c");
      await page.unroute(pattern);
    });

    await ready(page, "#/policy", "Decision Rules", "#broker-editor");
    await check("broker explainer names the mixed-policy list accurately", async () => {
      await page.locator('[data-tip="brokerpolicy"]').click();
      assert.equal((await page.locator("#tip > b").textContent()).trim(), "Broker Rules");
    });
    await check("broker lifecycle edits every identifier field and escapes hostile text", async () => {
      await page.getByRole("button", { name: "Add Broker" }).click();
      const form = page.locator("#broker-entry-form");
      await form.getByLabel("Name").fill('<img src=x onerror="window.__pwned=1">');
      await form.getByLabel("Policy").selectOption("blocked");
      const values = {
        Aliases: "Hostile Alias", Domains: "evil.example", "Email domains": "mail.evil.example",
        "Org IDs": "ORG-EVIL", "POC handles": "EVIL-ARIN", ASNs: "AS64500",
      };
      for (const [label, value] of Object.entries(values)) await form.getByLabel(label, { exact: true }).fill(value);
      await form.getByLabel("Notes").fill("Preview note only");
      await form.getByRole("button", { name: "Apply Entry" }).click();
      assert.equal(await page.evaluate(() => window.__pwned), undefined);
      assert.equal(await page.locator("#broker-editor img").count(), 0);
      assert.match(await page.locator("#broker-editor").textContent(), /AS64500/);
      await page.getByRole("button", { name: "Save Preview" }).last().click();
      await page.locator("#broker-status").getByText(/saved in this browser/i).waitFor();
      const stored = JSON.parse(await page.evaluate(key => localStorage.getItem(key), previewKeys.brokers));
      const added = stored.value.brokers.find(b => b.name.includes("img src"));
      assert.ok(added.id.startsWith("local-"));
      for (const key of ["aliases", "domains", "email_domains", "org_ids", "poc_handles", "asns"])
        assert.equal(added[key].length, 1, `${key} was not saved`);
      await page.reload();
      await page.locator("#broker-editor").getByText("AS64500", { exact: true }).waitFor();
      await page.getByPlaceholder("Search brokers").fill("ORG-EVIL");
      assert.equal(await page.locator("[data-broker-row]").count(), 1);
      await page.getByLabel("Broker policy filter").selectOption("allowed");
      assert.equal(await page.locator("[data-broker-row]").count(), 0);
      await page.getByLabel("Broker policy filter").selectOption("blocked");
      assert.equal(await page.locator("[data-broker-row]").count(), 1);
    });

    await check("broker duplicate and overlap validation is truthful", async () => {
      await page.getByPlaceholder("Search brokers").fill("");
      await page.getByLabel("Broker policy filter").selectOption("all");
      await page.getByRole("button", { name: "Add Broker" }).click();
      const form = page.locator("#broker-entry-form");
      await form.getByLabel("Name").fill("  ip   trading  ");
      await form.getByRole("button", { name: "Apply Entry" }).click();
      assert.match(await page.locator("#broker-entry-error").textContent(), /already exists/i);
      await form.getByLabel("Name").fill("Overlap Broker");
      await form.getByLabel("Policy").selectOption("allowed");
      await form.getByLabel("Domains", { exact: true }).fill("evil.example");
      await form.getByRole("button", { name: "Apply Entry" }).click();
      assert.match(await page.locator("#broker-overlap").textContent(), /blocked.*takes precedence/i);
      assert.match(await page.getByText(/Allowed brokers still need to pass the other checks/i).textContent(), /blocked match takes precedence/i);
    });

    await check("broker removal requires confirmation and reset removes only the local draft", async () => {
      const overlap = page.locator("[data-broker-row]", { hasText: "Overlap Broker" });
      await overlap.getByRole("button", { name: "Remove" }).click();
      assert.match(await page.locator("#broker-status").textContent(), /confirm removal/i);
      await overlap.getByRole("button", { name: "Confirm Remove" }).click();
      assert.equal(await page.locator("[data-broker-row]", { hasText: "Overlap Broker" }).count(), 0);
      await page.getByRole("button", { name: "Reset to Live" }).last().click();
      await page.getByRole("button", { name: "Confirm Reset" }).last().click();
      assert.equal(await page.evaluate(key => localStorage.getItem(key), previewKeys.brokers), null);
      assert.equal(await page.locator("[data-broker-row]").count(), 6);
    });

    await check("unapplied broker add and edit disable Save Preview until apply or cancel", async () => {
      const save = page.locator("#save-brokers"), before = await page.evaluate(key => localStorage.getItem(key), previewKeys.brokers);
      await page.getByRole("button", { name: "Add Broker" }).click();
      await page.locator("#broker-entry-form").getByLabel("Name").fill("Unsaved entry witness");
      assert.equal(await save.isDisabled(), true);
      assert.match(await page.locator("#broker-status").textContent(), /Apply Entry.*Cancel|apply.*or cancel/i);
      assert.equal(await page.evaluate(key => localStorage.getItem(key), previewKeys.brokers), before);
      await page.locator("#broker-entry-form").getByRole("button", { name: "Cancel" }).click();
      assert.equal(await save.isEnabled(), true);

      const row = page.locator("[data-broker-row]").first(); const original = (await row.locator("td").first().textContent()).trim();
      await row.getByRole("button", { name: "Edit" }).click();
      await page.locator("#broker-entry-form").getByLabel("Name").fill("Edited entry witness");
      assert.equal(await save.isDisabled(), true);
      assert.match(await page.locator("#broker-status").textContent(), /Apply Entry.*Cancel|apply.*or cancel/i);
      await page.locator("#broker-entry-form").getByRole("button", { name: "Cancel" }).click();
      assert.equal(await save.isEnabled(), true);
      assert.equal((await row.locator("td").first().textContent()).trim(), original);

      await row.getByRole("button", { name: "Edit" }).click();
      await page.locator("#broker-entry-form").getByLabel("Name").fill("Applied entry witness");
      await page.locator("#broker-entry-form").getByRole("button", { name: "Apply Entry" }).click();
      assert.equal(await save.isEnabled(), true);
      assert.match(await page.locator("#broker-status").textContent(), /Unsaved broker edits/i);
      await page.getByRole("button", { name: "Reset to Live" }).last().click();
      await page.getByRole("button", { name: "Confirm Reset" }).last().click();
    });

    await check("broker policy select fills its visible wrapper and remains native", async () => {
      await page.getByRole("button", { name: "Add Broker" }).click();
      const select = page.locator("#broker-policy"), wrap = select.locator(".."), arrow = wrap.locator(":scope > svg");
      const boxes = await page.evaluate(() => {
        const select = document.querySelector("#broker-policy"), wrap = select.parentElement;
        const arrow = wrap.querySelector(":scope > svg");
        const read = el => { const r = el.getBoundingClientRect(); return { x: r.x, width: r.width, right: r.right }; };
        return { select: read(select), wrap: read(wrap), arrow: read(arrow) };
      });
      assert.ok(Math.abs(boxes.select.x - boxes.wrap.x) <= 1);
      assert.ok(Math.abs(boxes.select.width - boxes.wrap.width) <= 1);
      assert.ok(Math.abs(boxes.select.right - boxes.arrow.right - 12) <= 1);
      await select.selectOption("blocked");
      assert.equal(await select.inputValue(), "blocked");
      await page.locator("#broker-entry-form").getByRole("button", { name: "Cancel" }).click();
    });

    await ready(page, "#/overview", "Overview", "#decision-distribution");
    await check("overview labels its all-time denominator and latest-company time source", async () => {
      assert.match(await page.locator("#decision-distribution").textContent(), /12 all-time decision records/i);
      const shares = await page.locator("#decision-distribution [data-decision-share]").allTextContents();
      assert.deepEqual(shares.sort(), ["8.3%", "91.7%"]);
      assert.match(await page.getByRole("heading", { name: "Latest decisions by company" }).locator("..").textContent(), /up to 100 most recently updated companies/i);
      const first = page.locator("#latest-company-decisions details").first();
      await first.locator("summary").click();
      assert.match(await first.textContent(), /Company updated/i);
      assert.doesNotMatch(await first.textContent(), /Decision timestamp/i);
      assert.equal(await first.locator('a[href^="#/case/"]').count(), 1);
      assert.match(await first.textContent(), /Pointer status/i);
      await page.getByLabel("Latest company decision filter").selectOption("approve");
      const visible = page.locator("#latest-company-decisions details:visible");
      assert.ok(await visible.count() > 0);
      assert.deepEqual([...new Set(await visible.evaluateAll(rows => rows.map(row => row.dataset.latestDecision)))], ["approve"]);
    });

    const heldContext = await browser.newContext();
    const heldWrites = [];
    heldContext.on("request", request => { if (request.url().includes("/ui/api/") && request.method() !== "GET") heldWrites.push(request.url()); });
    await heldContext.route("**/ui/api/cases?limit=100", async route => {
      const response = await route.fetch(); const body = await response.json();
      body.cases[0] = { ...body.cases[0], company_name: "Held state witness",
        decision_provenance: "latest_decision_row", latest_decision: "manual_review_insufficient",
        enforcement_held: true };
      await route.fulfill({ response, json: body });
    });
    const heldPage = await heldContext.newPage(); heldPage.setDefaultTimeout(8000);
    await ready(heldPage, "#/overview", "Overview", "#latest-company-decisions");
    await check("authoritative safety-held company is Held for Approval in overview summary and details", async () => {
      const row = heldPage.locator("#latest-company-decisions details", { hasText: "Held state witness" });
      assert.match(await row.locator("summary").textContent(), /Held for Approval/);
      assert.equal(await row.getAttribute("data-latest-decision"), "manual_review_insufficient");
      await row.locator("summary").click();
      assert.match(await row.locator(".latest-detail").textContent(), /Latest decision\s*Held for Approval/);
      await heldPage.getByLabel("Latest company decision filter").selectOption("manual_review_insufficient");
      assert.equal(await row.isVisible(), true, "raw published decision remains the filter authority");
      assert.deepEqual(heldWrites, []);
    });
    await heldContext.close();

    await check("auto-refresh preserves unsaved preview input and focus", async () => {
      await ready(page, "#/policy", "Decision Rules", "#points-editor");
      await page.getByRole("button", { name: "Edit Points" }).click();
      const first = page.locator("#points-editor input[data-check]").first();
      await first.fill("777");
      await page.getByLabel("Refresh every 5 seconds").check();
      await first.focus();
      await page.waitForTimeout(5250);
      assert.equal(await first.inputValue(), "777");
      assert.equal(await first.evaluate(el => document.activeElement === el), true);
      await page.getByLabel("Refresh every 5 seconds").uncheck();
    });

    await check("deep-linked policy auto-refresh preserves edits and rule navigation still updates", async () => {
      await ready(page, "#/policy?rule=approve", "Decision Rules", "#points-editor");
      await page.reload(); await page.getByRole("button", { name: "Edit Points" }).waitFor();
      await page.getByRole("button", { name: "Edit Points" }).click();
      const first = page.locator("#points-editor input[data-check]").first();
      await first.fill("778");
      await page.getByLabel("Refresh every 5 seconds").check(); await first.focus();
      await page.waitForTimeout(5250);
      assert.equal(await first.inputValue(), "778");
      assert.equal(await first.evaluate(el => document.activeElement === el), true);
      await page.getByLabel("Refresh every 5 seconds").uncheck();
      await page.evaluate(() => { location.hash = "#/policy?rule=reject"; });
      await page.waitForFunction(() => document.querySelector("#rule-reject")?.classList.contains("hl"));
      assert.equal(await first.inputValue(), "778");
      assert.equal(await page.locator("#rule-approve.hl").count(), 0);
    });

    await check("an older async route cannot overwrite the newer route", async () => {
      let release;
      const wait = new Promise(resolve => { release = resolve; });
      const race = await browser.newContext();
      await race.route("**/ui/api/policy", async route => { await wait; await route.continue(); });
      const racePage = await race.newPage(); racePage.setDefaultTimeout(8000);
      const old = racePage.goto(`${baseUrl}#/policy`).catch(error => error);
      await racePage.waitForTimeout(100);
      await racePage.evaluate(() => { location.hash = "#/overview"; });
      await racePage.getByRole("heading", { name: "Overview", level: 1 }).waitFor();
      release(); await old; await racePage.waitForTimeout(200);
      assert.equal((await racePage.locator("#page h1").textContent()).trim(), "Overview");
      await race.close();
    });

    for (const theme of ["light", "dark"]) for (const width of [390, 768, 1147]) {
      await page.setViewportSize({ width, height: 1137 });
      await page.evaluate(theme => localStorage.setItem("kyc-theme", theme), theme);
      await ready(page, "#/fieldmap", "Salesforce Fields", "#mapping-editor");
      await page.reload(); await page.getByRole("button", { name: "Edit Mappings" }).waitFor();
      await page.getByRole("button", { name: "Edit Mappings" }).click();
      await check(`${width}px ${theme} supplied Salesforce destinations are readable at rest`, async () => {
        const input = page.getByLabel("Destination for Business_Document_Status__c");
        await input.blur();
        const fit = await input.evaluate(el => {
          const style = getComputedStyle(el), canvas = document.createElement("canvas");
          const context = canvas.getContext("2d"); context.font = style.font;
          return { value: el.value, textWidth: context.measureText(el.value).width,
            contentWidth: el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) };
        });
        assert.equal(fit.value, "Business_Document_Status__c");
        assert.ok(fit.contentWidth >= fit.textWidth,
          `${fit.value} needs ${fit.textWidth}px but has ${fit.contentWidth}px`);
        const scroller = page.locator("#mapping-editor .bd.flush");
        if (width === 390) assert.equal(await scroller.evaluate(el => el.scrollWidth > el.clientWidth), true,
          "narrow mapping table preserves intentional horizontal scrolling");
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      });
    }

    for (const theme of ["light", "dark"]) for (const width of [390, 768, 1147, 1199, 1440]) {
      await page.setViewportSize({ width, height: width === 1147 ? 1137 : 950 });
      await page.evaluate(theme => localStorage.setItem("kyc-theme", theme), theme);
      for (const [hash, heading, selector] of [["#/policy", "Decision Rules", "#broker-editor"],
        ["#/fieldmap", "Salesforce Fields", "#mapping-editor"],
        ["#/overview", "Overview", "#latest-company-decisions"]]) {
        await ready(page, hash, heading, selector);
        await check(`${width}x${width === 1147 ? 1137 : 950} ${theme} ${heading} has no page overflow`, async () => {
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        });
      }
    }

    await check("all preview interactions generated zero API writes", async () => {
      assert.deepEqual(writes, []);
    });

    const blocked = await browser.newContext();
    await blocked.addInitScript(() => {
      Object.defineProperty(Storage.prototype, "getItem", { configurable: true, value() { throw Error("blocked get"); } });
      Object.defineProperty(Storage.prototype, "setItem", { configurable: true, value() { throw Error("blocked set"); } });
      Object.defineProperty(Storage.prototype, "removeItem", { configurable: true, value() { throw Error("blocked remove"); } });
    });
    const blockedPage = await blocked.newPage(); blockedPage.setDefaultTimeout(8000);
    await ready(blockedPage, "#/policy", "Decision Rules", "#points-editor");
    await check("blocked preview storage stays usable and reports recovery", async () => {
      await blockedPage.getByRole("button", { name: "Edit Points" }).click();
      const first = blockedPage.locator("#points-editor input").first();
      await first.fill("40");
      await blockedPage.getByRole("button", { name: "Save Preview" }).first().click();
      assert.match(await blockedPage.locator("#points-status").textContent(), /could not be saved/i);
      assert.equal(await first.inputValue(), "40");
      await blockedPage.getByRole("button", { name: "Reset to Live" }).first().click();
      await blockedPage.getByRole("button", { name: "Confirm Reset" }).first().click();
      assert.match(await blockedPage.locator("#points-status").textContent(), /could not be removed/i);
      assert.equal(await first.inputValue(), "40");
    });
    await blocked.close();
    await context.close();
  } finally {
    if (browser) await browser.close();
    const passed = results.filter(Boolean).length;
    console.log(`RESULT ${passed}/${results.length} checks passed`);
    if (passed !== results.length) process.exitCode = 1;
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
