const assert = require('assert/strict');
const { chromium } = require('playwright');
const childProcess = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');
const rendered = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv', 'bin', 'python')), [path.join(__dirname, 'render_conversation_ui_fixtures.py')], { cwd: root, encoding: 'utf8' }));
const initialText = rendered.conversation.match(/id="conversation-initial-snapshot"[^>]*>([\s\S]*?)<\/script>/)[1];
const initial = JSON.parse(initialText);
initial.goal = null; initial.goal_sync_state = 'synced';
const legacy = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv', 'bin', 'python')), [path.join(__dirname, 'render_conversation_ui_fixtures.py')], { cwd: root, encoding: 'utf8', env: {...process.env, COMPACT_CONTROLS_LEGACY_FIXTURE:'1'} }));
let settingsFailure = false; let settingsPosts = 0; let goalPosts = 0; let goalSuccess = false; let goalConflict = false; const goalActions=[];
function doc(body) { return '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><link rel="stylesheet" href="/static/css/app.css"><main>' + body + '</main><script src="/static/draft.js" defer></script><script src="/static/setting-segments.js" defer></script><script src="/static/light-dismiss.js" defer></script>'; }
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/conversation') { res.setHeader('content-type', 'text/html; charset=utf-8'); res.end(doc(rendered.conversation)); return; }
  if (url.pathname === '/legacy-conversation') { res.setHeader('content-type', 'text/html; charset=utf-8'); res.end(doc(legacy.conversation)); return; }
  if (url.pathname === '/legacy-project') { res.setHeader('content-type', 'text/html; charset=utf-8'); res.end(doc(legacy.project)); return; }
  if (url.pathname === '/project') { res.setHeader('content-type', 'text/html; charset=utf-8'); res.end(doc(rendered.project)); return; }
  if ((url.pathname === '/conversation/conversation-ui' || url.pathname === '/conversation/conversation-ui/snapshot') && req.method === 'GET') { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(initial)); return; }
  if (url.pathname.startsWith('/conversation/conversation-ui/actions/') && req.method === 'GET') { res.setHeader('content-type','application/json'); res.end(JSON.stringify({snapshot:initial,command:{id:url.pathname.split('/').pop(),state:'delivery_unknown'}})); return; }
  if (url.pathname === '/conversation/conversation-ui/goal' && req.method === 'GET') { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ snapshot: initial })); return; }
  if (['/conversation/conversation-ui/goal', '/conversation/conversation-ui/goal/start'].includes(url.pathname) && req.method === 'POST') {
    goalPosts++; let body=''; req.on('data',chunk=>body+=chunk); req.on('end',async()=>{
      const fields=await new Response(body,{headers:{'content-type':req.headers['content-type']}}).formData();
      goalActions.push(fields.get('action') || 'start'); res.setHeader('content-type','application/json');
      if (goalConflict) { res.statusCode=409; res.end(JSON.stringify({error:'目标已变化，请核对',snapshot:initial,command:{id:fields.get('command_id'),state:'rejected'}})); return; }
      if (!goalSuccess) { res.statusCode=503; res.end(JSON.stringify({error:'目标提交结果待核对。',command:{id:fields.get('command_id'),state:'delivery_unknown'}})); return; }
      initial.goal=fields.get('action')==='clear'?null:{objective:fields.get('objective')||'保存目标',status:fields.get('status')||'paused',version:String(goalPosts)};
      if (url.pathname.endsWith('/start')) initial.goal.status='active';
      res.end(JSON.stringify({snapshot:initial,command:{id:fields.get('command_id'),state:'accepted'}}));
    }); return;
  }
  if (url.pathname === '/conversation/conversation-ui/settings' && req.method === 'POST') { settingsPosts++; let body=''; req.on('data',chunk=>body+=chunk); req.on('end',async()=>{ const fields=await new Response(body,{headers:{'content-type':req.headers['content-type']}}).formData(); if (settingsFailure) { res.statusCode=409; res.setHeader('content-type','application/json'); res.end(JSON.stringify({error:'设置未保存',command:{id:fields.get('command_id'),state:'rejected'},snapshot:initial})); return; } for (const key of ['model','effort','permission','collaboration_mode','service_tier']) if(fields.has(key)) initial.conversation.settings[key]=fields.get(key)||null; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(initial)); }); return; }
  if (url.pathname.startsWith('/static/')) { const file = path.join(root, url.pathname); if (file.startsWith(path.join(root, 'static')) && fs.existsSync(file)) { res.end(fs.readFileSync(file)); return; } }
  res.statusCode = 404; res.end();
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    for (const width of [320, 360, 412, 430]) {
      const context = await browser.newContext({ viewport: { width, height: 844 } }); const page = await context.newPage();
      await page.goto(`http://127.0.0.1:${server.address().port}/conversation`, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(250);
      await page.screenshot({ path: `/tmp/compact-mobile-conversation-${width}.png`, fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth <= 1), true, `${width}px 横向溢出`);
      assert.equal(await page.locator('.native-compose-tools > [data-conversation-control]').count(), 0, 'composer 不应新增设置按钮');
      await page.locator('#conversation-settings > summary').click(); await page.waitForTimeout(50); assert.equal(await page.locator('#conversation-settings').evaluate(e=>e.open), true, '设置弹层未打开');
      await page.screenshot({ path: `/tmp/compact-mobile-settings-${width}.png`, fullPage: true });
      assert.equal(await page.locator('#conversation-settings [data-conversation-control]').count(), 3, '设置弹层应含三项紧凑按钮');
      assert.equal(await page.locator('.setting-segments-model .setting-segment').count() > 0, true, '模型分段控件缺失');
      assert.equal(await page.locator('.setting-segments-effort .setting-segment').count() > 0, true, '思考等级分段控件缺失');
      await page.locator('[data-conversation-control="mode"]').click(); await page.locator('input[value="plan"]').check(); await page.keyboard.press('Escape');
      assert.equal(await page.inputValue('#conversation-collaboration-mode'), 'default', '取消不应修改模式');
      if (width === 320) {
        await page.locator('[data-conversation-control="goal"]').dispatchEvent('click');
        await page.locator('#conversation-option-sheet[open]').waitFor();
        await page.locator('#conversation-goal-draft').fill('核对目标投递');
        await page.locator('#conversation-option-done').dispatchEvent('click');
        await page.waitForFunction(() => document.querySelector('#conversation-option-done').disabled);
        assert.equal(goalPosts, 1, '目标只可提交一次，不能自动重试');
        await page.locator('[data-close-conversation-option]').click();
        await page.locator('#conversation-option-sheet').evaluate(node => !node.open || Promise.reject(new Error('sheet still open')));
        await page.locator('[data-conversation-control="goal"]').dispatchEvent('click');
        await page.locator('#conversation-option-sheet[open]').waitFor();
        assert.equal(await page.inputValue('#conversation-goal-draft'), '核对目标投递', '不确定失败后应保留输入供重新核对');
        await page.keyboard.press('Escape');
      }
      assert.equal((await page.textContent('#conversation-tier-toggle')).trim(), '⚡Fast', '右上速度控件外观或逻辑被改变');
      const project = await context.newPage();
      await project.goto(`http://127.0.0.1:${server.address().port}/project`, { waitUntil: 'domcontentloaded' }); await project.waitForTimeout(100);
      await project.screenshot({ path: `/tmp/compact-mobile-project-${width}.png`, fullPage: true });
      assert.equal(await project.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth <= 1), true, `${width}px 项目页横向溢出`);
      assert.equal(await project.locator('.compact-control-grid [data-project-control]').count(), 4, '项目页应有四项紧凑按钮');
      await project.locator('[data-project-control="speed"]').click();
      await project.locator('input[name="project-option-choice"][value="priority"]').check();
      await project.locator('#project-option-done').click();
      assert.equal(await project.textContent('[data-project-control="speed"]'),'Fast');
      await project.locator('#project-conversation-model').selectOption('gpt-6-luna');
      assert.equal(await project.inputValue('#project-conversation-tier'),'');
      assert.equal(await project.textContent('[data-project-control="speed"]'),'标准速度','模型移除Fast能力后，摘要必须跟随实际提交值');
      await project.locator('#project-conversation-model').selectOption('gpt-6-sol');
      await project.locator('[data-project-control="permission"]').click(); await project.locator('input[value="read-only"]').check(); await project.keyboard.press('Escape');
      assert.equal(await project.inputValue('#project-conversation-permission'), 'danger-full-access', '项目页取消不应修改权限');
      if (width === 320) {
        await project.locator('[data-project-control="goal"]').click();
        await project.locator('#project-goal-draft').fill('上传后仍保留的目标');
        await project.locator('[data-project-goal-status="paused"]').click();
        await project.locator('#project-option-done').click();
        await project.reload({waitUntil:'domcontentloaded'});
        assert.equal(await project.inputValue('#project-goal-objective'), '上传后仍保留的目标');
        assert.equal(await project.inputValue('#project-goal-status'), 'paused');
        await project.locator('[data-project-control="goal"]').click();
        await project.locator('[data-project-goal-clear]').click();
        await project.locator('[data-project-goal-status="paused"]').click();
        assert.equal(await project.locator('#project-goal-draft').isEnabled(),true);
        assert.equal(await project.textContent('[data-project-goal-clear]'),'清除目标');
        await project.keyboard.press('Escape');
        assert.equal(await project.inputValue('#project-goal-objective'), '上传后仍保留的目标', '取消清除不应丢目标');
        await project.locator('[data-project-control="goal"]').click();
        await project.locator('[data-project-goal-clear]').click();
        await project.locator('#project-option-done').click();
        await project.reload({waitUntil:'domcontentloaded'});
        assert.equal(await project.inputValue('#project-goal-objective'), '', '清除必须跨上传往返保留');
      }
      await project.close();
      await context.close();
    }
    assert.equal(settingsPosts, 0, '取消不应保存设置'); assert.equal(goalPosts, 1, '不确定目标投递不能自动重试');
    const page = await browser.newPage({viewport:{width:390,height:844}}); let browserErrors=[]; page.on('pageerror', e=>browserErrors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/conversation`);
    for (const [kind,value] of [['mode','plan'],['permission','read-only'],['mode','default']]) {
      await page.locator('#conversation-settings').evaluate(e=>e.open=true);
      await page.locator('[data-conversation-control="'+kind+'"]').click();
      assert.equal(await page.locator('#conversation-option-done').isEnabled(), true, '成功保存后重新打开必须可提交');
      await page.locator('input[name="conversation-option-choice"][value="'+value+'"]').check();
      await page.locator('#conversation-option-done').click();
      await page.waitForFunction(()=>!document.querySelector('#conversation-option-sheet').open);
    }
    assert.equal(settingsPosts,3);
    settingsFailure=true;
    for (const [kind,value,originalValue,label] of [['permission','danger-full-access','read-only','只读'],['mode','plan','default','默认执行']]) {
      await page.locator('#conversation-settings').evaluate(e=>e.open=true);
      await page.locator('[data-conversation-control="'+kind+'"]').click();
      await page.locator('input[name="conversation-option-choice"][value="'+value+'"]').check();
      await page.locator('#conversation-option-done').click();
      await page.waitForFunction(()=>!document.querySelector('#conversation-option-error').hidden);
      const select=kind==='mode'?'#conversation-collaboration-mode':'#conversation-permission';
      assert.equal(await page.inputValue(select),originalValue);
      assert.equal(await page.textContent('[data-conversation-control="'+kind+'"]'),label,'保存失败必须恢复真实摘要，不能假示更低权限');
      await page.keyboard.press('Escape');
      assert.equal(await page.textContent('[data-conversation-control="'+kind+'"]'),label);
    }
    settingsFailure=false;
    goalSuccess=true;
    for (let n=0; n<2; n++) {
      await page.locator('#conversation-settings').evaluate(e=>e.open=true);
      await page.locator('[data-conversation-control="goal"]').click();
      await page.locator('#conversation-goal-draft').fill('保存目标');
      assert.equal(await page.locator('#conversation-option-done').isEnabled(), true);
      await page.locator('#conversation-option-done').click();
      await page.waitForFunction(()=>!document.querySelector('#conversation-option-sheet').open);
    }
    // A persisted active Goal is not proof of a running model turn. In
    // native Plan mode it waits for input and must not claim to be executing.
    initial.goal={objective:'等待用户继续',status:'active',version:'idle-plan'};
    initial.conversation.state='idle'; initial.conversation.settings.collaboration_mode='plan'; initial.conversation.revision++;
    await page.locator('#reconcile-conversation').dispatchEvent('click');
    await page.waitForFunction(()=>document.querySelector('[data-conversation-control="goal"]').textContent==='目标待继续（Plan）');
    assert.equal(await page.locator('.native-goal-summary strong').textContent(),'目标待继续（Plan）');
    assert.equal(await page.inputValue('#conversation-collaboration-mode'),'plan','显示等待状态不得偷偷切模式');
    initial.conversation.settings.collaboration_mode='default'; initial.conversation.revision++;
    await page.locator('#reconcile-conversation').dispatchEvent('click');
    await page.waitForFunction(()=>document.querySelector('[data-conversation-control="goal"]').textContent==='目标待继续');
    assert.equal(await page.locator('.native-goal-summary strong').textContent(),'目标待继续');
    initial.conversation.state='active'; initial.conversation.revision++;
    await page.locator('#reconcile-conversation').dispatchEvent('click');
    await page.waitForFunction(()=>document.querySelector('[data-conversation-control="goal"]').textContent==='目标进行中');
    assert.equal(await page.locator('.native-goal-summary strong').textContent(),'目标进行中');
    initial.conversation.state='idle';
    initial.goal={objective:'已完成目标',status:'complete',version:'native-complete'};
    await page.locator('#conversation-settings').evaluate(e=>e.open=true);
    await page.locator('[data-conversation-control="goal"]').click();
    await page.locator('[data-clear-goal]').click();
    await page.locator('#conversation-option-done').click();
    await page.waitForFunction(()=>!document.querySelector('#conversation-option-sheet').open);
    assert.equal(goalActions.at(-1),'clear','已完成目标选择清除必须真发clear而非complete');
    assert.equal(initial.goal,null);
    // The sheet owns a captured Goal state. A live clear is a normal stale
    // version rejection, not a synchronous null dereference or lost draft.
    initial.goal={objective:'核对实时清除',status:'active',version:'before-clear'};
    await page.locator('#conversation-settings').evaluate(e=>e.open=true);
    await page.locator('[data-conversation-control="goal"]').click();
    await page.locator('[data-clear-goal]').click();
    await page.locator('[data-goal-status="paused"]').click();
    assert.equal(await page.locator('#conversation-goal-draft').isEnabled(),true);
    assert.equal(await page.textContent('[data-clear-goal]'),'清除目标');
    initial.goal=null; initial.conversation.revision++;
    await page.locator('#reconcile-conversation').dispatchEvent('click');
    await page.waitForFunction(()=>document.querySelector('[data-conversation-control="goal"]').textContent==='设置目标');
    goalConflict=true; const beforeConflict=goalPosts;
    await page.locator('#conversation-option-done').click();
    await page.waitForFunction(()=>!document.querySelector('#conversation-option-error').hidden);
    assert.equal(goalPosts,beforeConflict+1);
    assert.equal(goalActions.at(-1),'pause');
    assert.equal(await page.inputValue('#conversation-goal-draft'),'核对实时清除');
    assert.equal(await page.locator('#conversation-option-sheet').evaluate(e=>e.open),true);
    goalConflict=false; await page.keyboard.press('Escape');
    for (const target of ['legacy-conversation','legacy-project']) {
      await page.goto(`http://127.0.0.1:${server.address().port}/`+target);
      await page.waitForTimeout(200);
      assert.equal(await page.locator('[data-conversation-control],[data-project-control]').count(),0,'旧进程不能提前露出未加载的Goal界面');
    }
    assert.deepEqual(browserErrors,[], '旧模板必须兼容新静态文件');
    await page.close();
    console.log('compact_mobile_controls=passed widths=320,360,412,430 settings_panel_only=true cancel_staged=true screenshots=/tmp/compact-mobile-{conversation,project}-*.png');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
