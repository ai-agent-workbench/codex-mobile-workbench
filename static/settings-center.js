(function () {
  'use strict';
  const root = document.getElementById('settings-center');
  if (!root) return;
  const $ = id => document.getElementById(id), copy = value => JSON.parse(JSON.stringify(value));
  const prefix = (location.pathname.match(/^\/[0-9a-f]{40,}(?=\/)/) || [''])[0];
  const titles = {home:'设置与安全',models:'模型与执行',rules:'偏好与规则',tools:'技能与连接',appearance:'外观与输入',notifications:'通知与声音',data:'数据与备份',security:'安全与维护'};
  const sourceNames = {system:'系统默认',account:'工作台默认',project:'项目覆盖'};
  const permissions = {'read-only':'只读','workspace-write':'工作区可写','danger-full-access':'完全访问'};
  const ranks = {'read-only':0,'workspace-write':1,'danger-full-access':2};
  const effortNames = {none:'无',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'更高',max:'最高',ultra:'超高'};
  const nativeKeys = ['model','effort','service_tier','collaboration_mode','permission','followup'];
  const help = {
    home:'普通偏好可在手机自主保存。账号设置可跨设备；外观与输入只在当前浏览器。后续能力会明确标注未接入。',
    models:'本次明确选择 ＞ 项目覆盖 ＞ 工作台默认 ＞ 系统默认。新默认仅用于新对话。每行显示保存后将采用的来源。完全访问是服务用户本来具有的能力，不等于 root。扩大权限时会单独确认。',
    rules:'个人偏好在新对话首轮提供，不替代高优先级或项目规则。项目文件保存前必须预览差异，版本不一致会拒绝覆盖；可把上一版载入编辑器，再预览并保存。',
    tools:'目录来自当前 App Server 与所选项目。默认选择不是禁用其他技能；已不可用或同名歧义的技能不能保存。此处不安装技能、不管理连接凭据。',
    appearance:'主题保留原有存储键。回复字号只改变回复阅读区；代码换行只影响代码块。Enter 发送时 Shift + Enter 换行，输入法组词期间不会发送。',
    notifications:'保存默认提醒不改变旧对话铃铛，不追发历史，也不发送测试通知。声音与朗读沿用当前设备原有设置。',
    data:'文件和对话导出入口已可用。整站备份、批量清理和恢复向导尚未接入；不会把普通文件复制声称为一致性备份。',
    security:'只读检查不执行 AI 请求、不外发通知、不重启服务。构建指纹对应已加载的设置实现，不是上游版本号。'};
  let boot = null, draft = {}, project = new URLSearchParams(location.search).get('project_id') || '';
  let view = titles[location.hash.slice(1)] ? location.hash.slice(1) : 'home';
  let ruleTab = 'personal', rule = null, ruleProject = project, ruleName = 'AGENTS.md', restoreAbsent = false;
  let busy = false, dialogResolve = null, diagnostics = null, ruleLoading = 0;
  const status = (text, error=false) => { $('sc-status').textContent=text; $('sc-status').classList.toggle('error', error); };
  function errorMessage(error) { return error && error.message || '操作失败，输入已保留，请重试。'; }
  async function request(path, data) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(prefix + path, {method:data === undefined ? 'GET':'POST',credentials:'same-origin',cache:'no-store',signal:controller.signal,
        headers:data === undefined ? {} : {'Content-Type':'application/json'},body:data === undefined ? undefined : JSON.stringify(data)});
      let value; try { value = await response.json(); } catch (_) { throw new Error('登录可能已失效或服务暂不可用；输入已保留。'); }
      if (!response.ok) { const error = new Error(typeof value.error === 'string' ? value.error : value.error && value.error.message || '保存失败，输入已保留。'); error.code=value.code; throw error; }
      return value;
    } catch (error) { if (error.name === 'AbortError') throw new Error('请求超时；结果尚未确认，输入已保留。可重新读取后比较。'); throw error; }
    finally { clearTimeout(timer); }
  }
  function el(tag, text, className) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text;if(className)node.className=className;return node; }
  function option(value, text) { const node=el('option', text); node.value=value; return node; }
  function sheet(title, content, confirmText='确认', input=null) {
    $('sc-dialog-title').textContent=title; $('sc-dialog-content').replaceChildren();
    (Array.isArray(content)?content:[el('p',content)]).forEach(node=>$('sc-dialog-content').append(node));
    if(input)$('sc-dialog-content').append(input);
    $('sc-dialog-confirm').hidden=!confirmText; $('sc-dialog-confirm').textContent=confirmText||'';
    $('sc-dialog-cancel').textContent=confirmText?'取消':'知道了'; $('sc-dialog').showModal();
    if(input)input.focus();
    return new Promise(resolve=>{dialogResolve=resolve;});
  }
  function closeSheet(answer) { const resolve=dialogResolve;dialogResolve=null;$('sc-dialog').close();if(resolve)resolve(answer); }
  $('sc-dialog-cancel').onclick=()=>closeSheet(false);$('sc-dialog-confirm').onclick=()=>closeSheet(true);
  $('sc-dialog').addEventListener('cancel', event=>{event.preventDefault();closeSheet(false);});
  function current() {return project ? boot.project : boot.account;}
  function inherited() {return {...boot.system,...(project ? boot.account.defaults : {})};}
  function effective() {return {...inherited(),...draft};}
  function dirty() {
    if (!boot) return false;
    if(view==='rules')return ruleTab==='personal' ? $('sc-rule-content').value!==boot.account.personal : !!rule && (restoreAbsent || $('sc-rule-content').value!==rule.content);
    return ['models','tools','notifications'].includes(view) && JSON.stringify(draft)!==JSON.stringify(current().defaults);
  }
  function saveState() {$('sc-save').disabled=busy||!boot||!dirty();}
  async function abandon() {return !dirty() || await sheet('更改尚未保存','离开会放弃当前未保存的修改。','放弃并离开');}
  function resetDraft() {if(boot)draft=copy(current().defaults);}
  async function navigate(next, push=true) {
    if(!titles[next]||next===view)return;
    if(busy||!await abandon())return;
    resetDraft();view=next;
    if(push)history.pushState({screen:next},'',location.pathname+location.search+(next==='home'?'':'#'+next));
    render();if(view==='rules'&&ruleTab==='project')loadRule();window.scrollTo(0,0);$('sc-title').focus({preventScroll:true});
  }
  document.querySelectorAll('[data-go]').forEach(node=>node.onclick=()=>navigate(node.dataset.go));
  $('sc-back').onclick=()=>navigate('home');
  window.addEventListener('popstate',async()=>{
    const next=titles[location.hash.slice(1)]?location.hash.slice(1):'home';
    if(next===view)return;
    if(busy||!await abandon()) {history.pushState({screen:view},'',location.pathname+location.search+(view==='home'?'':'#'+view));return;}
    resetDraft();view=next;render();if(view==='rules'&&ruleTab==='project')loadRule();window.scrollTo(0,0);
  });
  window.addEventListener('beforeunload',event=>{if(dirty()||busy){event.preventDefault();event.returnValue='';}});
  root.addEventListener('click',async event=>{
    const link=event.target.closest('a[href]');if(!link||!dirty())return;
    event.preventDefault();if(await abandon()){resetDraft();if(rule)rule.content=$('sc-rule-content').value;if(ruleTab==='personal')boot.account.personal=$('sc-rule-content').value;location.assign(link.href);}
  });
  $('sc-help').onclick=()=>sheet('使用说明',help[view],null);
  function fillScopes() {
    $('sc-scope').replaceChildren(option('','工作台默认'));
    $('sc-rule-project').replaceChildren(option('','请选择项目'));
    boot.projects.forEach(item=>{ $('sc-scope').append(option(item.id,item.name));$('sc-rule-project').append(option(item.id,item.name)); });
    $('sc-scope').value=project; $('sc-rule-project').value=ruleProject;
  }
  async function load(nextProject=project) {
    if(busy)return;busy=true;saveState();status('正在读取设置…');
    try {
      const value=await request('/settings/api/config'+(nextProject?'?project_id='+encodeURIComponent(nextProject):''));
      boot=value;project=nextProject;
      const params=new URLSearchParams(location.search);if(project)params.set('project_id',project);else params.delete('project_id');
      history.replaceState({screen:view},'',location.pathname+(params.toString()?'?'+params.toString():'')+location.hash);
      resetDraft();fillScopes();$('sc-reload').hidden=true;
      status(value.catalog_error?'模型与技能目录暂不可用；不能保存相关配置，可稍后重试。':'', value.catalog_error);render();
    } catch(error) {status(errorMessage(error),true);$('sc-reload').hidden=false;}
    finally{busy=false;saveState();}
  }
  $('sc-reload').onclick=async()=>{if(await abandon()){if(view==='rules'&&ruleTab==='project')await loadRule();else await load();}};
  $('sc-scope').onchange=async()=>{const next=$('sc-scope').value;$('sc-scope').value=project;if(await abandon())await load(next);};
  function fieldValues(key, values) {
    if(key==='model')return boot.models.map(row=>[row.model,row.name+(row.available?'':'（不可用）')]);
    const model=boot.models.find(row=>row.model===values.model);
    if(key==='effort')return (model?model.efforts:[]).map(value=>[value,effortNames[value]||value]);
    if(key==='service_tier')return [['','标准'],...(model?model.tiers:[]).map(row=>[row.id,row.name])];
    if(key==='permission')return Object.entries(permissions);
    if(key==='collaboration_mode')return [['default','默认'],['plan','Plan']];
    if(key==='followup')return [['steer','优先补充当前轮'],['queue','排到下一轮']];
    return [['true','开启'],['false','关闭']];
  }
  function display(key,value) {if(key==='model')return (boot.models.find(row=>row.model===value)||{}).name||value;if(key==='collaboration_mode')return value==='plan'?'Plan':'默认';if(key==='permission')return permissions[value];if(key==='completion_notification')return value?'开启':'关闭';if(key==='service_tier')return value||'标准';if(key==='followup')return value==='queue'?'下一轮':'当前轮';if(key==='effort')return effortNames[value]||value;return value;}
  function renderFields() {
    if(!boot)return;
    const values=effective(),base=inherited();
    document.querySelectorAll('[data-setting]').forEach(select=>{
      const key=select.dataset.setting,choices=fieldValues(key,values),own=Object.hasOwn(draft,key);
      select.replaceChildren(option('__inherit__',(project?'继承':'系统')+' · '+display(key,base[key])));
      choices.forEach(([value,label])=>select.append(option(String(value),label)));
      const selected=own?String(draft[key]===null?'':draft[key]):'__inherit__';
      if(own&&!choices.some(([value])=>String(value)===selected))select.append(option(selected,display(key,draft[key])+'（不可用）'));
      select.value=selected;
      const source=document.querySelector('[data-source="'+key+'"]');
      if(source)source.textContent=own?(project?'此项目覆盖':'账号默认'):(project&&Object.hasOwn(boot.account.defaults,key)?'继承工作台':'继承系统');
    });
    const selected=$('sc-presets').value;
    $('sc-presets').replaceChildren(option('','自定义'),option('builtin-daily','日常工作（系统基线）'),option('builtin-review','只读审阅'));
    boot.account.presets.forEach(item=>$('sc-presets').append(option(item.id,item.name)));$('sc-presets').value=selected;
    $('sc-default-reset').textContent=project?'恢复执行继承':'恢复执行默认';
    $('sc-preset-delete').hidden=!boot.account.presets.some(item=>item.id===$('sc-presets').value);
  }
  document.querySelectorAll('[data-setting]').forEach(select=>select.onchange=()=>{
    const key=select.dataset.setting;
    if(select.value==='__inherit__')delete draft[key];
    else draft[key]=key==='completion_notification'?select.value==='true':key==='service_tier'?select.value||null:select.value;
    if(key==='model'){
      const values=effective(),model=boot.models.find(row=>row.model===values.model);
      if(model&&!model.efforts.includes(values.effort))draft.effort=model.default_effort||model.efforts[0];
      if(model&&values.service_tier&&!model.tiers.some(row=>row.id===values.service_tier))draft.service_tier=null;
    }
    $('sc-presets').value='';renderFields();saveState();
  });
  async function persist(changes, targetProject=project) {
    const target=targetProject?boot.project:boot.account;
    let confirmed=false;
    if(changes.defaults){
      const base=targetProject?{...boot.system,...boot.account.defaults}:boot.system;
      if(ranks[({...base,...changes.defaults}).permission]>ranks[({...base,...target.defaults}).permission]){
        confirmed=await sheet('扩大默认执行权限？',targetProject?'仅影响此项目之后新建的对话。完全访问是服务用户现有能力，不等于 root。':'影响此账号及继承该权限的项目之后新建的对话。旧对话不变；完全访问是服务用户现有能力，不等于 root。','确认保存');
        if(!confirmed)return false;
      }
    }
    busy=true;root.inert=true;saveState();
    try {
      const result=await request('/settings/api/config',{project_id:targetProject,revision:target.revision,account_revision:boot.account.revision,changes,confirm_permission:confirmed});
      if(targetProject)boot.project=result.saved;else boot.account=result.saved;
      status('已保存 · 仅新对话生效');$('sc-reload').hidden=true;return true;
    }catch(error){status(errorMessage(error),true);$('sc-reload').hidden=error.code!=='conflict';return false;}
    finally{busy=false;root.inert=false;saveState();}
  }
  $('sc-save').onclick=async()=>{
    if(!dirty())return;
    if(view==='rules'){await saveRule();return;}
    if(await persist({defaults:copy(draft)})){resetDraft();renderFields();renderSkills();summaries();saveState();}
  };
  $('sc-default-reset').onclick=()=>{nativeKeys.forEach(key=>delete draft[key]);$('sc-presets').value='';renderFields();saveState();};
  $('sc-presets').onchange=()=>{
    const key=$('sc-presets').value;
    let defaults;
    if(key==='builtin-daily')defaults=boot.system;
    else if(key==='builtin-review')defaults={...boot.system,permission:'read-only',followup:'queue'};
    else defaults=(boot.account.presets.find(item=>item.id===key)||{}).defaults;
    if(defaults)nativeKeys.forEach(name=>{if(Object.hasOwn(defaults,name))draft[name]=defaults[name];else delete draft[name];});
    renderFields();saveState();
  };
  $('sc-preset-create').onclick=async()=>{
    if(!boot)return;
    const input=el('input');input.maxLength=24;input.setAttribute('aria-label','预设名称');input.placeholder='例如：深入分析';
    if(!await sheet('存为预设','保存模型、思考、速度、执行模式、权限与追加策略；不会立即改变默认。','保存预设',input))return;
    const name=input.value.trim();if(!name){status('请填写预设名称。',true);return;}
    const defaults=Object.fromEntries(nativeKeys.map(key=>[key,effective()[key]]));
    const id='p'+(crypto.randomUUID?crypto.randomUUID().replace(/-/g,''):Date.now().toString(36));
    if(await persist({presets:[...boot.account.presets,{id,name,defaults}]},'')){renderFields();$('sc-presets').value=id;$('sc-preset-delete').hidden=false;status('预设已保存；默认配置尚需点保存。');}
  };
  $('sc-preset-delete').onclick=async()=>{
    const id=$('sc-presets').value,preset=boot.account.presets.find(item=>item.id===id);if(!preset)return;
    if(await sheet('删除预设？','仅删除“'+preset.name+'”这个配置预设，不删除项目或对话。','删除')&&await persist({presets:boot.account.presets.filter(item=>item.id!==id)},'')){renderFields();status('预设已删除，默认配置未改变。');}
  };
  function renderSkills() {
    if(!boot)return;
    const values=effective(),rows=boot.skills.slice();
    values.skills.forEach(name=>{if(!rows.some(row=>row.name===name))rows.push({name,group_label:'已保存但不可用',enabled:false});});
    $('sc-skills-source').textContent=(Object.hasOwn(draft,'skills')?(project?'此项目覆盖':'工作台默认'):(project?'继承工作台默认':'使用系统默认'))+' · 只影响新对话';
    window.codexSkillPicker.render($('sc-skill-picker'),rows,values.skills);
  }
  $('sc-skill-picker').addEventListener('skill-picker-change',event=>{draft.skills=event.detail.selected;saveState();$('sc-skills-source').textContent=(project?'此项目覆盖':'工作台默认')+' · 只影响新对话';});
  $('sc-skills-inherit').onclick=()=>{delete draft.skills;renderSkills();saveState();};
  function renderRule() {
    if(!boot)return;
    document.querySelectorAll('[data-rule-tab]').forEach(node=>node.setAttribute('aria-pressed',String(node.dataset.ruleTab===ruleTab)));
    $('sc-rule-project-row').hidden=ruleTab==='personal';$('sc-rule-content').maxLength=ruleTab==='personal'?4000:131072;
    if(ruleTab==='personal'){
      $('sc-rule-content').disabled=false;$('sc-rule-content').value=boot.account.personal;
      $('sc-rule-meta').textContent='账号偏好 · 版本 '+boot.account.revision+' · 最多 4000 字';
      $('sc-rule-scope').textContent='仅此工作台 · 新对话首轮提供个人偏好，不改共享配置';
      $('sc-rule-previous').disabled=boot.account.previous_personal===null;
    }else{
      $('sc-rule-content').disabled=!rule;$('sc-rule-content').value=rule?rule.content:'';
      $('sc-rule-scope').textContent='真实项目文件 · 可影响桌面与 CLI · 请新建对话使用新规则';
      $('sc-rule-meta').textContent=rule?(rule.exists?'文件版本 '+rule.version.slice(0,10):'文件尚不存在；保存将创建该文件'):'请选择项目后读取规则';
      $('sc-rule-previous').disabled=!rule||rule.previous===null;
    }
    saveState();
  }
  async function loadRule() {
    const sequence=++ruleLoading;rule=null;restoreAbsent=false;renderRule();
    if(!ruleProject)return;
    try{const value=await request('/settings/api/rules?project_id='+encodeURIComponent(ruleProject)+'&name='+encodeURIComponent(ruleName));if(sequence!==ruleLoading||ruleTab!=='project'||view!=='rules')return;rule=value;renderRule();status('');$('sc-reload').hidden=true;}
    catch(error){if(sequence!==ruleLoading)return;status(errorMessage(error),true);$('sc-reload').hidden=false;}
  }
  document.querySelectorAll('[data-rule-tab]').forEach(node=>node.onclick=async()=>{
    if(ruleTab===node.dataset.ruleTab||!await abandon())return;ruleTab=node.dataset.ruleTab;renderRule();if(ruleTab==='project')await loadRule();
  });
  $('sc-rule-project').onchange=async()=>{const next=$('sc-rule-project').value;$('sc-rule-project').value=ruleProject;if(!await abandon())return;ruleProject=next;$('sc-rule-project').value=next;await loadRule();};
  $('sc-rule-name').onchange=async()=>{const next=$('sc-rule-name').value;$('sc-rule-name').value=ruleName;if(!await abandon())return;ruleName=next;$('sc-rule-name').value=next;await loadRule();};
  $('sc-rule-content').oninput=()=>{restoreAbsent=false;saveState();};
  $('sc-rule-previous').onclick=async()=>{
    const previous=ruleTab==='personal'?boot.account.previous_personal:rule&&rule.previous;
    if(previous===null||previous===undefined)return;
    if(await sheet('载入上一版？',ruleTab==='project'&&!previous.exists?'上一版是文件不存在。确认保存时会删除当前这个项目规则文件；可再从恢复点撤销。':'先放入编辑器；预览差异并保存后才会生效。','载入')){restoreAbsent=ruleTab==='project'&&!previous.exists;$('sc-rule-content').value=ruleTab==='personal'?previous:previous.content;saveState();}
  };
  async function saveRule(previewOnly=false) {
    if(!boot||busy)return;
    const content=$('sc-rule-content').value;
    if(ruleTab==='personal'){
      const nodes=[el('p','原内容'),el('pre',boot.account.personal||'（空）'),el('p','新内容'),el('pre',content||'（空）')];
      if(await sheet('确认个人偏好更改',nodes,previewOnly?null:'保存')&&!previewOnly&&await persist({personal:content},'')){renderRule();}
      return;
    }
    if(!rule){status('请先选择项目并读取文件。',true);return;}
    busy=true;root.inert=true;saveState();
    try{
      const payload={project_id:ruleProject,name:ruleName,version:rule.version,content,remove:restoreAbsent};
      const preview=await request('/settings/api/rules/preview',payload);
      if(await sheet('项目规则差异',[el('p',ruleName+' · 保存前保留上一版。'),el('pre',preview.diff)],previewOnly?null:'确认保存')&&!previewOnly){
        rule=await request('/settings/api/rules/save',{...payload,token:preview.token});restoreAbsent=false;renderRule();status('项目文件已保存；请新建对话使用新规则。');$('sc-reload').hidden=true;
      }
    }catch(error){status(errorMessage(error),true);$('sc-reload').hidden=error.code!=='conflict';}
    finally{busy=false;root.inert=false;saveState();}
  }
  $('sc-rule-preview').onclick=()=>saveRule(true);
  const localControls=['theme-select','sc-font','sc-wrap','sc-send','sc-appearance-reset','sc-input-test'];
  function localReady(){return typeof window.codexReadingPreferences==='function'&&typeof window.codexSaveReadingPreferences==='function'&&typeof window.codexShouldEnterSend==='function';}
  function localAvailability(){const ready=localReady();localControls.forEach(id=>$(id).disabled=!ready);$('sc-appearance-loading').hidden=ready;}
  function localRender(){if(!localReady())return;const prefs=window.codexReadingPreferences();$('theme-select').value=prefs.theme;$('sc-font').value=prefs.font;$('sc-wrap').checked=prefs.wrap;$('sc-send').value=prefs.send;summaries();}
  ['theme-select','sc-font','sc-wrap','sc-send'].forEach(id=>$(id).onchange=()=>{
    try {window.codexSaveReadingPreferences({theme:$('theme-select').value,font:$('sc-font').value,wrap:$('sc-wrap').checked,send:$('sc-send').value});status('已保存到当前浏览器');localRender();}
    catch(_){status('浏览器拒绝保存，设置未持久化。',true);}
  });
  $('sc-appearance-reset').onclick=async()=>{if(await sheet('恢复本机显示与输入默认值？','将恢复深色、标准字号、代码不换行、点按钮发送；声音、朗读和账号设置不变。','恢复')){try{window.codexSaveReadingPreferences({theme:'dark',font:'1',wrap:false,send:'button'});localRender();status('本机显示与输入已恢复默认');}catch(_){status('浏览器拒绝保存。',true);}}};
  $('sc-input-test').onclick=()=>{const input=el('textarea');input.setAttribute('aria-label','输入试用');input.placeholder='仅试用，不会发送真实消息';input.onkeydown=event=>{if(window.codexShouldEnterSend(event)){event.preventDefault();$('sc-dialog-title').textContent='已捕获发送 · 未发送真实消息';}};sheet('试一下输入','Enter 模式下 Shift + Enter 换行；输入法组词不会发送。',null,input);};
  async function loginStatus(){try{const data=await request('/login/status');$('sc-login-state').textContent=data.authenticated?(data.method==='passkey'?'Passkey 已登录':'已登录'):'未登录';$('sc-passkey-state').textContent=data.passkey_enabled?'已启用':'未启用';}catch(_){$('sc-login-state').textContent='检查失败';}}
  $('sc-diagnostic-run').onclick=async()=>{
    $('sc-diagnostic-run').disabled=true;status('正在只读检查…');
    try{diagnostics=await request('/settings/api/diagnostics');$('sc-diagnostics').replaceChildren();
      [['页面服务',diagnostics.web],['App Server',diagnostics.app_server],['模型目录',diagnostics.model_count+' 项'],['磁盘剩余',diagnostics.disk_free_gb===null?'不可读':diagnostics.disk_free_gb+' GB'],['构建指纹',diagnostics.build]].forEach(([label,value])=>{const row=el('div',undefined,'sc-row');row.append(el('span',label),el('span',value,'sc-value'));$('sc-diagnostics').append(row);});
      $('sc-diagnostic-copy').disabled=false;status(diagnostics.model_count>0?'检查完成；未调用模型或发送通知。':'检查完成：App Server 或模型目录异常。',!diagnostics.model_count);
    }catch(error){status(errorMessage(error),true);}finally{$('sc-diagnostic-run').disabled=false;}
  };
  $('sc-diagnostic-copy').onclick=async()=>{if(!diagnostics)return;const summary=JSON.stringify(diagnostics,null,2);try{await navigator.clipboard.writeText(summary);status('脱敏摘要已复制');}catch(_){await sheet('脱敏诊断摘要',[el('pre',summary)],null);}};
  $('sc-search').oninput=()=>{const query=$('sc-search').value.trim().toLowerCase();let count=0;document.querySelectorAll('.sc-home-row').forEach(row=>{row.hidden=!(row.textContent+' '+row.dataset.keywords).toLowerCase().includes(query);if(!row.hidden)count++;});document.querySelectorAll('.sc-home-group').forEach(group=>group.hidden=![...group.querySelectorAll('.sc-home-row')].some(row=>!row.hidden));$('sc-empty').hidden=count>0;$('codex-quota-card').hidden=!!query;};
  function summaries(){
    if(boot){const values={...boot.system,...boot.account.defaults};$('sc-summary-models').textContent=values.model.replace('gpt-','')+' · '+(effortNames[values.effort]||values.effort);$('sc-summary-tools').textContent=values.skills.length?'默认指定 '+values.skills.length+' 项':'按任务自动选择';$('sc-summary-notifications').textContent=values.completion_notification?'默认提醒开':'默认提醒关';}
    if(window.codexReadingPreferences){const p=window.codexReadingPreferences();$('sc-summary-appearance').textContent={dark:'深色',light:'浅色',system:'跟随系统'}[p.theme];}
  }
  function render(){
    document.querySelectorAll('[data-screen]').forEach(node=>node.hidden=node.dataset.screen!==view);
    $('sc-title').textContent=titles[view];$('sc-back').hidden=view==='home';$('sc-scope-row').hidden=!['models','tools','notifications'].includes(view);
    $('sc-save').hidden=!['models','rules','tools','notifications'].includes(view);
    renderFields();renderSkills();if(view==='rules')renderRule();if(view==='appearance'&&window.codexReadingPreferences)localRender();if(view==='security')loginStatus();summaries();saveState();
  }
  window.addEventListener('codex-reading-preferences-ready',()=>{localAvailability();if(view==='appearance')localRender();else summaries();});
  localAvailability();
  render();load();
})();
