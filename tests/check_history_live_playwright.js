const { chromium } = require('playwright');
const childProcess = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [
  path.join(__dirname, 'render_workspace_mobile_ui_fixtures.py'),
], {cwd: root, encoding: 'utf8'}));
const historyLive = fs.readFileSync(path.join(root, 'static/history-live.js'), 'utf8');
async function advance(page, milliseconds) {
  await page.clock.fastForward(milliseconds);
  await page.waitForTimeout(15);
}
let statusRequests = [];
let statusEndpoints = [];
let nextStatus = 'active';
let nextTitle = null;
let statusErrors = 0;
let statusUnauthorized = false;
let delayStatus = false;
let malformedStatus = false;

function assert(value, message) { if (!value) throw new Error(message); }
async function statusContrast(page) {
  return page.locator('.manage-card[data-id="initial-0"] .conversation-card-status').evaluate(node => {
    function luminance(color) {
      const rgb = color.match(/[\d.]+/g).slice(0, 3).map(value => {
        const channel = Number(value) / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
    }
    const style = getComputedStyle(node);
    const a = luminance(style.color), b = luminance(style.backgroundColor);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  });
}
function card(id, state, unread, formId = 'history-conversation-manage') {
  const running = ['starting_thread', 'starting_turn', 'active', 'interrupting', 'reconciling'].includes(state);
  const failed = state === 'failed', stopped = state === 'stopped';
  const classes = running ? ' running' : state === 'needs_user' ? ' needs' : failed ? ' failed' : stopped ? ' stopped' : state === 'idle' ? ' complete' : '';
  const isUnread = unread && state === 'idle';
  const label = failed ? '执行失败' : stopped ? '已停止' : isUnread ? '完成未读' : running ? '执行中' : state === 'idle' ? '已就绪' : '需要核对';
  const conversationState = failed || stopped ? 'idle' : state;
  const title = (id === 'initial-0' && nextTitle !== null ? nextTitle : `不应替换的标题 ${id}`)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return `<label class="manage-card conversation-index-card${classes}${isUnread ? ' unread' : ''}" data-id="${id}"><span class="conversation-state-rail"></span><input class="manage-check" type="checkbox" form="${formId}" name="ids" value="${id}"><a class="card" href="/conversation/${id}" data-conversation-state="${conversationState}" data-conversation-revision="${conversationState === 'idle' ? 9 : 8}"><span class="conversation-card-copy"><b>${title}</b><small>临时对话 · 09-05 12:00</small></span><span class="conversation-card-status"><i></i>${label}</span></a></label>`;
}
const server = http.createServer((request, response) => {
  const address = new URL(request.url, 'http://fixture.test');
  const pathname = address.pathname;
  if (pathname === '/history') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8'); return response.end(fixtures.history);
  }
  if (pathname === '/project/fixture') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8'); return response.end(fixtures.project);
  }
  if (pathname === '/projects/history/status' || pathname === '/project/fixture/conversations/status') {
    const ids = address.searchParams.getAll('ids'); statusRequests.push(ids); statusEndpoints.push(pathname);
    if (statusUnauthorized) { response.statusCode = 401; return response.end(JSON.stringify({code: 'auth_required'})); }
    if (statusErrors) { statusErrors -= 1; response.statusCode = 503; return response.end(JSON.stringify({error: 'fixture'})); }
    if (malformedStatus) return response.end(JSON.stringify({html: null}));
    const formId = pathname.startsWith('/project/') ? 'project-conversation-manage' : 'history-conversation-manage';
    const send = () => response.end(JSON.stringify({html: ids.filter(id => id !== 'missing').map(id => card(id, nextStatus, nextStatus === 'idle', formId)).join('')}));
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    return delayStatus ? setTimeout(send, 400) : send();
  }
  if (pathname === '/projects/history/page') {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    return response.end(JSON.stringify({html: fixtures.history_more_1, next_cursor: null}));
  }
  if (pathname === '/project/fixture/conversations/page') {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    return response.end(JSON.stringify({html: fixtures.project_more_1, next_cursor: null}));
  }
  if (pathname.startsWith('/static/')) {
    const relative = pathname.slice('/static/'.length);
    if (relative === 'history-live.js') { response.setHeader('Content-Type', 'application/javascript'); return response.end(historyLive); }
    const filename = path.resolve(root, 'static', relative);
    if (filename.startsWith(path.join(root, 'static')) && fs.existsSync(filename)) {
      response.setHeader('Content-Type', filename.endsWith('.css') ? 'text/css' : 'application/javascript');
      return fs.createReadStream(filename).pipe(response);
    }
  }
  if (pathname === '/sw.js') return response.end('');
  response.statusCode = 404; response.end();
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [360, 393]) for (const theme of ['dark', 'light']) {
      statusRequests = []; nextStatus = 'active'; nextTitle = null;
      const context = await browser.newContext({viewport: {width, height: 852}});
      const page = await context.newPage();
      await page.clock.install({time: new Date('2026-09-05T12:00:00Z')});
      await page.addInitScript(themeValue => {
        localStorage.setItem('codex-theme', themeValue);
        window.__fakeSockets = [];
        window.WebSocket = class {
          constructor() { window.__fakeSockets.push(this); setTimeout(() => this.onopen && this.onopen(), 0); }
          close() { this.readyState = 3; if (this.onclose) this.onclose({code: 1000}); }
          emit(value) { this.onmessage && this.onmessage({data: JSON.stringify(value)}); }
        };
      }, theme);
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      page.setDefaultTimeout(5000);
      await page.goto(base + '/history', {waitUntil: 'domcontentloaded'});
      await advance(page, 1);
      await page.waitForFunction(() => document.querySelector('.conversation-index-card.running'));
      const before = await page.evaluate(() => {
        const card = document.querySelector('.manage-card');
        const link = card.querySelector('a'); link.focus();
        card.querySelector('input').checked = true;
        card.querySelector('b').textContent = '这是一个用于核对滚动锚点是否保持稳定的很长很长很长的历史对话标题';
        window.scrollTo(0, 520);
        const anchor = [...document.querySelectorAll('.manage-card')].find(item => item.getBoundingClientRect().bottom > 0);
        return {title: card.querySelector('b').textContent, anchorId: anchor.dataset.id, anchorTop: anchor.getBoundingClientRect().top};
      });
      // This synthetic long title represents persisted server metadata, not
      // an unsaved local edit. Status changes must keep the same title node.
      nextTitle = before.title;
      // A same-state websocket burst must not turn every revision into a list query.
      const beforeBurst = statusRequests.length;
      await page.evaluate(() => { for (let i = 0; i < 12; i += 1) window.__fakeSockets[0].emit({type: 'conversation.updated', data: {conversation_id: 'initial-0', state: 'active', revision: 20 + i}}); });
      await advance(page, 800);
      assert(statusRequests.length === beforeBurst, 'same-state update burst requested history repeatedly');

      nextStatus = 'idle';
      await page.evaluate(() => window.__fakeSockets[0].emit({type: 'conversation.terminal', data: {conversation_id: 'initial-0', turn_id: 'turn-1'}}));
      // The global socket event is intentionally verified before the 25-second fallback.
      await advance(page, 1000);
      await page.waitForFunction(() => document.querySelector('.manage-card[data-id="initial-0"]').classList.contains('unread'));
      const preserved = await page.evaluate(anchorId => {
        const card = document.querySelector('.manage-card[data-id="initial-0"]');
        const anchor = document.querySelector('.manage-card[data-id="' + anchorId + '"]');
        return {
          title: card.querySelector('b').textContent,
          checked: card.querySelector('input').checked,
          focused: document.activeElement === card.querySelector('a'),
          state: card.querySelector('a').dataset.conversationState,
          revision: card.querySelector('a').dataset.conversationRevision,
          anchorTop: anchor.getBoundingClientRect().top,
        };
      }, before.anchorId);
      assert(preserved.title === before.title, 'status refresh replaced title');
      assert(preserved.checked && preserved.focused, 'status refresh replaced checkbox or focused link');
      assert(preserved.state === 'idle' && preserved.revision === '9', 'status/revision was not updated in place');
      assert(await page.locator('.manage-card[data-id="initial-0"] .conversation-card-copy small').textContent() === '临时对话 · 09-05 12:00', 'visual card repeated status in metadata');
      assert(await page.locator('.manage-card[data-id="initial-0"] .conversation-card-status').textContent() === '完成未读', 'terminal chip did not keep the single explicit status');
      assert(Math.abs(preserved.anchorTop - before.anchorTop) <= 1, 'long title/state chip update jumped reading scroll position');
      assert(await page.evaluate(() => location.pathname) === '/history', 'terminal refresh navigated page');
      assert(await statusContrast(page) >= 4.5, `unread status text contrast below 4.5 at ${width}/${theme}`);
      const afterIdle = statusRequests.length;
      await advance(page, 2000);
      assert(statusRequests.length === afterIdle, 'idle meta/chip update self-triggered a refresh loop');

      // Existing pagination appends cards. The live observer must include them without a full redraw.
      await page.locator('[data-load-more]').click();
      await page.waitForFunction(() => document.querySelectorAll('.manage-card').length === 40);
      await advance(page, 1000);
      assert(statusRequests.some(batch => batch.includes('more-0')), 'appended pagination card was not queried');
      assert(await page.evaluate(() => document.querySelector('.manage-card[data-id="initial-0"] input').checked), 'pagination/live update reset management checkbox');

      // A changed-state update is coalesced but does refresh before the fallback.
      nextStatus = 'active';
      await page.evaluate(() => window.__fakeSockets[0].emit({type: 'conversation.updated', data: {conversation_id: 'initial-0', state: 'active'}}));
      await advance(page, 1000);
      await page.waitForFunction(() => document.querySelector('.manage-card[data-id="initial-0"]').classList.contains('running'));
      assert(await page.locator('.manage-card[data-id="initial-0"] .conversation-card-copy small').textContent() === '临时对话 · 09-05 12:00', 'running card repeated status in metadata');
      assert(await page.locator('.manage-card[data-id="initial-0"] .conversation-card-status').textContent() === '执行中', 'running chip lost its explicit status');
      const runningColor = await page.evaluate(() => getComputedStyle(
        document.querySelector('.manage-card[data-id="initial-0"] .conversation-state-rail')
      ).backgroundColor);
      assert(theme === 'dark' ? runningColor === 'rgb(118, 151, 179)' : runningColor === 'rgb(88, 127, 159)', `running color is not the approved muted blue at ${width}/${theme}: ${runningColor}`);
      assert(await statusContrast(page) >= 4.5, `running status text contrast below 4.5 at ${width}/${theme}`);
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), `history layout overflow at ${width}/${theme}`);

      const generatedTitle = '采购单分组汇总 <script>window.badTitle=true</script>';
      const beforeTitleEvent = statusRequests.length;
      await page.evaluate(title => window.__fakeSockets[0].emit({type: 'conversation.updated', data: {
        conversation_id: 'initial-0', state: 'active', revision: 42, title,
      }}), generatedTitle);
      await advance(page, 800);
      assert(await page.locator('.manage-card[data-id="initial-0"] b').textContent() === generatedTitle,
        'generated title was not updated in place during the same active state');
      assert(statusRequests.length === beforeTitleEvent, 'title-only event made an unnecessary status request');
      assert(await page.evaluate(() => !window.badTitle), 'title event was interpreted as HTML');
      assert(await page.evaluate(() => document.querySelector('.manage-card[data-id="initial-0"] input').checked),
        'title update reset selection');
      await page.evaluate(() => window.__fakeSockets[0].emit({type: 'conversation.updated', data: {
        conversation_id: 'initial-0', state: 'active', revision: 41, title: '过时标题',
      }}));
      assert(await page.locator('.manage-card[data-id="initial-0"] b').textContent() === generatedTitle,
        'older title event replaced newer metadata');

      // The actual production 25-second fallback still queries all loaded cards.
      const beforePoll = statusRequests.length;
      await advance(page, 25000);
      await page.waitForTimeout(20);
      assert(statusRequests.length > beforePoll, '25-second foreground fallback did not query status');
      assert(await page.locator('.manage-card[data-id="initial-0"] b').textContent() === generatedTitle,
        'older in-flight status markup replaced the newer generated title');

      async function rebuildTitle(title, revision) {
        await page.evaluate(({title, revision}) => {
          const current = document.querySelector('.manage-card[data-id="initial-0"]');
          const replacement = current.cloneNode(true);
          const link = replacement.querySelector('a');
          delete link.dataset.titleRevision;
          link.dataset.conversationRevision = String(revision);
          replacement.querySelector('b').textContent = title;
          const group = current.parentNode;
          group.replaceChildren(...Array.from(group.childNodes).map(node => node === current ? replacement : node));
        }, {title, revision});
        await advance(page, 1);
      }
      await rebuildTitle('旧列表响应中的首问', 8);
      assert(await page.locator('.manage-card[data-id="initial-0"] b').textContent() === generatedTitle,
        'full list rebuild discarded the newer websocket title');
      assert(await page.locator('.manage-card[data-id="initial-0"] a').getAttribute('data-title-revision') === '42',
        'full list rebuild discarded the title revision');
      await rebuildTitle('更晚的服务端标题', 43);
      await rebuildTitle('再次到达的旧列表', 8);
      assert(await page.locator('.manage-card[data-id="initial-0"] b').textContent() === '更晚的服务端标题',
        'rebuilt authoritative title was not retained across another older rebuild');

      // The document visibility path is independent of pagehide/BFCache.
      await page.evaluate(() => {
        window.__historyVisibility = 'hidden';
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => window.__historyVisibility});
        document.dispatchEvent(new Event('visibilitychange'));
      });
      const beforeHidden = statusRequests.length;
      await advance(page, 60000);
      assert(statusRequests.length === beforeHidden, 'hidden document kept polling');
      await page.evaluate(() => {
        window.__historyVisibility = 'visible';
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await advance(page, 1);
      assert(statusRequests.length > beforeHidden, 'visibility restore did not refresh');

      // Malformed responses must back off even when ordinary events keep arriving.
      malformedStatus = true;
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await advance(page, 1);
      await page.waitForTimeout(30);
      const beforeStorm = statusRequests.length;
      await page.evaluate(() => {
        for (let i = 0; i < 100; i++) window.__fakeSockets[0].emit({
          type: 'conversation.updated', data: {conversation_id: 'initial-0', state: 'idle'}
        });
      });
      await advance(page, 1000);
      assert(statusRequests.length === beforeStorm, 'ordinary events bypassed malformed-response backoff');
      malformedStatus = false;
      await advance(page, 1100);
      assert(statusRequests.length > beforeStorm, 'protocol failure did not retry');

      // More than one batch still preserves loaded order and node identity.
      const bulkStart = statusRequests.length;
      const originalOrder = await page.evaluate(() => [...document.querySelectorAll('.manage-card')].map(card => card.dataset.id));
      await page.evaluate(() => {
        const group = document.querySelector('.conversation-date-cards');
        const template = group.querySelector('.manage-card');
        window.__originalHistoryCard = template;
        for (let i = 0; i < 65; i++) {
          const copy = template.cloneNode(true);
          copy.dataset.id = 'bulk-' + i;
          copy.querySelector('input').value = copy.dataset.id;
          copy.querySelector('input').checked = false;
          group.appendChild(copy);
        }
      });
      await advance(page, 1100);
      await page.waitForTimeout(50);
      const bulkRequests = statusRequests.slice(bulkStart);
      assert(bulkRequests.some(ids => ids.length === 100) && bulkRequests.some(ids => ids.length === 5), '105 loaded cards were not queried in bounded batches');
      assert(bulkRequests.every(ids => ids.length <= 100), 'status batch exceeded endpoint limit');
      const afterBulk = await page.evaluate(() => ({
        ids: [...document.querySelectorAll('.manage-card')].map(card => card.dataset.id),
        sameNode: window.__originalHistoryCard === document.querySelector('.manage-card'),
      }));
      assert(afterBulk.sameNode && JSON.stringify(afterBulk.ids.filter(id => !id.startsWith('bulk-'))) === JSON.stringify(originalOrder), 'live update replaced or reordered loaded cards');

      statusErrors = 1;
      const beforeRetry = statusRequests.length;
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:connection-restored')));
      await advance(page, 1);
      await page.waitForTimeout(25);
      await advance(page, 2000);
      await page.waitForTimeout(25);
      assert(statusRequests.length >= beforeRetry + 2, 'failed live request did not back off and retry');
      const beforeOnline = statusRequests.length;
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await advance(page, 1);
      await page.waitForTimeout(20);
      assert(statusRequests.length > beforeOnline, 'online recovery did not refresh history status');

      // pagehide cancels an in-flight request and must not restart a timer;
      // pageshow is also the BFCache recovery path and resumes it.
      delayStatus = true;
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:connection-restored')));
      await advance(page, 1);
      await page.waitForTimeout(25);
      const hiddenCount = statusRequests.length;
      await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
      await advance(page, 30000);
      await page.waitForTimeout(25);
      assert(statusRequests.length === hiddenCount, 'pagehide restarted history polling after abort');
      delayStatus = false;
      await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
      await advance(page, 1);
      await page.waitForTimeout(25);
      assert(statusRequests.length > hiddenCount, 'pageshow did not resume history polling');

      delayStatus = true;
      const timeoutCount = statusRequests.length;
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:connection-restored')));
      await advance(page, 1);
      await page.waitForTimeout(20);
      await advance(page, 10000);
      await page.waitForTimeout(20);
      await advance(page, 2000);
      await page.waitForTimeout(20);
      assert(statusRequests.length >= timeoutCount + 2, 'visible timeout did not retry with backoff');
      delayStatus = false;
      await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));

      // Authentication failure disables all later ordinary recovery events.
      await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
      await advance(page, 1);
      await page.waitForTimeout(20);
      statusUnauthorized = true;
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:connection-restored')));
      await advance(page, 1);
      await page.waitForTimeout(20);
      const afterUnauthorized = statusRequests.length;
      await page.evaluate(() => { window.dispatchEvent(new Event('online')); window.dispatchEvent(new Event('pageshow')); });
      await advance(page, 30000);
      assert(statusRequests.length === afterUnauthorized, '401 did not stop automatic history polling');
      statusUnauthorized = false;
      assert(errors.length === 0, `browser errors ${width}/${theme}: ${errors.join('; ')}`);
      await context.close();
    }
    // Project lists use the same grouped cards, pagination and live updater,
    // but only their project-scoped endpoint may be queried.
    const projectContext = await browser.newContext({viewport: {width: 393, height: 852}});
    const projectPage = await projectContext.newPage();
    projectPage.setDefaultTimeout(5000);
    statusRequests = []; statusEndpoints = []; nextStatus = 'active';
    await projectPage.goto(base + '/project/fixture', {waitUntil: 'domcontentloaded'});
    await projectPage.waitForFunction(() => document.querySelector('.conversation-history .conversation-index-card.running'));
    assert(statusEndpoints.every(endpoint => endpoint === '/project/fixture/conversations/status'), 'project list queried global status endpoint');
    assert(await projectPage.evaluate(() => {
      const list = document.querySelector('.conversation-history');
      return list && list.dataset.statusEndpoint === '/project/fixture/conversations/status' &&
        !list.textContent.includes('项目对话 ·');
    }), 'project list did not use shared scoped history markup');
    await projectPage.locator('[data-load-more]').click();
    await projectPage.waitForFunction(() => document.querySelectorAll('.conversation-history .manage-card').length === 40);
    await projectPage.waitForTimeout(1200);
    assert(statusRequests.some(ids => ids.includes('pmore-0')), 'project pagination cards were not live-refreshed');
    assert(await projectPage.evaluate(() => [...document.querySelectorAll('.conversation-history .conversation-date-group > h2')]
      .filter(node => node.textContent === '今天').length === 1), 'project pagination duplicated date group');
    nextStatus = 'failed';
    await projectPage.evaluate(() => window.dispatchEvent(new CustomEvent('codex:conversation-updated', {
      detail: {conversation_id: 'initial-0', state: 'idle'}
    })));
    await projectPage.waitForFunction(() => document.querySelector('.manage-card[data-id="initial-0"]').classList.contains('failed'));
    assert(await projectPage.locator('.manage-card[data-id="initial-0"] .conversation-card-status').textContent() === '执行失败', 'project failure state label missing');
    nextStatus = 'stopped';
    await projectPage.evaluate(() => window.dispatchEvent(new CustomEvent('codex:conversation-terminal', {
      detail: {conversation_id: 'initial-0'}
    })));
    await projectPage.waitForFunction(() => {
      const card = document.querySelector('.manage-card[data-id="initial-0"]');
      return card.classList.contains('stopped') && !card.classList.contains('failed');
    });
    assert(await projectPage.locator('.manage-card[data-id="initial-0"] .conversation-card-status').textContent() === '已停止', 'project stop state label missing');
    await projectContext.close();

    // The history-only loader must not issue status requests on unrelated pages.
    const context = await browser.newContext(); const page = await context.newPage();
    statusRequests = [];
    await page.setContent('<nav class="nav"></nav><div class="settings-page"></div>');
    await page.addScriptTag({content: historyLive});
    await page.waitForTimeout(30);
    assert(statusRequests.length === 0, 'non-history page queried history status');
    await context.close();
    console.log('history live Playwright passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { server.close(); console.error(error.stack || error); process.exit(1); });
