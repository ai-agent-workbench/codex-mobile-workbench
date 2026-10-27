/* Real shells + actual collector, entirely localhost/synthetic and bounded. */
const {chromium}=require('playwright'),http=require('http'),fs=require('fs'),path=require('path'),cp=require('child_process');
const root=path.resolve(__dirname,'..');
const render=`import json\nfrom jinja2 import Environment,FileSystemLoader,select_autoescape\ne=Environment(loader=FileSystemLoader('templates'),autoescape=select_autoescape())\ne.globals['static_url']=lambda p:p+'?v=fixture'\nbody='<button id="ready" onclick="this.textContent=123">ready</button>'\nprint(json.dumps({'home':e.get_template('base.html').render(title='startup fixture',content=body),'login':e.get_template('login.html').render(error=None),'passkey':e.get_template('passkey_login.html').render(mode='login',action='登录',title='登录',return_to='/home')}))`;
const shells=JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root,'.venv/bin/python')),['-c',render],{cwd:root,encoding:'utf8'}));
const wait=ms=>new Promise(r=>setTimeout(r,ms));function assert(v,m){if(!v)throw Error(m);}
async function until(fn,label){for(let i=0;i<200;i++){if(await fn())return;await wait(50);}throw Error('timeout: '+label);}
const receipts=[];
const server=http.createServer(async(req,res)=>{
 const u=new URL(req.url,'http://fixture');res.setHeader('Cache-Control','no-store');
 if(u.pathname==='/settings/diagnostics/client-events'){let b='';for await(const c of req)b+=c;const events=JSON.parse(b).events;receipts.push(...events);res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({accepted_event_ids:events.map(e=>e.event_id)}));}
 if(u.pathname.startsWith('/static/')){const f=path.resolve(root,'.'+u.pathname);if(f.startsWith(root+'/static/')&&fs.existsSync(f)){res.setHeader('Content-Type',f.endsWith('.js')?'application/javascript':f.endsWith('.css')?'text/css':'image/png');return res.end(fs.readFileSync(f));}}
 if(u.pathname==='/sw.js'){res.setHeader('Content-Type','application/javascript');return res.end(fs.readFileSync(path.join(root,'static/service-worker.js')));}
 res.setHeader('Content-Type','text/html');res.setHeader('Server-Timing','diagnostics;desc="request-id=fixture-startup-request-0001"');
 const name=u.pathname==='/login'?'login':u.pathname==='/passkey'?'passkey':'home';res.end(shells[name]);
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+server.address().port,browser=await chromium.launch({headless:true});
 try{
  // A slow diagnostic OR reading-preferences resource cannot hold the page or
  // its native controls hostage. The synchronous boot trace survives alone.
  const delayed=await browser.newContext({viewport:{width:391,height:849}}),held=[];
  await delayed.addInitScript(()=>{
   const worker=navigator.serviceWorker,register=worker.register.bind(worker);window.fixtureWorkerRegistrations=[];
   worker.register=function(url,options){window.fixtureWorkerRegistrations.push({url,updateViaCache:options&&options.updateViaCache});return register(url,options);};
  });
  await delayed.route('**/static/diagnostics.js*',route=>{held.push(route);});
  await delayed.route('**/static/reading-preferences.js*',route=>{held.push(route);});
  let page=await delayed.newPage();
  for(const target of ['/home','/login','/passkey']){
   await page.goto(base+target,{waitUntil:'domcontentloaded',timeout:5000});
   assert(await page.locator('main').isVisible(),'main content blocked by optional collector: '+target);
   if(target==='/home'){await page.locator('#ready').click();assert(await page.locator('#ready').innerText()==='123','page not interactive without collector');}
   const marker=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('codex-startup-v1')));
   assert(marker.phase==='dom_ready'&&marker.path===target,'no independent early boot phase for '+target);
   if(target!=='/login'){
    const calls=await page.evaluate(()=>window.fixtureWorkerRegistrations);
    assert(calls.some(call=>call.url==='/sw.js'&&call.updateViaCache==='none'),'real shell did not register push-only SW with cache-independent update: '+target);
    const registration=await page.evaluate(async()=>{const value=await navigator.serviceWorker.ready;return {active:!!value.active,updateViaCache:value.updateViaCache};});
    assert(registration.active&&registration.updateViaCache==='none','registered shell worker not active: '+target);
   }
  }
  await page.evaluate(()=>{for(let i=0;i<120;i++)window.dispatchEvent(new ErrorEvent('error',{error:new Error('token=DO_NOT_KEEP'),filename:location.origin+'/static/test.js?token=DO_NOT_KEEP',lineno:i}));});
  const early=await page.evaluate(()=>sessionStorage.getItem('codex-startup-v1'));
  assert(early.length<=12288&&!early.includes('DO_NOT_KEEP'),'early boot record is unbounded or retains credential-like error data');
  assert(JSON.parse(early).events.some(e=>e.type==='gap'),'early ring overflow has no explicit gap');
  for(const route of held)await route.abort().catch(()=>{});await delayed.close();

  const context=await browser.newContext();await context.addInitScript(()=>{
   if(location.pathname==='/storage-blocked')Object.defineProperty(window,'indexedDB',{value:{open(){return {};}}});
   if(location.pathname==='/resume')sessionStorage.setItem('codex-startup-v1',JSON.stringify({page_id:'prior-startup-page-0001',path:'/conversation/fixture',phase:'hidden',ts:new Date(Date.now()-60000).toISOString(),visibility:'hidden',version:'startup-v1',events:[]}));
  });
  page=await context.newPage();await page.goto(base+'/resume');
  await until(()=>receipts.some(e=>e.data.context==='startup'&&e.data.phase==='previous_session_unobserved'),'prior session evidence');
  const resumed=receipts.find(e=>e.data.phase==='previous_session_unobserved');
  assert(resumed.data.reason==='end_not_observed'&&resumed.data.previous_phase==='hidden','ordinary background interruption mislabeled as a crash');
  await until(()=>receipts.some(e=>e.type==='page_load'&&e.data.request_id==='fixture-startup-request-0001'),'async document correlation');
  const pageId=await page.evaluate(()=>window.CodexBootTrace.pageId);
  await until(()=>receipts.some(e=>e.page_id===pageId&&e.data.phase==='bootstrap'),'boot timeline ingestion');
  await until(()=>page.evaluate(()=>JSON.parse(sessionStorage.getItem('codex-startup-v1')).events.length===0),'server ACK clears boot backlog only after receipt');

  await page.goto(base+'/storage-blocked');await page.waitForFunction(()=>typeof codexDiagnostic==='function');
  await page.locator('#ready').click();assert(await page.locator('#ready').innerText()==='123','IDB open blocks business UI');
  await context.setOffline(true);
  const cdp=await context.newCDPSession(page);await cdp.send('HeapProfiler.collectGarbage');const before=await cdp.send('Runtime.getHeapUsage');
  await page.evaluate(()=>{for(let i=0;i<600;i++)codexDiagnostic('js_error',{message:'bounded-pressure-'+i,stack:'x'.repeat(45000)});window.dispatchEvent(new PageTransitionEvent('pagehide'));});
  await cdp.send('HeapProfiler.collectGarbage');const after=await cdp.send('Runtime.getHeapUsage');
  assert(after.usedSize-before.usedSize<20*1024*1024,'stalled storage retained an unbounded event burst');
  const backups=await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.startsWith('codex-diagnostics-backup-v2:')).map(k=>localStorage.getItem(k)));
  assert(backups.length&&backups.every(v=>new TextEncoder().encode(v).length<=64*1024),'emergency backup exceeded 64KiB');
  assert(backups.some(v=>JSON.parse(v).some(e=>e.data.reason==='emergency_backup_capacity')),'bounded emergency loss was silent');
  await page.waitForTimeout(2300);await context.setOffline(false);
  await until(()=>receipts.some(e=>e.type==='gap'&&e.data.reason==='indexeddb_open_timeout'),'storage deadline evidence');
  await until(()=>receipts.some(e=>e.type==='gap'&&e.data.reason==='staged_queue_capacity'),'bounded staging loss evidence');
  await page.goto(base+'/recover');
  await until(()=>receipts.some(e=>e.type==='gap'&&e.data.reason==='emergency_backup_capacity'),'emergency capacity marker reaches backend after recovery');
  await context.close();
  console.log('PWA startup passed: optional assets never gate content, bounded early trace, safe resume evidence, async correlation/ACK, stalled storage/heap cap, emergency replay');
 }finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;server.closeAllConnections();server.close();});
