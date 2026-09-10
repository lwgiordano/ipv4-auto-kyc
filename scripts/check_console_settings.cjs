const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:55717/ui";
const results = [];

async function check(name, fn) {
  try { await fn(); results.push({ name, ok: true }); console.log(`PASS ${name}`); }
  catch (error) { results.push({ name, ok: false }); console.log(`FAIL ${name}: ${error.message}`); }
}

async function launch() {
  try { return await chromium.launch({ headless: true }); }
  catch (error) {
    if (!/Executable doesn't exist|browserType\.launch/.test(error.message)) throw error;
    return chromium.launch({ headless: true, channel: "chrome" });
  }
}

async function ready(page) {
  await page.goto(`${baseUrl}#/overview`);
  await page.locator("#page h1").waitFor();
}

async function selectTheme(page, label) {
  await page.locator("#settings-button").click();
  await page.getByLabel(label, { exact: true }).check();
}

(async () => {
  let browser;
  try {
    browser = await launch();
    const requests = [];
    const context = await browser.newContext({ colorScheme: "light" });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.on("request", request => requests.push({ method: request.method(), url: request.url() }));
    await page.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        window.__themeAtDomReady = document.documentElement.getAttribute("data-theme");
      }, { once: true });
    });
    await ready(page);

    await check("fresh context defaults to System and follows OS", async () => {
      assert.equal(await page.locator("#settings-value").textContent(), "System");
      await page.locator("#settings-button").click();
      assert.equal(await page.getByLabel("System", { exact: true }).isChecked(), true);
      await page.keyboard.press("Escape");
      const light = await page.locator("body").evaluate(el => [getComputedStyle(el).backgroundColor, getComputedStyle(el).colorScheme]);
      await page.emulateMedia({ colorScheme: "dark" });
      const dark = await page.locator("body").evaluate(el => [getComputedStyle(el).backgroundColor, getComputedStyle(el).colorScheme]);
      assert.notEqual(light[0], dark[0]);
      assert.match(light[1], /light/); assert.match(dark[1], /dark/);
    });

    await check("explicit themes override OS and persist across reload", async () => {
      await page.emulateMedia({ colorScheme: "light" });
      await selectTheme(page, "Dark");
      assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
      assert.equal(await page.locator("#settings-value").textContent(), "Dark");
      await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
      await page.emulateMedia({ colorScheme: "dark" });
      await selectTheme(page, "Light");
      assert.equal(await page.locator("html").getAttribute("data-theme"), "light");
      await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.locator("html").getAttribute("data-theme"), "light");
    });

    await check("System resumes OS following and persists", async () => {
      await selectTheme(page, "System");
      assert.equal(await page.locator("html").getAttribute("data-theme"), null);
      assert.equal(await page.evaluate(() => localStorage.getItem("kyc-theme")), "system");
      await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.locator("#settings-value").textContent(), "System");
    });

    await check("saved theme is applied before DOMContentLoaded", async () => {
      await page.evaluate(() => localStorage.setItem("kyc-theme", "dark"));
      await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.evaluate(() => window.__themeAtDomReady), "dark");
    });

    await check("another tab updates theme and controls", async () => {
      const other = await context.newPage(); await ready(other);
      await selectTheme(other, "Light");
      await page.waitForFunction(() => document.documentElement.dataset.theme === "light" &&
        document.querySelector("#settings-value")?.textContent === "Light");
      await page.locator("#settings-button").click();
      assert.equal(await page.getByLabel("Light", { exact: true }).isChecked(), true);
      await page.keyboard.press("Escape"); await other.close();
    });

    await check("invalid storage falls back safely", async () => {
      await page.evaluate(() => localStorage.setItem("kyc-theme", "sepia"));
      await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.locator("html").getAttribute("data-theme"), null);
      assert.equal(await page.locator("#settings-value").textContent(), "System");
    });

    await check("keyboard selection and Escape restore trigger focus", async () => {
      await page.locator("#settings-button").focus(); await page.keyboard.press("Enter");
      assert.equal(await page.evaluate(() => document.activeElement?.value), "system",
        "opening Settings did not put keyboard focus on the selected appearance");
      await page.keyboard.press("ArrowRight");
      assert.equal(await page.getByLabel("Light", { exact: true }).isChecked(), true);
      await page.keyboard.press("Escape");
      assert.equal(await page.evaluate(() => document.activeElement?.id), "settings-button");
      assert.equal(await page.locator("#settings-panel").evaluate(el => el.matches(":popover-open")), false);
    });

    await check("outside click dismisses without stealing clicked control focus", async () => {
      await page.locator("#settings-button").click();
      await page.locator("#reviewer").click();
      assert.equal(await page.locator("#settings-panel").evaluate(el => el.matches(":popover-open")), false);
      assert.equal(await page.evaluate(() => document.activeElement?.id), "reviewer");
    });

    for (const width of [390, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 }); await page.reload(); await page.locator("#page h1").waitFor();
      if (width < 1024) await page.locator("#menubtn").click();
      await page.locator("#settings-button").click();
      await check(`${width}px panel is visible and inside the viewport`, async () => {
        const box = await page.locator("#settings-panel").boundingBox(); assert.ok(box);
        assert.ok(box.x >= 8 && box.y >= 8 && box.x + box.width <= width - 8 && box.y + box.height <= 892);
        assert.equal(await page.locator("#settings-panel label").count(), 3);
      });
      if (width < 1024) {
        await page.locator("#menubtn").click();
        assert.equal(await page.locator("#settings-panel").evaluate(el => el.matches(":popover-open")), false);
      } else await page.keyboard.press("Escape");
    }

    await check("crossing into the mobile layout closes an open desktop panel", async () => {
      await page.setViewportSize({ width: 1024, height: 900 });
      await page.locator("#settings-button").click();
      assert.equal(await page.locator("#settings-panel").evaluate(el => el.matches(":popover-open")), true);
      await page.setViewportSize({ width: 768, height: 900 });
      assert.equal(await page.locator("#settings-panel").evaluate(el => el.matches(":popover-open")), false);
      await page.setViewportSize({ width: 1440, height: 900 });
    });

    await check("navigation closes the panel without remounting the page", async () => {
      await page.locator("#settings-button").click(); await page.evaluate(() => { window.__settingsPageNode = document.querySelector("#page"); });
      const openAfterHashEvent = await page.evaluate(() => new Promise(resolve => {
        addEventListener("hashchange", () => resolve(document.querySelector("#settings-panel").matches(":popover-open")), { once: true });
        location.hash = "#/policy";
      }));
      assert.equal(openAfterHashEvent, false, "the old-route panel survived the navigation event");
      await page.getByRole("heading", { name: "Decision Rules", level: 1 }).waitFor();
      assert.equal(await page.locator("#settings-panel").evaluate(el => el.matches(":popover-open")), false);
      assert.equal(await page.evaluate(() => document.querySelector("#page") === window.__settingsPageNode), true);
    });

    await check("appearance changes do not navigate or mutate API data", async () => {
      const hash = await page.evaluate(() => location.hash);
      await page.evaluate(() => { window.__settingsPageNode = document.querySelector("#page"); });
      await selectTheme(page, "Dark");
      assert.equal(await page.evaluate(() => location.hash), hash);
      assert.equal(await page.evaluate(() => document.querySelector("#page") === window.__settingsPageNode), true);
      assert.equal(requests.filter(r => r.url.includes("/ui/api/") && r.method !== "GET").length, 0);
    });

    const blocked = await browser.newContext();
    const blockedPage = await blocked.newPage(); blockedPage.setDefaultTimeout(5000);
    await blockedPage.addInitScript(() => {
      Object.defineProperty(Storage.prototype, "getItem", { configurable: true, value() { throw new Error("blocked read"); } });
      Object.defineProperty(Storage.prototype, "setItem", { configurable: true, value() { throw new Error("blocked write"); } });
    });
    await ready(blockedPage);
    await check("storage exceptions preserve rendering and disclose failed saves", async () => {
      assert.equal(await blockedPage.locator("#settings-value").textContent(), "System");
      await selectTheme(blockedPage, "Dark");
      assert.equal(await blockedPage.locator("html").getAttribute("data-theme"), "dark");
      assert.match(await blockedPage.locator("#settings-hint").textContent(), /could not be saved/i);
    });
    await blocked.close();
  } finally {
    if (browser) await browser.close();
    const failures = results.filter(result => !result.ok);
    console.log(`RESULT ${results.length - failures.length}/${results.length} checks passed`);
    if (failures.length) process.exitCode = 1;
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
