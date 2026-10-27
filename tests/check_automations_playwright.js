const {chromium}=require('playwright'),cp=require('child_process'),path=require('path');
const root=path.resolve(__dirname,'..'),server=cp.spawn((process.env.CODEX_WEB_PYTHON || path.join(root,'.venv/bin/python')),[path.join(__dirname,'settings_center_fixture_server.py'),'--proxy'],{cwd:root,env:process.env});
const assert=(value,message)=>{if(!value)throw new Error(message);};
let browser;
(async()=>{
  const address=await new Promise((resolve,reject)=>{let out='',err='';const t=setTimeout(()=>reject(new Error('fixture timeout '+err)),30000);server.stderr.on('data',d=>err+=d);server.stdout.on('data',d=>{out+=d;const line=out.split('\n').find(l=>l.startsWith('{'));if(line){clearTimeout(t);resolve(JSON.parse(line));}});server.on('exit',code=>reject(new Error('fixture exited '+code+' '+err)));});
  const base='https://127.0.0.1:'+address.port;
  browser=await chromium.launch({headless:true});const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:393,height:852},isMobile:true,hasTouch:true});const errors=[];context.on('page',page=>page.on('pageerror',e=>errors.push(String(e))));
  async function fixturePost(route,data){
    const response=await context.request.post(base+route,{headers:{Origin:base},data});
    assert(response.ok(),'fixture request failed: '+route+' '+response.status()+' '+await response.text());
    return response.json();
  }
  let page=await context.newPage();await page.goto(base+'/settings/automations');await page.locator('#auto-empty').waitFor({state:'visible'});
  assert((await context.request.get(base+'/automations')).status()===404,'fixture must reproduce blocked legacy gateway path');
  // APIRequestContext does not automatically send the browser's Origin header.
  // Keep the actual same-origin middleware enabled for all fixture mutations.
  const seeded=await fixturePost('/fixture/automation-seed');const cid=seeded.conversation_id;
  const availableResponse=await context.request.get(base+'/settings/automations/api/tasks?conversation_id='+cid);
  assert(availableResponse.ok(),'seeded conversation list failed: '+availableResponse.status());
  const available=await availableResponse.json();
  assert((available.conversations||[]).some(c=>c.id===cid),'seeded conversation unavailable');
  await page.goto(base+'/settings/automations?conversation_id='+cid);await page.waitForFunction(()=>document.querySelector('#auto-create')&&!document.querySelector('#auto-create').disabled);
  await page.click('#auto-create');await page.locator('#auto-dialog[open]').waitFor();await page.fill('#auto-title','Windows 内存巡检');await page.fill('#auto-prompt','只读检查 Windows 剩余物理内存，报告总量、可用量和使用率，不修改系统。');await page.check('#auto-confirm');
  let layouts=0;
  for(const width of [320,393,412])for(const theme of ['light','dark'])for(const size of [16,20]){
    await page.setViewportSize({width,height:852});await page.evaluate(({theme,size})=>{window.codexSetTheme(theme);document.documentElement.style.fontSize=size+'px';},{theme,size});
    for(const kind of ['interval','daily','weekly','once']){
      await page.selectOption('#auto-kind',kind);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'page overflow '+width+'/'+theme+'/'+kind);
      assert(await page.evaluate(()=>{const d=document.querySelector('#auto-dialog');return d.scrollWidth<=d.clientWidth+1;}),'dialog overflow '+width+'/'+theme+'/'+kind);layouts++;
    }
  }
  await page.selectOption('#auto-kind','interval');await page.fill('#auto-interval','60');
  const invalidFields=await page.locator('#auto-form').evaluate(form=>[...form.elements].filter(field=>field.willValidate&&!field.validity.valid).map(field=>field.id));
  assert(!invalidFields.length,'create form invalid: '+invalidFields.join(','));
  await page.click('#auto-save');await page.waitForFunction(()=>document.querySelector('.automation-card')||document.querySelector('#auto-form-status').classList.contains('automation-error'));
  assert(!(await page.locator('#auto-form-status').getAttribute('class')||'').includes('automation-error'),'create failed: '+await page.textContent('#auto-form-status'));
  await page.locator('.automation-card').waitFor();
  const taskId=await page.locator('.automation-card').getAttribute('data-task-id');assert((await page.textContent('.automation-card')).includes('每 60 分钟'),'rule missing');
  // No browser page is open while three separate background turns execute.
  await page.close();
  for(let i=1;i<=3;i++)await fixturePost('/fixture/automation-tick',{task_id:taskId,text:'第'+i+'次 Windows 内存：'+(16-i)+' GiB'});
  page=await context.newPage();await page.goto(base+'/conversation/'+cid);await page.waitForFunction(()=>document.querySelector('#conversation-messages').textContent.includes('第3次 Windows 内存'));
  const history=await page.textContent('#conversation-messages');for(let i=1;i<=3;i++)assert(history.includes('第'+i+'次 Windows 内存'),'missing historical run '+i);assert(history.includes('定时执行'),'scheduled origin missing');
  const menuLink=page.locator('.native-head-menu-panel a').filter({hasText:'定时任务'});
  const automationLinks=page.locator('a[href*="/settings/automations?conversation_id="]');
  assert(await automationLinks.count()>=2,'conversation menu and summary must use gateway-safe path');
  await page.getByLabel('更多操作',{exact:true}).click();await menuLink.click();await page.locator('.automation-card').waitFor();
  assert(new URL(page.url()).pathname==='/settings/automations','menu must reach canonical settings route');
  await page.goto(base+'/conversation/'+cid);
  await page.locator('#conversation-automation-summary').waitFor({state:'visible'});
  await page.click('#conversation-automation-summary');await page.locator('.automation-card').waitFor();await page.getByRole('button',{name:'运行记录',exact:true}).click();await page.locator('#auto-runs-dialog[open]').waitFor();assert(await page.locator('.automation-run').count()===3,'run ledger count wrong');assert((await page.textContent('#auto-runs')).includes('已完成'),'completion missing');
  await page.click('#auto-runs-close');page.on('dialog',d=>d.accept());await page.getByRole('button',{name:'暂停',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.automation-card').textContent.includes('已暂停'));
  await page.getByRole('button',{name:'恢复',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.automation-card').textContent.includes('已启用'));
  await page.getByRole('button',{name:'修改',exact:true}).click();await page.fill('#auto-title','修改后的巡检');await page.check('#auto-confirm');await page.click('#auto-save');await page.waitForFunction(()=>document.querySelector('.automation-card h2').textContent==='修改后的巡检');
  await page.reload();await page.waitForFunction(()=>document.querySelector('.automation-card h2').textContent==='修改后的巡检');await page.getByRole('button',{name:'删除',exact:true}).click();await page.locator('#auto-empty').waitFor({state:'visible'});
  await page.goto(base+'/conversation/'+cid);await page.waitForFunction(()=>document.querySelector('#conversation-messages').textContent.includes('第3次 Windows 内存'));assert((await page.textContent('#conversation-messages')).includes('第1次 Windows 内存'),'delete removed chat');
  assert(!errors.length,'page errors: '+errors.join('\n'));console.log(JSON.stringify({status:'passed',layouts,background_runs:3,checks:['closed_page_execution','same_chat_history','origin','ledger','pause_resume_edit_delete','durability','no_page_errors']}));
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill('SIGTERM');});
