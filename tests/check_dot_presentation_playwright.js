/* Offline real-template regressions; all requests are intercepted synthetic fixtures. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const raw = '## 合成结果\n\n**重点**和[安全链接](https://example.test)\n\n| 项目 | 数量 |\n|---|---|\n| 示例 | 2 |\n\n```text\nhello\n```\n\n<img src=x onerror=window.dotXss=1> [坏链接](javascript:alert(1))';
const snapshot = {backend: 'dot_mailbox', conversation: {
  id: 'synthetic-dot-mobile', kind: 'dot', title: 'dot', revision: 3, reply_generation: 3,
}, messages: [{id:'message-one',text:'请检查 合成结果',reply:{id:'reply-one',text:raw}}]};
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


(async () => {
 const browser = await chromium.launch({headless:true,
  args:['--no-sandbox','--host-resolver-rules=MAP * ~NOTFOUND']});
 const results=[];
 try { for(const width of [320,390]) {
  const context=await browser.newContext({viewport:{width,height:844},isMobile:true,hasTouch:true,serviceWorkers:'block'});
  const current=structuredClone(snapshot), forbidden=[], posts=[], reads=[], errors=[];
  await context.addInitScript(()=>{
   window.WebSocket=class{close(){}};
   Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copiedText=text;}}});
  });
  await context.route('**/*',async route=>{
   const request=route.request(), pathname=new URL(request.url()).pathname.replace(/^\/[a-f0-9]{40}(?=\/)/,'');
   if(pathname.startsWith('/static/')){
    const target=path.resolve(root,'.'+pathname);
    if(!target.startsWith(path.join(root,'static')+path.sep)||!fs.existsSync(target))return route.abort();
    return route.fulfill({body:fs.readFileSync(target),contentType:target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'image/png'});
   }
   if(pathname==='/settings/dot/synthetic-dot-mobile')return route.fulfill({body:pageHtml,contentType:'text/html'});
   if(pathname.endsWith('/snapshot'))return route.fulfill({json:current});
   if(pathname==='/settings/dot/synthetic-dot-mobile/read'){reads.push(request.postData());return route.fulfill({json:{seen_reply_generation:current.conversation.reply_generation}});}
   if(request.method()==='POST' && pathname!=='/settings/diagnostics/client-events')posts.push(pathname);
   if(/^\/(conversation|files|notifications)/.test(pathname))forbidden.push(pathname);
   return route.fulfill({json:{}});
  });
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(3000);
  try{
   await page.goto('https://dot-test.invalid/'+'a'.repeat(40)+'/settings/dot/synthetic-dot-mobile');
   await page.waitForSelector('.native-message.agent table');
   assert.equal(await page.locator('.native-message.agent table').count(),1);
   assert.equal(await page.locator('.native-message.agent pre code').textContent(),'hello');
   assert.equal(await page.locator('.native-message.agent img,.native-message.agent script,.native-message.agent a[href^="javascript:"]').count(),0);
   assert.equal(await page.evaluate(()=>window.dotXss),undefined);
   assert.equal(await page.locator('.nav a[aria-current=page]').getAttribute('href'),'/projects/history');
   assert.equal(await page.locator('[data-message-attachments],#conversation-settings,#conversation-notify,#stop-conversation').count(),0);
   assert.equal(await page.locator('script[src*="/conversation.js"]').count(),0);
   const geo=await page.locator('.native-send').boundingBox();assert.ok(geo.x>width-65,'send stays on right');
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no overflow');
   const agent=page.locator('.native-message.agent');
   await agent.locator('.native-message-more').click();await agent.locator('[data-dot-action=copy]').click();
   assert.equal(await page.evaluate(()=>window.copiedText),raw);
   await page.locator('textarea[name=text]').fill('现有草稿');
   await agent.locator('.native-message-more').click();await agent.locator('[data-dot-action=quote]').click();
   assert.equal(await page.locator('textarea[name=text]').inputValue(),'> '+raw.replace(/\n/g,'\n> ')+'\n\n现有草稿');
   await page.locator('textarea[name=text]').blur();await page.waitForTimeout(220);
   await agent.locator('.native-code-fold').click();
   await page.evaluate(()=>{window.savedAgent=document.querySelector('.native-message.agent');window.savedCode=document.querySelector('.native-code-card');});
   const folded=await page.locator('.native-code-card').getAttribute('class');
   current.conversation.revision++;
   await page.locator('#reconcile-conversation').click();await page.waitForTimeout(100);
   assert.equal(await page.evaluate(()=>window.savedAgent===document.querySelector('.native-message.agent')),true);
   assert.equal(await page.locator('.native-code-card').getAttribute('class'),folded);
   await page.locator('.native-head-menu summary').click();await page.locator('#search-conversation').click();
   await page.locator('#conversation-search-input').fill('合成结果');
   assert.equal(await page.locator('#conversation-search-status').textContent(),'1 / 2');
   await page.locator('#conversation-search-next').click();assert.equal(await page.locator('#conversation-search-status').textContent(),'2 / 2');
   assert.equal(await page.locator('.native-message.agent.is-search-hit').count(),1);
   await page.locator('#conversation-search-input').fill('[.*');assert.equal(await page.locator('#conversation-search-status').textContent(),'没有找到');
   await page.locator('[data-close-search]').click();assert.equal(await page.locator('.is-search-hit').count(),0);
   await page.waitForTimeout(100);
   assert.equal(reads.length,1,'visible rendered reply acknowledged once');
   assert.ok(reads[0].includes('3'));
   await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,value:true}));
   current.conversation.revision=5; current.conversation.reply_generation=5;
   current.messages[0].reply.text=raw+'\n第二次回复';
   await page.locator('#reconcile-conversation').click();await page.waitForTimeout(150);
   assert.equal(reads.length,1,'hidden document must not acknowledge new reply');
   await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));});
   await page.waitForTimeout(150);
   assert.equal(reads.length,2,'visible latest reply acknowledged');
   assert.ok(reads[1].includes('5'));
   assert.deepEqual(posts,[]);assert.deepEqual(forbidden,[]);assert.deepEqual(errors,[]);
   results.push({width,passed:true,checks:['markdown','safe-content','copy-exact','quote-preserves-draft','search','stable-poll','send-layout','history-nav','no-native-or-file-actions']});
  }catch(error){results.push({width,passed:false,error:error.message});}
  await context.close();
 }}finally{await browser.close();}
 console.log(JSON.stringify({real_phone_tested:false,network:'intercepted synthetic only',results}));
 if(results.some(r=>!r.passed))process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1;});
