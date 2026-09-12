const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.argv[2] || "http://127.0.0.1:55717/ui";
const results = [];

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

const centerY = box => box.y + box.height / 2;

async function ready(page, hash, heading, content) {
  await page.goto(`${baseUrl}${hash}`);
  try { await page.waitForFunction(({ hash, heading }) => location.hash === hash &&
    document.querySelector("#page h1")?.textContent.trim() === heading, { hash, heading }); }
  catch (error) {
    const rendered = await page.locator("#page").innerText().catch(() => "<page unavailable>");
    throw new Error(`${error.message}; rendered page: ${rendered.slice(0, 500)}`);
  }
  await page.locator(content).first().waitFor();
}

async function textRect(locator) {
  return locator.evaluate(el => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.textContent.trim()) {
        const range = document.createRange();
        range.selectNodeContents(node);
        const r = range.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      }
    }
    return null;
  });
}

(async () => {
  let browser;
  try {
    browser = await launch();
    const context = await browser.newContext({ colorScheme: "light" });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);

    await ready(page, "#/options", "Options", ".options-form");
    await check("reviewer note and separated Options helper use approved copy", async () => {
      assert.equal(await page.locator("#reviewer-note").textContent(), "Recorded on every review action.");
      assert.equal(await page.locator(".options-footer .sub").textContent(),
        "Saved only in this browser. System follows your operating system setting.");
      const fieldset = await rect(page.locator(".options-form fieldset"));
      const footer = await rect(page.locator(".options-footer"));
      near(footer.y - (fieldset.y + fieldset.height), 24, "theme-controls-to-helper gap");
    });

    await check("Options legend and page title rhythm leave 12px before following content", async () => {
      const h1 = await rect(page.locator(".tbar h1"));
      const desc = await rect(page.locator(".tbar .desc"));
      near(desc.y - (h1.y + h1.height), 12, "title-to-description gap");
      const cardHeader = await rect(page.locator(".card > .hd").first());
      const form = await rect(page.locator(".card > .bd > .options-form"));
      near(form.y - (cardHeader.y + cardHeader.height), 12, "card-header-to-form gap");
      const legend = await rect(page.locator(".options-form legend"));
      const firstControl = await rect(page.getByLabel("System", { exact: true }).locator("..").first());
      near(firstControl.y - (legend.y + legend.height), 12, "legend-to-controls gap");
    });

    await ready(page, "#/case/demo-case-00131", "Acme Networks Ltd", ".published-gates");
    await check("inside-card subhead has one 12px container-owned gap", async () => {
      const heading = await rect(page.locator(".subsection > .sub-hd"));
      const gates = await rect(page.locator(".subsection > .gates"));
      near(gates.y - (heading.y + heading.height), 12, "Decision Rules-to-gates gap");
    });

    await check("website-review row vertically centers text, pills, and equal-height actions", async () => {
      const row = page.locator("table").filter({ hasText: "Approve Website" }).locator("tbody tr").first();
      const cell = await rect(row.locator("td").nth(1));
      const text = await textRect(row.locator("td").nth(1));
      const pill = await rect(row.locator(".pill").first());
      const approve = await rect(row.getByRole("button", { name: "Approve Website" }));
      const reject = await rect(row.getByRole("button", { name: "Reject Website" }));
      assert.ok(text, "task type text is rendered");
      near(centerY(text), centerY(cell), "task text center", 2);
      near(centerY(pill), centerY(cell), "status pill center", 2);
      near(centerY(approve), centerY(cell), "approve button center", 2);
      near(centerY(reject), centerY(cell), "reject button center", 2);
      near(approve.height, reject.height, "website action heights", 0.1);
      const style = locator => locator.evaluate(el => {
        const s = getComputedStyle(el);
        return [s.backgroundColor, s.color, s.borderColor, s.minHeight];
      });
      assert.deepEqual(await style(row.getByRole("button", { name: "Approve Website" })),
        await style(page.locator("#approvebtn")));
      assert.deepEqual(await style(row.getByRole("button", { name: "Reject Website" })),
        await style(page.locator("#sendbtn")));
    });

    await check("score fill and threshold use the same inset coordinate lane", async () => {
      const outer = await rect(page.locator(".btrack"));
      const scale = await rect(page.locator(".bscale"));
      const fill = await rect(page.locator(".bmeasure"));
      const tick = await rect(page.locator(".btick"));
      near(scale.x - outer.x, 4, "left score inset");
      near((outer.x + outer.width) - (scale.x + scale.width), 4, "right score inset");
      near(fill.x, scale.x, "fill lane origin");
      near(fill.width / scale.width, 45 / 130, "fill ratio", .01);
      near((tick.x - scale.x) / scale.width, 100 / 130, "threshold ratio", .01);
      assert.ok(fill.x >= scale.x && fill.x + fill.width <= scale.x + scale.width + 1,
        "fill remains inside the inset lane");
      assert.ok(fill.x + fill.width < tick.x, "45 points remains below the 100-point threshold");
      const colors = await page.locator(".bmeasure").evaluate(el => ({
        fill: getComputedStyle(el).backgroundColor,
        neutral: getComputedStyle(document.documentElement).getPropertyValue("--muted").trim(),
      }));
      const neutral = await page.evaluate(value => {
        const probe = document.createElement("span"); probe.style.color = value; document.body.append(probe);
        const resolved = getComputedStyle(probe).color; probe.remove(); return resolved;
      }, colors.neutral);
      assert.equal(colors.fill, neutral);
    });

    for (const score of [0, 100, 115]) {
      const scoreContext = await browser.newContext();
      await scoreContext.route("**/ui/api/cases/demo-case-00131/full", async route => {
        const response = await route.fetch();
        const body = await response.json();
        body.score.current_evidence_score = score;
        body.score.total = score;
        await route.fulfill({ response, json: body });
      });
      const scorePage = await scoreContext.newPage(); scorePage.setDefaultTimeout(7000);
      await ready(scorePage, "#/case/demo-case-00131", "Acme Networks Ltd", ".bscale");
      await check(`${score}-point score preserves truthful fill geometry`, async () => {
        const scale = await rect(scorePage.locator(".bscale"));
        const fill = await rect(scorePage.locator(".bmeasure"));
        const tick = await rect(scorePage.locator(".btick"));
        const max = Math.max(130, score + 10);
        near(fill.width / scale.width, Math.min(1, score / max), `${score}-point fill ratio`, .01);
        near((tick.x - scale.x) / scale.width, 100 / max, `${score}-point threshold ratio`, .01);
        if (score === 0) near(fill.width, 0, "zero score has no fill", .1);
        if (score === 100) near(fill.x + fill.width, tick.x, "100 points meets threshold exactly", 1);
        if (score === 115) assert.ok(fill.x + fill.width > tick.x,
          "above-threshold score extends beyond the threshold");
      });
      await scoreContext.close();
    }

    await ready(page, "#/fieldmap", "Salesforce Fields", "#fmcase");
    await check("drawn select arrow is inset 12px while select remains native", async () => {
      const select = page.locator("#fmcase");
      const wrap = select.locator("..");
      const arrow = wrap.locator(":scope > svg");
      assert.equal(await select.evaluate(el => el.tagName), "SELECT");
      const sb = await rect(select), wb = await rect(wrap), ab = await rect(arrow);
      near(sb.x, wb.x, "select and visible wrapper left edge");
      near(sb.width, wb.width, "select fills visible wrapper width");
      near(sb.x + sb.width - (ab.x + ab.width), 12, "select arrow right inset");
      const paddingRight = await select.evaluate(el => parseFloat(getComputedStyle(el).paddingRight));
      assert.ok(paddingRight >= 36, `select reserves arrow space: ${paddingRight}px`);
      const value = await select.locator("option:not([value=''])").first().getAttribute("value");
      await select.selectOption(value);
      assert.equal(await select.inputValue(), value, "native select interaction remains available");
    });

    for (const theme of ["light", "dark"]) for (const width of [390, 768, 1147, 1199, 1261, 1319, 1320, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      await ready(page, "#/options", "Options", ".options-form");
      await page.getByLabel(theme === "light" ? "Light" : "Dark", { exact: true }).check();
      await ready(page, "#/integrations", "Data Sources", ".irow");
      await check(`${width}px ${theme} source hierarchy fits without status legends`, async () => {
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        assert.equal(await page.locator(".tbar .legend").count(), 0);
        const cardHeader = await rect(page.locator("#page > .card .hd").first());
        const firstRow = await rect(page.locator("#page > .card .irow .meta").first());
        near(firstRow.y - (cardHeader.y + cardHeader.height), 12, "card-header-to-first-row-content gap");
        const notes = page.locator(".source-note");
        assert.ok(await notes.count() > 0);
        for (const note of await notes.all()) {
          const icon = await rect(note.locator(":scope > .ic"));
          const label = await rect(note.locator(":scope > span"));
          near(centerY(icon), centerY(label), "source icon and label center");
          assert.ok(label.x - (icon.x + icon.width) >= 3, "source icon has a text gap");
        }
      });

      await ready(page, "#/options", "Options", ".options-form");
      if (width < 1024) await page.locator("#menubtn").click();
      await check(`${width}px ${theme} navigation has distinct selected, hover, and focus states`, async () => {
        const active = page.locator('[data-r="options"]');
        const hover = page.locator('[data-r="integrations"]');
        const focus = page.locator('[data-r="fieldmap"]');
        await page.mouse.move(width - 2, 2);
        await page.waitForTimeout(220);
        const normal = await hover.evaluate(el => getComputedStyle(el).backgroundColor);
        await hover.hover(); await page.waitForTimeout(220);
        const hovered = await hover.evaluate(el => getComputedStyle(el).backgroundColor);
        await hover.focus(); await page.keyboard.press("Tab"); await page.waitForTimeout(220);
        assert.equal(await focus.evaluate(el => document.activeElement === el), true,
          "keyboard focus reaches the next navigation item");
        const focused = await focus.evaluate(el => getComputedStyle(el).backgroundColor);
        const selected = await active.evaluate(el => getComputedStyle(el).backgroundColor);
        assert.notEqual(hovered, normal, "hover background must be visible");
        assert.notEqual(focused, normal, "focus background must be visible");
        assert.notEqual(selected, normal, "selected background must be visible");
        assert.equal(new Set([hovered, focused, selected]).size, 3,
          `states must remain distinct: ${hovered}, ${focused}, ${selected}`);
      });
    }

    const metadata = await browser.newContext();
    const metadataPage = await metadata.newPage(); metadataPage.setDefaultTimeout(7000);
    await ready(metadataPage, "#/composer", "Company Actions", "#c-send");
    await check("direct route initializes accurately scoped global metadata", async () => {
      await metadataPage.locator("#envchip .pill").waitFor();
      assert.equal((await metadataPage.locator("#envchip").textContent()).trim(), "Message authentication: On");
      assert.equal(await metadataPage.locator("#policychip").count(), 0);
      assert.equal((await metadataPage.locator(".tbar .desc").textContent()).includes("signs it"), false);
    });
    await metadata.close();

    const authOff = await browser.newContext();
    await authOff.route("**/ui/api/overview", async route => {
      const response = await route.fetch(); const body = await response.json();
      body.config.auth_disabled = true; await route.fulfill({ response, json: body });
    });
    const authOffPage = await authOff.newPage(); authOffPage.setDefaultTimeout(7000);
    await ready(authOffPage, "#/options", "Options", ".options-form");
    await check("auth-disabled configuration reports message authentication Off", async () => {
      await authOffPage.locator("#envchip .pill").waitFor();
      assert.equal((await authOffPage.locator("#envchip").textContent()).trim(), "Message authentication: Off");
    });
    await authOff.close();

    const copySuccess = await browser.newContext();
    await copySuccess.addInitScript(() => Object.defineProperty(navigator, "clipboard", {
      configurable: true, value: { writeText: text => { window.__copiedHash = text; return Promise.resolve(); } },
    }));
    const copyPage = await copySuccess.newPage(); copyPage.setDefaultTimeout(7000);
    await ready(copyPage, "#/policy", "Decision Rules", "#rules-hash");
    await check("Decision Rules shows and copies the full labelled fingerprint", async () => {
      const hash = (await copyPage.locator("#rules-hash").textContent()).trim();
      assert.match(hash, /^[a-f0-9]{64}$/);
      assert.equal((await copyPage.locator(".policy-hash-label").textContent()).trim(), "Rules fingerprint");
      await copyPage.getByRole("button", { name: "Copy rules fingerprint" }).click();
      await copyPage.getByText("Rules fingerprint copied.", { exact: true }).waitFor();
      assert.equal(await copyPage.evaluate(() => window.__copiedHash), hash);
    });
    await copySuccess.close();

    const copyFailure = await browser.newContext();
    await copyFailure.addInitScript(() => Object.defineProperty(navigator, "clipboard", {
      configurable: true, value: { writeText: () => Promise.reject(new Error("denied")) },
    }));
    const failPage = await copyFailure.newPage(); failPage.setDefaultTimeout(7000);
    await ready(failPage, "#/policy", "Decision Rules", "#rules-hash");
    await check("clipboard failure selects the full fingerprint for manual copy", async () => {
      const hash = (await failPage.locator("#rules-hash").textContent()).trim();
      await failPage.getByRole("button", { name: "Copy rules fingerprint" }).click();
      await failPage.getByText(/selected.*copy it manually/i).waitFor();
      assert.equal(await failPage.evaluate(() => getSelection().toString()), hash);
    });
    await copyFailure.close();
  } finally {
    if (browser) await browser.close();
    const passed = results.filter(Boolean).length;
    console.log(`RESULT ${passed}/${results.length} checks passed`);
    if (passed !== results.length) process.exitCode = 1;
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
