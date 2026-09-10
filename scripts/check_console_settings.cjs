const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const baseUrl = process.argv[2] || "http://127.0.0.1:55717/ui";
const results = [];
async function check(name, fn) { try { await fn(); results.push(true); console.log(`PASS ${name}`); }
  catch (error) { results.push(false); console.log(`FAIL ${name}: ${error.message}`); } }
async function launch() { try { return await chromium.launch({ headless: true }); } catch (error) {
  if (!/Executable doesn't exist|browserType\.launch/.test(error.message)) throw error;
  return chromium.launch({ headless: true, channel: "chrome" }); } }
async function ready(page, hash = "#/options") { await page.goto(`${baseUrl}${hash}`); await page.locator("#page h1").waitFor(); }
const selectTheme = (page, label) => page.getByLabel(label, { exact: true }).check();
const cleanTitle = title => title.replace(/^(Full stack|Preview) · /, "");

(async () => { let browser;
  try {
    browser = await launch();
    const requests = [], context = await browser.newContext({ colorScheme: "light" });
    const page = await context.newPage(); page.setDefaultTimeout(6000);
    page.on("request", request => requests.push({ method: request.method(), url: request.url() }));
    await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => {
      window.__themeAtDomReady = document.documentElement.getAttribute("data-theme");
    }, { once: true }));
    await ready(page);

    await check("Options is a routed Configuration page, not a popup", async () => {
      assert.equal(await page.getByRole("heading", { name: "Options", level: 1 }).count(), 1);
      assert.equal(cleanTitle(await page.title()), "Options · KYC Tool");
      assert.equal(await page.locator("#settings-panel, #settings-button").count(), 0);
      assert.equal(await page.locator('[data-r="options"]').getAttribute("aria-current"), "page");
      assert.equal(await page.getByRole("group", { name: "Theme" }).count(), 1);
    });
    await check("shared cog outer path is centered on its hole", async () => {
      const c = await page.locator('[data-r="options"] svg').evaluate(svg => { const b = svg.querySelector("path").getBBox();
        const h = svg.querySelector("circle"); return { x: b.x+b.width/2, y: b.y+b.height/2, hx:+h.getAttribute("cx"), hy:+h.getAttribute("cy") }; });
      assert.ok(Math.abs(c.x-c.hx)<.01); assert.ok(Math.abs(c.y-c.hy)<.01);
    });
    await check("fresh System follows OS", async () => {
      assert.equal(await page.getByLabel("System", { exact: true }).isChecked(), true);
      const light = await page.locator("body").evaluate(el => [getComputedStyle(el).backgroundColor,getComputedStyle(el).colorScheme]);
      await page.emulateMedia({ colorScheme:"dark" });
      const dark = await page.locator("body").evaluate(el => [getComputedStyle(el).backgroundColor,getComputedStyle(el).colorScheme]);
      assert.notEqual(light[0],dark[0]); assert.match(light[1],/light/); assert.match(dark[1],/dark/);
    });
    await check("explicit themes override OS and persist", async () => {
      await page.emulateMedia({colorScheme:"light"}); await selectTheme(page,"Dark");
      assert.equal(await page.locator("html").getAttribute("data-theme"),"dark"); await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.locator("html").getAttribute("data-theme"),"dark"); await page.emulateMedia({colorScheme:"dark"}); await selectTheme(page,"Light");
      assert.equal(await page.locator("html").getAttribute("data-theme"),"light"); await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.locator("html").getAttribute("data-theme"),"light");
    });
    await check("System resumes OS following and persists", async () => {
      await selectTheme(page,"System"); assert.equal(await page.locator("html").getAttribute("data-theme"),null);
      assert.equal(await page.evaluate(()=>localStorage.getItem("kyc-theme")),"system"); await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.getByLabel("System",{exact:true}).isChecked(),true);
    });
    await check("saved theme is applied before DOMContentLoaded", async () => {
      await page.evaluate(()=>localStorage.setItem("kyc-theme","dark")); await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.evaluate(()=>window.__themeAtDomReady),"dark");
    });
    await check("another tab updates theme and controls", async () => {
      const other=await context.newPage(); await ready(other); await selectTheme(other,"Light");
      await page.waitForFunction(()=>document.documentElement.dataset.theme==="light"&&document.querySelector('[value=light]')?.checked); await other.close();
    });
    await check("invalid storage falls back safely", async () => {
      await page.evaluate(()=>localStorage.setItem("kyc-theme","sepia")); await page.reload(); await page.locator("#page h1").waitFor();
      assert.equal(await page.locator("html").getAttribute("data-theme"),null); assert.equal(await page.getByLabel("System",{exact:true}).isChecked(),true);
    });
    await check("selection stays local, focused, and sends no writes", async () => {
      const hash=await page.evaluate(()=>location.hash); await page.locator(".options-form").evaluate(el=>window.__optionsForm=el); await selectTheme(page,"Dark");
      assert.equal(await page.evaluate(()=>location.hash),hash); assert.equal(await page.evaluate(()=>document.querySelector(".options-form")===window.__optionsForm),true);
      assert.equal(await page.evaluate(()=>document.activeElement?.value),"dark"); assert.equal(requests.filter(r=>r.url.includes("/ui/api/")&&r.method!=="GET").length,0);
    });
    await check("auto-refresh preserves form and radio focus", async () => {
      await page.getByLabel("Refresh every 5 seconds").check(); await page.getByLabel("Light",{exact:true}).focus(); await page.waitForTimeout(5250);
      assert.equal(await page.evaluate(()=>document.querySelector(".options-form")===window.__optionsForm),true);
      assert.equal(await page.evaluate(()=>document.activeElement?.value),"light"); await page.getByLabel("Refresh every 5 seconds").uncheck();
    });
    await check("history returns to Options with title and current link", async () => {
      await page.locator('[data-r="policy"]').click(); await page.getByRole("heading",{name:"Decision Rules",level:1}).waitFor();
      await page.goBack(); await page.getByRole("heading",{name:"Options",level:1}).waitFor(); assert.equal(cleanTitle(await page.title()),"Options · KYC Tool");
      assert.equal(await page.locator('[data-r="options"]').getAttribute("aria-current"),"page");
    });
    for (const colorScheme of ["light","dark"]) for (const width of [390,768,1024,1199,1260,1440]) {
      await page.emulateMedia({colorScheme}); await page.setViewportSize({width,height:900}); await ready(page,width<1024?"#/overview":"#/options");
      if(width<1024){await page.locator("#menubtn").click(); await page.locator('[data-r="options"]').waitFor({state:"visible"});
        await page.waitForTimeout(250); await page.locator('[data-r="options"]').click(); await page.getByRole("heading",{name:"Options",level:1}).waitFor();}
      await selectTheme(page,colorScheme==="light"?"Light":"Dark");
      await check(`${width}px Options fits in ${colorScheme}`,async()=>{assert.match(await page.locator("html").evaluate(el=>getComputedStyle(el).colorScheme),new RegExp(colorScheme));
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
        const box=await page.locator(".options-form").boundingBox(); assert.ok(box&&box.x>=0&&box.x+box.width<=width);
        if(width<1024)assert.equal(await page.locator("aside").evaluate(el=>el.classList.contains("open")),false);});
    }
    const blocked=await browser.newContext(), blockedPage=await blocked.newPage(); blockedPage.setDefaultTimeout(6000);
    await blockedPage.addInitScript(()=>{Object.defineProperty(Storage.prototype,"getItem",{configurable:true,value(){throw Error("blocked read")}});
      Object.defineProperty(Storage.prototype,"setItem",{configurable:true,value(){throw Error("blocked write")}});}); await ready(blockedPage);
    await check("storage exceptions render and disclose failed saves",async()=>{assert.equal(await blockedPage.getByLabel("System",{exact:true}).isChecked(),true);
      await selectTheme(blockedPage,"Dark"); assert.equal(await blockedPage.locator("html").getAttribute("data-theme"),"dark");
      assert.match(await blockedPage.locator("#options-hint").textContent(),/could not be saved/i);}); await blocked.close();
  } finally { if(browser)await browser.close(); const passed=results.filter(Boolean).length; console.log(`RESULT ${passed}/${results.length} checks passed`);
    if(passed!==results.length)process.exitCode=1; }
})().catch(error=>{console.error(error);process.exitCode=1});
