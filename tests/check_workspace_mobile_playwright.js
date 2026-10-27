const { chromium } = require('playwright');
const childProcess = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [
  path.join(__dirname, 'render_workspace_mobile_ui_fixtures.py'),
], { cwd: root, encoding: 'utf8' }));
let quotaFails = false;
let paginationFails = 0;
let paginationRequests = 0;
const screenshotArg = process.argv.find(value => value.startsWith('--screenshots='));
const screenshotDir = screenshotArg && screenshotArg.slice('--screenshots='.length);
if (screenshotDir) fs.mkdirSync(screenshotDir, {recursive: true});
let checkedPages = 0;
const server = http.createServer((request, response) => {
  const address = new URL(request.url, 'http://fixture.test');
  const pathname = address.pathname;
  const publicPrefix = pathname.match(/^\/[0-9a-f]{40}(?=\/|$)/i);
  const relativePath = publicPrefix ? pathname.slice(publicPrefix[0].length) || '/' : pathname;
  const pages = {
    '/home': fixtures.home,
    '/settings/tts': fixtures.tts,
    '/settings/completion-sound': fixtures.sound,
    '/project/fixture': fixtures.project,
    '/history': address.searchParams.has('empty') ? fixtures.history_empty : fixtures.history,
    '/settings': address.searchParams.has('user') ? fixtures.settings_user : fixtures.settings_admin,
    '/notifications': address.searchParams.has('user') ? fixtures.notifications_user : fixtures.notifications_admin,
  };
  if (pages[relativePath]) { response.setHeader('Content-Type', 'text/html; charset=utf-8'); return response.end(pages[relativePath]); }
  if (relativePath === '/projects/history/page' || relativePath === '/project/fixture/conversations/page') {
    const isProject = relativePath.startsWith('/project/');
    paginationRequests += 1;
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    if (paginationFails > 0) { paginationFails -= 1; response.statusCode = 503; return response.end(JSON.stringify({error: 'fixture'})); }
    if (address.searchParams.get('cursor') === 'fixture-next-1') return setTimeout(() => response.end(JSON.stringify({html: isProject ? fixtures.project_more_1 : fixtures.history_more_1, next_cursor: 'fixture-next-2'})), 80);
    if (address.searchParams.get('cursor') === 'fixture-next-2') return response.end(JSON.stringify({html: isProject ? fixtures.project_more_2 : fixtures.history_more_2, next_cursor: null}));
    response.statusCode = 400; return response.end(JSON.stringify({error: 'bad cursor'}));
  }
  if (pathname === '/settings/api/config') return response.end(JSON.stringify({account:{revision:0,defaults:{},personal:'',previous_personal:null,presets:[]},project:null,effective:{},sources:{},system:{model:'gpt-6-sol',effort:'high',permission:'danger-full-access',service_tier:null,followup:'steer',completion_notification:true,skills:[]},models:[{model:'gpt-6-sol',name:'GPT-6 Sol',efforts:['high'],default_effort:'high',tiers:[],available:true}],skills:[],projects:[],catalog_error:false}));
  if (pathname === '/settings/codex-quota') {
    if (quotaFails) { response.statusCode = 503; return response.end(JSON.stringify({error: 'fixture'})); }
    return response.end(JSON.stringify({remaining_percent: 72, resets_at: Math.floor(Date.now() / 1000) + 4 * 86400 + 7 * 3600, plan_type: 'pro'}));
  }
  if (pathname.startsWith('/static/')) {
    const filename = path.resolve(root, pathname.slice(1));
    if (!filename.startsWith(path.join(root, 'static')) || !fs.existsSync(filename)) { response.statusCode = 404; return response.end(); }
    response.setHeader('Content-Type', filename.endsWith('.css') ? 'text/css; charset=utf-8' : filename.endsWith('.js') ? 'application/javascript; charset=utf-8' : 'application/octet-stream');
    return fs.createReadStream(filename).pipe(response);
  }
  if (pathname === '/sw.js') { response.setHeader('Content-Type', 'application/javascript; charset=utf-8'); return response.end(''); }
  if (pathname.startsWith('/settings/web-push/')) return response.end(JSON.stringify({public_key: 'BA' + 'A'.repeat(85)}));
  response.statusCode = 404; response.end();
});
function assert(value, message) { if (!value) throw new Error(message); }
async function pageFor(browser, route, theme) {
  const context = await browser.newContext({ viewport: { width: 393, height: 852 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  await page.addInitScript(() => {
    const fetch = window.fetch;
    window.__quotaRequests = [];
    window.fetch = function (url, options) {
      const address = url instanceof Request ? url.url : String(url);
      if (address.includes('/settings/codex-quota')) {
        const request = new Request(url, options);
        window.__quotaRequests.push({method: request.method, credentials: request.credentials, cache: request.cache});
      }
      return fetch.apply(this, arguments);
    };
  });
  if (theme) await page.addInitScript(value => { if (!localStorage.getItem('codex-theme')) localStorage.setItem('codex-theme', value); }, theme);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/' + route, { waitUntil: 'domcontentloaded' });
  await page.locator('main').waitFor();
  return {context, page, errors};
}
let base;
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({ headless: true });
  try {
    const visualMatrix = [360, 393].flatMap(width => ['dark', 'light'].flatMap(theme =>
      ['home', 'history', 'history?empty=1', 'settings', 'notifications', 'settings/tts', 'settings/completion-sound'].map(route => ({route, width, theme}))));
    visualMatrix.push({route: 'settings', width: 320, theme: 'dark'}, {route: 'settings', width: 320, theme: 'light'});
    for (const item of visualMatrix) {
      const {context, page, errors} = await pageFor(browser, item.route, item.theme);
      await page.setViewportSize({width: item.width, height: 852});
      assert(await page.evaluate(() => document.documentElement.dataset.theme) === item.theme, 'theme did not persist');
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), item.route + ' horizontal overflow');
      const expected = item.route === 'home' ? '首页' : item.route.startsWith('history') ? '历史' : '设置';
      assert(await page.locator('.nav a[aria-current="page"]').textContent() === expected, item.route + ' nav active');
      const geometry = await page.evaluate(() => {
        const nav = document.querySelector('.nav').getBoundingClientRect();
        const controls = [...document.querySelectorAll('.sc-row select, #codex-quota-refresh, .notifications-back, #web-push-toggle, .notification-status-card button, .settings-logout button')].filter(el => el.getClientRects().length && !el.closest('[hidden]'));
        return {navTop: nav.top, navBottom: nav.bottom, navHeight: nav.height,
          shortControls: controls.filter(el => el.getBoundingClientRect().height < 44).map(el => el.id || el.className)};
      });
      assert(geometry.navTop >= 0 && geometry.navBottom <= 853 && geometry.navHeight >= 44, 'bottom nav geometry: ' + JSON.stringify(geometry));
      assert(geometry.shortControls.length === 0, 'small touch targets: ' + geometry.shortControls.join(','));
      await page.waitForTimeout(300);
      if (item.route === 'settings') {
        await page.waitForFunction(() => document.getElementById('codex-quota-status').textContent.includes('还剩 4d 7h'));
        const quotaGeometry = await page.evaluate(() => {
          const status = document.getElementById('codex-quota-status');
          const refresh = document.getElementById('codex-quota-refresh').getBoundingClientRect();
          return {statusFits: status.scrollWidth <= status.clientWidth, whiteSpace: getComputedStyle(status).whiteSpace,
            refreshWidth: refresh.width, refreshHeight: refresh.height};
        });
        assert(quotaGeometry.statusFits, item.width + 'px quota countdown was truncated');
        assert(quotaGeometry.whiteSpace === 'nowrap', item.width + 'px quota countdown wrapped');
        assert(quotaGeometry.refreshWidth === 44 && quotaGeometry.refreshHeight === 44,
          item.width + 'px quota refresh target is not 44px square: ' + JSON.stringify(quotaGeometry));
      }
      if (screenshotDir) await page.screenshot({path: path.join(screenshotDir, item.route.replace(/[?\/]/g, '-') + '-' + item.theme + '-' + item.width + '.png'), fullPage: true});
      if (item.route === 'home') {
        assert(await page.locator('.home-actions small,.home-projects small,.home-hero p').count() === 0, 'redundant home copy returned');
        assert(await page.locator('.home-action-card').count() === 2, 'home must retain only project and native conversation actions');
        assert(await page.locator('.home-project-card').first().evaluate(el => el.getBoundingClientRect().height) < 84, 'home project card kept old empty space');
        assert(await page.locator('a[href="/settings/dot"]').count() === 0, 'hidden dot entry returned');
      }
      if (item.route.startsWith('history')) {
        assert(await page.locator('.dot-history-entry,[data-dot-history-card],a[href="/settings/dot"]').count() === 0, 'hidden dot history entry returned');
        assert(await page.locator('script[src*="/static/dot-history.js"]').count() === 0, 'hidden dot entry must not poll');
      }
      if (item.route === 'settings/tts' || item.route === 'settings/completion-sound') {
        const help = page.locator(item.route === 'settings/tts' ? 'details.tts-system-path' : 'details.completion-sound-rules');
        assert(!(await help.evaluate(el => el.open)), 'optional help expanded by default');
        assert(!(await help.locator('p').first().isVisible()), 'collapsed help still visible');
        assert((await help.locator('summary').boundingBox()).height >= 44, 'help summary too small');
        await help.locator('summary').click();
        assert(await help.locator('p').first().isVisible(), 'optional help cannot open');
        if (item.route === 'settings/tts') {
          assert(await page.locator('#tts-summary-title').innerText() !== '正在检查朗读能力…', 'TTS initialization stopped');
          await page.locator('#tts-save').click();
          assert((await page.locator('#tts-status').innerText()).includes('保存'), 'TTS save stopped');
        } else {
          await page.locator('#completion-sound-save').click();
          assert((await page.locator('#completion-sound-saved').innerText()).includes('保存'), 'sound save stopped');
        }
      }
      if (item.route === 'history') {
        assert(await page.locator('.conversation-index-card').first().evaluate(node => node.getBoundingClientRect().height) === 68, 'history card height is not the confirmed 68px');
        assert(await page.locator('.conversation-card-copy b').first().evaluate(node => getComputedStyle(node).fontWeight) === '600', 'history card title weight is not the approved medium-bold 600');
        await page.locator('.manage-card').first().dispatchEvent('pointerdown', {clientX: 20, clientY: 20});
        await page.waitForTimeout(550);
        assert(await page.locator('body.managing').count(), 'long press management regressed');
        const [checkBox, stateChip] = await Promise.all([page.locator('.manage-check').first().boundingBox(), page.locator('.conversation-card-status').first().boundingBox()]);
        // The state now sits on the metadata row below the title; check both axes.
        assert(stateChip.x + stateChip.width <= checkBox.x ||
          checkBox.x + checkBox.width <= stateChip.x ||
          stateChip.y + stateChip.height <= checkBox.y ||
          checkBox.y + checkBox.height <= stateChip.y, 'management checkbox overlaps state');
        if (screenshotDir) await page.screenshot({path: path.join(screenshotDir, 'history-managing-' + item.width + '.png'), fullPage: true});
        await page.locator('[data-load-more]').click();
        await page.waitForFunction(() => document.querySelectorAll('.manage-card').length === 40);
        assert(await page.locator('.conversation-date-group').count() === 1, 'same-date page append duplicated its heading');
        await page.locator('[data-load-more]').click();
        await page.waitForFunction(() => document.querySelectorAll('.manage-card').length === 45);
        assert(await page.locator('.conversation-date-group').count() === 3, 'later date group was not retained exactly once');
        assert(await page.locator('[data-load-more]').count() === 0, 'load more remained after final page');
        await page.locator('.manage-check').last().click();
        assert(await page.locator('[data-count]').textContent() === '2 项', 'appended checkbox count=' + await page.locator('[data-count]').textContent() + ', checked=' + await page.locator('.manage-check:checked').count());
        assert(await page.locator('.manage-check').last().isChecked(), 'checkbox state disagrees with count');
        await page.locator('[data-all]').click();
        assert(await page.locator('.manage-check:checked').count() === 45, 'select all excludes appended cards');
        await page.locator('[data-cancel]').click();
        assert(await page.locator('.manage-check:checked').count() === 0, 'cancel retained selections');
        await page.locator('.manage-card').last().dispatchEvent('pointerdown', {clientX: 20, clientY: 20});
        await page.waitForTimeout(550);
        assert(await page.locator('.manage-check').last().isChecked(), 'appended card long press failed');
      }
      assert(errors.length === 0, item.route + ' page error: ' + errors.join('; '));
      checkedPages += 1;
      await context.close();
    }
    quotaFails = false;
    const {context: settingsContext, page: settings, errors: settingsErrors} = await pageFor(browser, 'settings', 'dark');
    assert(await settings.locator('.sc-home-row').count() === 7, 'settings must have seven compact groups');
    await settings.click('[data-go="appearance"]');
    await settings.selectOption('#theme-select', 'light');
    await settings.reload({waitUntil: 'domcontentloaded'});
    quotaFails = false;
    assert(await settings.evaluate(() => document.documentElement.dataset.theme) === 'light', 'select theme did not persist after reload');
    assert(settingsErrors.length === 0, 'settings page error: ' + settingsErrors.join('; '));
    assert(await settings.locator('.nav a[aria-current="page"]').textContent() === '设置', 'settings nav active');
    await settings.waitForFunction(() => document.getElementById('codex-weekly-value').textContent.includes('72'));
    await settings.click('#sc-back');
    quotaFails = true;
    await settings.locator('#codex-quota-refresh').click();
    await settings.waitForFunction(() => document.getElementById('codex-quota-status').textContent.includes('刷新失败'));
    quotaFails = false;
    await settings.locator('#codex-quota-refresh').click();
    await settings.waitForFunction(() => document.getElementById('codex-quota-status').textContent.includes('还剩 4d 7h'));
    const quotaRequests = await settings.evaluate(() => window.__quotaRequests);
    assert(quotaRequests.length >= 3 && quotaRequests.every(options => options.method === 'GET' && options.credentials === 'same-origin' && options.cache === 'no-store'), 'quota requests lost private same-origin/no-store contract');
    assert(!(await settings.evaluate(() => document.documentElement.scrollWidth > innerWidth)), 'settings horizontal overflow');
    assert(settingsErrors.length === 0, 'settings actions error: ' + settingsErrors.join('; '));
    await settingsContext.close();
    const {context: notificationUserContext, page: notificationUser} = await pageFor(browser, 'notifications?user=1');
    assert(await notificationUser.locator('#web-push-toggle').count() === 0, 'user received admin notification toggle');
    await notificationUserContext.close();
    for (const scenario of ['unsupported', 'denied', 'subscription', 'error']) {
      const context = await browser.newContext({ viewport: {width: 393, height: 852} });
      const page = await context.newPage();
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(kind => {
        if (kind === 'unsupported') { delete window.Notification; delete window.PushManager; delete navigator.serviceWorker; return; }
        Object.defineProperty(window, 'Notification', {value: {permission: kind === 'denied' ? 'denied' : 'granted', requestPermission: async () => kind === 'error' ? 'granted' : 'denied'}});
        Object.defineProperty(navigator, 'serviceWorker', {value: {register: async () => { throw new Error('fixture'); }, getRegistration: async () => kind === 'subscription' ? {pushManager: {getSubscription: async () => ({})}} : null}});
        Object.defineProperty(window, 'PushManager', {value: function () {}});
      }, scenario);
      await page.goto(base + '/notifications', {waitUntil: 'networkidle'});
      const text = await page.locator('#web-push-status').textContent();
      if (scenario === 'unsupported') assert(text.includes('不支持'), 'unsupported status missing');
      if (scenario === 'denied') assert(text.includes('阻止'), 'denied status missing');
      if (scenario === 'subscription') assert(text.includes('无法在此页确认送达'), 'subscription claim is too strong');
      if (scenario === 'error') { await page.locator('#web-push-toggle').click(); await page.waitForFunction(() => document.getElementById('web-push-status').textContent.includes('通知设置失败')); }
      assert(errors.length === 0, scenario + ' notification page error: ' + errors.join('; '));
      checkedPages += 1;
      await context.close();
    }

    const {context: narrowContext, page: narrow, errors: narrowErrors} = await pageFor(browser, 'history', 'dark');
    await narrow.setViewportSize({width: 320, height: 640});
    await narrow.evaluate(() => {
      document.documentElement.style.fontSize = '20px';
      const list = document.querySelector('.conversation-date-cards');
      const source = list.querySelector('.manage-card');
      for (let i = 0; i < 12; i += 1) list.appendChild(source.cloneNode(true));
    });
    const lastCard = narrow.locator('.manage-card').last();
    await lastCard.scrollIntoViewIfNeeded();
    const [lastBox, navBox, titleBox, statusBox] = await Promise.all([
      lastCard.boundingBox(), narrow.locator('.nav').boundingBox(),
      narrow.locator('.conversation-card-copy b').first().boundingBox(), narrow.locator('.conversation-card-status').first().boundingBox(),
    ]);
    assert(lastBox.y + lastBox.height <= navBox.y, 'long list cannot scroll clear of bottom nav');
    assert(titleBox.y + titleBox.height <= statusBox.y + 1, '320px status occupies title row');
    await narrow.locator('.manage-card').first().dispatchEvent('pointerdown', {clientX: 20, clientY: 20});
    await narrow.waitForTimeout(550);
    assert(await narrow.locator('body.managing').count(), 'narrow long press management missing');
    await narrow.locator('[data-cancel]').click();
    assert(!(await narrow.locator('body.managing').count()), 'management cancel regressed');
    assert(narrowErrors.length === 0, 'narrow history page error: ' + narrowErrors.join('; '));
    checkedPages += 1;
    await narrowContext.close();

    // Only the project header whitespace may shrink; its controls and all
    // downstream components retain their original dimensions and styling.
    const baselineCss = process.env.CODEX_WEB_HEADER_BASELINE_CSS &&
      fs.readFileSync(process.env.CODEX_WEB_HEADER_BASELINE_CSS, 'utf8');
    for (const width of [320, 360, 390, 412, 640, 768]) for (const theme of ['dark', 'light']) {
      const {context, page, errors} = await pageFor(browser, 'project/fixture', theme);
      await page.setViewportSize({width, height:852});
      await page.locator('.setting-segment').first().waitFor();
      const geometry = async () => page.evaluate(() => {
        const main = document.querySelector('main');
        const title = main.querySelector(':scope > h1');
        const links = document.querySelector('.project-quick-links');
        const composer = document.querySelector('#new-conversation');
        const selectors = ['.project-quick-links > a', '.project-quick-menu > summary',
          '#new-conversation', '#project-send textarea', '.setting-segment', '.compact-control',
          '.composer-actions > button', '.search-field', '.conversation-index-card', '.nav'];
        return {paddingTop:parseFloat(getComputedStyle(main).paddingTop),
          titleGap:links.getBoundingClientRect().top - title.getBoundingClientRect().bottom,
          composerTop:composer.getBoundingClientRect().top + window.scrollY,
          components:selectors.flatMap(selector => [...document.querySelectorAll(selector)]
            .filter(node => node.getClientRects().length).map(node => {
            const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
            const headerControl = node.closest('.project-quick-links');
            return {selector, x:rect.x, width:rect.width, height:rect.height,
              top:headerControl ? rect.top - links.getBoundingClientRect().top :
                selector === '.nav' ? rect.top : rect.top - composer.getBoundingClientRect().top,
              font:style.font, padding:style.padding, margin:style.margin,
              color:style.color, background:style.backgroundColor, border:style.border,
              radius:style.borderRadius};
          }))};
      });
      const compact = await geometry();
      if (width <= 640) {
        assert(compact.paddingTop === 8, 'project top whitespace is not compact: ' + width);
        assert(compact.titleGap === 0, 'project title kept excess shortcut gap: ' + width);
      } else {
        assert(compact.paddingTop === 24 && compact.titleGap > 0, 'desktop project spacing changed');
      }
      if (baselineCss) {
        await page.route('**/static/css/app.css*', route => route.fulfill({body:baselineCss, contentType:'text/css'}));
        await page.reload({waitUntil:'networkidle'});
        await page.locator('.setting-segment').first().waitFor();
        const baseline = await geometry();
        const differences = compact.components.flatMap((component, index) =>
          Object.keys(component).filter(key => component[key] !== baseline.components[index][key])
            .map(key => ({selector:component.selector, key, compact:component[key], baseline:baseline.components[index][key]})));
        assert(differences.length === 0,
          'project header change resized or restyled controls/downstream content: ' + width + '/' + theme + ' ' + JSON.stringify(differences));
        assert(width <= 640 ? baseline.composerTop - compact.composerTop > 25 :
          baseline.composerTop === compact.composerTop, 'unexpected project content offset: ' + width);
        await page.unroute('**/static/css/app.css*');
        await page.reload({waitUntil:'networkidle'});
      }
      await page.locator('main > h1').evaluate(node => { node.textContent = 'Codex Web 工作台维护'; });
      assert(await page.locator('.project-quick-links').evaluate(node =>
        node.getBoundingClientRect().top >= document.querySelector('main > h1').getBoundingClientRect().bottom),
        'wrapped project title overlaps shortcuts');
      await page.locator('.project-quick-menu > summary').click();
      assert(await page.locator('.project-quick-menu a').first().isVisible(), 'project menu lost its interaction');
      await page.locator('.project-quick-menu > summary').click();
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), 'compact project header overflow');
      assert(errors.length === 0, 'compact project header page errors: ' + errors.join('; '));
      if (screenshotDir && width === 390) await page.screenshot({path:path.join(screenshotDir, 'project-header-' + theme + '.png')});
      await context.close(); checkedPages += 1;
    }

    const {context: projectContext, page: projectPage, errors: projectErrors} = await pageFor(browser, 'project/fixture', 'light');
    assert(await projectPage.locator('.conversation-index-card').first().evaluate(node => node.getBoundingClientRect().height) === 68, 'shared project cards are not 68px');
    assert(await projectPage.locator('.manage-card').count() === 20, 'project initial page is not 20');
    await projectPage.locator('[data-load-more]').click();
    await projectPage.waitForFunction(() => document.querySelectorAll('.manage-card').length === 40);
    await projectPage.locator('[data-load-more]').click();
    await projectPage.waitForFunction(() => document.querySelectorAll('.manage-card').length === 45);
    assert(await projectPage.locator('[data-load-more]').count() === 0, 'project final page retained button');
    assert(await projectPage.locator('.conversation-date-group').count() === 3, 'project date groups were not merged correctly');
    assert(await projectPage.evaluate(() => [...document.querySelectorAll('.conversation-date-group > h2')]
      .map(node => node.textContent).join('|') === '今天|昨天|更早'), 'project date group order changed');
    assert(!(await projectPage.evaluate(() => document.documentElement.scrollWidth > innerWidth)), 'project pagination overflow');
    assert(projectErrors.length === 0, 'project pagination error: ' + projectErrors.join('; '));
    await projectContext.close(); checkedPages += 1;

    const prefix = 'a'.repeat(40);
    const {context: prefixContext, page: prefixPage, errors: prefixErrors} = await pageFor(browser, prefix + '/history', 'dark');
    paginationFails = 1;
    await prefixPage.locator('[data-load-more]').click();
    await prefixPage.waitForFunction(() => document.querySelector('[data-pagination-status]').textContent.includes('加载失败'));
    const requestsBeforeRetry = paginationRequests;
    await prefixPage.locator('[data-load-more]').evaluate(button => {button.click(); button.click();});
    await prefixPage.waitForFunction(() => document.querySelectorAll('.manage-card').length === 40);
    assert(paginationRequests === requestsBeforeRetry + 1, 'double click issued duplicate pagination fetches');
    assert((await prefixPage.locator('.manage-card').last().locator('a').getAttribute('href')).startsWith('/' + prefix + '/conversation/'), 'prefixed pagination card link lost public prefix');
    assert(prefixErrors.length === 0, 'prefixed pagination page error: ' + prefixErrors.join('; '));
    checkedPages += 1;
    await prefixContext.close();
    console.log('workspace mobile Playwright passed: ' + checkedPages + ' page/state checks' + (screenshotDir ? '; screenshots: ' + screenshotDir : ''));
  } finally { await browser.close(); server.close(); }
})().catch(error => { server.close(); console.error(error.stack || error); process.exit(1); });
