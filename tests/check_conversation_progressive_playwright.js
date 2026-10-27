/* Real enhancement renderer; synthetic 500-message history, no product services. */
const {chromium} = require('playwright');
const assert = require('assert/strict');
const http = require('http'), fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..');
const markdown = n => '# 第 ' + n + ' 条\n\n**可读正文**\n\n```js\nconst n = ' + n + ';\n```\n\n| A | B |\n| --- | --- |\n| 1 | 2 |';
const escape = value => value.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const html = '<!doctype html><style>.native-message{min-height:200px}.native-bubble{width:360px}</style>' +
  '<main id="native-conversation" data-conversation-id="progressive-fixture"><textarea id="responsive-input"></textarea><div id="conversation-messages">' +
  Array.from({length:500}, (_,n) => '<article class="native-message agent" data-message-key="m'+n+'" data-auto-fold-candidate="1"><div class="native-bubble native-final"><div class="native-message-body">'+escape(markdown(n))+'</div></div></article>').join('') +
  '</div></main><script src="/enhancements.js"></script><script>window.__responsive=false;requestAnimationFrame(()=>{const input=document.querySelector("textarea");input.addEventListener("input",()=>{window.__responsive=document.querySelectorAll("[data-native-enhanced]").length<500&&input.value==="不等待历史";});input.value="不等待历史";input.dispatchEvent(new Event("input"));});</script>';
const message = (id,text) => '<article class="native-message agent" id="'+id+'" data-message-key="'+id+'"><div class="native-bubble native-final"><div class="native-message-body">'+text+'</div></div></article>';
const fixture = (style,content) => '<!doctype html><style>html{overflow-anchor:none}body{margin:0}'+style+'</style><main id="native-conversation" data-conversation-id="scheduler-fixture">'+content+'</main><script src="/enhancements.js"></script>';
const anchorHtml = fixture('.native-conversation-head{position:fixed;top:0;height:20px}#conversation-messages{margin-top:60px}.native-message{height:100px}#upper:has(strong){height:200px}',
  '<header class="native-conversation-head"></header><div id="conversation-messages">'+message('upper','**upper**')+message('lower','**lower**')+'</div><div style="height:2000px"></div>');
const selectionHtml = fixture('.native-message{height:10px;font-size:6px;line-height:6px}',
  '<div id="conversation-messages">'+Array.from({length:25},(_,n)=>message('dense'+n,'**dense '+n+'**')).join('')+'</div><div style="height:2000px"></div>');
