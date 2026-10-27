/* Offline real-template regressions; all requests are intercepted synthetic fixtures. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const snapshot = {backend: 'dot_mailbox', conversation: {
  id: 'synthetic-dot-mobile', kind: 'dot', title: 'dot', revision: 3, reply_generation: 3,
}, messages: [{id:'m1',text:'合成输入',reply:{id:'r1',text:'合成回复'}}]};
const pageHtml = execFileSync(process.env.DOT_TEST_PYTHON || (process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), ['-c', `
import json, sys
from shared.html_lib import html, nav, templates
s=json.load(sys.stdin)
print(html('dot', templates.get_template('conversation.html').render(
 conversation=s['conversation'], initial_snapshot=s, back_url='/projects/history',
 project_name='dot', nav_html=nav(), settings={}, models=[], skills=[], catalog_error=False,
 upload_notice_html='', upload_button_html='', upload_panel_html='')).body.decode())
`], {cwd: root, input: JSON.stringify(snapshot), encoding: 'utf8',
  env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});

const historyHtml = execFileSync(process.env.DOT_TEST_PYTHON || (process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), ['-c', `
from tests.test_dot_history import DotHistoryTests
from routes import home
fixture=DotHistoryTests()
fixture.setUp()
try:
 print(home.history(object()).body.decode())
finally:
 fixture.doCleanups()
`], {cwd: root, encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});

(async () => {
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP * ~NOTFOUND']});
 const results=[];
 try {for(const width of [320,390]){
  const context=await browser.newContext({viewport:{width,height:844},isMobile:true,hasTouch:true,serviceWorkers:'block'});
  let seen=0,generation=3,reads=0;const errors=[],forbidden=[];
  await context.addInitScript(()=>{window.WebSocket=class{close(){}};});
  await context.route('**/*',async route=>{
   const req=route.request(),pathname=new URL(req.url()).pathname.replace(/^\/[a-f0-9]{40}(?=\/)/,'');
   if(pathname.startsWith('/static/')){
    const target=path.resolve(root,'.'+pathname);if(!target.startsWith(path.join(root,'static')+path.sep)||!fs.existsSync(target))return route.abort();
    return route.fulfill({body:fs.readFileSync(target),contentType:target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'image/png'});
   }
   if(pathname==='/projects/history')return route.fulfill({body:historyHtml,contentType:'text/html'});
   if(pathname==='/settings/dot/status')return route.fulfill({json:{unread:generation>seen,reply_generation:generation,waiting:false}});
   // ASGI suite verifies the entry's 303. Resolve it here without DNS-followed redirects.
   if(pathname==='/settings/dot')return route.fulfill({body:pageHtml,contentType:'text/html'});
   if(pathname==='/settings/dot/synthetic-dot-mobile')return route.fulfill({body:pageHtml,contentType:'text/html'});
   if(pathname.endsWith('/snapshot'))return route.fulfill({json:snapshot});
   if(pathname==='/settings/dot/synthetic-dot-mobile/read'){seen=3;reads++;return route.fulfill({json:{seen_reply_generation:seen}});}
   if(req.method()==='POST'&&pathname!=='/settings/diagnostics/client-events')forbidden.push(pathname);
   return route.fulfill({json:{html:'',values:[]}});
  });
  const page=await context.newPage();page.setDefaultTimeout(4000);page.on('pageerror',e=>errors.push(e.message));
  try{
   await page.goto('https://dot-test.invalid/'+'a'.repeat(40)+'/projects/history');
   await page.waitForFunction(()=>document.querySelector('[data-dot-history-card] .conversation-card-status').textContent==='已完成未读');
   assert.equal(await page.locator('[data-dot-history-card].unread').count(),1);
   var receipt = context.waitForEvent('request',{predicate:req=>new URL(req.url()).pathname.endsWith('/synthetic-dot-mobile/read'),timeout:5000});
   await page.locator('[data-dot-history-card] a').click();
   await receipt;await page.waitForTimeout(100);assert.equal(seen,3);assert.equal(reads,1);
   await page.locator('a[aria-label=返回]').click();
   await page.waitForFunction(()=>document.querySelector('[data-dot-history-card] .conversation-card-status').textContent==='已完成');
   assert.equal(await page.locator('[data-dot-history-card].unread').count(),0);
   generation=5;
   await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
   await page.waitForFunction(()=>document.querySelector('[data-dot-history-card] .conversation-card-status').textContent==='已完成未读');
   assert.equal(reads,1,'history never marks new replies read');
   assert.deepEqual(errors,[]);assert.deepEqual(forbidden,[]);
   results.push({width,passed:true,checks:['unread-history','open-marks-displayed-read','return-clears-badge','new-reply-restores-unread']});
  }catch(error){results.push({width,passed:false,error:error.message,url:page.url(),errors,debug:await page.evaluate(()=>({root:!!document.getElementById('native-conversation'),snapshot:document.getElementById('conversation-initial-snapshot')?.textContent,scripts:Array.from(document.scripts).map(s=>s.src)}))});}
  await context.close();
 }}finally{await browser.close();}
 console.log(JSON.stringify({synthetic_only:true,results}));if(results.some(r=>!r.passed))process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
