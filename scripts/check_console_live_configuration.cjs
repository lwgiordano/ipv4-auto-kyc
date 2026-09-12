const assert=require("node:assert/strict");
const {chromium}=require("playwright");
const base=process.argv[2]||"http://127.0.0.1:55717/ui";
const clone=x=>JSON.parse(JSON.stringify(x));
async function fixture(page,url=base){
  const policy=await(await page.request.get(url.replace(/\/ui$/,"")+"/ui/api/policy")).json();
  const cfg={active:true,revision:"1",bundle_hash:policy.bundle_hash,threshold:policy.threshold,points:Object.fromEntries(policy.rubric.map(r=>[r.check_type,r.points])),
    mappings:Object.fromEntries(Object.keys(policy.field_sources).map(k=>[k,k])),brokers:policy.broker_entities.map((b,i)=>({id:b.id||"fixture-"+i,name:b.name,policy:b.policy,notes:null,
      ...Object.fromEntries(["aliases","domains","email_domains","org_ids","poc_handles","asns"].map(k=>[k,b[k]||[]]))})),broker_overlaps:[],can_edit:true};
  const state={cfg,requests:[],reply:null,hold:null,unavailableHistory:false};
  await page.route("**/ui/api/**",async route=>{
    const req=route.request(),path=new URL(req.url()).pathname;
    if(path==="/ui/api/configuration"&&req.method()==="GET")return route.fulfill({json:cfg});
    if(req.method()!=="GET"){
      state.requests.push({path,headers:req.headers(),body:req.postData(),method:req.method()});
      if(!path.startsWith("/ui/api/configuration/"))return route.fulfill({status:202,json:{status:"queued",run_id:"fixture-run"}});
      if(state.hold)await state.hold;if(state.reply)return route.fulfill(state.reply);
      const data=req.postDataJSON(),section=path.split("/").pop();
      if(data.expected_revision!==cfg.revision)return route.fulfill({status:409,json:{error:"configuration_conflict",detail:"Configuration changed; reload before saving.",expected_revision:data.expected_revision,current_revision:cfg.revision}});
      cfg[section]=clone(data.value);cfg.revision=String(Number(cfg.revision)+1);
      return route.fulfill({json:{revision:cfg.revision,current_revision:cfg.revision,changed:true,replayed:false,configuration:clone(cfg)}});
    }
    if(path==="/ui/api/policy")return route.fulfill({json:{...policy,mapping_revision:cfg.revision,field_sources:Object.fromEntries(Object.entries(policy.field_sources).map(([k,v])=>[cfg.mappings[k],v]))}});
    if(/^\/ui\/api\/cases\/[^/]+\/full$/.test(path)){
      const response=await route.fetch(),body=await response.json();if(response.status()>=300)return route.fulfill({response});
      body.salesforce=Object.fromEntries(Object.entries(body.salesforce).map(([k,v])=>[cfg.mappings[k]||k,v]));
      body.field_sources=Object.fromEntries(Object.entries(body.field_sources).map(([k,v])=>[cfg.mappings[k]||k,v]));body.mapping_revision=cfg.revision;
      if(state.unavailableHistory)Object.assign(body.score,{items:[],bundle_hash:null,configuration_revision:null,rubric_provenance:"unavailable_legacy_bundle",rubric_scope:"legacy_unrecorded"});
      return route.fulfill({response,json:body});
    }return route.continue();
  });return state;
}
async function run(){
  const results=[];let browser;
  const check=async(name,fn)=>{try{await fn();results.push(true);console.log("PASS "+name)}catch(e){results.push(false);console.log("FAIL "+name+": "+e.message)}};
  try{
    try{browser=await chromium.launch({headless:true})}catch{browser=await chromium.launch({headless:true,channel:"chrome"})}
    const page=await browser.newPage();page.setDefaultTimeout(5000);const state=await fixture(page);
    const go=async hash=>{await page.evaluate(h=>location.hash=h,hash);await page.waitForTimeout(200)};
    const points=page.locator("#points-editor"),input=page.locator("[data-point=website_verified]");
    await page.goto(base+"#/options");await page.locator(".options-form").waitFor();
    await check("credential password input clears after accepting and stays out of storage",async()=>{
      assert.equal(await page.locator("input[type=password]").count(),1);
      await page.locator("#operator-credential").fill("browser-fixture-credential");await page.getByRole("button",{name:"Use credential",exact:true}).click();
      assert.equal(await page.locator("#operator-credential").inputValue(),"");
      assert.doesNotMatch(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage})),/browser-fixture-credential/);
    });
    await go("#/policy");await points.waitFor();
    await check("headers replace preview controls and broker search has a real icon",async()=>{
      assert.doesNotMatch(await page.locator("#broker-editor").innerText(),/undefined|Preview|Apply Entry/);
      assert.equal(await page.getByRole("button",{name:"Save Preview",exact:true}).count(),0);assert.equal(await page.locator("#points-bound").count(),0);
      assert.equal(await points.locator(".header-title [data-tip=rubric]").count(),1);
    });
    await check("points PUT freezes inputs until server confirmation",async()=>{
      await points.locator("[data-config-edit]").click();await input.fill("30");
      let release;state.hold=new Promise(r=>release=r);await points.locator("[data-config-save]").click();await points.getByRole("button",{name:"Saving…",exact:true}).waitFor();
      assert.equal(await input.isDisabled(),true);assert.doesNotMatch(await page.locator("#points-status").innerText(),/^Saved/);
      release();state.hold=null;await page.locator("#points-status").getByText(/^Saved/).waitFor();
      const r=state.requests.at(-1),body=JSON.parse(r.body);assert.equal(r.method,"PUT");assert.equal(body.expected_revision,"1");assert.match(body.request_id,/^[0-9a-f-]{36}$/);
      assert.equal(body.value.website_verified,30);assert.equal(Object.keys(body.value).length,8);assert.equal(r.headers.authorization,"Bearer browser-fixture-credential");
      assert.match(await page.locator("[data-check-row=website_verified]").innerText(),/\+30/);
    });
    await check("validation and refresh preserve draft nodes; Cancel sends nothing",async()=>{
      await points.locator("[data-config-edit]").click();await input.fill("2.5");await points.locator("[data-config-save]").click();
      assert.match(await page.locator("#points-status").innerText(),/whole number/);const invalidFocused=await input.evaluate(el=>el===document.activeElement);await input.fill("777");await input.evaluate(el=>window.__retained=el);await input.focus();await page.evaluate(()=>route());
      assert.equal(await input.evaluate(el=>el===window.__retained&&el===document.activeElement),true);assert.equal(await input.inputValue(),"777");
      const n=state.requests.length;await points.locator("[data-config-cancel]").click();assert.equal(state.requests.length,n);assert.equal(invalidFocused,true,"the invalid website field receives focus");
    });
    await check("stale revision preserves edits until deliberate reload",async()=>{
      await points.locator("[data-config-edit]").click();await input.fill("40");state.cfg.revision="3";await points.locator("[data-config-save]").click();await points.locator("[data-config-reload]").waitFor();
      assert.equal(await input.inputValue(),"40");page.once("dialog",d=>d.accept());await points.locator("[data-config-reload]").click();await points.locator("[data-config-edit]").waitFor();
    });
    await check("unknown outcome retains identical request across navigation",async()=>{
      await points.locator("[data-config-edit]").click();await input.fill("41");state.reply={status:503,json:{error:"configuration_unavailable",detail:"unknown"}};
      await points.locator("[data-config-save]").click();await points.locator("[data-config-retry]").waitFor();const body=state.requests.at(-1).body;
      await go("#/options");await go("#/policy");await points.locator("[data-config-retry]").waitFor();assert.equal(await input.isDisabled(),true);
      state.reply={status:401,json:{error:"unauthorized",detail:"retry credential refused"}};await points.locator("[data-config-retry]").click();await page.waitForTimeout(180);
      const retained=await points.locator("[data-config-retry]").count();const cancel=await points.locator("[data-config-cancel]").count();state.reply=null;
      if(retained){await go("#/options");await page.locator("#operator-credential").fill("replacement-fixture");await page.getByRole("button",{name:"Use credential",exact:true}).click();await go("#/policy");await points.locator("[data-config-retry]").click();await page.locator("#points-status").getByText(/^Saved/).waitFor();assert.equal(state.requests.at(-1).body,body)}
      else if(cancel)await points.locator("[data-config-cancel]").click();
      await go("#/options");await page.locator("#operator-credential").fill("browser-fixture-credential");await page.getByRole("button",{name:"Use credential",exact:true}).click();
      assert.equal(retained,1,"refused retry must retain unknown identity");assert.equal(cancel,0,"unknown cannot offer Cancel");
    });
    await check("mapping save projects renamed destinations and value refresh keeps input identity",async()=>{
      await go("#/fieldmap");const root=page.locator("#mapping-editor");await root.waitFor();await root.locator("[data-config-edit]").click();
      const map=page.locator("[data-source-field=KYC_Status__c]");await map.fill("Review_State__c");await root.locator("[data-config-save]").click();await page.locator("#mapping-status").getByText(/^Saved/).waitFor();
      assert.equal(state.cfg.mappings.KYC_Status__c,"Review_State__c");await root.locator("[data-config-edit]").click();
      const duplicate=page.locator('[data-source-field="KYC_Score__c"]');await duplicate.fill("Review_State__c");await root.locator("[data-config-save]").click();const duplicateFocused=await duplicate.evaluate(el=>el===document.activeElement);await duplicate.fill("KYC_Score__c");
      const target=await page.locator('#fmcase option:not([value=""])').last().getAttribute("value");let release,seen;const called=new Promise(r=>seen=r),hold=new Promise(r=>release=r);
      const pattern="**/ui/api/cases/"+target+"/full";await page.route(pattern,async route=>{seen();await hold;await route.fallback()});
      await page.locator("#fmcase").selectOption(target);await called;await map.fill("During_Fetch__c");await map.evaluate(el=>window.__mapping=el);await map.focus();release();await page.waitForTimeout(300);
      assert.equal(await map.evaluate(el=>el===window.__mapping&&el===document.activeElement),true);assert.equal(await map.inputValue(),"During_Fetch__c");await page.unroute(pattern);
      await root.locator("[data-config-cancel]").click();assert.match(await page.locator("[data-source-row=KYC_Status__c] [data-map-value]").innerText(),/\S/);assert.equal(duplicateFocused,true,"duplicate destination field receives focus");
    });
    await check("broker header Save directly commits complete list with stable IDs and nullable notes",async()=>{
      await go("#/policy");const root=page.locator("#broker-editor");await root.locator("[data-config-edit]").click();const form=page.locator("#broker-entry-form");
      assert.equal(await form.getByRole("button",{name:"Apply Entry"}).count(),0);assert.ok((await form.boundingBox()).y<(await root.locator("table").boundingBox()).y);
      await form.getByLabel("Name",{exact:true}).fill("Fixture Broker");await form.getByLabel("Domains",{exact:true}).fill("fixture.example");await form.getByLabel("Policy",{exact:true}).selectOption("blocked");
      const aliases=form.getByLabel("Aliases",{exact:true});await aliases.fill("duplicate\nduplicate");await root.locator("[data-config-save]").click();const aliasesFocused=await aliases.evaluate(el=>el===document.activeElement);await aliases.fill("");
      const n=state.cfg.brokers.length;await root.locator("[data-config-save]").click();await page.locator("#broker-status").getByText(/^Saved/).waitFor();assert.equal(state.cfg.brokers.length,n+1);
      assert.equal(state.cfg.brokers[0].notes,null);assert.match(state.cfg.brokers.at(-1).id,/^[0-9a-f-]{36}$/);assert.equal(aliasesFocused,true,"invalid alias list receives focus");
    });
    await check("unavailable history does not invent rubric or NaN",async()=>{
      state.unavailableHistory=true;await go("#/case/demo-case-00131");await page.getByText(/Historical rubric unavailable/).waitFor();assert.doesNotMatch(await page.locator("#page").innerText(),/NaN/);state.unavailableHistory=false;
    });
    await check("standalone legends removed and Company Actions retains composer route",async()=>{
      await go("#/integrations");assert.equal(await page.locator(".legend").count(),0);await go("#/composer");await page.locator("#composer-form").waitFor();assert.equal((await page.locator("#page h1").innerText()).trim(),"Company Actions");
      assert.equal(await page.locator(".composer-actions button").first().getAttribute("id"),"c-send");
    });
    await check("credential routes only to same-origin mutations, including Company Actions",async()=>{
      const headers=[];await page.route("**/ui/api/auth-fixture",route=>{headers.push(route.request().headers());return route.fulfill({json:{ok:true},headers:{"Access-Control-Allow-Origin":"*"}})});
      await page.evaluate(async()=>{await consoleFetch("/ui/api/auth-fixture");await consoleFetch("/ui/api/auth-fixture",{method:"POST"});await consoleFetch("https://external.invalid/ui/api/auth-fixture",{method:"POST"})});
      assert.equal(headers[0].authorization,undefined);assert.equal(headers[1].authorization,"Bearer browser-fixture-credential");assert.equal(headers[2].authorization,undefined);
      await page.locator("#c-send").click();await page.getByRole("button",{name:"Confirm Send",exact:true}).click();await page.waitForTimeout(150);assert.equal(state.requests.at(-1).headers.authorization,"Bearer browser-fixture-credential");
    });
    await check("Companies filters combine search and show server total separately",async()=>{
      const requests=[];await page.route("**/ui/api/cases?*",async route=>{const url=new URL(route.request().url());requests.push(url.searchParams);const response=await route.fetch(),body=await response.json();return route.fulfill({response,json:{...body,total:73}})});
      await go("#/cases");await page.locator("#case-filter").selectOption("review");await page.locator("#q").fill("Acme");await page.waitForTimeout(500);
      assert.equal(requests.at(-1).get("filter"),"review");assert.equal(requests.at(-1).get("q"),"Acme");assert.match(await page.locator("#case-count").innerText(),/shown of 73 matching/);
      await page.unroute("**/ui/api/cases?*");
    });
    await check("mobile policy and broker fields remain contained",async()=>{
      const failures=[];for(const theme of ["light","dark"]){await page.emulateMedia({colorScheme:theme});await page.setViewportSize({width:390,height:1137});await go("#/policy");
        if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))failures.push(theme+" policy");
        await page.locator("#broker-editor [data-config-edit]").click();const clipped=await page.locator("#broker-entry-form input,#broker-entry-form textarea,#broker-entry-form select").evaluateAll(ns=>ns.some(n=>n.getBoundingClientRect().right>innerWidth));if(clipped)failures.push(theme+" fields");await page.locator("#broker-editor [data-config-cancel]").click();}
      await page.setViewportSize({width:1280,height:900});assert.deepEqual(failures,[]);
    });
    await check("Clear and reload remove credential",async()=>{
      await go("#/options");await page.locator("#clear-credential").click();assert.match(await page.locator("#operator-status").innerText(),/No operator/);
      await page.locator("#operator-credential").fill("browser-fixture-credential");await page.getByRole("button",{name:"Use credential",exact:true}).click();await page.reload();await page.locator("#operator-status").waitFor();assert.match(await page.locator("#operator-status").innerText(),/No operator/);
      assert.doesNotMatch(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage})),/browser-fixture-credential/);
    });
  }finally{await browser?.close();console.log("RESULT "+results.filter(Boolean).length+"/"+results.length);if(results.some(x=>!x))process.exitCode=1}
}
module.exports={fixture,run};
if(require.main===module)run().catch(e=>{console.error(e);process.exitCode=1});