const server = http.createServer((req,res) => {
  res.setHeader('Content-Type', req.url === '/enhancements.js' ? 'application/javascript' : 'text/html;charset=utf-8');
  res.end(req.url === '/enhancements.js' ? fs.readFileSync(path.join(root,'static/conversation-enhancements.js')) : req.url === '/anchor' ? anchorHtml : req.url === '/selection' ? selectionHtml : html);
});
async function scheduledPage(browser,url,errors) {
  const page=await browser.newPage({viewport:{width:390,height:844}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
    // Keep real DOM/layout/selection, but deliver observer and frame order
    // deterministically so an unlucky browser scheduling race is reproducible.
    window.__frames=[];
    window.requestAnimationFrame=callback=>{window.__frames.push(callback);return window.__frames.length;};
    window.IntersectionObserver=class{constructor(callback){window.__intersection=callback;}observe(){}unobserve(){}};
    window.__pump=async count=>{for(let i=0;i<count&&window.__frames.length;i++){window.__frames.shift()(performance.now());await Promise.resolve();}};
  });
  await page.goto(url,{waitUntil:'domcontentloaded'});
  return page;
}
(async () => {
  let browser;
  try {
    await new Promise(r => server.listen(0,'127.0.0.1',r)); browser = await chromium.launch({headless:true});
    const page = await browser.newPage({viewport:{width:390,height:844}}), errors=[];
    page.on('pageerror', e => errors.push(e.message));
    // Freeze one raw-text selection before optional Markdown can replace it.
    await page.addInitScript(() => {
      document.addEventListener('DOMContentLoaded', () => {
        const body=document.querySelector('[data-message-key="m0"] .native-message-body');
        const range=document.createRange();range.setStart(body.firstChild,0);range.setEnd(body.firstChild,4);
        getSelection().removeAllRanges();getSelection().addRange(range);window.__selected=getSelection().toString();
      });
    });
    await page.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
    assert.equal(await page.evaluate(() => document.querySelectorAll('.native-message-body').length),500);
    await page.evaluate(() => {
      const body=document.querySelector('[data-message-key="m495"] .native-message-body');
      body.textContent='# 同 ID 正文修正\n\n**必须采用新正文**';body.removeAttribute('data-native-enhanced');
      const target=document.querySelector('[data-message-key="m10"]');
      document.querySelector('#conversation-messages').dispatchEvent(new CustomEvent('codex:open-message',{detail:{key:'m10'}}));
      window.__explicit=target.querySelector('.native-message-body').dataset.nativeEnhanced==='1';
    });
    await page.waitForFunction(() => document.querySelectorAll('.native-message-body[data-native-enhanced="1"]').length===499);
    const selected = await page.evaluate(() => ({selection:getSelection().toString()===window.__selected,
      raw:!document.querySelector('[data-message-key="m0"] .native-message-body').dataset.nativeEnhanced,
      responsive:window.__responsive, explicit:window.__explicit,
      correction:document.querySelector('[data-message-key="m495"] strong')?.textContent}));
    assert.deepEqual(selected,{selection:true,raw:true,responsive:true,explicit:true,correction:'必须采用新正文'});
    await page.evaluate(() => getSelection().removeAllRanges());
    await page.waitForFunction(() => document.querySelectorAll('.native-message-body[data-native-enhanced="1"]').length===500);
    const equivalent=await page.evaluate(() => ({headings:document.querySelectorAll('.native-message-body h2').length,
      code:document.querySelectorAll('.native-code-card').length,tables:document.querySelectorAll('.native-table-wrap').length,
      source:document.querySelector('[data-message-key="m0"] .native-message-body').dataset.speechSource,
      unsafe:document.querySelectorAll('.native-message-body script').length}));
    assert.equal(equivalent.headings,500);assert.equal(equivalent.code,499);assert.equal(equivalent.tables,499);
    assert.equal(equivalent.source,markdown(0));assert.equal(equivalent.unsafe,0);assert.deepEqual(errors,[]);
    console.log('progressive rendering passed: 500 loaded bodies, responsive input before completion, selected raw text retained, corrected body wins, explicit target priority, eventual equivalent Markdown');
    await page.close();
    const slow = await browser.newPage({viewport:{width:390,height:844}});
    slow.on('pageerror',e=>errors.push(e.message));
    await slow.addInitScript(() => {
      const original=Element.prototype.getBoundingClientRect;
      Element.prototype.getBoundingClientRect=function(){
        const until=performance.now()+7;while(performance.now()<until){}
        return original.apply(this,arguments);
      };
      const Native=window.IntersectionObserver;
      window.__unobserved=new WeakMap();
      window.IntersectionObserver=class {
        constructor(callback,options){window.__intersection=callback;this.native=new Native(callback,options);}
        observe(node){this.native.observe(node);}
        unobserve(node){window.__unobserved.set(node,(window.__unobserved.get(node)||0)+1);this.native.unobserve(node);}
      };
    });
    await slow.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
    await slow.waitForFunction(()=>document.querySelectorAll('.native-message-body[data-native-enhanced="1"]').length===500);
    assert.equal(await slow.evaluate(()=>window.__responsive),true,'slow geometry prevented budgeted progress/input');
    const cleanup=await slow.evaluate(async()=>{
      const target=document.querySelector('[data-message-key="m42"]');target.remove();
      await new Promise(resolve=>setTimeout(resolve,0));
      const before=window.__unobserved.get(target)||0;
      window.__intersection([{target:target,isIntersecting:true}]);
      return {before,after:window.__unobserved.get(target)||0,detached:!target.isConnected};
    });
    assert.ok(cleanup.before>0&&cleanup.after>cleanup.before&&cleanup.detached,'late intersection entry resurrected a detached history target');
    await slow.close();
    const origin='http://127.0.0.1:'+server.address().port;
    const anchor=await scheduledPage(browser,origin+'/anchor',errors);
    const reading=await anchor.evaluate(async()=>{
      const upper=document.querySelector('#upper'),lower=document.querySelector('#lower');
      window.scrollTo(0,40);
      // A gap/narrow bubble may hit the container. Lower became visible first,
      // then upper while scrolling upward: insertion order is not reading order.
      document.elementFromPoint=()=>document.querySelector('#conversation-messages');
      window.__intersection([{target:lower,isIntersecting:true},{target:upper,isIntersecting:true}]);
      const before=upper.getBoundingClientRect().top,adjustments=[];
      const original=window.scrollBy;window.scrollBy=(x,y)=>{adjustments.push(y);original.call(window,x,y);};
      await window.__pump(10);
      return {before,after:upper.getBoundingClientRect().top,adjustment:adjustments.reduce((sum,n)=>sum+n,0),
        complete:document.querySelectorAll('.native-message-body strong').length};
    });
    assert.equal(reading.complete,2);assert.ok(Math.abs(reading.after-reading.before)<1,'fallback anchor displaced the upper message being read');
    assert.equal(reading.adjustment,0,'observer insertion order selected a lower scroll anchor');
    await anchor.close();
    const dense=await scheduledPage(browser,origin+'/selection',errors);
    const fair=await dense.evaluate(async()=>{
      const first=document.querySelector('#dense0 .native-message-body'),last=document.querySelector('#dense23 .native-message-body');
      const range=document.createRange();range.setStart(first.firstChild,0);range.setEnd(last.firstChild,last.firstChild.length);
      getSelection().removeAllRanges();getSelection().addRange(range);const selected=getSelection().toString();
      window.__intersection(Array.from(document.querySelectorAll('.native-message')).slice(0,24).map(target=>({target,isIntersecting:true})));
      await window.__pump(10);
      return {other:!!document.querySelector('#dense24 strong'),selectedRaw:!document.querySelector('#dense0 strong'),
        selection:getSelection().toString()===selected,frames:window.__frames.length};
    });
    assert.deepEqual(fair,{other:true,selectedRaw:true,selection:true,frames:0},'selected visible jobs starved independent work or spun animation frames');
    const resumed=await dense.evaluate(async()=>{getSelection().removeAllRanges();document.dispatchEvent(new Event('selectionchange'));await window.__pump(20);return document.querySelectorAll('.native-message-body strong').length;});
    assert.equal(resumed,25,'selection-blocked jobs did not resume');
    await dense.close();assert.deepEqual(errors,[]);
    console.log('progressive scheduler passed: slow geometry progresses, detached observer targets stay retired, timeline-order fallback anchor, dense selection fairness and resumption');
  } finally {if(browser)await browser.close();server.closeAllConnections();await new Promise(r => server.close(r));}
})().catch(error => {console.error(error);process.exitCode=1;});
