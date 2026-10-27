/* Offline real-template regressions; all requests are intercepted synthetic fixtures. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const snapshot = {backend: 'dot_mailbox', conversation: {
  id: 'synthetic-dot-mobile', kind: 'dot', title: 'dot', revision: 1,
}, messages: []};
const pageHtml = execFileSync(process.env.DOT_TEST_PYTHON || (process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), ['-c', `
import json, sys
from shared.html_lib import html, nav, templates
s=json.load(sys.stdin)
print(html('dot', templates.get_template('conversation.html').render(
 conversation=s['conversation'], initial_snapshot=s, back_url='/projects/history',
 project_name='dot', nav_html=nav(), settings={}, models=[], skills=[], catalog_error=False,
 upload_notice_html='', upload_button_html='', upload_panel_html='')).body.decode())
`], {cwd: root, input: JSON.stringify(snapshot), encoding: 'utf8',
  env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});

const historyHtml = execFileSync(process.env.DOT_TEST_PYTHON || (process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), ['-c', `
from tests.test_dot_history import DotHistoryTests
from routes import home
fixture=DotHistoryTests()
fixture.setUp()
try:
 print(home.history(object()).body.decode())
finally:
 fixture.doCleanups()
`], {cwd: root, encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.DOT_TEST_BROWSER || process.env.PLAYWRIGHT_EXECUTABLE_PATH,
    args: ['--no-sandbox', '--host-resolver-rules=MAP * ~NOTFOUND']});
  const results = [];
  try {
    for (const scenario of ['blur-click', 'touch', 'viewport', 'composition']) {
      const context = await browser.newContext({viewport: {width: 390, height: 844},
        isMobile: true, hasTouch: true, serviceWorkers: 'block'});
      const posts = [], errors = [], forbidden = [];
      const current = structuredClone(snapshot);
      await context.addInitScript(() => {
        window.WebSocket = class { close() {} };
        localStorage.setItem('codex-send-mode', 'enter');
        const vv = new EventTarget();
        Object.assign(vv, {offsetTop: 0, offsetLeft: 0, width: 390, height: 844});
        Object.defineProperty(window, 'visualViewport', {configurable: true, value: vv});
      });
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        const pathname = url.pathname.replace(/^\/[a-f0-9]{40}(?=\/)/, '');
        if (pathname.startsWith('/static/')) {
          const target = path.resolve(root, '.' + pathname);
          if (!target.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(target)) return route.abort();
          return route.fulfill({body: fs.readFileSync(target), contentType:
            target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'image/png'});
        }
        if (pathname === '/settings/dot/synthetic-dot-mobile') return route.fulfill({body: pageHtml, contentType: 'text/html'});
        if (pathname === '/settings/dot/synthetic-dot-mobile/messages') {
          const body = new URLSearchParams(request.postData());
          posts.push(Object.fromEntries(body));
          if (!current.messages.some(item => item.id === body.get('message_id'))) {
            current.messages.push({id: body.get('message_id'), text: body.get('text'), reply: null});
            current.conversation.revision++;
          }
          return route.fulfill({json: current});
        }
        if (pathname === '/settings/dot/synthetic-dot-mobile/snapshot') return route.fulfill({json: current});
        if (pathname.startsWith('/conversation/')) forbidden.push(pathname);
        return route.fulfill({json: {}});
      });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      page.setDefaultTimeout(2500);
      try {
        await page.goto('https://dot-test.invalid/' + 'a'.repeat(40) + '/settings/dot/synthetic-dot-mobile');
        await page.locator('textarea[name=text]').fill('合成中文：发送验证');
        await page.waitForTimeout(30);
        if (scenario === 'blur-click') {
          const box = await page.locator('.native-send').boundingBox();
          await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
          await page.mouse.down();
          const after = await page.locator('.native-send').boundingBox();
          await page.mouse.up();
          await page.waitForTimeout(80);
          assert.equal(posts.length, 1, `send lost after blur; button moved ${Math.round(after.y - box.y)}px`);
        } else if (scenario === 'touch') {
          const box = await page.locator('.native-send').boundingBox();
          await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
          await page.waitForTimeout(80);
          assert.equal(posts.length, 1, 'real touch tap must send once');
        } else if (scenario === 'viewport') {
          const geometry = await page.evaluate(() => {
            Object.assign(window.visualViewport, {offsetTop: 18, height: 490});
            window.visualViewport.dispatchEvent(new Event('resize'));
            const box = document.getElementById('conversation-composer').getBoundingClientRect();
            return {bottom: Math.round(box.bottom), nav: getComputedStyle(document.querySelector('.nav')).display};
          });
          assert.equal(geometry.bottom, 508, 'composer must remain above synthetic keyboard');
          assert.equal(geometry.nav, 'none');
          await page.locator('textarea').fill('合成多行\n'.repeat(7));
          const bottom = await page.locator('#conversation-composer').evaluate(node => Math.round(node.getBoundingClientRect().bottom));
          assert.equal(bottom, 508, 'growing composer must stay above keyboard');
        } else {
          await page.locator('textarea').dispatchEvent('compositionstart', {data: ''});
          await page.locator('textarea').dispatchEvent('keydown', {key: 'Enter', keyCode: 229, isComposing: true});
          assert.equal(posts.length, 0, 'IME candidate confirmation must not submit');
          await page.locator('textarea').dispatchEvent('compositionend', {data: '中文'});
          await page.locator('textarea').dispatchEvent('keydown', {key: 'Enter', keyCode: 13, isComposing: false});
          assert.equal(posts.length, 0, 'composition tail Enter must not submit');
          const box = await page.locator('.native-send').boundingBox();
          await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
          await page.waitForTimeout(80);
          assert.equal(posts.length, 1, 'explicit button after composition must send once');
          assert.equal(posts[0].text, '合成中文：发送验证');
        }
        assert.deepEqual(forbidden, [], 'must not invoke native conversation backend');
        assert.deepEqual(errors, []);
        results.push({scenario, passed: true});
      } catch (error) { results.push({scenario, passed: false, error: error.message}); }
      await context.close();
    }
    for (const width of [320, 390]) {
      const context = await browser.newContext({viewport: {width, height: 844}, isMobile: true, hasTouch: true, serviceWorkers: 'block'});
      const errors = [];
      await context.addInitScript(() => { window.WebSocket = class { close() {} }; });
      await context.route('**/*', async route => {
        const pathname = new URL(route.request().url()).pathname;
        if (pathname.startsWith('/static/')) {
          const target = path.resolve(root, '.' + pathname);
          if (!target.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(target)) return route.abort();
          return route.fulfill({body: fs.readFileSync(target), contentType:
            target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'image/png'});
        }
        if (pathname === '/projects/history') return route.fulfill({body: historyHtml, contentType: 'text/html'});
        // A browser navigation keeps the synthetic redirect inside interception.
        if (pathname === '/settings/dot') return route.fulfill({contentType: 'text/html', body: '<script>location.replace("/settings/dot/synthetic-dot-mobile")</script>'});
        if (pathname === '/settings/dot/synthetic-dot-mobile') return route.fulfill({body: pageHtml, contentType: 'text/html'});
        if (pathname.endsWith('/snapshot')) return route.fulfill({json: snapshot});
        return route.fulfill({json: {}});
      });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      page.setDefaultTimeout(2500);
      try {
        await page.goto('https://dot-test.invalid/projects/history');
        const entry = page.locator('.dot-history-entry');
        assert.equal(await entry.count(), 1);
        assert.equal(await entry.locator('input,.manage-card,[data-id]').count(), 0);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        assert.ok(overflow <= 1, `history entry overflows ${width}px viewport`);
        await page.evaluate(() => document.querySelector('[data-status-endpoint]').replaceChildren());
        assert.equal(await entry.isVisible(), true, 'native refresh must preserve the fixed entry');
        await entry.locator('a').tap();
        await page.waitForURL('**/settings/dot/synthetic-dot-mobile');
        assert.equal(await page.locator('#native-conversation').getAttribute('data-conversation-kind'), 'dot');
        await page.locator('.native-back').tap();
        await page.waitForURL('**/projects/history');
        assert.equal(await page.locator('.dot-history-entry').count(), 1);
        assert.deepEqual(errors, []);
        results.push({scenario: 'history-roundtrip-' + width, passed: true});
      } catch (error) { results.push({scenario: 'history-roundtrip-' + width, passed: false, error: error.message}); }
      await context.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify({real_phone_tested: false, network: 'intercepted synthetic only', results}));
  if (results.some(item => !item.passed)) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
