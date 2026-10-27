/* Real templates + browser APIs; no production account, socket or database. */
const {chromium} = require('playwright');
const http = require('http'), fs = require('fs'), path = require('path'), cp = require('child_process'), crypto = require('crypto');
const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root,'.venv/bin/python')),[path.join(__dirname,'render_workspace_mobile_ui_fixtures.py')],{cwd:root,encoding:'utf8'}));
let failAck=false, receipts=[], requests=[], payloads=[], intakeCount=0;
const prefix='/'+ 'a'.repeat(40), rid='d'.repeat(32);
function assert(v,m){if(!v)throw Error(m);}
const server=http.createServer(async(req,res)=>{
  const pathname=new URL(req.url,'http://fixture').pathname;
  const relative=pathname.startsWith(prefix)?pathname.slice(prefix.length):pathname;
  if(relative==='/settings/diagnostics/client-events'){
    intakeCount++;let text='';for await(const chunk of req)text+=chunk;
    let events=JSON.parse(text).events;payloads.push(events);receipts.push(...events);requests.push(pathname);
    res.setHeader('Content-Type','application/json');res.statusCode=failAck?503:200;
    return res.end(JSON.stringify({accepted:failAck?0:events.length,accepted_event_ids:failAck?[]:events.map(e=>e.event_id)}));
  }
  if(relative==='/history'||relative==='/projects'||relative==='/projects/history'){
    res.setHeader('Content-Type','text/html; charset=utf-8');res.setHeader('Server-Timing',`diagnostics;desc="request-id=${rid}"`);return res.end(fixtures.history);
  }
  if(relative.startsWith('/static/')){
    const file=path.resolve(root,relative.slice(1));if(file.startsWith(root+'/static/')&&fs.existsSync(file)){res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream');return res.end(fs.readFileSync(file));}
  }
  if(relative==='/api/check'){let body='';for await(const chunk of req)body+=chunk;res.setHeader('X-Request-ID',rid);res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({method:req.method,body,custom:req.headers['x-custom']}));}
  if(relative==='/login/status'){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({authenticated:true,method:'passkey'}));}
  if(relative.endsWith('/status')){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({html:''}));}
  if(relative==='/sw.js'){res.setHeader('Content-Type','application/javascript');return res.end('');}
  res.statusCode=404;res.end();
});
server.on('upgrade',(req,socket)=>{const key=crypto.createHash('sha1').update(req.headers['sec-websocket-key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${key}\r\n\r\n`);socket.on('error',()=>{});socket.on('data',()=>{});setTimeout(()=>{socket.end(Buffer.from([0x88,0x02,0x03,0xe8]));},150);});
async function until(fn,label){const until=Date.now()+15000;while(Date.now()<until){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw Error('timeout: '+label);}
async function queueState(page){return page.evaluate(()=>new Promise((resolve,reject)=>{
  const request=indexedDB.open('codex-workbench-diagnostics-v2',1);
  request.onsuccess=()=>{const db=request.result,tx=db.transaction('events','readonly'),read=tx.objectStore('events').getAll();
    read.onsuccess=()=>{resolve({version:db.version,stores:[...db.objectStoreNames],events:read.result});db.close();};read.onerror=()=>reject(read.error);};
  request.onerror=()=>reject(request.error);
}));}
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const base=`http://127.0.0.1:${server.address().port}`;const browser=await chromium.launch({headless:true});
try{
 const context=await browser.newContext({viewport:{width:393,height:852}});let page=await context.newPage();
 await page.goto(base+'/history');
 await until(()=>receipts.some(e=>e.type==='page_load'&&e.data.request_id===rid),'document correlation');
 await page.locator('.nav a[href="/projects"]').click();await page.waitForURL('**/projects');
 await page.locator('.nav a[href="/projects/history"]').click();await page.waitForURL('**/projects/history');
 await until(()=>receipts.some(e=>e.type==='navigation'&&e.data.path==='/projects'),'real bottom navigation');
 const result=await page.evaluate(async()=>{const request=new Request('/api/check',{method:'POST',headers:{'X-Custom':'preserved'},body:'unchanged-body'});return (await fetch(request)).json();});
 assert(result.body==='unchanged-body'&&result.method==='POST'&&result.custom==='preserved','Request semantics changed');
 await until(()=>receipts.some(e=>e.type==='fetch'&&e.data.request_id===rid),'fetch correlation');
 failAck=true; // Keep v1 upgrade/replay records durable until both checks finish.
 await page.evaluate(()=>new Promise((resolve,reject)=>{const q=indexedDB.open('codex-workbench-diagnostics-v2',1);q.onsuccess=()=>{const db=q.result,tx=db.transaction('events','readwrite'),s=tx.objectStore('events');s.put({event_id:'legacy-queue-event-0001',ts:'2000-01-01T00:00:00.000Z',type:'click',page_id:'legacy-page-event-0001',tab_id:'legacy-tab-event-0001',action_id:'legacy-action-event-0001',seq:1,data:{id:'legacy'}});tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>reject(tx.error);};q.onerror=()=>reject(q.error);}));
 await page.evaluate(()=>codexDiagnostic('click',{id:'forces-v1-lazy-size'}));
 await until(async()=>{const q=await queueState(page),legacy=q.events.find(e=>e.event_id==='legacy-queue-event-0001');return legacy&&legacy._dq;},'v1 queue lazy size');
 let queue=await queueState(page),legacy=queue.events.find(e=>e.event_id==='legacy-queue-event-0001');
 assert(queue.version===1&&queue.stores.length===1&&queue.stores[0]==='events','diagnostic queue schema changed');
 assert(legacy._dq.bytes===Buffer.byteLength(JSON.stringify(legacy)),'legacy record did not receive exact cached JSON byte size');
 await page.evaluate(()=>new Promise((resolve,reject)=>{const q=indexedDB.open('codex-workbench-diagnostics-v2',1);q.onsuccess=()=>{const db=q.result,tx=db.transaction('events','readwrite'),s=tx.objectStore('events');s.put({event_id:'legacy-queue-event-0001',ts:'2000-01-01T00:00:00.000Z',type:'click',page_id:'legacy-page-event-0001',tab_id:'legacy-tab-event-0001',action_id:'legacy-action-event-0001',seq:2,data:{id:'legacy-replayed-with-more-bytes'}});tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>reject(tx.error);};q.onerror=()=>reject(q.error);}));
 await page.evaluate(()=>codexDiagnostic('click',{id:'forces-overwrite-recount'}));
 await until(async()=>{const q=await queueState(page),event=q.events.find(e=>e.event_id==='legacy-queue-event-0001');return event&&event._dq&&event.data.id==='legacy-replayed-with-more-bytes';},'same UUID overwrite recount');
 queue=await queueState(page);legacy=queue.events.find(e=>e.event_id==='legacy-queue-event-0001');assert(legacy._dq.bytes===Buffer.byteLength(JSON.stringify(legacy)),'same UUID replay retained stale cached byte size');
 failAck=false;
 await page.evaluate(()=>{setTimeout(()=>{throw Error('fixture-boom token=NEVER_PERSIST');},0);});
 await until(()=>receipts.some(e=>e.type==='js_error'&&e.data.stack.includes('fixture-boom')),'JS full stack');
 assert(!JSON.stringify(receipts).includes('NEVER_PERSIST'),'secret persisted');
 await page.evaluate(()=>{
   codexDiagnostic('fetch',{url:'/login?pwa=manifest&entry=%2F'+'s'.repeat(32),message:'encoded-entry-fixture'});
   codexDiagnostic('fetch',{url:'/login?%74oken=ENCODED_CREDENTIAL_FIXTURE',message:'encoded-key-fixture'});
   codexDiagnostic('fetch',{url:'/login?next=%2Flogin%3Ftoken%3DNESTED_CREDENTIAL_FIXTURE',message:'nested-query-fixture'});
   codexDiagnostic('fetch',{url:'/login?next=https%3A%2F%2Fuser%40name%3ANESTED_CREDENTIAL_FIXTURE%40example.test',message:'nested-userinfo-fixture'});
 });
 await until(()=>receipts.some(e=>e.data.message==='nested-userinfo-fixture'),'encoded credentials projected');
 const ws=await page.evaluate(()=>new Promise(resolve=>{let noNew=false;try{WebSocket('ws://bad');}catch(_){noNew=true;}const w=new WebSocket('ws://'+location.host+'/'+'s'.repeat(32)+'/ws');w.onopen=()=>resolve({instance:w instanceof WebSocket,constant:WebSocket.OPEN,noNew});}));
 assert(ws.instance&&ws.constant===1&&ws.noNew,'WebSocket semantics changed');
 await until(()=>receipts.some(e=>e.type==='ws'&&e.data.phase==='close'),'WS lifecycle');
 assert(!JSON.stringify(receipts).includes('s'.repeat(32))&&!JSON.stringify(receipts).includes('ENCODED_CREDENTIAL_FIXTURE')&&!JSON.stringify(receipts).includes('NESTED_CREDENTIAL_FIXTURE'),'encoded/nested entry/key or ws secret persisted');
 failAck=true;await page.evaluate(()=>codexDiagnostic('js_error',{message:'retained-on-503'}));
 await until(()=>receipts.some(e=>e.data.message==='retained-on-503'),'503 send');
 await context.setOffline(true);await page.evaluate(()=>codexDiagnostic('js_error',{message:'offline-reopen'}));await page.waitForTimeout(300);await page.close();
 failAck=false;await context.setOffline(false);page=await context.newPage();await page.goto(base+prefix+'/history');
 await until(()=>receipts.some(e=>e.data.message==='offline-reopen'),'offline reopen replay');
 await until(()=>requests.includes(prefix+'/settings/diagnostics/client-events'),'prefixed intake');
 const second=await context.newPage();await second.goto(base+'/history');await context.setOffline(true);
 await Promise.all([page.evaluate(()=>{for(let i=0;i<60;i++)codexDiagnostic('click',{id:'tab-A-'+i});}),second.evaluate(()=>{for(let i=0;i<60;i++)codexDiagnostic('click',{id:'tab-B-'+i});})]);await page.waitForTimeout(500);await context.setOffline(false);
 await until(()=>new Set(receipts.filter(e=>/^tab-[AB]-/.test(e.data.id||'')).map(e=>e.data.id)).size===120,'two tab transactional replay');
 // Abort a real store transaction after the module queues its put. The
 // uncommitted event must stay out of IDB, survive the emergency fallback,
 // reopen with its original ID and be deleted only by the normal server ACK.
 await context.setOffline(true);
 await page.evaluate(()=>{
   const original=IDBObjectStore.prototype.put;
   IDBObjectStore.prototype.put=function(event){
     const result=original.apply(this,arguments);
     if(event.data&&event.data.id==='forced-transaction-abort'){
       window.__abortedFixtureId=event.event_id;
       IDBObjectStore.prototype.put=original;this.transaction.abort();
     }
     return result;
   };
   codexDiagnostic('click',{id:'forced-transaction-abort'});
 });
 await page.waitForFunction(()=>!!window.__abortedFixtureId);
 const abortedId=await page.evaluate(()=>window.__abortedFixtureId);
 await page.waitForTimeout(100);
 assert(!(await queueState(page)).events.some(event=>event.event_id===abortedId),'aborted diagnostic transaction committed a partial event');
 await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide')));
 await page.close();await context.setOffline(false);page=await context.newPage();await page.goto(base+'/history');
 await until(()=>receipts.some(event=>event.event_id===abortedId),'aborted event emergency replay with original ID');
 await until(async()=>!(await queueState(page)).events.some(event=>event.event_id===abortedId),'replayed aborted event ACK deletion');
 await context.setOffline(true);
 // Each burst stays below the staged cap, but the committed total exceeds the
 // IDB cap. A single synchronous burst may correctly trim in staging before
 // IDB sees it; that distinct behavior is covered by the startup pressure test.
 await page.evaluate(()=>{for(let i=0;i<60;i++)codexDiagnostic('js_error',{message:'capacity-probe-'+i,stack:'x'.repeat(45000)});});
 await until(async()=>(await queueState(page)).events.some(event=>event.data.message==='capacity-probe-59'),'first capacity burst really committed');
 await page.evaluate(()=>{for(let i=60;i<100;i++)codexDiagnostic('js_error',{message:'capacity-probe-'+i,stack:'x'.repeat(45000)});});
 await until(async()=>(await queueState(page)).events.some(event=>event.data.message==='capacity-probe-99'),'second capacity burst really committed');
 queue=await queueState(page);const cap=3*1024*1024-1024;
 assert(queue.events.reduce((sum,event)=>sum+Buffer.byteLength(JSON.stringify(event)),0)<=cap,'stored queue exceeded hard JSON byte cap');
 assert(queue.events.every(event=>event._dq&&event._dq.bytes===Buffer.byteLength(JSON.stringify(event))),'stored queue has an invalid cached byte size');
 await context.setOffline(false);
 await until(()=>receipts.some(e=>e.type==='gap'&&e.data.reason==='queue_capacity'&&e.data.count>0),'capacity loss explicitly recorded');
 assert(payloads.every(events=>events.every(event=>!Object.prototype.hasOwnProperty.call(event,'_dq'))),'private queue metadata leaked onto the wire');
 const before=intakeCount;await page.waitForTimeout(1500);assert(intakeCount-before<15,'self-report recursion');
 await context.close();console.log('diagnostics Playwright passed: navigation, correlation, Request/WS compatibility, errors, secret masking, 503 retention, offline reopen, prefix, concurrent tabs, explicit capacity gaps');
}finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
