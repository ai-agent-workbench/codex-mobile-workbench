// Real Jinja composers and shared dismissal module; loopback-only fixtures.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const http = require('http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')),
  [path.join(root, 'tests/render_conversation_ui_fixtures.py')], {cwd: root, encoding: 'utf8'}));
const initial = JSON.parse(fixtures.conversation.match(/<script id="conversation-initial-snapshot"[^>]*>([\s\S]*?)<\/script>/)[1]);
let snapshot, settingsWrites = [];
const extra = ['light-dismiss', 'behavior', 'draft', 'setting-segments', 'file-actions', 'skill-picker'];
function html(content) {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><link rel="stylesheet" href="/static/css/app.css"><link rel="stylesheet" href="/static/css/skill-picker.css"></head><body><main>${content}</main>${extra.map(name => `<script src="/static/${name}.js" defer></script>`).join('')}</body></html>`;
}
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (url.pathname.startsWith('/static/')) {
    const file = path.join(root, url.pathname);
    if (fs.existsSync(file)) { response.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'application/javascript'); response.end(fs.readFileSync(file)); return; }
  }
  if (url.pathname === '/conversation/conversation-ui') {
    snapshot = JSON.parse(JSON.stringify(initial)); settingsWrites = [];
    response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(html(fixtures.conversation)); return;
  }
  if (url.pathname === '/project/project-one') {response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(html(fixtures.project)); return;}
  if (url.pathname.endsWith('/snapshot')) {response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(snapshot)); return;}
  if (url.pathname.endsWith('/settings')) {
    let body = ''; request.on('data', chunk => body += chunk); request.on('end', () => {
      const values = {};
      for (const match of body.matchAll(/Content-Disposition: form-data; name="([^"]+)"\r\n\r\n([^]*?)\r\n(?=--)/g)) values[match[1]] = match[2];
      settingsWrites.push(values);
      for (const key of ['model', 'effort', 'permission', 'service_tier']) if (key in values) snapshot.conversation.settings[key] = values[key] || null;
      snapshot.conversation.revision++;
      response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(snapshot));
    }); return;
  }
  if (url.pathname.endsWith('/read')) {response.setHeader('Content-Type', 'application/json'); response.end('{}'); return;}
  response.statusCode = 404; response.end('fixture only');
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    for (const width of [320, 390, 412]) for (const theme of ['dark', 'light']) {
      const page = await browser.newPage({viewport: {width, height: 844}, hasTouch: true, isMobile: true});
      page.setDefaultTimeout(5000); const errors = []; page.on('pageerror', error => errors.push(error.message));
      await page.goto(base + '/conversation/conversation-ui'); await page.waitForSelector('.native-message-more');
      await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
      async function outside(selector) {
        const rect = await page.locator(selector).boundingBox();
        const point = [{x: 2, y: 420}, {x: width / 2, y: 600}, {x: width / 2, y: 830}]
          .find(point => point.x < rect.x || point.x > rect.x + rect.width || point.y < rect.y || point.y > rect.y + rect.height);
        assert(point, 'fixture must expose an outside point: ' + selector); return point;
      }
      async function tapOutside(selector) {const p = await outside(selector); await page.touchscreen.tap(p.x, p.y);}
      async function modal(selector, open) {
        await open(); const dialog = page.locator(selector); assert(await dialog.evaluate(el => el.open), selector + ' opened');
        const r = await dialog.boundingBox(); await page.touchscreen.tap(r.x + 6, r.y + 6);
        assert(await dialog.evaluate(el => el.open), 'inside padding must not close ' + selector);
        await tapOutside(selector); assert(!await dialog.evaluate(el => el.open), 'outside tap must close ' + selector);
      }
      await page.locator('.native-head-menu > summary').tap(); await page.locator('#search-conversation').tap();
      const searchField = await page.locator('#conversation-search-input').evaluate(input => {
        const field=input.closest('.search-field'),icon=field.querySelector('.search-icon');
        const gray=document.createElement('span');gray.style.color='var(--muted)';field.append(gray);
        const color=getComputedStyle(gray).color;gray.remove();
        const rect=icon.getBoundingClientRect(),parent=field.getBoundingClientRect();
        return {gray:getComputedStyle(icon).color===color,inside:rect.left>=parent.left&&rect.right<=parent.right,
          height:input.getBoundingClientRect().height,label:input.getAttribute('aria-label'),buttons:field.querySelectorAll('button').length};
      });
      assert(searchField.gray&&searchField.inside&&searchField.height>=44&&searchField.label&&!searchField.buttons,'本地查找的搜索框图标不统一');
      await page.locator('#conversation-search-input').fill('首屏'); assert(await page.locator('.is-search-hit').count() > 0);
      await tapOutside('#conversation-search-dialog'); await page.waitForFunction(() => !document.querySelector('.is-search-hit')); assert.equal(await page.locator('.is-search-hit').count(), 0, 'outside close clears search highlight');
      await page.locator('.native-head-menu > summary').tap(); await page.locator('#search-conversation').tap();
      await page.locator('#conversation-search-input').fill('首屏'); await page.locator('[data-close-search]').focus(); await page.keyboard.press('Escape'); await page.waitForFunction(() => !document.querySelector('.is-search-hit'));
      assert(!await page.locator('#conversation-search-dialog').evaluate(el => el.open)); assert.equal(await page.locator('.is-search-hit').count(), 0, 'native Escape clears search highlight');
      if (process.argv.includes('--search-only')) {
        assert.deepEqual(errors, [], 'search page JS errors');
        await page.close();
        console.log(`conversation_search=passed width=${width} theme=${theme} gray_icon=true live_lookup=true dismiss=true`);
        continue;
      }
      await modal('#conversation-upload', () => page.locator('.attach-button').tap());
      await page.locator('.attach-button').tap();
      const file = {name: 'fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('safe fixture')};
      await page.locator('#conversation-upload input[type=file]').setInputFiles(file);
      await tapOutside('#conversation-upload');
      assert(await page.locator('#conversation-upload').evaluate(el => el.open), 'selected upload must remain open');
      assert.equal(await page.locator('#conversation-upload input[type=file]').evaluate(el => el.files.length), 1, 'selected file retained');
      await page.locator('#conversation-upload .secondary').tap();
      await page.locator('#conversation-upload input[type=file]').setInputFiles([]);
      // A dragged pointer is not a backdrop tap, even if its click target is the dialog.
      await page.locator('.attach-button').tap();
      const uploadRect = await page.locator('#conversation-upload').boundingBox(), uploadOutside = await outside('#conversation-upload');
      await page.mouse.move(uploadRect.x + 8, uploadRect.y + 8); await page.mouse.down();
      await page.mouse.move(uploadOutside.x, uploadOutside.y, {steps: 5}); await page.mouse.up();
      assert(await page.locator('#conversation-upload').evaluate(el => el.open), 'inside-to-outside drag must not dismiss');
      const cdp = await page.context().newCDPSession(page);
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [uploadOutside]});
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: uploadOutside.x, y: uploadOutside.y + 70}]});
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
      assert(await page.locator('#conversation-upload').evaluate(el => el.open), 'backdrop swipe must not dismiss');
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [uploadOutside]});
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchCancel', touchPoints: []});
      assert(await page.locator('#conversation-upload').evaluate(el => el.open), 'cancelled pointer must not dismiss'); await cdp.detach();
      const multi = await page.context().newCDPSession(page);
      await multi.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [uploadOutside, {x: uploadOutside.x + 24, y: uploadOutside.y + 24}]});
      await multi.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
      assert(await page.locator('#conversation-upload').evaluate(el => el.open), 'two-finger gesture must not dismiss'); await multi.detach();
      await tapOutside('#conversation-upload'); assert(!await page.locator('#conversation-upload').evaluate(el => el.open), 'next real tap after drag/cancel closes');
      await page.locator('.attach-button').tap();
      await page.locator('#conversation-upload form').evaluate(form => {
        form.addEventListener('submit', event => event.preventDefault(), {once: true});
        form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
      });
      await tapOutside('#conversation-upload'); assert(!await page.locator('#conversation-upload').evaluate(el => el.open), 'cancelled submit does not leave an uploading guard');

      await page.locator('#settings-summary').tap();
      await page.locator('.native-settings-head strong').tap(); assert(await page.locator('#conversation-settings').evaluate(el => el.open));
      await page.locator('.setting-segments-model [data-value="gpt-6-sol"]').tap();
      await tapOutside('.native-settings-panel'); assert(!await page.locator('#conversation-settings').evaluate(el => el.open));
      assert.equal(settingsWrites.length, 0, 'outside dismissal must not save model');
      await page.locator('#settings-summary').tap(); assert.equal(await page.locator('#conversation-model').inputValue(), 'gpt-6-sol', 'unsaved selection retained');
      await page.locator('[data-close-settings]').tap(); await page.waitForFunction(() => !document.querySelector('#conversation-settings').open);
      await page.waitForTimeout(60); assert.equal(settingsWrites.length, 1, 'explicit Done saves once'); assert.equal(settingsWrites[0].model, 'gpt-6-sol');
      // Pointer down inside followed by up outside must not dismiss settings either.
      await page.locator('#settings-summary').tap(); const settingsRect = await page.locator('.native-settings-panel').boundingBox();
      const settingsOutside = await outside('.native-settings-panel');
      await page.mouse.move(settingsRect.x + 15, settingsRect.y + 15); await page.mouse.down();
      await page.mouse.move(settingsOutside.x, settingsOutside.y, {steps: 5}); await page.mouse.up();
      assert(await page.locator('#conversation-settings').evaluate(el => el.open), 'settings drag retains panel');
      await tapOutside('.native-settings-panel');
      await page.locator('#settings-summary').focus(); await page.keyboard.press('Enter'); await page.keyboard.press('Escape');
      assert(!await page.locator('#conversation-settings').evaluate(el => el.open), 'Escape closes settings');
      assert(await page.locator('#settings-summary').evaluate(el => el === document.activeElement), 'Escape restores summary focus');
      await modal('#conversation-context', () => page.locator('#conversation-context-toggle').tap());
      for (const [dialog, button] of [['#conversation-export-dialog', '#export-conversation'], ['#conversation-search-dialog', '#search-conversation'], ['#conversation-diagnostics', '#diagnose-conversation']]) {
        await modal(dialog, async () => {await page.locator('.native-head-menu > summary').tap(); await page.locator(button).tap();});
      }
      await page.locator('#conversation-audio-bar').evaluate(el => el.hidden = false);
      await modal('#conversation-audio-settings-dialog', () => page.locator('#conversation-audio-settings').tap());
      await page.locator('#conversation-audio-bar').evaluate(el => el.hidden = true);
      await page.locator('.native-head-menu > summary').tap(); await page.touchscreen.tap(2, 420);
      assert(!await page.locator('.native-head-menu').evaluate(el => el.open));
      await page.locator('.native-message-more').last().tap(); const bubble = await page.locator('.native-bubble').last().boundingBox();
      const bubblePoint = {x: bubble.x + bubble.width / 2, y: bubble.y + bubble.height - 5};
      assert(await page.evaluate(p => !!document.elementFromPoint(p.x, p.y).closest('.native-bubble') && !document.elementFromPoint(p.x, p.y).closest('.native-message-menu'), bubblePoint), 'tap is bubble whitespace outside menu');
      await page.touchscreen.tap(bubblePoint.x, bubblePoint.y); assert(await page.locator('.native-message-menu').last().evaluate(el => el.hidden), 'bubble whitespace closes menu');
      await page.locator('.native-message-more').last().tap(); assert(await page.locator('.native-message-menu').last().isVisible());
      await page.locator('.native-message-more').last().tap(); assert(await page.locator('.native-message-menu').last().isHidden(), 'own trigger toggles closed');
      await page.locator('.native-message-more').last().tap(); await page.locator('.native-message-menu button').last().focus(); await page.keyboard.press('Escape');
      assert(await page.locator('.native-message-menu').last().isHidden()); assert(await page.locator('.native-message-more').last().evaluate(el => el === document.activeElement));
      // Capture-phase handling must survive a message button's stopPropagation.
      await page.locator('.native-head-menu > summary').tap(); await page.locator('.native-message-more').last().evaluate(el => el.click());
      assert(!await page.locator('.native-head-menu').evaluate(el => el.open), 'opening message menu dismisses header despite stopPropagation');
      await page.touchscreen.tap(2, 420);
      // Outside tap still activates its actual target; dismissal must not consume clicks.
      await page.evaluate(() => {const button = document.createElement('button'); button.id = 'outside-target'; button.textContent = 'fixture'; button.style = 'position:fixed;top:410px;left:0;z-index:80'; button.onclick = () => button.dataset.clicked = 'yes'; document.body.append(button);});
      await page.locator('#settings-summary').tap(); await page.locator('#outside-target').tap();
      assert(!await page.locator('#conversation-settings').evaluate(el => el.open)); assert.equal(await page.locator('#outside-target').getAttribute('data-clicked'), 'yes'); await page.locator('#outside-target').evaluate(el => el.remove());
      // Content disclosure and unmarked confirmation dialogs are not dismissible overlays.
      await page.evaluate(() => {const details = document.createElement('details'); details.id = 'reading-details'; details.open = true; details.innerHTML = '<summary>正文详情</summary><p>content</p>'; document.querySelector('main').append(details); const dialog = document.createElement('dialog'); dialog.id = 'confirm-fixture'; dialog.innerHTML = '<p>确认权限变更</p><button type="button">确认</button>'; document.body.append(dialog); dialog.showModal();});
      await tapOutside('#confirm-fixture'); assert(await page.locator('#confirm-fixture').evaluate(el => el.open), 'confirmation remains protected');
      await page.locator('#confirm-fixture').evaluate(el => el.close()); await page.touchscreen.tap(2, 420); assert(await page.locator('#reading-details').evaluate(el => el.open));
      await page.evaluate(() => {const body = document.querySelector('.native-message.agent .native-message-body'); body.removeAttribute('data-native-enhanced'); body.textContent = '| A | B |\n| --- | --- |\n| 1 | 2 |';});
      await page.locator('.native-table-wrap .native-local-copy').tap(); await page.touchscreen.tap(2, 420); assert(await page.locator('#native-table-copy-sheet').isHidden(), 'table copy still dismisses');
      assert.deepEqual(errors, [], 'page JS errors');
      await page.goto(base + '/project/project-one'); await modal('#project-upload', () => page.locator('.attach-button').tap());
      await page.locator('.attach-button').tap(); await page.locator('#project-upload input[type=file]').setInputFiles(file);
      await tapOutside('#project-upload'); assert(await page.locator('#project-upload').evaluate(el => el.open));
      await page.close();
      console.log(`light_dismiss=passed width=${width} theme=${theme} touch=true protected_upload=true unsaved_settings_retained=true`);
    }
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(error => {console.error(error); process.exitCode = 1; server.close();});
