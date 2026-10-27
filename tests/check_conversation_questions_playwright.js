/* Real mobile template + real JS, isolated HTTP/notification fixtures only. */
const {chromium} = require('playwright');
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const http = require('http');
const cp = require('child_process');
const root = path.resolve(__dirname, '..');
const rendered = JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [path.join(__dirname, 'render_conversation_ui_fixtures.py')], {cwd:root, encoding:'utf8'}));
const base = JSON.parse(rendered.conversation.match(/<script id="conversation-initial-snapshot"[^>]*>([\s\S]*?)<\/script>/)[1]);
let snapshot, posts = [], failNext = false;
function scene({blocking=false, secret=false, long=false}={}) {
  const data=structuredClone(base), now=Date.now();
  data.conversation.state='active'; data.conversation.active_turn_id='turn-question';
  data.conversation.terminal_generation=0; data.conversation.last_terminal_turn_id=null;
  data.conversation.context_compaction_completed_count=2;data.conversation.context_compaction_manual_completed_count=1;data.conversation.context_compaction_other_completed_count=1;data.conversation.context_compaction_state='idle';
  data.conversation.context_switches=[{observed_at:new Date(now-60000).toISOString(),state:'output_seen'}];
  data.conversation.context_switch_count=2;
  data.conversation.usage={context_used:240,context_limit:1000,context_percent:24,input_tokens:480,cached_input_tokens:300,output_tokens:20,updated_at:new Date(now).toISOString()};
  data.conversation.revision=10;
  data.turns=[{id:'turn-question',ordinal:1,status:'inProgress',started_at:new Date(now-2000).toISOString(),commands:[{id:'start',kind:'start',state:'accepted',text:'请完成测试任务',created_at:new Date(now-2000).toISOString()}],items:[],artifacts:[]}];
  data.messages=[];data.queue=[];data.artifacts=[];
  data.questions=[{id:'question-test',turn_id:'turn-question',attempt:1,
    questions:[{id:'choice',header:'方案',question:long?'这是一个很长的中文问题，用来核对手机上的自动换行、选项和操作按钮是否仍可正常使用。'.repeat(3):'采用哪一种方案？',options:[{label:'A (Recommended)',description:long?'这个推荐方案的详细说明需要在窄屏中完整显示而不能产生横向滚动。'.repeat(4):'推荐方案说明'},{label:'B',description:'另一方案说明'}],isOther:true,isSecret:false},
      {id:'text',header:'补充',question:'请补充你的要求',options:[],isOther:false,isSecret:secret}],
    received_at:new Date(now-1000).toISOString(),deadline_at:blocking?null:new Date(now+60000).toISOString(),deadline_source:blocking?null:'host_optional_default',is_blocking:blocking,state:'awaiting_answer',answer_source:null,answers_masked:null,resolved_at:null}];
  snapshot=data;
}
function publicData(){return {...snapshot,server_now:new Date().toISOString()};}
function html(){const fragment=rendered.conversation.replace(/(<script id="conversation-initial-snapshot"[^>]*>)[\s\S]*?(<\/script>)/,(_,a,b)=>a+JSON.stringify(publicData()).replace(/</g,'\\u003c')+b);
return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><link rel="stylesheet" href="/static/css/app.css"></head><body><main>${fragment}</main></body></html>`;}
function reply(res,data,status=200){res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data));}
const server=http.createServer(async(req,res)=>{try{
  const pathname=new URL(req.url,'http://localhost').pathname;
  if(pathname.startsWith('/static/')){const file=path.join(root,pathname);if(!file.startsWith(path.join(root,'static')+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}res.writeHead(200,{'Content-Type':pathname.endsWith('.js')?'application/javascript':pathname.endsWith('.css')?'text/css':'application/octet-stream'});res.end(fs.readFileSync(file));return;}
  if(pathname.endsWith('/snapshot')){reply(res,publicData());return;}
  if(pathname.endsWith('/questions/question-test/answer')){let chunks=[];for await(const part of req)chunks.push(part);const form=await new Request('http://localhost',{method:'POST',headers:req.headers,body:Buffer.concat(chunks)}).formData();const command=form.get('command_id'),answers=JSON.parse(form.get('answers_json'));posts.push({command,answers});
    if(failNext){failNext=false;reply(res,{error:{message:'模拟临时故障'}},503);return;}
    const q=snapshot.questions[0];q.state='answered';q.answer_source='user';q.answers_masked={...answers};q.questions.forEach(x=>{if(x.isSecret)q.answers_masked[x.id]='***';});q.resolved_at=new Date().toISOString();snapshot.conversation.revision++;
    snapshot.turns[0].items=[{native_item_id:'final-test',kind:'agent_message',phase:'final_answer',text:'采用你提交的方案，测试执行完成。',created_at:new Date().toISOString()}];reply(res,publicData());return;}
  if(req.method==='POST'){reply(res,publicData());return;}
  res.writeHead(200,{'Content-Type':'text/html','Cache-Control':'no-store'});res.end(html());
}catch(error){res.writeHead(500);res.end(String(error));}});
(async()=>{await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const url=`http://127.0.0.1:${server.address().port}/conversation/conversation-ui`;const browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:390,height:844}});let errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.addInitScript(()=>{class TestSocket{constructor(){this.readyState=0;window.testSocket=this;setTimeout(()=>{this.readyState=1;if(this.onopen)this.onopen();},0);}send(text){if(text==='ping'&&this.onmessage)this.onmessage({data:'{"type":"pong"}'});}close(){this.readyState=3;}static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;}window.WebSocket=TestSocket;});
async function load(options){scene(options);await page.goto(url);await page.locator('.native-question-card').waitFor();}
async function update(){snapshot.conversation.revision++;const marker='同步检查'+snapshot.conversation.revision;snapshot.turns[0].items=snapshot.turns[0].items.filter(x=>x.native_item_id!=='sync-marker');snapshot.turns[0].items.push({native_item_id:'sync-marker',kind:'agent_message',phase:'commentary',text:marker,created_at:new Date().toISOString()});await page.evaluate(()=>window.testSocket.onmessage({data:JSON.stringify({type:'conversation.updated',data:{conversation_id:'conversation-ui'}})}));await page.waitForFunction(marker=>document.getElementById('conversation-messages').textContent.includes(marker),marker,{timeout:15000});}
try{
  await load();
  if(process.env.QUESTION_SCREENSHOT) await page.screenshot({path:process.env.QUESTION_SCREENSHOT,fullPage:true});
  assert.equal(await page.locator('.native-question-recommended').count(),1);
  assert.equal(await page.locator('.native-question-option input:checked').count(),0,'recommendation must not auto-select');
  await update(); // Keep one existing streaming event before local validation.
  await page.click('.native-question-submit');assert.match(await page.textContent('.native-question-error'),/回答全部/);assert.equal(posts.length,0);
  await page.check('.native-question-option input[value="A (Recommended)"]');await page.fill('[data-question-field="1:free"]','保持现有体验');
  await page.focus('[data-question-field="1:free"]');await page.evaluate(()=>{const e=document.activeElement;e.setSelectionRange(2,4);});
  await page.evaluate(()=>{window.__validatedQuestionCard=document.querySelector('.native-question-card');});
  await update(); // Same event count/key: only its body changes.
  assert.equal(await page.evaluate(()=>window.__validatedQuestionCard===document.querySelector('.native-question-card')),true,'streaming must not resurrect the pre-validation cached card');
  assert.equal(await page.locator('[data-question-field="1:free"]').inputValue(),'保持现有体验');
  assert.equal(await page.locator('.native-question-option input[value="A (Recommended)"]').isChecked(),true);
  assert.deepEqual(await page.evaluate(()=>({field:document.activeElement.dataset.questionField,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd})),{field:'1:free',start:2,end:4});
  snapshot.turns[0].items.push({native_item_id:'stream-1',kind:'agent_message',phase:'commentary',text:'另一条进度消息',created_at:new Date().toISOString()});await update();
  assert.equal(await page.locator('[data-question-field="1:free"]').inputValue(),'保持现有体验');
  assert.deepEqual(await page.evaluate(()=>({field:document.activeElement.dataset.questionField,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd})),{field:'1:free',start:2,end:4});
  failNext=true;await page.click('.native-question-submit');await page.waitForSelector('.native-question-error:not([hidden])');await page.waitForTimeout(300);await page.click('.native-question-submit');await page.waitForSelector('.native-question-receipt');
  assert.equal(posts.length,2);assert.equal(posts[0].command,posts[1].command,'retry must keep idempotency key');assert.equal(posts[1].answers.choice,'A (Recommended)');assert.equal(posts[1].answers.text,'保持现有体验');assert.match(await page.textContent('.native-question-answers'),/保持现有体验/);assert.match(await page.textContent('#conversation-messages'),/测试执行完成/);
  await page.click('.native-question-history summary');assert.match(await page.textContent('.native-question-history'),/采用哪一种/);
  await page.reload();await page.locator('.native-question-receipt').waitFor();assert.match(await page.textContent('.native-question-answers'),/保持现有体验/);
  await load({blocking:true,secret:true});assert.equal(await page.textContent('.native-question-countdown'),'需要回答');
  await page.fill('[data-question-field="0:free"]','我的方案');await page.fill('[data-question-field="1:free"]','secret-only-in-memory');assert.equal(await page.locator('[data-question-field="1:free"]').getAttribute('type'),'password');
  assert.equal(await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}).includes('secret-only-in-memory')),false);
  await page.click('.native-question-submit');await page.locator('.native-question-receipt').waitFor();assert.doesNotMatch(await page.textContent('.native-question-card'),/secret-only-in-memory/);assert.match(await page.textContent('.native-question-answers'),/不回显/);
  await load();const deadline=snapshot.questions[0].deadline_at;await page.reload();assert.equal(snapshot.questions[0].deadline_at,deadline);
  snapshot.questions[0].deadline_at=new Date(Date.now()+100).toISOString();await update();const before=posts.length;await page.waitForTimeout(1300);assert.match(await page.textContent('.native-question-countdown'),/后台/);assert.equal(posts.length,before,'browser countdown must never send empty answers');assert.equal(await page.locator('.native-question-submit').isDisabled(),true);
  snapshot.questions[0].state='answered';snapshot.questions[0].answer_source='deadline';snapshot.questions[0].answers_masked={};await update();assert.match(await page.textContent('.native-question-receipt'),/已按推荐项自动继续/);
  for(const state of ['delivery_unknown','awaiting_reissue','closed']){snapshot.questions[0].state=state;await update();assert.doesNotMatch(await page.textContent('.native-question-receipt'),/已自动继续/);assert.equal(await page.locator('.native-question-submit').count(),0);}
  for(const width of [360,390,430]){await page.setViewportSize({width,height:844});await load({blocking:true,long:true});for(const theme of ['dark','light']){await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`overflow ${width} ${theme}`);await page.locator('.native-question-submit').scrollIntoViewIfNeeded();assert.equal(await page.locator('.native-question-submit').isVisible(),true);}}
  await page.setViewportSize({width:360,height:520});await page.evaluate(()=>document.documentElement.style.fontSize='20px');await page.focus('[data-question-field="1:free"]');assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'large font/short viewport overflow');
  await page.click('#conversation-context-toggle');
  assert.equal(await page.locator('#context-compaction-count').textContent(),'已记录 2 次');
  assert.equal(await page.locator('#context-switch-count').textContent(),'已记录 2 次');
  assert.equal(await page.locator('#context-details').evaluate(node=>node.open),false);
  for(const [id,value] of [['context-input','480'],['context-cached','300'],['context-output','20']]){
    assert.equal(await page.locator('#'+id).isVisible(),true,`${id} must be visible with details closed`);
    assert.equal(await page.locator('#'+id).textContent(),value);
    assert.equal(await page.locator('#'+id).evaluate(node=>node.closest('details')),null,`${id} must remain outside details`);
  }
  assert.equal(await page.locator('.native-context-scope').isVisible(),false);
  if(process.env.CONTEXT_SCREENSHOT) await page.screenshot({path:process.env.CONTEXT_SCREENSHOT,fullPage:true});
  await page.locator('[data-close-context]').focus();
  await page.keyboard.press('Tab');await page.keyboard.press('Tab');
  assert.equal(await page.locator('#context-details > summary').evaluate(node=>node===document.activeElement),true,'details must be reachable by keyboard');
  assert.equal(await page.locator('#context-details > summary').evaluate(node=>getComputedStyle(node).outlineStyle),'solid','keyboard focus must have a visible outline');
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#context-details').evaluate(node=>node.open),true);
  assert.equal(await page.locator('#context-compaction-breakdown').textContent(),'手动请求 1 次 · 其他压缩 1 次');
  assert.equal(await page.locator('#context-input').textContent(),'480');
  assert.equal(await page.locator('#context-cached').textContent(),'300');
  assert.equal(await page.locator('#context-output').textContent(),'20');
  assert.match(await page.textContent('.native-context-scope'),/Token 消耗与当前上下文占用不是同一指标/);
  assert.match(await page.textContent('.native-context-scope'),/记录可能重叠，不相加/);
  const savedSwitches=snapshot.conversation.context_switches;
  snapshot.conversation.context_compaction_completed_count=0;snapshot.conversation.context_compaction_manual_completed_count=0;snapshot.conversation.context_compaction_other_completed_count=0;snapshot.conversation.context_switch_count=0;snapshot.conversation.context_switches=[];await update();
  assert.equal(await page.locator('#context-compaction-count').textContent(),'暂无记录');
  assert.equal(await page.locator('#context-switch-count').textContent(),'暂无记录');
  assert.match(await page.textContent('#context-switch-events'),/暂无记录/);
  assert.equal(await page.locator('#context-compaction-count').evaluate(node=>getComputedStyle(node).color===getComputedStyle(node.parentElement.querySelector('span')).color),true,'empty records must use neutral muted text');
  assert.equal(await page.locator('#context-details').evaluate(node=>node.open),true);
  snapshot.conversation.context_compaction_completed_count=2;snapshot.conversation.context_compaction_manual_completed_count=1;snapshot.conversation.context_compaction_other_completed_count=1;snapshot.conversation.context_switch_count=2;snapshot.conversation.context_switches=savedSwitches;snapshot.conversation.context_compaction_state='busy';await update();
  assert.equal(await page.locator('#context-compaction-count').textContent(),'已记录 2 次');
  assert.equal(await page.locator('#compact-conversation').isDisabled(),true);
  assert.match(await page.textContent('#context-compaction-hint'),/等待 App Server 返回完成事件/);
  assert.equal(await page.locator('#context-details').evaluate(node=>node.open),true);
  await page.locator('#context-details > summary').focus();await page.keyboard.press('Enter');
  assert.equal(await page.locator('#context-details').evaluate(node=>node.open),false);
  snapshot.conversation.usage.input_tokens=640;snapshot.conversation.usage.cached_input_tokens=400;snapshot.conversation.usage.output_tokens=40;await update();
  for(const [id,value] of [['context-input','640'],['context-cached','400'],['context-output','40']]){
    assert.equal(await page.locator('#'+id).isVisible(),true,`${id} must stay visible after a live update`);
    assert.equal(await page.locator('#'+id).textContent(),value);
  }
  assert.equal(await page.locator('#context-details').evaluate(node=>node.open),false,'live updates must not expand details');
  assert.equal(await page.locator('#context-compaction-hint').isVisible(),true,'busy status must remain visible with details closed');
  snapshot.conversation.context_compaction_state='failed';await update();
  assert.equal(await page.locator('#context-compaction-count').textContent(),'已记录 2 次');
  assert.equal(await page.locator('#compact-conversation').isDisabled(),false);
  assert.match(await page.textContent('#context-compaction-hint'),/本次手动压缩未完成/);
  assert.equal(await page.locator('#context-compaction-hint').isVisible(),true,'failure status must remain visible with details closed');
  await page.locator('#context-details > summary').focus();await page.keyboard.press('Enter');
  assert.equal(await page.locator('#context-details').evaluate(node=>node.open),true);
  assert.match(await page.textContent('#context-switch-details'),/已收到返回/);
  assert.match(await page.textContent('#context-switch-details'),/换窗工具已返回/);
  for(const width of [320,360,390,430]){await page.setViewportSize({width,height:520});for(const theme of ['dark','light']){await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);await page.locator('#compact-conversation').scrollIntoViewIfNeeded();assert.equal(await page.locator('#compact-conversation').isVisible(),true);await page.locator('[data-close-context]').scrollIntoViewIfNeeded();assert.equal(await page.locator('[data-close-context]').isVisible(),true);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`context overflow ${width} ${theme}`);assert.equal(await page.locator('#conversation-context').evaluate(node=>node.scrollWidth>node.clientWidth),false,`context contents overflow ${width} ${theme}`);}}
  assert.deepEqual(errors,[]);
  console.log('PASS: questions choice/text/secret, recommendation, validation, streaming focus/caret, idempotent retry, history/reload, server-only deadline, unknown/closed states, 360/390/430 dark/light, large font/keyboard viewport; compact context defaults/details, always-visible input/cache/output with live updates, live-update openness, neutral empty records, keyboard focus/toggle, busy/failed status outside details, 320/360/390/430 dark/light without overflow.');
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
