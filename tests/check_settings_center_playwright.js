/* Real settings routes and SQLite, with loopback TLS + isolated native fixture. */
const {chromium}=require('playwright');
const {spawn}=require('child_process');
const path=require('path');
const fs=require('fs');
const screenshotArg=process.argv.find(value=>value.startsWith('--screenshots='));
const screenshotDir=screenshotArg&&screenshotArg.slice(14);if(screenshotDir)fs.mkdirSync(screenshotDir,{recursive:true});
const root=path.resolve(__dirname,'..');
const assert=(ok,message)=>{if(!ok)throw new Error(message);};
async function assertSecondaryFilter(input) {
  const state=await input.evaluate(input=>{
    const field=input.closest('.search-field'),icon=field.querySelector('.search-icon');
    const gray=document.createElement('span');gray.style.color='var(--muted)';field.append(gray);
    const color=getComputedStyle(gray).color;gray.remove();
    const rect=icon.getBoundingClientRect(),parent=field.getBoundingClientRect();
    return {gray:getComputedStyle(icon).color===color,inside:rect.left>=parent.left&&rect.right<=parent.right,
      height:input.getBoundingClientRect().height,font:parseFloat(getComputedStyle(input).fontSize),
      buttons:field.querySelectorAll('button').length,label:input.getAttribute('aria-label')};
  });
  assert(state.gray&&state.inside&&state.height>=44&&state.font>=16&&!state.buttons&&state.label,
    '即时筛选不是框内次级灰色图标 '+JSON.stringify(state));
}
(async()=>{
  const proxy=process.env.CODEX_SETTINGS_GATEWAY!=='direct';
  const server=spawn((process.env.CODEX_WEB_PYTHON || path.join(root,'.venv/bin/python')),[path.join(__dirname,'settings_center_fixture_server.py'),...(proxy?['--proxy']:[])],{cwd:root,stdio:['ignore','pipe','pipe']});
  let stderr='';server.stderr.on('data',chunk=>stderr+=chunk);
  const port=await new Promise((resolve,reject)=>{let data='';server.stdout.on('data',chunk=>{data+=chunk;if(data.includes('\n'))resolve(JSON.parse(data.split('\n')[0]).port);});server.on('exit',code=>reject(new Error('fixture exited '+code+' '+stderr)));});
  const base='https://127.0.0.1:'+port;
  const browser=await chromium.launch({headless:true});
  const errors=[];let debugPage;
  try{
    // Slow optional preferences must not lose an early user change or block
    // unrelated settings. Only dependent controls wait for their real API.
    const delayedContext=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:393,height:852}});
    let releasePreferences;
    const heldPreferences=new Promise(resolve=>{releasePreferences=resolve;});
    await delayedContext.route('**/static/reading-preferences.js*',async route=>{await heldPreferences;await route.continue();});
    const delayedPage=await delayedContext.newPage();
    await delayedPage.goto(base+'/settings#appearance',{waitUntil:'domcontentloaded'});
    await delayedPage.waitForFunction(()=>document.querySelector('#sc-summary-models').textContent.includes('6-sol'));
    for(const id of ['theme-select','sc-font','sc-wrap','sc-send','sc-appearance-reset','sc-input-test'])assert(await delayedPage.locator('#'+id).isDisabled(),'preference control enabled before API ready: '+id);
    assert(await delayedPage.locator('#sc-appearance-loading').isVisible(),'missing local preference loading explanation');
    await delayedPage.click('#sc-back');await delayedPage.click('[data-go="models"]');
    assert(await delayedPage.locator('[data-setting="permission"]').isEnabled(),'optional preferences blocked ordinary settings');
    await delayedPage.click('#sc-back');await delayedPage.click('[data-go="appearance"]');
    releasePreferences();await delayedPage.waitForFunction(()=>!document.querySelector('#theme-select').disabled);
    await delayedPage.selectOption('#theme-select','light');
    assert(await delayedPage.evaluate(()=>localStorage.getItem('codex-theme'))==='light','late-ready theme choice not durable');
    assert(!await delayedPage.locator('#sc-appearance-loading').isVisible(),'ready preferences remained blocked');
    await delayedContext.close();
    const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:393,height:852}});
    const page=await context.newPage();debugPage=page;page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base+'/settings');await page.waitForFunction(()=>document.querySelector('#sc-summary-models').textContent.includes('6-sol'));
    await assertSecondaryFilter(page.locator('#sc-search'));
    // Optional focused search check; the comprehensive flow also follows current UI.
    if(process.argv.includes('--search-only')){
      let layouts=0;
      for(const width of [320,390,412])for(const theme of ['dark','light'])for(const size of [16,20]){
        await page.setViewportSize({width,height:852});
        await page.evaluate(({theme,size})=>{window.codexSetTheme(theme);document.documentElement.style.fontSize=size+'px';},{theme,size});
        await assertSecondaryFilter(page.locator('#sc-search'));
        await page.fill('#sc-search','预设');
        assert(await page.locator('.sc-home-row:visible').count()===1,'settings live filter failed');
        await page.fill('#sc-search','不存在的设置');
        assert(await page.locator('#sc-empty').isVisible(),'settings empty state missing');
        await page.fill('#sc-search','');
        await page.click('[data-go="tools"]');await page.click('#sc-skill-picker summary');
        const picker=page.locator('#sc-skill-picker');
        await assertSecondaryFilter(picker.locator('[data-skill-search]'));
        await picker.locator('[data-skill-search]').fill('多维表');
        assert(await picker.locator('[data-skill-choice]:visible').count()===1,'skills live filter failed');
        const selected=await picker.locator('input[value="dingtalk-aitable"]').isChecked();
        await picker.locator('input[value="dingtalk-aitable"]').check();
        await picker.locator('[data-skill-search]').fill('没有这样的技能');
        assert(await picker.locator('[data-skill-empty]').isVisible(),'skills empty state missing');
        assert(await picker.locator('input[value="dingtalk-aitable"]').isChecked(),'filter lost skill selection');
        await picker.locator('[data-skill-search]').fill('');
        await picker.locator('input[value="dingtalk-aitable"]').setChecked(selected);
        await picker.locator('summary').click();
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'filter overflow');
        await page.click('#sc-back');
        // Selecting inherited skills can create an explicit override even after
        // restoring its values. Discard via the real UI; never save test changes.
        if(await page.locator('#sc-dialog').evaluate(node=>node.open))await page.click('#sc-dialog-confirm');
        await page.locator('[data-screen="home"]').waitFor({state:'visible'});
        layouts++;
      }
      assert(!errors.length,'search script errors '+errors.join(';'));
      console.log('settings_search=passed layouts='+layouts+' settings_live_filter=true skills_live_filter=true gray_icons=true selected_skills_retained=true');
      await context.close();return;
    }
    // The actual nginx hop strips the Host port, exactly like production.
    // Requests from a different origin or port must still fail before writing.
    for(const origin of ['https://evil.test','https://127.0.0.1','http://127.0.0.1:'+port]){
      const response=await context.request.post(base+'/settings/api/config',{headers:{Origin:origin,'X-Forwarded-Host':'evil.test'},data:{revision:0,changes:{personal:'must not save'}}});
      assert(response.status()===403,'wrong origin accepted through gateway');
    }
    assert((await page.evaluate(()=>fetch('/settings/api/config').then(r=>r.json()))).account.revision===0,'rejected origin wrote settings');
    const projectPage=await context.newPage();debugPage=projectPage;projectPage.on('pageerror',error=>errors.push(error.message));await projectPage.goto(base+'/project/fixture');
    // Current composer keeps skills in the settings center, not the old toolbar.
    assert(await projectPage.locator('#project-skill-picker').isHidden(),'retired project skill control became visible');
    await projectPage.locator('.project-quick-menu summary').click();
    assert(await projectPage.locator('.project-quick-menu a[href="/settings?project_id=fixture#models"]').innerText()==='项目设置','project settings entry missing');
    await projectPage.locator('.project-quick-menu summary').click();
    for(const width of [320,393])for(const theme of ['dark','light'])for(const size of [16,20]){
      await projectPage.setViewportSize({width,height:852});
      await projectPage.evaluate(({theme,size})=>{window.codexSetTheme(theme);document.documentElement.style.fontSize=size+'px';},{theme,size});
      assert(await projectPage.locator('[data-project-control]:visible').count()===4,'compact composer lost a setting control');
      assert(await projectPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'compact project composer overflow');
    }
    assert(await projectPage.evaluate(()=>new FormData(document.querySelector('#project-send')).get('skills_explicit'))==='1','hidden inherited skills lost their explicit marker');
    if(screenshotDir)await projectPage.screenshot({path:path.join(screenshotDir,'project-compact-controls.png'),fullPage:true});
    // The exact production failure: no-referrer + real native project form
    // through nginx Host-port loss, into the real create/start route + fake RPC.
    await projectPage.fill('#project-send textarea[name="prompt"]','隐私网关原生表单回归');
    await projectPage.click('#project-send .send-button');
    await projectPage.waitForURL(/\/conversation\/c[a-f0-9]+$/);
    await projectPage.locator('#conversation-initial-snapshot').waitFor({state:'attached'});
    const created=await projectPage.evaluate(()=>JSON.parse(document.querySelector('#conversation-initial-snapshot').textContent));
    assert(created.conversation.thread_id,'native form did not start the isolated fake thread');
    await projectPage.close();debugPage=page;
    let layouts=0;
    for(const width of [320,393])for(const theme of ['dark','light'])for(const size of [16,20]){
      await page.setViewportSize({width,height:852});
      await page.evaluate(({theme,size})=>{window.codexSetTheme(theme);document.documentElement.style.fontSize=size+'px';},{theme,size});
      for(const screen of ['home','models','rules','tools','appearance','notifications','data','security']){
        await page.evaluate(screen=>{history.pushState({},'','#'+screen);dispatchEvent(new PopStateEvent('popstate'));},screen);
        await page.locator('[data-screen="'+screen+'"]').waitFor({state:'visible'});
        if(screen==='home')await assertSecondaryFilter(page.locator('#sc-search'));
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${width}/${theme}/${size}/${screen} overflow`);
        const small=await page.evaluate(()=>[...document.querySelectorAll('.settings-center button,.settings-center select,.settings-center a')].filter(el=>el.getClientRects().length&&!el.closest('[hidden]')).filter(el=>el.getBoundingClientRect().height<43.5).map(el=>el.id||el.className));
        assert(!small.length,'small targets '+small.join(','));layouts++;
        if(screenshotDir&&width===393&&theme==='dark'&&size===16)await page.screenshot({path:path.join(screenshotDir,screen+'.png'),fullPage:true});
      }
    }
    await page.setViewportSize({width:393,height:852});await page.evaluate(()=>document.documentElement.style.fontSize='16px');
    await page.goto(base+'/settings#models');await page.waitForFunction(()=>document.querySelector('[data-setting="permission"]').options.length>1);
    await page.selectOption('[data-setting="permission"]','read-only');await page.selectOption('[data-setting="model"]','gpt-6-astra');await page.selectOption('[data-setting="collaboration_mode"]','plan');
    await page.click('#sc-preset-create');await page.fill('[aria-label="预设名称"]','手机审阅');await page.click('#sc-dialog-confirm');
    await page.waitForFunction(()=>document.querySelector('#sc-status').textContent.includes('预设已保存'));
    await page.click('#sc-save');await page.waitForFunction(()=>document.querySelector('#sc-status').textContent.includes('已保存'));
    await page.reload();await page.waitForFunction(()=>document.querySelector('[data-setting="permission"]').value==='read-only');
    assert(await page.inputValue('[data-setting="model"]')==='gpt-6-astra','account model not durable');
    // Increase confirmation and cancellation both work.
    await page.selectOption('[data-setting="permission"]','danger-full-access');await page.click('#sc-save');await page.locator('#sc-dialog[open]').waitFor();await page.click('#sc-dialog-cancel');
    let serverState=await page.evaluate(()=>fetch('/settings/api/config').then(r=>r.json()));assert(serverState.account.defaults.permission==='read-only','cancel expanded permission');
    await page.click('#sc-save');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);
    // Project overlay + source + reset inheritance.
    await page.selectOption('#sc-scope','fixture');await page.waitForFunction(()=>document.querySelector('[data-source="permission"]').textContent.includes('继承工作台'));
    await page.selectOption('[data-setting="permission"]','read-only');await page.click('#sc-save');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);
    serverState=await page.evaluate(()=>fetch('/settings/api/config?project_id=fixture').then(r=>r.json()));assert(serverState.sources.permission==='project','project source missing');
    const newPage=await context.newPage();await newPage.goto(base+'/quick?project_id=fixture');
    const snapshot=await newPage.evaluate(()=>JSON.parse(document.querySelector('#conversation-initial-snapshot').textContent));
    assert(snapshot.conversation.settings.permission==='read-only','quick ignored project defaults');assert(snapshot.conversation.settings.model==='gpt-6-astra','quick ignored account model');assert(snapshot.conversation.settings.collaboration_mode==='plan','quick ignored account execution mode');await newPage.close();
    await page.click('#sc-default-reset');await page.click('#sc-save');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);
    serverState=await page.evaluate(()=>fetch('/settings/api/config?project_id=fixture').then(r=>r.json()));assert(!Object.keys(serverState.project.defaults).length,'reset did not remove project overrides');
    // Personal preferences, revision conflict and preserve input.
    await page.click('#sc-back');await page.click('[data-go="rules"]');await page.fill('#sc-rule-content','默认简体中文，先给结论。');await page.click('#sc-save');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);
    await page.fill('#sc-rule-content','手机未保存草稿');await page.evaluate(()=>fetch('/fixture/conflict',{method:'POST'}));await page.click('#sc-save');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>document.querySelector('#sc-status').textContent.includes('其他页面'));
    assert(await page.inputValue('#sc-rule-content')==='手机未保存草稿','conflict lost editor');
    await page.click('#sc-reload');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>document.querySelector('#sc-rule-content').value==='其他页面刚修改');
    // Real project rule preview, save, restore absence, restore content.
    await page.click('[data-rule-tab="project"]');await page.selectOption('#sc-rule-project','fixture');await page.waitForFunction(()=>!document.querySelector('#sc-rule-content').disabled);
    await page.fill('#sc-rule-content','# 项目规则\n先验证再修改。');await page.click('#sc-save');await page.locator('#sc-dialog[open]').waitFor();assert((await page.textContent('#sc-dialog-content')).includes('保存后'),'missing unified diff');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>document.querySelector('#sc-status').textContent.includes('项目文件已保存'));
    await page.click('#sc-rule-previous');await page.click('#sc-dialog-confirm');await page.click('#sc-save');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>document.querySelector('#sc-rule-meta').textContent.includes('尚不存在'));
    await page.click('#sc-rule-previous');await page.click('#sc-dialog-confirm');await page.click('#sc-save');await page.click('#sc-dialog-confirm');await page.waitForFunction(()=>document.querySelector('#sc-rule-meta').textContent.includes('文件版本'));
    await page.fill('#sc-rule-content','我的草稿');await page.evaluate(()=>fetch('/fixture/external-rule',{method:'POST'}));await page.click('#sc-save');await page.waitForFunction(()=>document.querySelector('#sc-status').textContent.includes('其他操作修改'));
    assert(await page.inputValue('#sc-rule-content')==='我的草稿','file conflict lost draft');
    await page.click('#sc-back');await page.click('#sc-dialog-confirm');
    // Skills and notifications are durable, not simulated.
    await page.click('[data-go="tools"]');await page.click('#sc-skill-picker summary');await page.fill('#sc-skill-picker [data-skill-search]','fixture-skill');await page.check('[aria-label="默认选择 fixture-skill"]');await page.click('#sc-save');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);await page.click('#sc-back');
    await page.click('[data-go="notifications"]');await page.selectOption('[data-setting="completion_notification"]','false');await page.click('#sc-save');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);await page.click('#sc-back');
    await page.click('[data-go="appearance"]');await page.selectOption('#theme-select','system');await page.emulateMedia({colorScheme:'light'});assert(await page.evaluate(()=>document.documentElement.dataset.theme)==='light','system theme not applied');
    await page.selectOption('#sc-font','1.2');await page.check('#sc-wrap');await page.selectOption('#sc-send','enter');
    await page.click('#sc-input-test');const input=page.locator('[aria-label="输入试用"]');await input.fill('测试');await input.dispatchEvent('keydown',{key:'Enter',isComposing:true});assert(await page.textContent('#sc-dialog-title')==='试一下输入','IME sent');await input.press('Shift+Enter');assert(await page.textContent('#sc-dialog-title')==='试一下输入','Shift Enter sent');await input.press('Enter');assert((await page.textContent('#sc-dialog-title')).includes('已捕获'),'Enter ignored');await page.click('#sc-dialog-cancel');
    const enterPage=await context.newPage();await enterPage.goto(base+'/quick?project_id=fixture');
    const persisted=await enterPage.evaluate(()=>JSON.parse(document.querySelector('#conversation-initial-snapshot').textContent));
    assert(persisted.conversation.completion_notification===false,'new draft ignored notification default');
    assert(persisted.conversation.settings.default_skills.includes('fixture-skill'),'new draft ignored default skills');
    assert(await enterPage.locator('input[name=skills][value=fixture-skill]').isChecked(),'default skill not checked');
    assert((await enterPage.locator('#conversation-skill-picker [data-skill-summary]').textContent()).includes('已指定 1 项'),'saved defaults misleadingly called automatic');
    let sent=0;enterPage.on('request',req=>{if(/\/(start|messages)$/.test(new URL(req.url()).pathname)&&req.method()==='POST')sent++;});
    const composer=enterPage.locator('#conversation-composer textarea[name=text]');await composer.fill('only test the fixture');
    await composer.dispatchEvent('keydown',{key:'Enter',isComposing:true});await enterPage.waitForTimeout(100);assert(sent===0,'real composer sent during IME');
    await composer.press('Shift+Enter');assert(sent===0,'real composer sent on Shift+Enter');await composer.press('Enter');await enterPage.waitForFunction(()=>document.querySelector('#conversation-composer textarea[name=text]').value==='');assert(sent===1,'real composer Enter did not send exactly once');await enterPage.close();
    await page.click('#sc-back');await page.click('[data-go="security"]');await page.click('#sc-diagnostic-run');await page.waitForFunction(()=>document.querySelector('#sc-diagnostics').textContent.includes('fixture-version'));assert(!(await page.textContent('#sc-diagnostics')).includes('/home/'),'diagnostics leaked path');
    await page.click('#sc-back');await page.fill('#sc-search','预设');assert(await page.locator('.sc-home-row:visible').count()===1,'search failed');
    await page.fill('#sc-search','');await page.click('[data-go="models"]');await page.selectOption('[data-setting="permission"]','read-only');
    await page.evaluate(()=>history.back());await page.locator('#sc-dialog[open]').waitFor();await page.click('#sc-dialog-cancel');
    assert(await page.locator('[data-screen=models]').isVisible(),'browser back discarded dirty page');
    assert(await page.inputValue('[data-setting=permission]')==='read-only','browser back lost draft');
    await page.click('#sc-back');await page.click('#sc-dialog-confirm');
    // Explicit automatic [] must stay distinct from inheriting account defaults.
    await page.evaluate(async()=>{
      const value=await fetch('/settings/api/config').then(r=>r.json());
      const saved=await fetch('/settings/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:value.account.revision,changes:{defaults:{...value.account.defaults,skills:['fixture-skill']}}})});
      if(!saved.ok)throw new Error('failed to set isolated account skill default');
    });
    await page.goto(base+'/settings?project_id=fixture#tools');await page.reload();await page.waitForFunction(()=>document.querySelector('#sc-skill-picker [data-skill-summary]').textContent.includes('已指定'));
    await page.click('#sc-skill-picker [data-skill-clear]');await page.click('#sc-save');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);
    await page.reload();await page.waitForFunction(()=>document.querySelector('#sc-skills-source').textContent.includes('此项目覆盖'));
    assert(await page.locator('#sc-skill-picker [data-skill-summary]').isHidden(),'explicit empty kept a selected summary');
    assert(await page.locator('#sc-skill-picker [data-skill-input]:checked').count()===0,'explicit empty inherited unwanted default');
    serverState=await page.evaluate(()=>fetch('/settings/api/config?project_id=fixture').then(r=>r.json()));
    assert(serverState.project.defaults.skills.length===0&&serverState.account.defaults.skills.length===1,'automatic mode changed the account default');
    await page.click('#sc-skills-inherit');assert((await page.textContent('#sc-skill-picker [data-skill-summary]')).includes('已指定 1'),'inherit failed to restore account default');
    await page.click('#sc-save');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);
    serverState=await page.evaluate(()=>fetch('/settings/api/config?project_id=fixture').then(r=>r.json()));assert(!Object.hasOwn(serverState.project.defaults,'skills'),'inherit saved an explicit override');
    // A vanished saved skill remains visible and removable, never silently cleared.
    await page.evaluate(()=>fetch('/fixture/missing-skill',{method:'POST'}));await page.reload();
    await page.waitForFunction(()=>document.querySelector('#sc-skill-picker [data-skill-summary]').textContent.includes('已指定'));
    assert(await page.locator('#sc-skill-picker [data-skill-warning]').isVisible(),'unavailable default hidden in collapsed list');
    await page.click('#sc-skill-picker summary');
    const missing=page.locator('#sc-skill-picker input[value="removed-skill"]');
    assert(await missing.isChecked(),'vanished selection silently lost');assert((await missing.locator('..').textContent()).includes('已不可用'),'missing unavailable warning');
    assert(await page.locator('#sc-skill-picker input[value="disabled-skill"]').isDisabled(),'unavailable unselected skill remained selectable');
    if(screenshotDir)await page.screenshot({path:path.join(screenshotDir,'skills-manual.png'),fullPage:true});
    await missing.uncheck();assert(await missing.isDisabled(),'removed unavailable skill could be reselected');await page.click('#sc-save');await page.waitForFunction(()=>!document.querySelector('#settings-center').inert&&document.querySelector('#sc-save').disabled);
    serverState=await page.evaluate(()=>fetch('/settings/api/config?project_id=fixture').then(r=>r.json()));assert(serverState.project.defaults.skills.length===0,'failed to save removal of vanished skill');
    assert(!errors.length,'script errors '+errors.join(';'));
    console.log('settings_center_browser=passed layouts='+layouts+' persistence=real rules=real origin='+(proxy?'nginx_TLS_stripped_Host_port':'direct_TLS')+' rejected_origins=passed long_skills=passed inheritance=passed unavailable_skills=passed conflicts=passed IME=passed');
    await context.close();
  } catch(error) {if(debugPage){console.error(await debugPage.evaluate(()=>({status:document.querySelector('#sc-status')?.textContent,dialog:document.querySelector('#sc-dialog')?.open,dialogText:document.querySelector('#sc-dialog-content')?.textContent,scope:document.querySelector('#sc-scope')?.value})));await debugPage.screenshot({path:'/tmp/codex-settings-browser-failure.png',fullPage:true});}throw error;}
  finally {await browser.close();server.kill('SIGTERM');}
})().catch(error=>{console.error(error);process.exitCode=1;});
