/* Offline loopback regression for the first permission-choice page. */
const {chromium}=require('playwright');
const {spawn}=require('child_process');
const path=require('path');
const assert=(value,message)=>{if(!value)throw new Error(message);};
(async()=>{
  const root=path.resolve(__dirname,'..');
  const proxy=process.env.CODEX_SETTINGS_GATEWAY!=='direct';
  const server=spawn(process.env.CODEX_WEB_PYTHON||path.join(root,'.venv/bin/python'),[path.join(__dirname,'settings_center_fixture_server.py'),'--permission-choice',...(proxy?['--proxy']:[])],{cwd:root,stdio:['ignore','pipe','pipe']});
  let stderr='';server.stderr.on('data',chunk=>stderr+=chunk);
  let browser;
  try{
  const port=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('fixture startup timed out '+stderr)),30000);
    let data='';
    server.stdout.on('data',chunk=>{data+=chunk;const line=data.indexOf('\n');if(line>=0){
      try{const value=JSON.parse(data.slice(0,line));clearTimeout(timer);resolve(value.port);}
      catch(error){clearTimeout(timer);reject(error);}
    }});
    server.on('error',error=>{clearTimeout(timer);reject(error);});
    server.on('exit',code=>{clearTimeout(timer);reject(new Error('fixture exited '+code+' '+stderr));});
  });
  const base='https://127.0.0.1:'+port;
  browser=await chromium.launch({headless:true});
    for(const [permission,label] of [['read-only','只读'],['workspace-write','工作区可写'],['danger-full-access','完全访问']]){
      const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:390,height:844}});const page=await context.newPage();
      // Reset is a POST endpoint, perform it from the trusted page origin.
      await page.goto(base+'/settings',{waitUntil:'domcontentloaded'});
      assert((await page.evaluate(()=>fetch('/fixture/permission-reset',{method:'POST'}).then(r=>r.ok)))===true,'permission reset failed');
      await page.goto(base+'/settings/permission-choice?return_to=/quick');
      assert(await page.locator('input[name=permission]:checked').count()===0,'permission page preselected an option');
      assert((await page.locator('.permission-choice-page').innerText()).includes(label),'permission option missing');
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'390px choice page overflows');
      await page.locator('#permission-choice-save').click();
      assert(await page.locator('#permission-choice-error').isVisible(),'missing selection was silently accepted');
      await page.locator('input[value="'+permission+'"]').check();await page.locator('#permission-choice-save').click();
      await page.waitForURL('**/conversation/*');
      const state=await page.evaluate(()=>fetch('/fixture/permission-state').then(r=>r.json()));
      assert(state.completed&&state.values.permission===permission,'selection was not persisted');
      const snapshot=await page.evaluate(()=>fetch(location.pathname+'/snapshot').then(r=>r.json()));
      assert(snapshot.conversation.settings.permission===permission,'new conversation did not inherit the selected permission');
      await context.close();
    }
    {
      const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:390,height:844}}),page=await context.newPage();
      await page.goto(base+'/settings');
      assert(await page.evaluate(()=>fetch('/fixture/permission-reset',{method:'POST'}).then(r=>r.ok)),'reset before quick context failed');
      const prompt='保留项目 50% & 中文上下文';
      let conversationNavigation;
      page.on('request',request=>{const url=new URL(request.url());if(request.resourceType()==='document'&&url.pathname.startsWith('/conversation/'))conversationNavigation=url;});
      await page.goto(base+'/quick?'+new URLSearchParams({project_id:'fixture',prompt,quoted:'1'}));
      assert(new URL(page.url()).pathname==='/settings/permission-choice','pending quick did not open selection');
      await page.locator('input[value="read-only"]').check();await page.locator('#permission-choice-save').click();
      await page.waitForURL('**/conversation/*');
      // The composer intentionally removes consumed prefill parameters with
      // history.replaceState. Check the navigation and the actual input, not
      // the cleaned-up address bar.
      assert(conversationNavigation&&conversationNavigation.searchParams.get('prefill')===prompt&&conversationNavigation.searchParams.get('quoted')==='1','quick prompt or quoted flag was lost');
      await page.waitForFunction(expected=>document.querySelector('#conversation-composer textarea').value===expected,prompt);
      const snapshot=await page.evaluate(()=>fetch(location.pathname+'/snapshot').then(r=>r.json()));
      assert(snapshot.conversation.project_id==='fixture','first quick left the selected project');
      assert(snapshot.conversation.settings.permission==='read-only','first project quick ignored the chosen permission');
      await context.close();
    }
    const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:360,height:844}});const page=await context.newPage();
    await page.goto(base+'/settings');assert((await page.evaluate(()=>fetch('/fixture/permission-reset',{method:'POST'}).then(r=>r.ok)))===true,'permission reset failed');await page.goto(base+'/settings/permission-choice');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'360px choice page overflows');
    await page.locator('input[value="workspace-write"]').check();await page.evaluate(()=>fetch('/fixture/conflict',{method:'POST'}));await page.locator('#permission-choice-save').click();
    await page.waitForFunction(()=>document.querySelector('#permission-choice-error').textContent.includes('再次点击'));
    assert(await page.locator('input[value="workspace-write"]').isChecked(),'conflict lost selected permission');
    await page.locator('#permission-choice-save').click();await page.waitForURL('**/home');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'360px choice page overflows');await context.close();
    console.log('permission_choice=passed choices=3 no_preselection=true persisted_and_inherited=true conflict_retry=true gateway='+(proxy?'nginx':'direct'));
  }finally{if(browser)await browser.close();server.kill('SIGTERM');}
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
