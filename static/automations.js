(function(){
  'use strict';
  if(!document.getElementById('automation-page'))return;
  const $=id=>document.getElementById(id), prefix=(location.pathname.match(/^\/[0-9a-f]{40,}(?=\/)/)||[''])[0];
  const conversationId=new URLSearchParams(location.search).get('conversation_id')||'';
  const permissions={'read-only':'只读','workspace-write':'工作区可写','danger-full-access':'完全访问'};
  const modes={default:'默认',plan:'Plan'};
  const states={dispatching:'准备执行',queued:'等待下一轮',accepted:'执行中',completed:'已完成',failed:'失败',interrupted:'已停止',skipped:'已跳过',delivery_unknown:'投递待核对'};
  const reasons={module_disabled:'部署停用期间错过的时段，未补跑',missed_maintenance:'错过执行时段，未补跑',previous_run_pending:'前一次尚未结束，未重复执行',conversation_source_changed:'权限或目录改变，任务已暂停',owner_disabled:'任务所有者已停用',interrupted_before_dispatch:'维护中断派发，未重发',scheduled_authorization_changed:'任务或授权已改变，旧轮次未执行',command_rejected:'未能启动轮次'};
  let data={tasks:[],conversations:[]}, editing=null, busy=false, mutation=null;
  function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
  function status(text,error=false,node=$('auto-status')){node.textContent=text;node.classList.toggle('automation-error',error);}
  function time(value){if(!value)return '—';const d=new Date(value);return Number.isNaN(d.valueOf())?'—':d.toLocaleString('zh-CN',{hour12:false});}
  function cadence(s){if(s.kind==='interval')return '每 '+s.interval_minutes+' 分钟';if(s.kind==='once')return '一次 · '+time(s.run_at);return (s.kind==='daily'?'每天':'每周 '+s.weekdays.map(d=>'一二三四五六日'[d]).join('、'))+' '+s.time+' · '+s.timezone;}
  function key(){return crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);}
  async function request(path,value){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),25000);
    try{
      const response=await fetch(prefix+path,{method:value===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',signal:controller.signal,headers:value===undefined?{}:{'Content-Type':'application/json'},body:value===undefined?undefined:JSON.stringify(value)});
      let result;try{result=await response.json();}catch(_){throw new Error('登录可能已失效或服务不可用；请保留输入后重试。');}
      if(!response.ok)throw new Error(typeof result.error==='string'?result.error:'请求失败，请重新读取后核对。');return result;
    }catch(error){if(error.name==='AbortError')throw new Error('请求超时，结果尚未确认。请重新查询；再次提交相同内容不会重复创建。');throw error;}finally{clearTimeout(timer);}
  }
  function lock(value){busy=value;$('auto-refresh').disabled=value;$('auto-create').disabled=value;$('auto-save').disabled=value;document.querySelectorAll('#auto-list button').forEach(b=>b.disabled=value);}
  async function load(){if(busy)return;lock(true);try{data=await request('/settings/automations/api/tasks'+(conversationId?'?conversation_id='+encodeURIComponent(conversationId):''));render();status(data.module_enabled===false?'此部署未启用定时任务；历史记录保留，可查看、暂停或删除。':data.scheduler_running?'':'后台调度器尚未运行，任务不会自动执行。',data.module_enabled!==false&&!data.scheduler_running);}catch(error){status(error.message,true);}finally{lock(false);}}
  function button(text,fn){const b=el('button',text,'secondary');b.type='button';b.onclick=fn;return b;}
  function render(){
    $('auto-list').replaceChildren();$('auto-empty').hidden=!!data.tasks.length;$('auto-all').hidden=!conversationId;
    data.tasks.forEach(task=>{
      const card=el('article',undefined,'automation-card');card.dataset.taskId=task.id;
      card.append(el('h2',task.title),el('p',cadence(task.schedule)),el('p',(task.state==='paused'?'已暂停':task.state==='finished'?'已结束':task.next_run_at?'已启用':'正在执行（一次性）')+' · 下次 '+time(task.next_run_at)),el('p','执行权限：'+(permissions[task.permission]||task.permission)+' · 执行模式：'+(modes[task.collaboration_mode]||task.collaboration_mode)));
      const link=el('a','查看原对话');link.href=prefix+'/conversation/'+encodeURIComponent(task.conversation_id);card.append(link);
      const actions=el('div',undefined,'automation-buttons');actions.append(button('运行记录',()=>showRuns(task)),button('修改',()=>openEditor(task)),button(task.state==='finished'?'设置下一次':task.state==='paused'?'恢复':'暂停',()=>task.state==='finished'?openEditor(task):changeState(task,task.state==='paused'?'active':'paused')),button('删除',()=>changeState(task,'deleted')));card.append(actions);$('auto-list').append(card);
    });
  }
  function fields(){const kind=$('auto-kind').value;$('auto-interval-row').hidden=kind!=='interval';$('auto-time-row').hidden=!['daily','weekly'].includes(kind);$('auto-zone-row').hidden=!['daily','weekly'].includes(kind);$('auto-days-row').hidden=kind!=='weekly';$('auto-once-row').hidden=kind!=='once';}
  function scopeNote(){const selected=data.conversations.find(c=>c.id===$('auto-conversation').value);const task=editing||selected;$('auto-permission').textContent='无人值守权限：'+(permissions[task&&task.permission]||'请先选择对话')+' · 执行模式：'+(modes[task&&task.collaboration_mode]||'—')+'。不提供 root、管理员令牌或额外认证。';}
  function openEditor(task){
    if(busy)return;editing=task||null;mutation=null;$('auto-form').reset();$('auto-form-status').textContent='';$('auto-dialog-title').textContent=task?'修改任务':'新建任务';$('auto-save').textContent=task?'确认并保存':'确认并创建';
    $('auto-conversation').replaceChildren();data.conversations.filter(c=>!conversationId||c.id===conversationId).forEach(c=>{const o=el('option',c.title);o.value=c.id;$('auto-conversation').append(o);});
    if(task){$('auto-conversation').value=task.conversation_id;$('auto-title').value=task.title;$('auto-prompt').value=task.prompt;$('auto-kind').value=task.schedule.kind;$('auto-interval').value=task.schedule.interval_minutes||60;$('auto-time').value=task.schedule.time||'09:00';$('auto-zone').value=task.schedule.timezone||'Asia/Hong_Kong';$('auto-once').value=task.schedule.run_at||'';document.querySelectorAll('[name=weekday]').forEach(n=>n.checked=(task.schedule.weekdays||[]).includes(Number(n.value)));}
    $('auto-conversation').disabled=!!task;fields();scopeNote();$('auto-dialog').showModal();
  }
  function rule(){const kind=$('auto-kind').value;if(kind==='interval')return {kind,interval_minutes:Number($('auto-interval').value)};if(kind==='once')return {kind,run_at:$('auto-once').value};const s={kind,time:$('auto-time').value,timezone:$('auto-zone').value};if(kind==='weekly')s.weekdays=[...document.querySelectorAll('[name=weekday]:checked')].map(n=>Number(n.value));return s;}
  $('auto-form').onsubmit=async event=>{
    event.preventDefault();if(busy)return;
    let value={title:$('auto-title').value,prompt:$('auto-prompt').value,schedule:rule(),confirm_unattended:$('auto-confirm').checked};
    if(editing){value.revision=editing.revision;if(editing.state==='finished')value.state='active';}else{value.conversation_id=$('auto-conversation').value;}
    const fingerprint=JSON.stringify(value);if(!mutation||mutation.fingerprint!==fingerprint)mutation={fingerprint,key:key()};value.request_key=mutation.key;
    lock(true);status('正在保存…',false,$('auto-form-status'));
    try{await request('/settings/automations/api/tasks'+(editing?'/'+encodeURIComponent(editing.id):''),value);$('auto-dialog').close();mutation=null;lock(false);await load();status('任务已保存，结果将留在原对话。');}catch(error){status(error.message,true,$('auto-form-status'));}finally{lock(false);}
  };
  async function changeState(task,state){
    if(busy)return;
    const message=state==='deleted'?'删除此任务？聊天记录保留，已经开始的轮次不会被中断。':state==='active'?'恢复定时执行？将按原权限无人值守调用模型。':'暂停后不再启动新的轮次；已经开始的轮次不受影响。';
    if(!window.confirm(message))return;lock(true);
    try{await request('/settings/automations/api/tasks/'+encodeURIComponent(task.id),{revision:task.revision,state,request_key:key(),confirm_unattended:state==='active'});lock(false);await load();status(state==='deleted'?'任务已删除，聊天记录保留。':state==='active'?'任务已恢复。':'任务已暂停。');}catch(error){status(error.message,true);}finally{lock(false);}
  }
  async function showRuns(task){if(busy)return;lock(true);try{
    const value=await request('/settings/automations/api/tasks/'+encodeURIComponent(task.id)+'/runs');$('auto-runs-title').textContent=task.title+' · 运行记录';$('auto-runs').replaceChildren();
    if(!value.runs.length)$('auto-runs').append(el('p','还没有运行记录。'));
    value.runs.forEach(run=>{const row=el('article',undefined,'automation-run');row.append(el('strong',time(run.scheduled_at)+' · '+(states[run.status]||run.status)));if(run.skipped_count>1)row.append(el('p','合并 '+run.skipped_count+' 个未执行时段 · 截至 '+time(run.last_skipped_at)));if(run.reason)row.append(el('p',reasons[run.reason]||'执行未正常完成，请查看原对话或重新核对状态。'));if(run.turn_id){const link=el('a','查看该轮回复');link.href=prefix+'/conversation/'+encodeURIComponent(run.conversation_id)+'#turn-'+encodeURIComponent(run.turn_id);row.append(link);}$('auto-runs').append(row);});$('auto-runs-dialog').showModal();
  }catch(error){status(error.message,true);}finally{lock(false);}}
  $('auto-refresh').onclick=load;$('auto-create').onclick=()=>openEditor(null);$('auto-kind').onchange=fields;$('auto-conversation').onchange=scopeNote;
  $('auto-cancel').onclick=()=>{if(!busy)$('auto-dialog').close();};$('auto-dialog').addEventListener('cancel',e=>{if(busy)e.preventDefault();});$('auto-runs-close').onclick=()=>$('auto-runs-dialog').close();
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!$('auto-dialog').open&&!$('auto-runs-dialog').open)load();});
  setInterval(()=>{if(!document.hidden&&!busy&&!$('auto-dialog').open&&!$('auto-runs-dialog').open)load();},30000);load();
})();
