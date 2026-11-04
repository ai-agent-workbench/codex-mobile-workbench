(()=>{
  const page=document.querySelector('[data-permission-choice]');
  if(!page)return;
  const form=document.getElementById('permission-choice-form');
  const save=document.getElementById('permission-choice-save');
  const error=document.getElementById('permission-choice-error');
  const show=message=>{error.textContent=message;error.hidden=!message;};
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const choice=form.querySelector('input[name="permission"]:checked');
    if(!choice){show('请选择一种执行权限后再继续。');return;}
    save.disabled=true;show('');
    try{
      const response=await fetch('/settings/api/permission-choice',{method:'POST',headers:{'content-type':'application/json'},credentials:'same-origin',body:JSON.stringify({permission:choice.value,revision:Number(page.dataset.revision),return_to:page.dataset.returnTo})});
      const value=await response.json().catch(()=>({}));
      if(!response.ok){
        show(value.error||'保存未完成，请重试。');
        if(response.status===409&&value.code==='conflict'&&Number.isInteger(value.revision)&&value.revision>=0){
          page.dataset.revision=String(value.revision);
          show('设置刚刚发生变化；已保留你的选择，请再次点击保存。');
        }
        return;
      }
      location.assign(typeof value.redirect==='string'?value.redirect:'/home');
    }catch(_error){show('暂时无法保存，请检查连接后重试。');}
    finally{save.disabled=false;}
  });
})();
