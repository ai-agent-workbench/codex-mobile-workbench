/* Real rendered template and FormData callback UI at phone widths. No production calls. */
const {chromium} = require('playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const rendered = JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root,'.venv/bin/python')),[path.join(__dirname,'render_conversation_ui_fixtures.py')],{cwd:root,encoding:'utf8'}));
const template = rendered.conversation.replace(/<script\b[^>]*src=[^>]*>[\s\S]*?<\/script>/gi,'');
const js = fs.readFileSync(path.join(root,'static/conversation-interactions.js'));
const css = fs.readFileSync(path.join(root,'static/css/app.css'));
const prefix = '/'+'b'.repeat(40);
let current, bodies=[], responseStatus=200;
function snapshot(record) {return {conversation:{id:'conversation-ui'},native_interactions:record?[record]:[]};}
function record(kind, extra={}) {
  const display = {kind, reason:'测试<非HTML>', availableDecisions:['accept','acceptForSession','decline','cancel'], ...extra};
  return {id:'ni-test',attempt:1,fingerprint:'fingerprint',state:'awaiting',native_item_id:'item-test',display};
}
const server = http.createServer((req,res)=>{
  if(req.method==='POST') {
    const chunks=[];req.on('data',c=>chunks.push(c));req.on('end',()=>{
      bodies.push({path:req.url,body:Buffer.concat(chunks).toString()});
      res.statusCode=responseStatus;res.setHeader('Content-Type','application/json');
      if(responseStatus===200) res.end(JSON.stringify(snapshot({...current,state:'response_sent'})));
      else res.end(JSON.stringify({error:'原生请求拒绝测试'}));
    });return;
  }
  if(req.url==='/widget.js') {res.setHeader('Content-Type','text/javascript');res.end(js);return;}
  if(req.url==='/style.css') {res.setHeader('Content-Type','text/css');res.end(css);return;}
  res.setHeader('Content-Type','text/html');res.end('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/style.css"></head><body><main>'+template+'</main><script src="/widget.js"></script></body></html>');
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage(); const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const url=`http://127.0.0.1:${server.address().port}${prefix}/conversation/conversation-ui`;
    async function load(r) {
      current=r;await page.goto(url);
      await page.evaluate(s=>window.dispatchEvent(new CustomEvent('codex:conversation-snapshot',{detail:s})),snapshot(r));
    }
    for(const width of [320,360,412,430]) {
      await page.setViewportSize({width,height:850});await load(record('command',{command:'printf '+('非常长的原生命令路径'.repeat(35)),cwd:'/home/example'}));
      assert(await page.locator('.native-interaction-card button[type=submit]').isDisabled());
      assert.strictEqual(await page.locator('.native-interaction-card input[type=radio]:checked').count(),0);
      assert.strictEqual(await page.evaluate(()=>document.querySelector('.native-interaction-card').querySelectorAll('script').length),0);
      const overflow=await page.evaluate(()=>({ok:document.documentElement.scrollWidth<=innerWidth+1,width:document.documentElement.scrollWidth,offenders:[...document.querySelectorAll('body *')].filter(x=>x.getBoundingClientRect().right>innerWidth+1).slice(0,8).map(x=>[x.tagName,x.className,x.getBoundingClientRect().right])}));
      assert(overflow.ok,JSON.stringify(overflow));
      await page.locator('.native-interaction-card input[value="1"]').check();await page.locator('.native-interaction-card button').click();
      await page.waitForFunction(()=>document.querySelector('.native-interaction-card form').textContent.includes('决定已记录'));
      const posted=bodies.at(-1);assert(posted.path.startsWith(prefix+'/conversation/conversation-ui/native-interactions/ni-test/answer'));
      assert(posted.body.includes('acceptForSession'));assert(!posted.body.includes('approve-everything'));
    }
    await load(record('permissions',{requested:{network:{enabled:true}},scopes:['turn','session']}));
    await page.locator('.native-interaction-card input[value="0"]').check();
    await page.locator('.native-interaction-card select').selectOption('session');
    await page.locator('.native-interaction-card button').click();await page.waitForTimeout(100);
    assert(bodies.at(-1).body.includes('"scope":"session"'));assert(bodies.at(-1).body.includes('strictAutoReview'));
    await load(record('mcpElicitation',{mode:'form',serverName:'fixture',requestedSchema:{type:'object',properties:{token:{type:'string'},age:{type:'integer'}},required:['token']}}));
    const secret='private-browser-secret';await page.locator('.native-interaction-card input[type=password]').fill(secret);
    await page.evaluate(s=>window.dispatchEvent(new CustomEvent('codex:conversation-snapshot',{detail:s})),snapshot(current));
    assert.strictEqual(await page.locator('.native-interaction-card input[type=password]').inputValue(),secret);
    assert(!await page.evaluate(()=>JSON.stringify(localStorage)).then(s=>s.includes(secret)));
    await page.locator('.native-interaction-card input[value="0"]').check();await page.locator('.native-interaction-card button').click();await page.waitForTimeout(100);
    assert(bodies.at(-1).body.includes(secret));assert.strictEqual(await page.locator('.native-interaction-card input[type=password]').count(),0);
    await load(record('mcpElicitation',{mode:'url',url:'javascript:alert("no")',serverName:'fixture'}));
    assert.strictEqual(await page.locator('.native-interaction-card a').count(),0);
    await load(record('mcpElicitation',{mode:'openai/userVerification',title:'Verify',serverName:'fixture'}));
    assert.strictEqual(await page.locator('.native-interaction-card input[type=radio]').count(),2);
    assert((await page.locator('.native-interaction-card').textContent()).includes('不能伪造'));
    await load(record('command'));current={...current,state:'awaiting_reissue'};
    await page.evaluate(s=>window.dispatchEvent(new CustomEvent('codex:conversation-snapshot',{detail:s})),snapshot(current));
    assert.strictEqual(await page.locator('.native-interaction-card button').count(),0);
    assert((await page.locator('.native-interaction-status').textContent()).includes('不会自动批准'));
    assert.deepStrictEqual(errors,[]);console.log('native interactions: phone widths, full choices, actual prefixed FormData, schema form, secret erasure, lifecycle passed');
  } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
