/* Push-only worker + native navigation + old-worker upgrade, all synthetic. */
const {chromium}=require('playwright');
const http=require('http'),fs=require('fs'),path=require('path'),vm=require('vm');
const root=path.resolve(__dirname,'..'),source=fs.readFileSync(path.join(root,'static/service-worker.js'),'utf8');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
function assert(v,m){if(!v)throw Error(m);}
async function until(fn,label){for(let n=0;n<150;n++){if(await fn())return;await delay(50);}throw Error('timeout: '+label);}
let old=false,aborted=0,receipts=[];
const server=http.createServer(async(req,res)=>{
 const u=new URL(req.url,'http://fixture');res.setHeader('Cache-Control','no-store');
 if(u.pathname==='/sw.js'){res.setHeader('Content-Type','application/javascript');return res.end(old?"self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(clients.claim()));self.addEventListener('fetch',e=>e.respondWith(fetch(e.request)));":source);}
 if(u.pathname==='/static/diagnostics.js'){res.setHeader('Content-Type','application/javascript');return res.end(fs.readFileSync(path.join(root,'static/diagnostics.js')));}
 if(u.pathname==='/broken'){req.socket.destroy();return;}
 if(u.pathname==='/slow'){req.on('aborted',()=>aborted++);return setTimeout(()=>{if(!res.destroyed)res.end('<p>late</p>');},1300);}
 if(u.pathname==='/settings/diagnostics/client-events'){let body='';for await(const c of req)body+=c;const events=JSON.parse(body).events;receipts.push(...events);res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({accepted_event_ids:events.map(e=>e.event_id)}));}
 if(u.pathname==='/echo'){let body='';for await(const c of req)body+=c;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({body,custom:req.headers['x-custom']}));}
 res.setHeader('Content-Type','text/html');res.end('<!doctype html><p>synthetic workbench</p>');
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+server.address().port;
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext();let page=await context.newPage();await page.goto(base+'/home');
  await page.evaluate(async()=>{await navigator.serviceWorker.register('/sw.js',{updateViaCache:'none'});await navigator.serviceWorker.ready;});
  await until(()=>page.evaluate(()=>!!navigator.serviceWorker.controller),'initial worker control');
  let error='';try{await page.goto(base+'/broken');}catch(e){error=e.message;}
  assert(error.includes('ERR_EMPTY_RESPONSE')&&!error.includes('ERR_FAILED'),'worker must preserve the native connection error');
  await page.close();page=await context.newPage();await page.goto(base+'/home');
  const request=page.goto(base+'/slow').catch(e=>e.message);await delay(100);await page.goto(base+'/home');
  assert(String(await request).includes('ERR_ABORTED'),'second navigation must cancel the old one');
  await until(()=>aborted===1,'native upstream abort');
  const echo=await page.evaluate(async()=> (await fetch('/echo',{method:'POST',headers:{'X-Custom':'preserved'},body:'unchanged'})).json());
  assert(echo.body==='unchanged'&&echo.custom==='preserved','push worker changed an ordinary POST');
  await context.close();

  old=true;const legacy=await browser.newContext();page=await legacy.newPage();await page.goto(base+'/home');
  await page.evaluate(async()=>{await navigator.serviceWorker.register('/sw.js');await navigator.serviceWorker.ready;});
  await until(()=>page.evaluate(()=>!!navigator.serviceWorker.controller),'legacy control');
  error='';try{await page.goto(base+'/broken');}catch(e){error=e.message;}
  assert(error.includes('ERR_FAILED'),'upgrade fixture must actually intercept navigation first');
  await page.close();page=await legacy.newPage();await page.goto(base+'/home');
  await page.evaluate(()=>new Promise((resolve,reject)=>{
   const r=indexedDB.open('codex-workbench-diagnostics-v2',1);r.onupgradeneeded=()=>r.result.createObjectStore('events',{keyPath:'event_id'}).createIndex('ts','ts');
   r.onsuccess=()=>{const db=r.result,t=db.transaction('events','readwrite');t.objectStore('events').put({event_id:'legacy-worker-navigation-0001',ts:new Date().toISOString(),type:'fetch',page_id:'legacy-worker-page-0001',tab_id:'legacy-worker-tab-0001',action_id:'legacy-worker-action-0001',seq:1,data:{context:'service_worker',phase:'error',message:'Failed to fetch',url:'/home'}});t.oncomplete=()=>{db.close();resolve();};t.onerror=()=>reject(t.error);};
  }));
  old=false;
  await page.evaluate(()=>new Promise(async(resolve,reject)=>{navigator.serviceWorker.addEventListener('controllerchange',()=>resolve(),{once:true});try{await(await navigator.serviceWorker.getRegistration()).update();}catch(e){reject(e);}}));
  error='';try{await page.goto(base+'/broken');}catch(e){error=e.message;}
  assert(error.includes('ERR_EMPTY_RESPONSE'),'updated installed PWA still uses its old fetch proxy');
  await page.close();page=await legacy.newPage();await page.goto(base+'/home');await page.addScriptTag({url:base+'/static/diagnostics.js'});
  await until(()=>receipts.some(e=>e.event_id==='legacy-worker-navigation-0001'),'old worker evidence retained and uploaded');
  await legacy.close();

  // Execute unchanged push/click handlers against local stand-ins, without
  // requesting notification permission or delivering a real notification.
  const handlers={},shown=[],navigated=[],work=[];let claimed=0,skipped=0;
  const prefix='/'+ 'a'.repeat(40),scope='https://fixture.test'+prefix+'/';
  const self={location:{origin:'https://fixture.test'},addEventListener:(type,fn)=>handlers[type]=fn,skipWaiting:()=>{skipped++;},registration:{scope,showNotification:(title,options)=>{shown.push({title,options});return Promise.resolve();}}};
  const client={url:scope+'home',navigate:async url=>{navigated.push(url);return client;},focus:async()=>{}};
  vm.runInNewContext(source,{self,clients:{claim:async()=>{claimed++;},matchAll:async()=>[client],openWindow:async url=>navigated.push(url)},URL,Promise});
  assert(!handlers.fetch,'push-only worker unexpectedly reintroduced a fetch handler');
  handlers.install({});handlers.activate({waitUntil:p=>work.push(p)});
  handlers.push({data:{json:()=>({title:'fixture',final_text:'done',conversation_id:'conversation-fixture',turn_id:'turn-fixture'})},waitUntil:p=>work.push(p)});
  handlers.notificationclick({notification:{data:{conversation_id:'conversation-fixture',turn_id:'turn-fixture'},close(){}},waitUntil:p=>work.push(p)});
  await Promise.all(work);assert(skipped===1&&claimed===1&&shown.length===1,'install/activation/push behavior changed');
  assert(new URL(navigated[0],scope).href===scope+'conversation/conversation-fixture?turn=turn-fixture','notification click lost its protected scope/target');
  console.log('push-only PWA passed: native errors/abort, unchanged POST, real old-worker upgrade, queued evidence replay, push/click behavior');
 }finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;server.closeAllConnections();server.close();});
