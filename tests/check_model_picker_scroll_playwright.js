// Comfortable single-row model strip: common models first, old models scroll.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')),
  [path.join(__dirname, 'render_conversation_ui_fixtures.py')], { cwd: root, encoding: 'utf8' }));
(async () => {
  const browser = await chromium.launch({headless:true,
    args:['--no-sandbox']});
  try {
    const page = await browser.newPage({hasTouch:true});
    for (const theme of ['dark','light']) for (const width of [320,360,390,412]) {
      for (const kind of ['project','conversation']) {
        await page.setViewportSize({width,height:844});
        await page.setContent(`<html data-theme="${theme}"><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>${fs.readFileSync(path.join(root,'static/css/app.css'),'utf8')}</style></head><body><main>${fixtures[kind]}</main></body></html>`);
        const selector = kind === 'project' ? '.project-start-settings' : '.native-settings-panel';
        const select = page.locator(`${selector} select[data-segment-source=model]`);
        await select.evaluate(s=>s.value='gpt-6-sol');
        await page.addScriptTag({content:fs.readFileSync(path.join(root,'static/setting-segments.js'),'utf8')});
        if(kind==='conversation') await page.locator('#conversation-settings').evaluate(e=>e.open=true);
        const strip = page.locator(`${selector} .setting-segments-model`);
        await page.waitForTimeout(50);
        const facts=await strip.evaluate(group=>{
          const bs=[...group.querySelectorAll('button')], g=group.getBoundingClientRect();
          return { rows:new Set(bs.map(b=>Math.round(b.getBoundingClientRect().top))).size,
            overflow:group.scrollWidth-group.clientWidth, rightHint:group.classList.contains('has-more-right'),
            firstThreeVisible:bs.slice(0,3).every(b=>{const r=b.getBoundingClientRect();return r.left>=g.left-1&&r.right<=g.right-14}),
            selected:group.querySelector('.selected').dataset.value,
            comfortable:bs.every(b=>b.offsetHeight>=44&&b.offsetWidth>=44&&parseFloat(getComputedStyle(b).fontSize)>=13),
            pageOverflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};
        });
        const context=`${kind} ${theme} ${width}px ${JSON.stringify(facts)}`;
        assert.equal(facts.rows,1,context);assert(facts.overflow>1,context);
        assert(facts.rightHint&&facts.firstThreeVisible&&facts.comfortable&&facts.pageOverflow<=1,context);
        assert.equal(facts.selected,'gpt-6-sol',context);
        if(theme==='dark'&&width===390&&kind==='project') {
          if(process.env.MODEL_PICKER_SCREENSHOT) await page.locator('.project-start-composer').screenshot({path:process.env.MODEL_PICKER_SCREENSHOT});
          // Exercise a real horizontal touch gesture rather than only setting scrollLeft.
          const rect=await strip.boundingBox(), cdp=await page.context().newCDPSession(page);
          await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:rect.x+rect.width-25,y:rect.y+22}]});
          for(let step=1;step<=8;step++) {
            await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:rect.x+rect.width-25-step*24,y:rect.y+22}]});
          }
          await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
          await page.waitForTimeout(100);
          assert(await strip.evaluate(g=>g.scrollLeft>0),'horizontal touch gesture must scroll the strip');
          assert.equal(await select.inputValue(),'gpt-6-sol','swiping must not change the chosen model');
          await cdp.detach();
        }
        await strip.evaluate(g=>g.scrollLeft=g.scrollWidth);
        await page.waitForTimeout(50);
        await strip.locator('button[data-value="gpt-5.6-luna"]').click();
        assert.equal(await select.inputValue(),'gpt-5.6-luna');
        const visible=()=>strip.evaluate(g=>{const r=g.querySelector('.selected').getBoundingClientRect(),v=g.getBoundingClientRect();return r.left>=v.left-1&&r.right<=v.right+1});
        await page.waitForTimeout(30);assert(await visible(),'selected legacy model visible after selection');
        if(kind==='conversation') {
          await page.locator('#conversation-settings').evaluate(e=>e.open=false);
          await strip.evaluate(g=>g.scrollLeft=0);
          await page.locator('#conversation-settings').evaluate(e=>e.open=true);
          await page.waitForTimeout(50);assert(await visible(),'reopening reveals selected old model');
        }
        await page.setViewportSize({width:320,height:844});
        await page.waitForTimeout(50);assert(await visible(),'resize keeps selected old model visible');
        assert(await strip.evaluate(g=>g.classList.contains('has-more-left')),'left edge indicates earlier models');
      }
    }
    // Existing conversation loaded with a legacy model, while its panel is closed.
    await page.setViewportSize({width:320,height:844});
    await page.setContent(`<html><head><style>${fs.readFileSync(path.join(root,'static/css/app.css'),'utf8')}</style></head><body><main>${fixtures.conversation}</main></body></html>`);
    await page.locator('#conversation-model').evaluate(s=>s.value='gpt-5.6-luna');
    await page.addScriptTag({content:fs.readFileSync(path.join(root,'static/setting-segments.js'),'utf8')});
    await page.locator('#conversation-settings').evaluate(e=>e.open=true);
    await page.waitForTimeout(50);
    assert(await page.locator('.native-settings-panel .setting-segments-model').evaluate(g=>{
      const b=g.querySelector('.selected'), r=b.getBoundingClientRect(), v=g.getBoundingClientRect();
      return b.dataset.value==='gpt-5.6-luna'&&g.scrollLeft>0&&r.left>=v.left-1&&r.right<=v.right+1;
    }),'initial saved legacy choice is revealed when its panel first opens');
    console.log('model_strip=passed themes=2 widths=320,360,390,412 common_models_visible=true legacy_scroll_and_reopen=true');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
