// The former preview runner retains asynchronous editor regressions against server saves.
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {fixture}=require('./check_console_live_configuration.cjs');
const base=process.argv[2]||'http://127.0.0.1:55717/ui';
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve}};
(async()=>{let browser;const results=[];
const check=async(name,fn)=>{try{await fn();results.push(true);console.log('PASS '+name)}catch(e){results.push(false);console.log('FAIL '+name+': '+e.message)}};
try{try{browser=await chromium.launch({headless:true})}catch{browser=await chromium.launch({headless:true,channel:'chrome'})}
const page=await browser.newPage();page.setDefaultTimeout(5000);const state=await fixture(page);
await page.goto(base+'#/options');await page.locator('#operator-credential').fill('intercepted-fixture');await page.getByRole('button',{name:'Use credential',exact:true}).click();
const go=async(hash,selector)=>{await page.evaluate(h=>location.hash=h,hash);await page.locator(selector).waitFor()};
await go('#/policy?rule=approve','#points-editor');
await check('deep-link auto-refresh retains unsaved input identity and focus',async()=>{
  await page.locator('#points-editor [data-config-edit]').click();const input=page.locator('[data-point=website_verified]');await input.fill('778');await input.evaluate(el=>window.__point=el);await input.focus();
  await page.getByLabel('Refresh every 5 seconds').check();await input.focus();await page.waitForTimeout(5250);await page.getByLabel('Refresh every 5 seconds').uncheck();
  assert.equal(await input.evaluate(el=>window.__point===el),true);assert.equal(await input.inputValue(),'778');
  await go('#/policy?rule=reject','#rule-reject.hl');assert.equal(await input.inputValue(),'778');assert.equal(await page.locator('#rule-approve.hl').count(),0);
  await page.locator('#points-editor [data-config-cancel]').click();
});
await check('obsolete asynchronous route cannot replace a newly selected route',async()=>{
  await go('#/options','.operator-form');const seen=deferred(),release=deferred();
  await page.route('**/ui/api/policy',async route=>{seen.resolve();await release.promise;await route.fallback()});
  await page.evaluate(()=>location.hash='#/policy');await seen.promise;await go('#/options','.operator-form');release.resolve();await page.waitForTimeout(300);
  assert.equal((await page.locator('#page h1').innerText()).trim(),'Options');await page.unroute('**/ui/api/policy');
});
await go('#/fieldmap','#mapping-editor');
await check('mapping validation rejects duplicate and invalid destination identifiers',async()=>{
  await page.locator('#mapping-editor [data-config-edit]').click();const inputs=page.locator('[data-source-field]'),first=inputs.first();
  await first.fill(await inputs.nth(1).inputValue());await page.locator('[data-config-save]').click();assert.match(await page.locator('#mapping-status').innerText(),/unique/);
  for(const bad of ['', '1Wrong','bad-name','x'.repeat(81)]){await first.fill(bad);await page.locator('[data-config-save]').click();assert.match(await page.locator('#mapping-status').innerText(),/ASCII|start with a letter/)}
  await first.fill('Kept_Draft__c');
});
await check('late company response cannot overwrite newer selected company or input',async()=>{
  const choices=await page.locator('#fmcase option:not([value=""])').evaluateAll(nodes=>nodes.map(n=>n.value));assert.ok(choices.length>1);
  const first=page.locator('[data-source-field]').first(),seen=deferred(),release=deferred(),pattern='**/ui/api/cases/'+choices[0]+'/full';
  await page.route(pattern,async route=>{seen.resolve();await release.promise;await route.fallback()});
  await page.locator('#fmcase').selectOption(choices[0]);await seen.promise;await page.locator('#fmcase').selectOption(choices[1]);await page.waitForTimeout(300);
  const values=await page.locator('[data-map-value]').allTextContents();await first.evaluate(el=>window.__map=el);await first.focus();release.resolve();await page.waitForTimeout(300);
  assert.deepEqual(await page.locator('[data-map-value]').allTextContents(),values);assert.equal(await first.evaluate(el=>el===window.__map&&el===document.activeElement),true);
  assert.equal(await first.inputValue(),'Kept_Draft__c');await page.unroute(pattern);
});
await check('failed company read preserves input, draft, focus and honest value error',async()=>{
  const selected=await page.locator('#fmcase').inputValue(),pattern='**/ui/api/cases/'+selected+'/full';
  await page.route(pattern,route=>route.fulfill({status:503,json:{detail:'fixture offline'}}));const input=page.locator('[data-source-field]').first();
  await page.locator('#fmcase').selectOption(selected);await page.locator('#mapping-status').getByText(/Values could not load/).waitFor();await input.focus();await page.evaluate(()=>route());
  assert.equal(await input.inputValue(),'Kept_Draft__c');assert.equal(await input.evaluate(el=>el===document.activeElement),true);assert.match(await page.locator('[data-map-value]').first().innerText(),/could not load/);
  await page.locator('#mapping-editor [data-config-cancel]').click();assert.match(await page.locator('[data-map-value]').first().innerText(),/could not load/);await page.unroute(pattern);
});
await check('mapping revision mismatch suppresses projected values without rebasing a draft',async()=>{
  await page.locator('#mapping-editor [data-config-edit]').click();const input=page.locator('[data-source-field]').first();await input.fill('Revision_Draft__c');state.cfg.revision='9';
  await page.locator('#fmcase').selectOption(await page.locator('#fmcase').inputValue());await page.locator('[data-map-value]').first().getByText(/revision changed/).waitFor();assert.equal(await input.inputValue(),'Revision_Draft__c');
  await page.locator('#mapping-editor [data-config-cancel]').click();
});
await check('old local previews are ignored and never submitted',async()=>{
  await page.evaluate(()=>localStorage.setItem('kyc-preview-v1:points',JSON.stringify({version:1,base:'old',value:{website_verified:999}})));
  await go('#/policy','#points-editor');assert.doesNotMatch(await page.locator('[data-check-row=website_verified]').innerText(),/999/);assert.equal(state.requests.length,0);
});
await check('broker form escapes hostile text and rejected access retains edits',async()=>{
  await page.locator('#broker-editor [data-config-edit]').click();await page.locator('#broker-name').fill('<img src=x onerror="window.__pwned=1">');
  state.reply={status:401,json:{error:'unauthorized',detail:'fixture denied'}};await page.locator('#broker-editor [data-config-save]').click();await page.locator('#broker-status').getByText(/Access refused/).waitFor();
  assert.equal(await page.locator('#broker-name').inputValue(),'<img src=x onerror="window.__pwned=1">');assert.equal(await page.locator('#broker-editor img').count(),0);assert.equal(await page.evaluate(()=>window.__pwned),undefined);
  await go('#/options','.operator-form');await go('#/policy','#broker-entry-form');assert.match(await page.locator('#broker-name').inputValue(),/img src/);state.reply=null;await page.locator('#broker-editor [data-config-cancel]').click();
});
}finally{await browser?.close();console.log('RESULT '+results.filter(Boolean).length+'/'+results.length);if(results.some(x=>!x))process.exitCode=1}
})().catch(e=>{console.error(e);process.exitCode=1});
