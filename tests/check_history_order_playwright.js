const {chromium} = require('playwright');
const {spawn} = require('child_process');
const path = require('path');
const root = path.resolve(__dirname, '..');
function assert(value, message) { if (!value) throw new Error(message); }
const fixture = spawn((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [path.join(__dirname, 'serve_history_order_fixture.py')], {cwd: root});
let output = '';
fixture.stdout.on('data', data => { output += data.toString(); });
fixture.stderr.on('data', data => { output += data.toString(); });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const ids = page => page.locator('.conversation-history .manage-card').evaluateAll(nodes => nodes.map(node => node.dataset.id));
async function advance(page, ms = 1100) { await page.clock.fastForward(ms); await sleep(60); }
async function terminal(page, cid) {
  await page.evaluate(value => window.dispatchEvent(new CustomEvent('codex:conversation-terminal', {
    detail: {conversation_id: value}
  })), cid);
  await advance(page);
}
async function top(page, clock = true) {
  await page.evaluate(() => {
    document.activeElement.blur();
    const list = document.querySelector('.conversation-history');
    scrollTo(0, Math.max(0, scrollY + list.getBoundingClientRect().top - 12));
  });
  if (clock) await advance(page); else await sleep(1100);
}

(async () => {
  let browser;
  try {
    for (let n = 0; n < 100 && !/FIXTURE_PORT=(\d+)/.test(output); n++) {
      if (fixture.exitCode !== null) throw new Error(output);
      await sleep(100);
    }
    const port = output.match(/FIXTURE_PORT=(\d+)/);
    assert(port, '隔离排序服务未启动：' + output);
    const base = 'http://127.0.0.1:' + port[1];
    async function post(url, data) {
      const response = await fetch(base + '/_fixture/' + url, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data || {})});
      assert(response.ok, 'fixture control failed: ' + url);
    }
    const requests = async () => (await fetch(base + '/_fixture/requests')).json();
    browser = await chromium.launch({headless: true});
    let cases = 0;
    for (const scope of ['history', 'project']) for (const width of [320, 390]) for (const theme of ['dark', 'light']) {
      await post('reset'); await post('pin/search-044');
      const context = await browser.newContext({viewport: {width, height: 844}});
      await context.addInitScript(value => {
        localStorage.setItem('codex-theme', value);
        window.WebSocket = class {
          static OPEN = 1;
          constructor() { this.readyState = 1; setTimeout(() => this.onopen && this.onopen(), 0); }
          send(data) { if (data === 'ping') setTimeout(() => this.onmessage && this.onmessage({data: '{"type":"pong"}'}), 0); }
          close() { this.readyState = 3; }
        };
      }, theme);
      const page = await context.newPage();
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      page.setDefaultTimeout(8000);
      await page.clock.install();
      const prefix = width === 390 ? '/' + 'a'.repeat(40) : '';
      const url = base + prefix + (scope === 'history' ? '/projects/history' : '/project/fixture');
      await page.goto(url); await advance(page);
      assert((await ids(page))[0] === 'search-044', '置顶不在首位');
      assert(!(await ids(page)).includes('search-000'), '目标不在未加载页，测试前提错误');
      const initialCursor = await page.locator('[data-conversation-pagination]').getAttribute('data-cursor');
      await post('complete/search-000'); await terminal(page, 'search-000');
      await page.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card')[1].dataset.id === 'search-000');
      assert((await ids(page))[0] === 'search-044', '完成挤走了置顶');
      assert(await page.locator('[data-conversation-pagination]').getAttribute('data-cursor') !== initialCursor, '刷新仍沿用旧分页游标');
      assert((await requests()).some(req => req.path.endsWith('/page') && req.cursor === null), '没有取得新的后端排序');
      assert(await page.locator('.conversation-date-group > h2').allTextContents().then(headings => headings.includes('已完成未读')), '没有刷新未读分组');

      await page.locator('[data-load-more]').click();
      await page.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card').length === 40);
      await page.evaluate(() => scrollBy(0, 400)); await advance(page);
      const oldIds = await ids(page), oldScroll = await page.evaluate(() => scrollY);
      const oldCursor = await page.locator('[data-conversation-pagination]').getAttribute('data-cursor');
      await post('complete/search-001'); await terminal(page, 'search-001');
      const update = page.locator('[data-history-update]');
      assert(await update.isVisible(), '浏览中没有提供更新入口');
      assert((await ids(page)).join() === oldIds.join(), '浏览中自动挪动卡片');
      assert(Math.abs(await page.evaluate(() => scrollY) - oldScroll) <= 1, '提示导致阅读位置变化');
      assert(await update.textContent() === '有新完成的对话，点此更新', '完成提示文案错误');
      await page.screenshot({path: '/tmp/codex-history-order-' + scope + '-' + width + '-' + theme + '.png'});
      await update.click();
      await page.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card')[1].dataset.id === 'search-001');
      assert((await ids(page)).length === 40 && new Set(await ids(page)).size === 40, '手动刷新丢失页数或产生重复');
      assert(await page.locator('[data-conversation-pagination]').getAttribute('data-cursor') !== oldCursor, '新排序混用了旧续页');
      await page.locator('[data-load-more]').click();
      await page.waitForFunction(() => !document.querySelector('[data-load-more]'));
      assert(new Set(await ids(page)).size === (await ids(page)).length, '刷新后的续页重复');
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '窄屏提示横向溢出');

      await top(page);
      await page.locator('.conversation-history .manage-card').first().dispatchEvent('pointerdown', {clientX: 30, clientY: 80});
      await advance(page, 600);
      await page.locator('.conversation-history .manage-card').first().dispatchEvent('pointerup');
      assert(await page.evaluate(() => document.body.classList.contains('managing')), '未进入真实长按管理');
      const managedIds = await ids(page);
      await post('complete/search-002'); await terminal(page, 'search-002');
      assert((await ids(page)).join() === managedIds.join(), '管理中重排');
      assert(await page.locator('.manage-check:checked').count() === 1, '管理中丢失勾选');
      assert(await update.isHidden(), '管理中出现干扰提示');
      await page.locator('[data-cancel]').click(); await advance(page);
      await top(page);
      await page.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card')[1].dataset.id === 'search-002');

      if (scope === 'history') await page.locator('[data-search-toggle]').click();
      await page.locator('[data-history-search-input]').fill('共同关键词');
      await page.locator('[data-search-form] button').click();
      await page.waitForFunction(() => document.querySelector('[data-history-search]').dataset.searchBusy === 'false');
      const searchIds = await page.locator('.history-search-card').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')));
      const ordinaryIds = await ids(page);
      await post('complete/search-003'); await terminal(page, 'search-003');
      assert((await ids(page)).join() === ordinaryIds.join(), '搜索中改变普通列表');
      assert(await update.isHidden(), '搜索中出现更新提示');
      assert((await page.locator('.history-search-card').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')))).join() === searchIds.join(), '搜索结果被重排');
      assert(await page.locator('[data-history-search-input]').inputValue() === '共同关键词', '关键词丢失');
      await page.locator('[data-search-clear]').click(); await top(page);
      await page.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card')[1].dataset.id === 'search-003');

      // Timing fallback is tested below against the real clock: fast-forwarding
      // time while real network/scroll callbacks are pending is not evidence.
      const expectedEndpoint = scope === 'history' ? '/projects/history' : '/project/fixture/conversations';
      assert((await requests()).every(req => req.path.startsWith(expectedEndpoint)),
        '项目和全局刷新范围混用 ' + scope + '/' + width + '/' + theme + ': ' + JSON.stringify(await requests()));
      assert(errors.length === 0, errors.join('\n'));
      await context.close(); cases += 1;
    }

    // Independent real-clock proof: both scopes discover an unseen completion
    // without a terminal event, within the 25-second polling fallback.
    await post('reset'); await post('pin/search-044');
    const realContexts = [], realPages = [];
    for (const route of ['/projects/history', '/project/fixture']) {
      const context = await browser.newContext({viewport: {width: 390, height: 844}});
      await context.addInitScript(() => { window.WebSocket = class { close() {} }; });
      const page = await context.newPage();
      await page.goto(base + route);
      await sleep(200);
      assert(!(await ids(page)).includes('search-000'), '真实计时场景目标已经加载');
      realContexts.push(context); realPages.push(page);
    }
    await post('complete/search-000');
    await Promise.all(realPages.map(page => page.waitForFunction(
      () => document.querySelectorAll('.conversation-history .manage-card')[1].dataset.id === 'search-000',
      null, {timeout: 30000},
    )));
    for (const context of realContexts) await context.close();

    // Chromium's real touch-pan lifecycle emits pointercancel while the touch
    // is still on screen. It must not be treated as finger release.
    for (const route of ['/projects/history', '/project/fixture']) {
      await post('reset'); await post('pin/search-044');
      const context = await browser.newContext({viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true});
      await context.addInitScript(() => { window.WebSocket = class { close() {} }; });
      const page = await context.newPage();
      await page.goto(base + route);
      await page.evaluate(() => {
        window.__gesture = {pointercancel: 0, start: 0, end: 0};
        document.addEventListener('pointercancel', () => { window.__gesture.pointercancel++; });
        document.addEventListener('touchstart', () => { window.__gesture.start++; }, {passive: true});
        document.addEventListener('touchend', () => { window.__gesture.end++; }, {passive: true});
        const list = document.querySelector('.conversation-history');
        scrollTo(0, scrollY + list.getBoundingClientRect().top + 160);
      });
      await sleep(200);
      const original = await ids(page);
      await post('complete/search-000');
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:conversation-terminal', {detail: {conversation_id: 'search-000'}})));
      await page.locator('[data-history-update]').waitFor({state: 'visible'});
      const cdp = await context.newCDPSession(page);
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: 190, y: 160}]});
      for (const y of [220, 340, 480, 600]) {
        await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: 190, y}]});
        await sleep(45);
      }
      const gesture = await page.evaluate(() => ({...window.__gesture, top: document.querySelector('.conversation-history').getBoundingClientRect().top}));
      assert(gesture.start > 0 && gesture.pointercancel > 0 && gesture.end === 0, '未复现真实触摸接管与 pointercancel');
      assert(gesture.top >= -8, '手指未将列表滑回顶部，测试前提错误');
      await sleep(1500);
      assert((await ids(page)).join() === original.join(), '手指未离屏但 pointercancel 后自动重排');
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
      await page.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card')[1].dataset.id === 'search-000', null, {timeout: 8000});
      assert(await page.evaluate(() => window.__gesture.end) > 0, '没有真实触摸释放');
      await context.close();
    }

    // Slow multi-page refresh: reject the entire staged result if the user
    // scrolls, starts searching or a continuation fails before atomic commit.
    for (const action of ['scroll', 'search', 'management', 'failure', 'auth']) {
      await post('reset');
      const context = await browser.newContext({viewport: {width: 390, height: 844}});
      await context.addInitScript(() => { window.WebSocket = class { close() {} }; });
      const page = await context.newPage(); page.setDefaultTimeout(8000);
      await page.goto(base + '/projects/history');
      await page.locator('[data-load-more]').click();
      await page.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card').length === 40);
      await top(page, false);
      const original = await ids(page), cursor = await page.locator('[data-conversation-pagination]').getAttribute('data-cursor');
      const beforeRequests = (await requests()).filter(req => req.path.endsWith('/page') && !req.cursor).length;
      await post('flags', {delay: 0.3, fail_cursor: action === 'failure'});
      await post('complete/search-000');
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:conversation-terminal', {detail: {conversation_id: 'search-000'}})));
      for (let n = 0; n < 100; n++) {
        if ((await requests()).filter(req => req.path.endsWith('/page') && !req.cursor).length > beforeRequests) break;
        await sleep(30);
      }
      if (action === 'scroll') await page.evaluate(() => scrollTo(0, 600));
      if (action === 'search') await page.locator('[data-search-toggle]').click();
      if (action === 'management') await page.locator('.conversation-history .manage-card').first().dispatchEvent('pointerdown', {clientX: 30, clientY: 80});
      if (action === 'auth') await post('flags', {auth: true});
      await sleep(800);
      assert((await ids(page)).join() === original.join(), action + ' 导致部分提交或丢失旧列表');
      assert(await page.locator('[data-conversation-pagination]').getAttribute('data-cursor') === cursor, action + ' 覆写旧游标');
      if (action === 'failure') {
        assert(await page.locator('[data-history-update]').textContent() === '更新失败，点此重试', '失败没有重试入口');
        await post('flags', {fail_cursor: false, delay: 0});
        await page.locator('[data-history-update]').click();
        await page.waitForFunction(() => document.querySelector('.conversation-history .manage-card').dataset.id === 'search-000');
        assert((await ids(page)).length === 40, '失败重试未恢复已加载页数');
      }
      if (action === 'auth') assert(await page.locator('[data-history-update]').isHidden(), '鉴权失效后仍提供排序操作');
      await context.close();
    }
    console.log('history order Playwright passed: ' + cases + ' scope/width/theme cases, 2 real-clock fallback cases, 2 native touch-pan cases, 5 atomicity/failure cases');
  } finally {
    if (browser) await browser.close();
    fixture.kill('SIGTERM');
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
