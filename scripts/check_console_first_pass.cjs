const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:55717/ui";
const results = [];
let staleRace = null;

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

async function bounded(promise, label, timeoutMs = 5000) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function check(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`PASS ${name}`);
  } catch (error) {
    results.push({ name, ok: false, error: error.message });
    console.log(`FAIL ${name}: ${error.message}`);
  }
}

function near(actual, expected, message) {
  assert.ok(Math.abs(actual - expected) <= 1,
    `${message}: expected ${expected} +/- 1px, got ${actual}`);
}

const cleanTitle = title => title.replace(/^(Full stack|Preview) · /, "");

async function rect(locator) {
  const box = await locator.boundingBox();
  assert.ok(box, `element is not rendered: ${await locator.evaluate(el => el.outerHTML)}`);
  return box;
}

async function openHash(page, hash) {
  const headings = {
    "#/overview": "Overview",
    "#/cases": "Companies",
    "#/integrations": "Data Sources",
    "#/fieldmap": "Salesforce Fields",
    "#/policy": "Decision Rules",
    "#/options": "Options",
    "#/composer": "Send Message",
  };
  await page.evaluate(next => { location.hash = next; }, hash);
  await page.waitForFunction(({ next, heading }) => location.hash === next &&
    document.querySelector("#page h1")?.textContent.trim() === heading, { next: hash, heading: headings[hash] });
}

