(function () {
  'use strict';
  const keys={theme:'codex-theme',font:'codex-reply-font',wrap:'codex-code-wrap',send:'codex-send-mode'};
  let compositionEnded=-Infinity;
  const media=window.matchMedia('(prefers-color-scheme: light)');
  function read(key){try{return localStorage.getItem(key);}catch(_){return null;}}
  function preferences(){
    const theme=read(keys.theme),font=read(keys.font),send=read(keys.send);
    return {theme:['dark','light','system'].includes(theme)?theme:'dark',font:['1','1.1','1.2'].includes(font)?font:'1',wrap:read(keys.wrap)==='1',send:send==='enter'?'enter':'button'};
  }
  function apply(){
    const value=preferences(),theme=value.theme==='system'?(media.matches?'light':'dark'):value.theme;
    document.documentElement.dataset.theme=theme;
    document.documentElement.style.setProperty('--reader-scale',value.font);
    document.documentElement.dataset.codeWrap=String(value.wrap);
    const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.content=theme==='light'?'#f5f7f9':'#151925';
  }
  window.codexReadingPreferences=preferences;
  window.codexSaveReadingPreferences=function(value){
    if(!['dark','light','system'].includes(value.theme)||!['1','1.1','1.2'].includes(String(value.font))||!['button','enter'].includes(value.send)||typeof value.wrap!=='boolean')throw new Error('Invalid preferences');
    const previous=Object.fromEntries(Object.values(keys).map(key=>[key,read(key)]));
    try {Object.entries(keys).forEach(([name,key])=>localStorage.setItem(key,name==='wrap'?(value.wrap?'1':'0'):String(value[name])));}
    catch(error){Object.entries(previous).forEach(([key,old])=>{try{if(old===null)localStorage.removeItem(key);else localStorage.setItem(key,old);}catch(_){}});apply();throw error;}
    apply();
  };
  window.codexSetTheme=value=>window.codexSaveReadingPreferences({...preferences(),theme:value});
  window.codexShouldEnterSend=event=>preferences().send==='enter'&&event.key==='Enter'&&!event.shiftKey&&!event.ctrlKey&&!event.altKey&&!event.metaKey&&!event.isComposing&&event.keyCode!==229&&performance.now()-compositionEnded>150;
  document.addEventListener('compositionend',()=>{compositionEnded=performance.now();},true);
  document.addEventListener('keydown',event=>{
    if(!event.target.matches('#conversation-composer textarea[name="text"], #project-send textarea[name="prompt"]')||!window.codexShouldEnterSend(event))return;
    event.preventDefault();const form=event.target.form,button=form&&form.querySelector('button[type="submit"],.send-button');
    if(form&&(!button||!button.disabled))form.requestSubmit(button||undefined);
  });
  media.addEventListener('change',apply);window.addEventListener('storage',event=>{if(Object.values(keys).includes(event.key))apply();});apply();
  window.dispatchEvent(new Event('codex-reading-preferences-ready'));
})();
