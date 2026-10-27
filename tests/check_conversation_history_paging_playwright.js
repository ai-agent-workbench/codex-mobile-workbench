/* Real template + real public projection; localhost synthetic data only. */
const assert = require('assert/strict');
const { chromium } = require('playwright');
const cp = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');
const root = path.resolve(__dirname, '..');
function python(file) { return JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [path.join(__dirname,file)], {cwd:root,encoding:'utf8',maxBuffer:20*1024*1024})); }
const template = python('render_conversation_ui_fixtures.py');
const fixture = python('render_conversation_history_fixtures.py');
let phase = 'initial', failView = false, fullDelay = 0, olderDelay = 0, viewDelay = 0, hangFull = false, fullOverride = null, readFailure = null;
let receiptMode = 'completed', receiptDelay = 0;
const requests = [];
function current() { return phase === 'tail' ? fixture.tail : phase === 'corrected' ? fixture.corrected : phase === 'gap' ? fixture.gap : fixture.pages[0]; }
function full() { return phase === 'gap' ? fixture.gap_full : phase === 'initial' ? fixture.initial : fixture.full; }
function documentHtml() {
  return template.page_shell.replace('<!--fixture-content-->',template.conversation.replace(/conversation-ui/g,'history-fixture')
    .replace(/(<script id="conversation-initial-snapshot" type="application\/json">)[\s\S]*?(<\/script>)/,'$1'+JSON.stringify(fixture.pages[0])+'$2'));
}
const server = http.createServer((req,res)=>{
  const url = new URL(req.url,'http://fixture'); requests.push({path:url.pathname,query:url.search,method:req.method});
  const json = data => { res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data)); };
  const failedRead = () => {
    if (!readFailure) return false;
    const headers = {'Content-Type':'application/json','Cache-Control':'no-store'};
    if (readFailure.retryAfter) headers['Retry-After'] = String(readFailure.retryAfter);
    res.writeHead(readFailure.status,headers);res.end('{"error":"fixture read failure"}');return true;
  };
  if(url.pathname==='/case') {res.writeHead(200,{'Content-Type':'text/html;charset=utf-8'});return res.end(documentHtml());}
  if(url.pathname.startsWith('/static/')) {const file=path.join(root,url.pathname);if(!fs.existsSync(file)){res.writeHead(404);return res.end();}res.writeHead(200,{'Content-Type':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream'});return fs.createReadStream(file).pipe(res);}
  if(url.pathname.endsWith('/view')) {
    if(failedRead())return;
    if(failView){res.writeHead(409,{'Content-Type':'application/json'});return res.end('{"error":"page changed"}');}
    const before = url.searchParams.get('before');
    const pages = phase === 'gap' ? fixture.gap_pages : fixture.pages;
    let data = before ? pages.find((page,index)=>index>0&&pages[index-1].history.cursor===before) : current();
    if(!data){res.writeHead(409,{'Content-Type':'application/json'});return res.end('{"error":"cursor unknown"}');}
    data=structuredClone(before && olderDelay ? fixture.race_older : data);
    const tokens=(url.searchParams.get('pages')||'').split(',');
    data.history.replacements=data.history.replacements.filter(page=>tokens.includes(page.previous));
    if(before && olderDelay) return setTimeout(()=>json(data),olderDelay);
    if(!before && viewDelay) return setTimeout(()=>json(data),viewDelay);
    return json(data);
  }
  if(url.pathname.endsWith('/snapshot')) {if(failedRead())return;if(hangFull)return;const data=structuredClone(fullOverride || full());if(fullDelay)return setTimeout(()=>json(data),fullDelay);return json(data);}
  if(url.pathname.endsWith('/actions/command-1')) {
    if(receiptMode==='missing'){res.writeHead(404,{'Content-Type':'application/json'});return res.end('{"error":"receipt not found"}');}
    const snapshot=structuredClone(fixture.initial);
    const command=snapshot.turns[0].commands.find(command=>command.id==='command-1');
    command.state=receiptMode;
    for(const message of snapshot.messages || [])if(message.id===command.id)message.state=receiptMode;
    const data={command,snapshot};
    if(receiptDelay)return setTimeout(()=>json(data),receiptDelay);
    return json(data);
  }
  if(url.pathname==='/login/status')return json({authenticated:true});
  if(url.pathname.endsWith('/export')) {res.writeHead(200,{'Content-Type':'text/markdown'});return res.end(full().turns.map(turn=>turn.final_text).join('\n'));}
  res.writeHead(204);res.end();
});
async function createPage(browser,port,suffix='',pending=null) {
  const page=await browser.newPage({viewport:{width:390,height:844}});
  page.on('pageerror',error=>console.error('PAGEERROR',error.message));
  page.__receiptResponses=[];page.on('response',response=>{if(response.url().endsWith('/actions/command-1'))page.__receiptResponses.push(response);});
  await page.addInitScript(()=>{window.__historySockets=[];window.__historyRenders=0;window.addEventListener('codex:conversation-snapshot',event=>{window.__historyRenders++;window.__lastHistorySnapshot=event.detail;});window.WebSocket=class{static OPEN=1;static CONNECTING=0;static CLOSING=2;constructor(){this.readyState=1;window.__historySockets.push(this);setTimeout(()=>{if(this.onopen)this.onopen();this.__opened=true;},0);}send(value){if(value==='ping')setTimeout(()=>this.onmessage&&this.onmessage({data:'{"type":"pong"}'}),0);}close(){this.readyState=3;}};});
  if(pending)await page.addInitScript(pending=>{
    const key='codex-conversation-draft:history-fixture';
    localStorage.setItem(key,pending.draft || '第 1 轮问题');
    localStorage.setItem(key+':revision',pending.revision || 'old-draft');
    localStorage.setItem('codex-conversation-pending:history-fixture',JSON.stringify({id:'command-1',text:'第 1 轮问题',composer_value:'第 1 轮问题',draft_revision:'old-draft'}));
  },pending);
  await page.goto(`http://127.0.0.1:${port}/case${suffix}`);
  await page.waitForFunction(()=>document.querySelectorAll('.native-message.agent .native-message-body[data-native-enhanced]').length>=20&&window.__historyRenders>=2&&window.__historySockets.at(-1).__opened);
  return page;
}
async function sync(page) {
  await page.evaluate(()=>{const socket=window.__historySockets.at(-1);socket.onmessage({data:JSON.stringify({type:'conversation.updated',data:{conversation_id:'history-fixture'}})});});
}
async function count(page,n){await page.waitForFunction(expected=>document.querySelectorAll('.native-message.agent').length===expected,n);}
async function loadOlder(page,n){await page.locator('#conversation-history-older').dispatchEvent('click');await count(page,n);await page.waitForFunction(()=>!document.querySelector('#conversation-history-older').disabled);}
(async()=>{let browser;try{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const port=server.address().port;
  browser=await chromium.launch({headless:true});
  let page=await createPage(browser,port);
  await count(page,20);assert.equal(requests.filter(r=>r.path.endsWith('/snapshot')).length,0,'initial paged visit must not fetch the full transcript');
  await loadOlder(page,40);await loadOlder(page,60);await loadOlder(page,65);
  assert.equal(await page.locator('#conversation-history-older').isVisible(),false);
  assert.equal(await page.locator('.native-message.agent[data-turn-id]').evaluateAll(nodes=>new Set(nodes.map(n=>n.dataset.turnId)).size),65);
  await page.evaluate(()=>{
    const node=document.querySelector('[data-turn-id="turn-1"]');window.__firstHistoryNode=node;window.__firstBody=node.querySelector('.native-message-body');
    node.scrollIntoView({block:'start'});window.__anchorTop=node.getBoundingClientRect().top;
    const text=window.__firstBody.querySelector('p').firstChild;const range=document.createRange();range.setStart(text,0);range.setEnd(text,8);getSelection().removeAllRanges();getSelection().addRange(range);window.__selectedText=getSelection().toString();
  });
  phase='tail';await sync(page);await page.waitForFunction(()=>document.querySelector('[data-turn-id="turn-65"]').textContent.includes('尾部增量'));
  const preserved=await page.evaluate(()=>({node:window.__firstHistoryNode===document.querySelector('[data-turn-id="turn-1"]'),body:window.__firstBody===document.querySelector('[data-turn-id="turn-1"] .native-message-body'),selection:getSelection().toString()===window.__selectedText,anchor:Math.abs(window.__firstHistoryNode.getBoundingClientRect().top-window.__anchorTop)}));
  assert.equal(preserved.node,true);assert.equal(preserved.body,true);assert.equal(preserved.selection,true);assert.ok(preserved.anchor<3,JSON.stringify(preserved));
  await page.locator('textarea[name="text"]').fill('保留输入和光标');
  await page.locator('textarea[name="text"]').evaluate(node=>{node.focus();node.setSelectionRange(2,4);});
  phase='corrected';await sync(page);await page.waitForFunction(()=>document.querySelector('[data-turn-id="turn-30"]').textContent.includes('旧页修正'));
  assert.deepEqual(await page.locator('textarea[name="text"]').evaluate(node=>[document.activeElement===node,node.selectionStart,node.selectionEnd]),[true,2,4]);
  assert.equal(await page.evaluate(()=>window.__firstHistoryNode===document.querySelector('[data-turn-id="turn-1"]')),true);
  phase='gap';await sync(page);await count(page,85);
  await loadOlder(page,100);assert.equal(await page.locator('#conversation-history-older').isVisible(),false,'missing reconnect range must remain reachable');
  await page.close();
  // An old history request may contain the active tail. A newer sync wins
  // every overlap, even if the old page arrives last and adds unseen turns.
  phase='initial';page=await createPage(browser,port);olderDelay=3500;
  const olderRequested=page.waitForRequest(request=>request.url().includes('/view?')&&request.url().includes('before='));
  await page.locator('#conversation-history-older').dispatchEvent('click');await olderRequested;
  phase='tail';await sync(page);
  await page.waitForFunction(()=>document.querySelector('[data-turn-id="turn-65"]').textContent.includes('尾部增量'),null,{timeout:7000}).catch(async error=>{console.error('RACEDEBUG',requests.slice(-12),await page.locator('#conversation-note').textContent(),await page.locator('[data-turn-id="turn-65"]').textContent());throw error;});
  await count(page,40);await page.waitForFunction(()=>!document.querySelector('#conversation-history-older').disabled);
  assert.match(await page.locator('[data-turn-id="turn-65"]').textContent(),/尾部增量/,'late older page must not regress the active tail');
  await page.close();olderDelay=0;
  // Likewise, a delayed full search read fills old history without replacing
  // a newer live tail or its authoritative controls/revision.
  phase='initial';page=await createPage(browser,port);fullDelay=3500;
  const fullRequested=page.waitForRequest(request=>request.url().includes('/snapshot?'));
  await page.locator('#search-conversation').dispatchEvent('click');await fullRequested;
  phase='tail';await sync(page);
  await page.waitForFunction(()=>document.querySelector('[data-turn-id="turn-65"]').textContent.includes('尾部增量'));
  await count(page,65);
  assert.match(await page.locator('[data-turn-id="turn-65"]').textContent(),/尾部增量/,'late full read must not regress the active tail');
  await page.close();fullDelay=0;
  // Reverse ordering: the full read finishes first, then a newer in-flight
  // view is the only carrier of final text and current controls.
  phase='initial';page=await createPage(browser,port);viewDelay=2500;
  const viewRequested=page.waitForRequest(request=>request.url().includes('/view?'));
  phase='tail';await sync(page);await viewRequested;
  fullOverride=fixture.initial;
  await page.locator('#search-conversation').dispatchEvent('click');await count(page,65);
  await page.waitForFunction(()=>document.querySelector('[data-turn-id="turn-65"]').textContent.includes('尾部增量'));
  await count(page,65);
  assert.equal(await page.evaluate(()=>window.__lastHistorySnapshot.conversation.state),fixture.tail.conversation.state);
  assert.equal(await page.evaluate(()=>window.__lastHistorySnapshot.conversation.revision),fixture.tail.conversation.revision);
  assert.equal(await page.locator('[data-turn-id="turn-1"]').count(),1,'a newer view must preserve the full transcript');
  await page.close();viewDelay=0;fullOverride=null;
  // Search explicitly loads all history, including a hit outside the first page.
  phase='initial';page=await createPage(browser,port);fullDelay=150;
  await page.locator('#search-conversation').dispatchEvent('click');
  await page.locator('#conversation-search-input').fill('第 1 轮完整答复');
  assert.match(await page.locator('#conversation-search-status').textContent(),/完整历史/);
  await count(page,65);await page.waitForFunction(()=>document.querySelector('#conversation-search-status').textContent==='1 / 1');
  await page.close();fullDelay=0;
  // All existing notification/search links keep exact turn/command/item targets.
  for(const target of ['?turn=turn-1','?turn=turn-1&command=command-1','?turn=turn-1&item=item-1-0']) {
    page=await createPage(browser,port,target);await count(page,65);
    await page.waitForFunction(()=>document.querySelector('.native-turn-highlight'));
    assert.ok((await page.locator('.native-turn-highlight').first().textContent()).includes('第 1 轮'));
    await page.close();
  }
  // Accelerate only the existing 15-second read deadline, not network state.
  // A hanging search read releases its loading state and leaves history usable.
  phase='initial';page=await createPage(browser,port);hangFull=true;
  await page.evaluate(()=>{const nativeTimeout=window.setTimeout;window.setTimeout=function(callback,delay,...args){return nativeTimeout(callback,delay===15000?180:delay,...args);};});
  await page.locator('#search-conversation').dispatchEvent('click');
  await page.waitForFunction(()=>document.querySelector('#conversation-search-status').textContent.includes('重新打开查找可重试'));
  await count(page,20);
  assert.equal(await page.locator('#conversation-history-older').isDisabled(),false);
  assert.match(await page.locator('#conversation-history-status').textContent(),/超过15秒/);
  hangFull=false;await page.locator('[data-close-search]').click();
  await page.locator('#search-conversation').dispatchEvent('click');await count(page,65);
  await page.close();
  // Recovery reads the single stored ID even after more than 20 background
  // turns. Its full receipt payload must not expand the visible history.
  phase='initial';receiptMode='completed';
  for(const pending of [{},{revision:'new-draft'},{draft:'后来写的新草稿',revision:'new-draft'}]) {
    const before=requests.length;
    page=await createPage(browser,port,'',pending);
    await page.waitForFunction(()=>!localStorage.getItem('codex-conversation-pending:history-fixture'));
    await count(page,20);
    assert.equal(await page.locator('[data-message-key="pending:command-1"]').count(),0);
    assert.equal(await page.locator('textarea[name="text"]').inputValue(),pending.revision ? pending.draft || '第 1 轮问题' : '');
    assert.equal(requests.slice(before).filter(request=>request.path.endsWith('/actions/command-1')).length,1);
    assert.equal(requests.slice(before).some(request=>request.path.endsWith('/snapshot')),false);
    await page.close();
  }
  // Typing while the receipt is in flight is also a new owned draft.
  receiptDelay=2500;page=await createPage(browser,port,'',{});
  await page.locator('textarea[name="text"]').fill('回执等待时的新草稿');
  await page.waitForFunction(()=>!localStorage.getItem('codex-conversation-pending:history-fixture'));
  assert.equal(await page.locator('textarea[name="text"]').inputValue(),'回执等待时的新草稿');
  await count(page,20);await page.close();receiptDelay=0;
  // A receipt is not the response to a queued live refresh. Both a known
  // and an unknown old receipt must leave that one final WS update pending.
  for(const mode of ['completed','delivery_unknown']) {
    phase='initial';receiptMode=mode;receiptDelay=900;
    page=await createPage(browser,port,'',{});
    assert.equal(page.__receiptResponses.length,0,'fixture must queue WS before the receipt returns');
    phase='tail';await sync(page);
    await page.waitForResponse(response=>response.url().endsWith('/actions/command-1'));
    await page.waitForFunction(()=>document.querySelector('[data-turn-id="turn-65"]').textContent.includes('尾部增量'),null,{timeout:5000});
    assert.equal(await page.evaluate(()=>window.__lastHistorySnapshot.conversation.revision),fixture.tail.conversation.revision);
    await count(page,20);await page.close();receiptDelay=0;phase='initial';
  }
  // Unknown and missing receipts retain their exact ID without replay or a
  // query loop; an explicit refresh may read the same receipt again.
  for(const mode of ['delivery_unknown','missing']) {
    receiptMode=mode;const before=requests.length;page=await createPage(browser,port,'',{});
    if(!page.__receiptResponses.length)await page.waitForResponse(response=>response.url().endsWith('/actions/command-1'));
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('codex-conversation-pending:history-fixture')).id),'command-1');
    assert.equal(await page.locator('textarea[name="text"]').inputValue(),'第 1 轮问题');
    phase='tail';await sync(page);await page.waitForFunction(()=>document.querySelector('[data-turn-id="turn-65"]').textContent.includes('尾部增量'));
    assert.equal(requests.slice(before).filter(request=>request.path.endsWith('/actions/command-1')).length,1,'tail refresh must not repeatedly fetch an unknown receipt');
    receiptMode='completed';await page.locator('#reconcile-conversation').dispatchEvent('click');
    await page.waitForFunction(()=>!localStorage.getItem('codex-conversation-pending:history-fixture'));
    await count(page,20);
    assert.equal(requests.slice(before).filter(request=>request.path.endsWith('/actions/command-1')).length,2);
    await page.close();phase='initial';
  }
  // Background reads are disposable: hiding retires both the delayed GET and
  // its timer; restoring creates a fresh foreground generation.  The test
  // also covers local-backoff recovery, authoritative Retry-After, and makes
  // the read-only contract explicit (no messages/settings/reconcile POST).
  phase='tail';page=await createPage(browser,port);
  const recoveryStart=requests.length;viewDelay=450;
  await sync(page);await page.waitForTimeout(2600);
  assert.equal(requests.slice(recoveryStart).filter(request=>request.path.endsWith('/view')).length,1,'fixture did not start the delayed background view');
  phase='corrected';viewDelay=0;
  async function visibility(state) {
    await page.evaluate(next=>{
      window.__fixtureVisibility=next;
      Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>window.__fixtureVisibility});
      document.dispatchEvent(new Event('visibilitychange'));
    },state);
  }
  await visibility('hidden');await page.waitForTimeout(80);
  assert.equal(requests.slice(recoveryStart).filter(request=>request.path.endsWith('/view')).length,1,'hidden page started another automatic GET');
  const rendersBeforeForeground=await page.evaluate(()=>window.__historyRenders);
  await visibility('visible');
  await page.waitForFunction(previous=>window.__historyRenders>previous,rendersBeforeForeground);
  assert.equal(requests.slice(recoveryStart).filter(request=>request.path.endsWith('/view')).length,2,'foreground did not replace the retired read with one fresh generation');
  const foregroundRevision=await page.evaluate(()=>window.__lastHistorySnapshot.conversation.revision);
  await page.waitForTimeout(500);
  assert.equal(await page.evaluate(()=>window.__lastHistorySnapshot.conversation.revision),foregroundRevision,'retired read overwrote the newer foreground result');

  // A manual read can immediately recover only local exponential backoff.
  // It must not bypass a server-provided Retry-After deadline.
  readFailure={status:503};const localFailureStart=requests.length;
  await page.locator('#reconcile-conversation').click();
  await page.waitForFunction(()=>document.querySelector('#conversation-note').textContent.includes('fixture read failure'));
  const afterLocalFailure=requests.length;readFailure=null;
  const localRecovery=page.waitForResponse(response=>response.url().includes('/view?')&&response.status()===200);
  await page.locator('#reconcile-conversation').click();await localRecovery;
  assert.ok(requests.length>afterLocalFailure,'manual refresh did not reset local backoff');

  readFailure={status:429,retryAfter:1};const serverFailureStart=requests.length;
  await page.locator('#reconcile-conversation').click();
  await page.waitForFunction(()=>document.querySelector('#conversation-note').textContent.includes('fixture read failure'));
  const afterServerFailure=requests.length;readFailure=null;
  await page.locator('#reconcile-conversation').click();await page.waitForTimeout(120);
  assert.equal(requests.length,afterServerFailure,'manual refresh bypassed server Retry-After');
  await page.waitForTimeout(1600);
  assert.ok(requests.length>afterServerFailure,'server Retry-After did not allow a later read');
  assert.equal(requests.slice(recoveryStart).some(request=>request.method==='POST'&&(/\/messages$|\/settings$|\/reconcile$/).test(request.path)),false,'visibility/read recovery sent a command or settings POST');
  assert.ok(afterLocalFailure>localFailureStart&&afterServerFailure>serverFailureStart,'read failure fixtures did not run');
  await page.close();readFailure=null;viewDelay=0;phase='initial';
  // A retired reader may already be decoding a response whose mock transport
  // ignores abort. Its late auth error must not halt the newer generation.
  page=await createPage(browser,port);
  await page.evaluate(()=>{
    const native=window.fetch;window.__holdLateRead=true;
    window.fetch=function(url,options){
      if(window.__holdLateRead&&String(url).includes('/view?'))return new Promise(resolve=>{
        window.__lateReadPending=true;window.__finishLateRead=()=>resolve(new Response('{"error":"retired auth"}',{status:403,headers:{'Content-Type':'application/json'}}));
      });
      return native.call(this,url,options);
    };
  });
  await sync(page);await page.waitForFunction(()=>window.__lateReadPending);
  await visibility('hidden');
  await page.evaluate(()=>{window.__holdLateRead=false;});
  const resumed=page.waitForResponse(response=>response.url().includes('/view?')&&response.status()===200);
  await visibility('visible');await resumed;
  await page.evaluate(()=>window.__finishLateRead());await page.waitForTimeout(100);
  const afterRetiredAuth=requests.length;phase='tail';await sync(page);
  await page.waitForFunction(()=>window.__lastHistorySnapshot.conversation.revision>1&&document.querySelector('[data-turn-id="turn-65"]').textContent.includes('尾部增量'));
  assert.ok(requests.length>afterRetiredAuth,'retired auth response stopped the current GET generation');
  await page.close();phase='initial';
  // Paging failure falls back to the old full read, without execution/reconcile.
  page=await createPage(browser,port);failView=true;await page.locator('#conversation-history-older').dispatchEvent('click');await count(page,65);
  assert.match(await page.locator('#conversation-history-status').textContent(),/完整历史/);
  assert.equal(requests.some(request=>request.method==='POST'&&request.path.startsWith('/conversation/')&& !request.path.endsWith('/read')),false);
  assert.equal(requests.some(request=>request.path.endsWith('/reconcile')),false);
  assert.equal(await page.locator('.native-message.agent[data-turn-id]').evaluateAll(nodes=>new Set(nodes.map(n=>n.dataset.turnId)).size),65);
  console.log('conversation history paging passed: complete pages, gaps, stable nodes/selection/anchor/focus, corrections, bidirectional older/full/view races, out-of-window read-only receipt/draft recovery, bounded hung reads/retry, search, deep links, read-only fallback');
}finally{if(browser)await browser.close();server.close();}})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