(async () => {
  let browser;
  try {
    try {
      browser = await chromium.launch({ headless: true });
    } catch (error) {
      if (!/Executable doesn't exist|browserType\.launch/.test(error.message)) throw error;
      browser = await chromium.launch({ headless: true, channel: "chrome" });
    }
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    await page.route("**/ui/api/cases?*", async route => {
      const q = new URL(route.request().url()).searchParams.get("q");
      if (staleRace && q === staleRace.oldQuery) {
        staleRace.oldRequestSeen.resolve();
        await bounded(staleRace.releaseOld.promise, "release old query response");
        const response = await route.fetch();
        const body = await response.json();
        staleRace.oldRowCount = body.cases.length;
        await route.fulfill({ response });
        staleRace.oldResponseCompleted.resolve();
        return;
      }
      if (staleRace && q === staleRace.newQuery) {
        const response = await route.fetch();
        const body = await response.json();
        staleRace.newRowCount = body.cases.length;
        await route.fulfill({ response });
        staleRace.newResponseCompleted.resolve();
        return;
      }
      if (q === "FocusDelay" || q === "LeaveDelay") {
        await new Promise(resolve => setTimeout(resolve, 650));
      }
      await route.continue();
    });

    for (const width of [390, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`${baseUrl}#/overview`);
      await page.locator("#page h1").waitFor();
      await check(`${width}px overview cards use one 16px layout gap`, async () => {
        const first = await rect(page.locator(".split > .card").nth(0));
        const second = await rect(page.locator(".split > .card").nth(1));
        if (width > 980) near(second.y, first.y, "overview card top difference");
        else near(second.y - (first.y + first.height), 16, "stacked overview card gap");
      });

      await openHash(page, "#/composer");
      await check(`${width}px stacked cards have a 16px gap`, async () => {
        const first = await rect(page.locator(".stack > .card").nth(0));
        const second = await rect(page.locator(".stack > .card").nth(1));
        near(second.y - (first.y + first.height), 16, "stacked card gap");
      });
      await check(`${width}px reviewer avatar aligns with the input`, async () => {
        if (width < 1024) await page.locator("#menubtn").click();
        const avatar = await rect(page.locator(".avatar"));
        const input = await rect(page.locator("#reviewer"));
        near(avatar.y + avatar.height / 2, input.y + input.height / 2,
          "reviewer avatar/input center difference");
      });

      await openHash(page, "#/integrations");
      await check(`${width}px legend is 12px below its description`, async () => {
        const description = await rect(page.locator(".tbar .desc"));
        const legend = await rect(page.locator(".tbar .legend"));
        near(legend.y - (description.y + description.height), 12, "description-to-legend gap");
      });
      await check(`${width}px header group is 24px above content`, async () => {
        const legend = await rect(page.locator(".tbar .legend"));
        const content = await rect(page.locator("#page > .card").first());
        near(content.y - (legend.y + legend.height), 24, "header-to-content gap");
      });
      await check(`${width}px actionless headers emit no action slot`, async () => {
        assert.equal(await page.locator(".tbar > .actions").count(), 0);
      });
    }

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => sessionStorage.removeItem("caseq"));
    await openHash(page, "#/cases");
    const firstCompany = page.locator("table.companies a.rowlink").first();
    await firstCompany.waitFor({ state: "visible" });
    const companyName = (await firstCompany.textContent()).trim();
    const companyHash = await firstCompany.getAttribute("href");
    await firstCompany.click();
    await page.locator(".idgrid").waitFor();
    await check("company detail owns its company-specific document title", async () => {
      assert.equal(cleanTitle(await page.title()), `${companyName} · KYC Tool`);
    });
    await check("company primary values share their identity-grid row", async () => {
      const fields = page.locator(".idgrid .idf");
      const count = Math.min(await fields.count(), 4);
      assert.ok(count > 1, "at least two company identity fields are required");
      const tops = [];
      for (let index = 0; index < count; index += 1) {
        tops.push((await rect(fields.nth(index).locator(":scope > .v"))).y);
      }
      tops.slice(1).forEach(top => near(top, tops[0], "identity primary-value top difference"));
    });

    await page.evaluate(() => { location.hash = "#/case/no-such-console-regression-case"; });
    await page.getByText("No company with this ID").waitFor();
    await check("a missing-company error does not retain the previous company title", async () => {
      assert.equal(cleanTitle(await page.title()), "KYC Tool · Console");
      assert.ok(!cleanTitle(await page.title()).includes(companyName));
    });

    await openHash(page, "#/cases");
    const search = page.locator("#q");
    await search.fill("");
    await search.type("Ac");
    await page.waitForTimeout(400);
    await page.keyboard.type("me");
    await check("search stays focused while typing across a refresh", async () => {
      assert.equal(await page.locator("#q").inputValue(), "Acme");
      assert.equal(await page.evaluate(() => document.activeElement?.id), "q");
    });

    await page.locator("#q").evaluate(input => input.setSelectionRange(1, 3));
    await page.locator("#q").fill("Acme ");
    await page.locator("#q").evaluate(input => input.setSelectionRange(1, 3));
    await page.waitForTimeout(400);
    await check("search selection survives a result refresh", async () => {
      assert.deepEqual(await page.locator("#q").evaluate(input => [input.selectionStart, input.selectionEnd]), [1, 3]);
    });

    const newQuery = `DefinitelyMissing-${Date.now()}`;
    staleRace = {
      oldQuery: companyName,
      newQuery,
      oldRequestSeen: deferred(),
      newResponseCompleted: deferred(),
      releaseOld: deferred(),
      oldResponseCompleted: deferred(),
      oldRowCount: null,
      newRowCount: null,
    };
    await page.locator("#q").fill(staleRace.oldQuery);
    await bounded(staleRace.oldRequestSeen.promise, "old query request");
    await page.locator("#q").fill(staleRace.newQuery);
    await bounded(staleRace.newResponseCompleted.promise, "new query response");
    await page.waitForFunction(expected =>
      document.querySelector("#q")?.value === expected &&
      document.querySelector("#case-count")?.textContent === "0 companies" &&
      document.querySelector("#case-rows")?.textContent.includes("No matches"), newQuery);
    const oldBrowserResponsePromise = page.waitForResponse(response =>
      new URL(response.url()).searchParams.get("q") === staleRace.oldQuery);
    staleRace.releaseOld.resolve();
    const oldBrowserResponse = await bounded(oldBrowserResponsePromise, "browser old query response");
    await bounded(oldBrowserResponse.finished(), "browser old query response body");
    await bounded(staleRace.oldResponseCompleted.promise, "old query route completion");
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    await check("an older same-view query cannot replace newer results", async () => {
      assert.ok(staleRace.oldRowCount > 0, "the delayed real response must contain company rows");
      assert.equal(staleRace.newRowCount, 0, "the newer real response must differ from the delayed response");
      assert.equal(await page.locator("#q").inputValue(), newQuery);
      assert.equal(await page.locator("#case-count").textContent(), "0 companies");
      assert.ok((await page.locator("#case-rows").textContent()).includes("No matches"));
      assert.equal(await page.locator("table.companies a.rowlink").count(), 0);
    });
    staleRace = null;

    await page.locator("#q").fill("FocusDelay");
    await page.waitForTimeout(300);
    await page.locator('.nav-item[data-r="integrations"]').focus();
    await page.waitForTimeout(750);
    await check("a delayed result does not steal navigation focus", async () => {
      assert.equal(await page.evaluate(() => document.activeElement?.dataset?.r), "integrations");
    });

    await page.locator("#q").fill("LeaveDelay");
    await page.waitForTimeout(300);
    await openHash(page, "#/integrations");
    await page.waitForTimeout(750);
    await check("a late Companies result cannot overwrite a new view", async () => {
      assert.equal(await page.locator("#page h1").textContent(), "Data Sources");
      assert.equal(await page.locator("#q").count(), 0);
    });

    const expectedTitles = [
      ["#/overview", "Overview · KYC Tool"],
      ["#/cases", "Companies · KYC Tool"],
      ["#/integrations", "Data Sources · KYC Tool"],
      ["#/fieldmap", "Salesforce Fields · KYC Tool"],
      ["#/policy", "Decision Rules · KYC Tool"],
      ["#/options", "Options · KYC Tool"],
      ["#/composer", "Send Message · KYC Tool"],
    ];
    for (const [hash, title] of expectedTitles) {
      await page.evaluate(next => { location.hash = next; }, companyHash);
      await page.locator(".idgrid").waitFor();
      await check(`company title is set before navigating to ${title}`, async () => {
        assert.equal(cleanTitle(await page.title()), `${companyName} · KYC Tool`);
      });
      await openHash(page, hash);
      await check(`company detail to ${title} resets the document title`, async () => {
        assert.equal(cleanTitle(await page.title()), title);
      });
    }

    for (const colorScheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme });
      await page.reload();
      await page.locator("#page h1").waitFor();
      await check(`${colorScheme} theme renders the console`, async () => {
        assert.equal(await page.locator("#page h1").textContent(), "Send Message");
        assert.notEqual(await page.locator("body").evaluate(el => getComputedStyle(el).backgroundColor), "rgba(0, 0, 0, 0)");
      });
    }
  } finally {
    if (browser) await browser.close();
    const failures = results.filter(result => !result.ok);
    console.log(`RESULT ${results.length - failures.length}/${results.length} checks passed`);
    if (failures.length) process.exitCode = 1;
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
