const {chromium} = require('playwright');
const {spawn} = require('child_process');
const path = require('path');
const root = path.resolve(__dirname, '..');
function assert(value, message) { if (!value) throw new Error(message); }
const fixture = spawn((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [path.join(__dirname, 'serve_history_search_fixture.py')], {cwd: root});
let output = '';
fixture.stdout.on('data', data => { output += data.toString(); });
fixture.stderr.on('data', data => { output += data.toString(); });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function assertSecondarySearch(page) {
  const style = await page.locator('[data-search-form] button').evaluate(button => {
    const field = button.closest('.search-field');
    const rect = button.getBoundingClientRect(), parent = field.getBoundingClientRect();
    const icon = button.querySelector('.search-icon');
    const muted = document.createElement('span'); muted.style.color = 'var(--muted)'; field.append(muted);
    const gray = getComputedStyle(muted).color; muted.remove();
    return {label:button.getAttribute('aria-label'), text:button.textContent.trim(),
      icon:!!icon, color:getComputedStyle(icon).color, gray,
      background:getComputedStyle(button).backgroundColor,
      contained:rect.left >= parent.left && rect.right <= parent.right && rect.top >= parent.top && rect.bottom <= parent.bottom,
      width:rect.width, height:rect.height};
  });
  assert(style.label === '提交搜索' && !style.text && style.icon, '搜索缺少可访问的图标提交按钮');
  assert(style.background === 'rgba(0, 0, 0, 0)' && style.color === style.gray, '搜索仍采用主操作颜色');
  assert(style.contained && style.width >= 44 && style.height >= 44, '搜索图标不在框内或触控区域不足');
}

async function search(page, text) {
  if (!(await page.locator('[data-history-search-input]').isVisible())) {
    await page.locator('[data-search-toggle]').click();
  }
  await page.locator('[data-history-search-input]').fill(text);
  await page.locator('[data-search-form] button').click();
  await page.waitForFunction(() => document.querySelector('[data-history-search]').dataset.searchBusy === 'false');
}

async function setup(context, theme) {
  await context.addInitScript(value => {
    localStorage.setItem('codex-theme', value);
    window.WebSocket = class {
      static OPEN = 1;
      constructor() { this.readyState = 1; setTimeout(() => this.onopen && this.onopen(), 0); }
      send(data) { if (data === 'ping') setTimeout(() => this.onmessage && this.onmessage({data:'{"type":"pong"}'}), 0); }
      close() { this.readyState = 3; }
    };
  }, theme);
}

(async () => {
  let browser;
  try {
    for (let tries = 0; tries < 100 && !/FIXTURE_PORT=(\d+)/.test(output); tries++) {
      if (fixture.exitCode !== null) throw new Error(output);
      await sleep(100);
    }
    const match = output.match(/FIXTURE_PORT=(\d+)/);
    assert(match, '隔离服务未启动：' + output);
    const base = 'http://127.0.0.1:' + match[1];
    for (let tries = 0; tries < 50; tries++) {
      try { const response = await fetch(base + '/projects/history'); if (response.ok) break; } catch (_) {}
      await sleep(100);
    }
    const bfcacheChannel = process.env.CODEX_WEB_BFCACHE_BROWSER || undefined;
    browser = await chromium.launch({headless:true, channel:bfcacheChannel,
      ignoreDefaultArgs:['--disable-back-forward-cache']});
    for (const width of [320, 390, 412]) for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({viewport:{width, height:844}});
      await setup(context, theme);
      const page = await context.newPage();
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      const prefix = width === 390 ? '/' + 'a'.repeat(40) : '';
      const requests = []; page.on('request', request => { if (request.url().includes('/search?')) requests.push(request.url()); });
      await page.goto(base + prefix + '/projects/history');
      assert(await page.locator('[data-history-search-input]').isHidden(), '全局搜索没有默认收起');
      assert(await page.locator('.history-page-head h1').isVisible(), '普通模式缺少紧凑抬头');
      const header = await page.locator('.history-page-head').boundingBox();
      assert(header.height === 44, '历史抬头不再是一行44px工具栏');
      assert(await page.locator('[data-search-toggle]').evaluate(node => node.getBoundingClientRect().width >= 44 && node.getBoundingClientRect().height >= 44), '搜索图标触控区域不足');
      await search(page, '共同关键词');
      await assertSecondarySearch(page);
      assert(await page.locator('.history-page-head h1').isHidden(), '搜索模式没有替换抬头');
      assert((await page.locator('.history-page-head').boundingBox()).height === header.height, '展开搜索增加了顶部高度');
      assert(await page.locator('.history-search-card').count() === 20, '首屏搜索不是20条');
      assert(await page.locator('[data-history-browse]').isHidden(), '搜索未隐藏普通列表');
      await page.locator('[data-search-more]').click();
      await page.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 40);
      await page.locator('[data-search-more]').click();
      await page.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 47);
      assert(await page.locator('[data-search-more]').isHidden(), '最后一页仍有加载按钮');
      const links = await page.locator('.history-search-card').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')));
      assert(new Set(links).size === 47, '搜索分页重复');
      if (prefix) {
        assert(requests.every(url => new URL(url).pathname.startsWith(prefix + '/')), '搜索丢失入口前缀');
        assert(links.every(link => link.startsWith(prefix + '/conversation/')), '命中链接丢失入口前缀');
      }
      await search(page, '回答专属 000');
      assert(await page.locator('.history-search-card').count() === 1, '多词搜索结果错误');
      assert(await page.locator('.history-search-card mark').count() >= 2, '命中没有高亮');
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '搜索窄屏横向溢出');
      if (width === 390) await page.screenshot({path:'/tmp/codex-history-search-' + theme + '.png', fullPage:true});
      await page.locator('[data-search-clear]').click();
      assert(await page.locator('[data-search-panel]').isHidden(), '清除未恢复普通列表');
      assert(!new URL(page.url()).searchParams.has('q'), '清除保留了搜索URL');
      await page.goBack();
      await page.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 1);

      await page.goto(base + prefix + '/project/fixture');
      assert(await page.locator('[data-history-search-input]').isVisible(), '项目搜索不应跟随全局收起');
      await assertSecondarySearch(page);
      const start = page.locator('#project-send .send-button');
      const startStyle = await start.evaluate(button => ({html:button.outerHTML, color:getComputedStyle(button).backgroundColor}));
      assert(await start.innerText() === '开始', '开始按钮文案被改动');
      assert(await start.evaluate(button => getComputedStyle(button).backgroundColor !== 'rgba(0, 0, 0, 0)'), '开始按钮不再是主操作');
      const draft = page.locator('#project-send textarea');
      await draft.fill('搜索不能丢失的未发送草稿');
      await draft.evaluate(node => { node.dataset.fixtureIdentity = 'same-node'; });
      await search(page, 'outside');
      assert(await page.locator('.history-search-card').count() === 0, '项目搜索越过项目范围');
      await search(page, '提问专属 000');
      assert(JSON.stringify(await start.evaluate(button => ({html:button.outerHTML, color:getComputedStyle(button).backgroundColor}))) === JSON.stringify(startStyle), '搜索改变了开始按钮');
      assert(await draft.inputValue() === '搜索不能丢失的未发送草稿', '搜索丢失项目草稿');
      assert(await draft.getAttribute('data-fixture-identity') === 'same-node', '搜索重建了项目输入区');
      assert(await page.locator('.history-search-card').getAttribute('href') === prefix + '/conversation/search-000?command=command-search-000', '提问未定位消息');
      assert(errors.length === 0, '页面脚本异常：' + errors.join(';'));
      await context.close();
    }

    const context = await browser.newContext({viewport:{width:390,height:844}});
    await setup(context, 'dark');
    const page = await context.newPage();
    await page.goto(base + '/projects/history');
    const literalQuery = '\ufeff字面字符检索\ufeff';
    await search(page, literalQuery);
    assert(await page.locator('.history-search-card').count() === 1, '浏览器删除了字面的FEFF搜索字符');
    assert((await page.locator('.history-search-card').getAttribute('href')).endsWith('/search-feff'), '字面FEFF命中了不含字符的正文');
    assert(new URL(page.url()).searchParams.get('q') === literalQuery, '搜索URL没有保留字面FEFF');
    await search(page, '\u0085字面字符检索\u001f');
    assert(await page.locator('.history-search-card').count() === 2, 'Python空白搜索规则不一致');
    assert(new URL(page.url()).searchParams.get('q') === '字面字符检索', 'Python边缘空白没有归一');
    await page.goBack();
    await page.waitForFunction(() => document.querySelector('[data-history-search]').dataset.searchBusy === 'false');
    assert(await page.locator('.history-search-card').count() === 1, '返回搜索改变了字面FEFF结果');
    await page.reload();
    assert(await page.locator('.history-search-card').count() === 1, '重载搜索改变了字面FEFF结果');
    assert(await page.locator('[data-history-search-input]').inputValue() === literalQuery, '重载没有恢复原搜索词');
    const paddedQuery = ' '.repeat(120) + '共同关键词' + '\u0085'.repeat(120);
    await page.goto(base + '/projects/history?q=' + encodeURIComponent(paddedQuery));
    assert(await page.locator('.history-search-card').count() === 20, '合法的长边缘空白SSR查询没有显示结果');
    assert(await page.locator('[data-history-search-input]').inputValue() === '共同关键词', 'SSR输入框截断了未经归一的查询');
    assert(new URL(await page.locator('[data-search-more]').getAttribute('href'), base).searchParams.get('q') === '共同关键词', 'SSR分页链接没有使用真实搜索词');
    await page.locator('[data-search-more]').click();
    await page.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 40);
    await search(page, '字面字符检索');
    await page.goBack();
    await page.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 40);
    assert(await page.locator('[data-history-search-input]').inputValue() === '共同关键词', '返回恢复没有归一合法的长边缘空白查询');
    await page.reload();
    assert(await page.locator('.history-search-card').count() === 20, '重载合法的长边缘空白查询改变了结果');
    await page.route('**/history/search?*', route => route.abort());
    await search(page, '共同关键词');
    assert((await page.locator('[data-search-status]').textContent()).includes('搜索失败'), '网络失败没有提示');
    await page.unroute('**/history/search?*');
    await search(page, '共同关键词');
    await page.route('**/history/search?*', route => route.fulfill({status:503,contentType:'application/json',body:'{}'}));
    await page.locator('[data-search-more]').click();
    await page.waitForFunction(() => document.querySelector('[data-search-status]').textContent.includes('加载失败'));
    assert(await page.locator('.history-search-card').count() === 20, '加载失败清空了已有结果');
    await page.unroute('**/history/search?*');
    await page.locator('[data-search-more]').click();
    await page.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 40);
    await page.route('**/history/search?*', route => route.fulfill({status:403,contentType:'application/json',body:'{"reason_code":"authentication_required"}'}));
    await search(page, '回答专属');
    assert((await page.locator('[data-search-status]').textContent()).includes('登录已失效'), '鉴权失败没有明确提示');
    assert(await page.locator('[data-history-search-input]').inputValue() === '回答专属', '鉴权失败丢失搜索词');
    await page.unroute('**/history/search?*');
    await page.route('**/history/search?*', async route => {
      if (new URL(route.request().url()).searchParams.get('q') !== '慢请求') return route.continue();
      await sleep(350);
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({html:'<a class="history-search-card">过期结果</a>',next_cursor:null})}).catch(() => {});
    });
    await page.locator('[data-history-search-input]').fill('慢请求');
    await page.locator('[data-search-form] button').click();
    await search(page, '回答专属 000');
    await page.waitForTimeout(500);
    assert(!(await page.locator('[data-search-results]').textContent()).includes('过期结果'), '旧请求覆盖了新搜索');
    assert(await page.locator('.history-search-card').count() === 1, '快速换词结果错误');
    await context.close();

    // Actual conversation navigation with the production no-store contract.
    // BFCache availability depends on the browser; separately test its event
    // path without calling a synthetic event a real cached browser return.
    let bfcacheReturns = 0;
    const cacheReasons = [];
    for (const cold of [false, true]) {
      const navigationContext = await browser.newContext({viewport:{width:390,height:844}});
      await setup(navigationContext, 'dark');
      await navigationContext.addInitScript(forceCold => {
        if (forceCold) window.addEventListener('unload', () => {});
        window.addEventListener('pageshow', event => { window.fixtureBfcache = event.persisted; });
      }, cold);
      const navigationPage = await navigationContext.newPage();
      const cacheSession = await navigationContext.newCDPSession(navigationPage);
      await cacheSession.send('Page.enable');
      cacheSession.on('Page.backForwardCacheNotUsed', event => {
        if (!cold) cacheReasons.push(...event.notRestoredExplanations.map(item => item.reason));
      });
      const navigationErrors = [];
      navigationPage.on('pageerror', error => navigationErrors.push(error.message));
      const address = base + '/' + 'a'.repeat(40) + '/projects/history';
      await navigationPage.goto(address);
      await navigationPage.locator('[data-load-more]').click();
      await navigationPage.waitForFunction(() => document.querySelectorAll('.manage-card').length === 40);
      await navigationPage.evaluate(() => scrollTo(0, 600));
      await navigationPage.waitForTimeout(150);
      const browseY = await navigationPage.evaluate(() => scrollY);
      const browseIds = await navigationPage.locator('.manage-card input').evaluateAll(nodes => nodes.map(node => node.value));
      await navigationPage.locator('[data-search-toggle]').evaluate(node => node.click());
      assert(await navigationPage.locator('[data-history-search-input]').isVisible(), '打开搜索失败');
      assert(await navigationPage.locator('[data-history-search-input]').evaluate(node => node === document.activeElement), '搜索输入未获得焦点');
      await navigationPage.locator('[data-search-clear]').click();
      assert(Math.abs((await navigationPage.evaluate(() => scrollY)) - browseY) < 2, '取消搜索没有恢复进入前的浏览位置');
      assert(await navigationPage.locator('.manage-card').count() === 40, '取消搜索丢失普通分页');

      const ordinaryLink = await navigationPage.locator('.manage-card > a.card').nth(25).getAttribute('href');
      await navigationPage.goto(new URL(ordinaryLink, base).href);
      await navigationPage.goBack({waitUntil:'commit'});
      await navigationPage.waitForFunction(() => document.querySelectorAll('.manage-card').length === 40 && document.querySelector('[data-history-search]').dataset.navigationRestoring !== 'true');
      assert(JSON.stringify(await navigationPage.locator('.manage-card input').evaluateAll(nodes => nodes.map(node => node.value))) === JSON.stringify(browseIds), '普通对话返回丢失已加载卡片或顺序');
      assert(Math.abs((await navigationPage.evaluate(() => scrollY)) - browseY) < 2, '普通对话返回没有恢复滚动位置');
      assert(await navigationPage.locator('[data-history-search-input]').isHidden(), '普通返回意外展开搜索');
      const ordinaryCached = await navigationPage.evaluate(() => !!window.fixtureBfcache);
      if (ordinaryCached) bfcacheReturns += 1;
      if (cold) assert(!ordinaryCached, '冷返回场景未排除BFCache');

      await navigationPage.locator('[data-search-toggle]').evaluate(node => node.click());
      await search(navigationPage, '共同关键词');
      await navigationPage.locator('[data-search-more]').click();
      await navigationPage.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 40);
      await navigationPage.evaluate(() => scrollTo(0, 700));
      await navigationPage.waitForTimeout(150);
      const searchY = await navigationPage.evaluate(() => scrollY);
      const searchLinks = await navigationPage.locator('.history-search-card').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')));
      await navigationPage.goto(new URL(searchLinks[25], base).href);
      await navigationPage.goBack({waitUntil:'commit'});
      await navigationPage.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 40 && document.querySelector('[data-history-search]').dataset.navigationRestoring !== 'true');
      assert(await navigationPage.locator('[data-history-search-input]').inputValue() === '共同关键词', '搜索对话返回丢失关键词');
      const returnedLinks = await navigationPage.locator('.history-search-card').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')));
      assert(JSON.stringify(returnedLinks) === JSON.stringify(searchLinks), '搜索对话返回丢失分页或结果顺序 cold=' + cold + ' cached=' + await navigationPage.evaluate(() => !!window.fixtureBfcache) + ' mismatch=' + searchLinks.findIndex((link,index) => link !== returnedLinks[index]) + ' before=' + searchLinks.slice(22,28) + ' after=' + returnedLinks.slice(22,28));
      assert(Math.abs((await navigationPage.evaluate(() => scrollY)) - searchY) < 2, '搜索对话返回没有恢复滚动位置');
      const searchCached = await navigationPage.evaluate(() => !!window.fixtureBfcache);
      if (searchCached) bfcacheReturns += 1;
      if (cold) {
        assert(!searchCached, '搜索冷返回场景未排除BFCache');
        assert(await navigationPage.evaluate(() => performance.getEntriesByType('navigation')[0].type === 'back_forward'), '搜索未经过真实冷返回');
      }
      // Keep the existing live DOM intact on a persisted pageshow. This is an
      // event-path regression, not proof that this browser accepted BFCache.
      await navigationPage.evaluate(() => dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true})));
      assert(await navigationPage.locator('.history-search-card').count() === 40, 'BFCache事件重置搜索分页');
      assert(Math.abs((await navigationPage.evaluate(() => scrollY)) - searchY) < 2, 'BFCache事件改变滚动位置');
      assert(!JSON.stringify(await navigationPage.evaluate(() => history.state)).includes('snippet'), '历史状态保存了正文内容');
      await navigationPage.locator('[data-search-clear]').click();
      assert(Math.abs((await navigationPage.evaluate(() => scrollY)) - browseY) < 2, '搜索结果取消没有恢复原浏览位置');
      if (cold) for (const userMoves of [false, true]) {
        // Cancel while ordinary-list reconstruction is awaiting a slow page.
        // Its remaining pages must finish independently of search cancellation.
        await navigationPage.goto(address);
        await navigationPage.locator('[data-load-more]').click();
        await navigationPage.waitForFunction(() => document.querySelectorAll('.manage-card').length === 40);
        await navigationPage.locator('[data-load-more]').click();
        await navigationPage.waitForFunction(() => !document.querySelector('[data-load-more]'));
        const fullCount = await navigationPage.locator('.manage-card').count();
        await navigationPage.evaluate(() => scrollTo(0, 2600));
        await navigationPage.waitForTimeout(150);
        const fullY = await navigationPage.evaluate(() => scrollY);
        await navigationPage.locator('[data-search-toggle]').evaluate(node => node.click());
        await search(navigationPage, '共同关键词');
        let delayedPages = 0;
        await navigationPage.route('**/projects/history/page?*', async route => {
          delayedPages += 1;
          await sleep(600);
          await route.continue();
        });
        const resultLink = await navigationPage.locator('.history-search-card').first().getAttribute('href');
        await navigationPage.goto(new URL(resultLink, base).href);
        await navigationPage.goBack({waitUntil:'commit'});
        while (!delayedPages) await sleep(20);
        assert(await navigationPage.locator('.manage-card').count() === 20, '慢分页没有保留待恢复窗口');
        await navigationPage.locator('[data-search-clear]').click();
        let expectedY = fullY;
        if (userMoves) {
          await navigationPage.mouse.wheel(0, -100);
          await navigationPage.waitForTimeout(100);
          expectedY = await navigationPage.evaluate(() => scrollY);
        }
        await navigationPage.waitForFunction(count => document.querySelectorAll('.manage-card').length === count, fullCount);
        await navigationPage.waitForFunction(y => Math.abs(scrollY - y) < 2, expectedY);
        await navigationPage.waitForTimeout(100);
        assert(Math.abs((await navigationPage.evaluate(() => scrollY)) - expectedY) < 2, '用户主动滚动后仍被恢复抢回');
        assert(delayedPages === 2, '取消搜索截断了后续普通分页恢复');
        assert(await navigationPage.locator('[data-history-search-input]').isHidden(), '恢复普通分页意外重新打开搜索');
        assert(!new URL(navigationPage.url()).searchParams.has('q'), '取消后复活了旧搜索URL');
        await navigationPage.unroute('**/projects/history/page?*');
      }
      if (cold) for (const action of ['cancel', 'new-query']) {
        // Preserve the departing entry's full target, not its partial DOM, when
        // search reconstruction is interrupted and then revisited with Back.
        await navigationPage.goto(address);
        await search(navigationPage, '共同关键词');
        await navigationPage.locator('[data-search-more]').click();
        await navigationPage.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 40);
        await navigationPage.locator('[data-search-more]').click();
        await navigationPage.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 47);
        await navigationPage.evaluate(() => scrollTo(0, 1200));
        await navigationPage.waitForTimeout(150);
        const targetY = await navigationPage.evaluate(() => scrollY);
        let pendingPages = 0;
        await navigationPage.route('**/projects/history/search?*', async route => {
          if (new URL(route.request().url()).searchParams.has('cursor')) {
            pendingPages += 1;
            await sleep(600);
          }
          await route.continue().catch(() => {});
        });
        const link = await navigationPage.locator('.history-search-card').first().getAttribute('href');
        await navigationPage.goto(new URL(link, base).href);
        await navigationPage.goBack({waitUntil:'commit'});
        while (!pendingPages) await sleep(20);
        assert(await navigationPage.locator('.history-search-card').count() === 20, '搜索未处于部分恢复窗口');
        if (action === 'cancel') await navigationPage.locator('[data-search-clear]').click();
        else await search(navigationPage, '回答专属 000');
        await navigationPage.goBack();
        await navigationPage.waitForFunction(() => document.querySelectorAll('.history-search-card').length === 47 && document.querySelector('[data-history-search]').dataset.navigationRestoring !== 'true');
        assert(Math.abs((await navigationPage.evaluate(() => scrollY)) - targetY) < 2, action + '覆盖了旧搜索的完整滚动目标');
        assert(await navigationPage.locator('[data-history-search-input]').inputValue() === '共同关键词', action + '丢失旧搜索词');
        await navigationPage.unroute('**/projects/history/search?*');
      }
      if (cold) {
        await navigationPage.goto(address);
        await search(navigationPage, '共同关键词');
        await search(navigationPage, '回答专属 000');
        let pendingQuery = false;
        await navigationPage.route('**/projects/history/search?*', async route => {
          pendingQuery = true;
          await sleep(600);
          await route.continue().catch(() => {});
        });
        await navigationPage.goBack();
        while (!pendingQuery) await sleep(20);
        await navigationPage.locator('[data-history-search-input]').fill('尚未提交的新关键词');
        await navigationPage.waitForFunction(() => document.querySelector('[data-history-search]').dataset.navigationRestoring === 'false');
        assert(await navigationPage.locator('[data-history-search-input]').inputValue() === '尚未提交的新关键词', '迟到返回响应覆盖了搜索草稿');
        await navigationPage.unroute('**/projects/history/search?*');
      }
      assert(navigationErrors.length === 0, '往返脚本异常：' + navigationErrors.join(';'));
      await navigationContext.close();
    }
    if (!bfcacheReturns) console.log('实际BFCache不可用：' + [...new Set(cacheReasons)].join(',') + '；保留DOM路径按persisted事件单独核验。');

    for (const timing of ['normal', 'late', 'retry']) for (const target of ['?command=command-search-000', '?turn=turn-search-000&item=item-search-000']) {
      const context = await browser.newContext({viewport:{width:390,height:844}});
      await setup(context, 'dark');
      await context.addInitScript(() => {
        for (const key of ['command:command-search-000', 'final:turn-search-000:item-search-000']) {
          localStorage.setItem('codex-conversation-message:search-000:' + key, JSON.stringify({folded:true,autoFoldExpanded:false}));
        }
      });
      const page = await context.newPage();
      if (timing !== 'normal') {
        let requests = 0;
        await page.route('**/static/conversation-enhancements.js*', async route => {
          requests += 1;
          if (timing === 'retry' && requests === 1) return route.abort();
          await sleep(700);
          await route.continue();
        });
      }
      await page.goto(base + '/conversation/search-000' + target);
      const selector = target.startsWith('?command') ? '[data-command-id="command-search-000"]' : '[data-item-id="item-search-000"]';
      await page.waitForFunction(selector => {
        const node = document.querySelector(selector);
        return node && node.classList.contains('native-turn-highlight') && !node.classList.contains('is-folded') && !node.classList.contains('is-auto-folded');
      }, selector);
      await page.waitForFunction(() => window.codexConversationModules && window.codexConversationModules.markdown === 'ready');
      await page.waitForTimeout(850);
      const top = await page.locator(selector).evaluate(node => node.getBoundingClientRect().top);
      assert(top >= 40 && top <= 180, '长历史命中没有定位到消息起始位置：' + timing + ' ' + target + ' top=' + top);
      if (!target.startsWith('?command')) assert(await page.locator(selector + ' .native-code-card').count() > 0, '命中代码未增强：' + timing);
      assert(await page.locator(selector + ' .native-code-card.auto-folded').count() === 0, '命中里的代码仍隐藏');
      await page.waitForTimeout(800);
      assert(!(await page.locator(selector).evaluate(node => node.classList.contains('is-auto-folded'))), '增强重绘又折叠了命中');
      await context.close();
    }

    for (const scenario of [
      {id:'search-fallback-proof',item:'fallback',query:'缓存命中专用短语'},
      {id:'search-duplicate-final',item:'msg_final',query:'副本精确定位关键词'},
      {id:'search-distinct-source',item:'item-1',query:'独立消息定位关键词',copyVisible:true}
    ]) for (const timing of ['normal','late','retry']) {
      const context = await browser.newContext({viewport:{width:390,height:844}});
      await setup(context, 'dark');
      await context.addInitScript(value => localStorage.setItem(
        'codex-conversation-message:'+value.id+':final:turn-'+value.id+':'+value.item,
        JSON.stringify({folded:true,autoFoldExpanded:false})
      ),scenario);
      const page = await context.newPage();
      if (timing !== 'normal') {
        let requests=0;
        await page.route('**/static/conversation-enhancements.js*',async route => {
          requests+=1;
          if (timing==='retry' && requests===1) return route.abort();
          await sleep(700); await route.continue();
        });
      }
      const prefix='/'+'a'.repeat(40);
      await page.goto(base+prefix+'/projects/history');
      await search(page,scenario.query);
      assert(await page.locator('.history-search-card').count()===1,'保存的独立来源缓存未命中');
      assert((await page.locator('.history-search-card').getAttribute('href')).endsWith('&item='+scenario.item),'命中未指向可见消息');
      await page.locator('.history-search-card').click();
      const selector='[data-item-id="'+scenario.item+'"]';
      await page.waitForFunction(selector => {
        const target=document.querySelector(selector);
        return target && target.classList.contains('native-turn-highlight') && !target.classList.contains('is-auto-folded') && !target.classList.contains('is-folded');
      },selector);
      await page.waitForFunction(() => window.codexConversationModules && window.codexConversationModules.markdown==='ready');
      await page.waitForTimeout(850);
      assert((await page.locator(selector).textContent()).includes(scenario.query),'跳转未显示正确的命中正文');
      assert(await page.locator('[data-item-id="item-1"]').count()===(scenario.copyVisible?1:0),'浏览器与服务端的来源去重规则不一致');
      assert(!(await page.locator('#conversation-messages').textContent()).includes('被更正的不同正文'),'失效旧kind正文覆盖了缓存');
      const top=await page.locator(selector).evaluate(target => target.getBoundingClientRect().top);
      assert(top>=40 && top<=180,'历史命中没有精确定位：'+scenario.id+' '+timing+' '+top);
      assert(await page.locator(selector+' .native-code-card').count()>0,'历史缓存代码没有增强');
      assert(await page.locator(selector+' .native-code-card.auto-folded').count()===0,'历史缓存代码没有展开');
      await page.mouse.wheel(0,420);
      await page.waitForTimeout(200);
      const userScroll=await page.evaluate(() => scrollY);
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:conversation-messages-enhanced')));
      await page.waitForTimeout(120);
      assert(Math.abs((await page.evaluate(() => scrollY))-userScroll)<2,'用户主动滚动后搜索命中仍抢占滚动');
      await context.close();
    }

    const noJS = await browser.newContext({javaScriptEnabled:false,viewport:{width:320,height:844}});
    const fallback = await noJS.newPage();
    await fallback.goto(base + '/projects/history');
    assert(await fallback.locator('[data-history-search-input]').isHidden(), '无JS默认搜索应收起');
    await fallback.locator('[data-search-toggle]').click();
    await fallback.locator('[data-history-search-input]').fill('回答专属 000');
    await fallback.locator('[data-search-form] button').click();
    assert(await fallback.locator('.history-search-card').count() === 1, '无JS无法从收起入口提交搜索');
    await fallback.goto(base + '/projects/history?q=' + encodeURIComponent(literalQuery));
    assert(await fallback.locator('.history-search-card').count() === 1, '无JS字面FEFF搜索与浏览器不一致');
    await fallback.goto(base + '/projects/history?q=' + encodeURIComponent('\u0085字面字符检索\u001f'));
    assert(await fallback.locator('.history-search-card').count() === 2, '无JS Python空白规则与浏览器不一致');
    await fallback.goto(base + '/projects/history?q=' + encodeURIComponent(paddedQuery));
    assert(await fallback.locator('[data-history-search-input]').inputValue() === '共同关键词', '无JS SSR输入没有使用归一查询');
    const paddedFirst = await fallback.locator('.history-search-card').first().getAttribute('href');
    await fallback.locator('[data-search-more]').click();
    assert(await fallback.locator('.history-search-card').count() === 20, '无JS长边缘空白查询不能翻页');
    assert(await fallback.locator('.history-search-card').first().getAttribute('href') !== paddedFirst, '无JS长边缘空白查询重复第一页');
    assert(new URL(fallback.url()).searchParams.get('q') === '共同关键词', '无JS分页查询与结果游标不一致');
    await fallback.goto(base + '/projects/history?q=' + encodeURIComponent('回答专属 000'));
    assert(await fallback.locator('.history-search-card').count() === 1, '无JS搜索失效');
    await fallback.locator('[data-search-clear]').click();
    assert(await fallback.locator('[data-history-browse]').isVisible(), '无JS清除失效');
    await fallback.goto(base + '/project/fixture');
    await assertSecondarySearch(fallback);
    await fallback.locator('[data-history-search-input]').fill('提问专属 000');
    await fallback.locator('[data-search-form] button').click();
    assert(await fallback.locator('.history-search-card').count() === 1, '无JS项目图标搜索失效');
    await fallback.goto(base + '/projects/history?q=' + encodeURIComponent('共同关键词'));
    assert(await fallback.locator('.history-search-card').count() === 20, '无JS首屏搜索错误');
    const firstLink = await fallback.locator('.history-search-card').first().getAttribute('href');
    await fallback.locator('[data-search-more]').click();
    assert(await fallback.locator('.history-search-card').count() === 20, '无JS搜索无法翻页');
    assert(await fallback.locator('.history-search-card').first().getAttribute('href') !== firstLink, '无JS搜索重复第一页');
    await noJS.close();
    console.log('历史搜索浏览器回归通过：3种宽度×双主题、紧凑抬头/收起入口、全局/项目、分页、草稿、前缀、错误/重试、精确长消息定位、无JS、冷返回与BFCache事件、取消恢复位置；真实BFCache返回=' + bfcacheReturns + '。');
  } finally {
    if (browser) await browser.close();
    fixture.kill('SIGTERM');
    await sleep(250);
    if (fixture.exitCode === null) fixture.kill('SIGKILL');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
