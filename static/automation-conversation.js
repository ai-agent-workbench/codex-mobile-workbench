(function(){
  'use strict';
  const root=document.getElementById('native-conversation'), summary=document.getElementById('conversation-automation-summary');
  if(!root||!summary)return;
  const prefix=(location.pathname.match(/^\/[0-9a-f]{40,}(?=\/)/)||[''])[0];let pending=false;
  async function load(){if(pending||document.hidden)return;pending=true;try{
    const r=await fetch(prefix+'/settings/automations/api/tasks?conversation_id='+encodeURIComponent(root.dataset.conversationId)+'&summary=true',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return;
    const data=await r.json(),tasks=data.tasks||[];summary.hidden=!tasks.length;
    if(tasks.length){const active=tasks.filter(t=>t.state==='active'&&t.next_run_at),next=active.map(t=>t.next_run_at).sort()[0];summary.textContent='◷ 定时任务 '+tasks.length+' 项 · '+(active.length?active.length+' 项启用 · 下次 '+new Date(next).toLocaleString('zh-CN',{hour12:false}):'已暂停或完成')+' · 管理';}
  }catch(_){/* Task summaries must never break the conversation. */}finally{pending=false;}}
  if(location.hash.startsWith('#turn-'))requestAnimationFrame(()=>requestAnimationFrame(()=>{const target=document.getElementById(decodeURIComponent(location.hash.slice(1)));if(target)target.scrollIntoView({block:'start'});}));
  document.addEventListener('visibilitychange',load);setInterval(load,15000);load();
})();
