/*
 * Browser-only regression fixture.  It deliberately exercises the rendered
 * project composer plus its real static assets; its upload/start endpoints are
 * tiny local HTTP doubles, not the production upload backend.
 */
const assert = require('assert');
const childProcess = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(childProcess.execFileSync(
  (process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')),
  [path.join(__dirname, 'render_conversation_ui_fixtures.py')],
  { cwd: root, encoding: 'utf8' },
));
const assets = {
  '/static/light-dismiss.js': fs.readFileSync(path.join(root, 'static/light-dismiss.js')),
  '/static/css/app.css': fs.readFileSync(path.join(root, 'static/css/app.css')),
  '/static/draft.js': fs.readFileSync(path.join(root, 'static/draft.js')),
  '/static/setting-segments.js': fs.readFileSync(path.join(root, 'static/setting-segments.js')),
  '/static/project-conversation.js': fs.readFileSync(path.join(root, 'static/project-conversation.js')),
  '/static/message-attachments.js': fs.readFileSync(path.join(root, 'static/message-attachments.js')),
};
const prefix = 'a'.repeat(40);
const projectPath = id => `/${prefix}/project/${id}`;
const draftKey = id => `codex-project-conversation-draft:${id}`;
const settingsKey = id => `codex-project-settings:/${prefix}:${draftKey(id)}`;

function projectHtml(id, removeModel) {
  let composer = fixtures.project.replaceAll('project-one', id);
  if (removeModel) composer = composer.replace(
    /\s*<option value="gpt-6-sol"[\s\S]*?<\/option>/,
    '',
  );
  return `<!doctype html><html data-theme="dark"><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/app.css">
<script src="/static/light-dismiss.js" defer></script>
<script src="/static/draft.js" defer></script>
<script src="/static/setting-segments.js" defer></script>
</head><body><main>${composer}</main></body></html>`;
}

function geometryHtml() {
  return `<!doctype html><html data-theme="dark"><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/app.css"></head><body><main>
<section class="native-conversation"><div class="native-messages">
  <article class="native-message agent"><div class="native-bubble native-progress">过程消息</div></article>
  <article class="native-message agent"><div class="native-bubble native-progress">第二条过程消息</div></article>
  <article class="native-message agent"><div class="native-bubble native-final">最终答复</div></article>
</div></section></main></body></html>`;
}

function reply(response, status, headers, body = '') {
  response.writeHead(status, headers);
  response.end(body);
}

function fixtureServer() {
  return http.createServer((request, response) => {
    const url = new URL(request.url, 'http://fixture.invalid');
    if (assets[url.pathname]) {
      return reply(response, 200, { 'content-type': url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript' }, assets[url.pathname]);
    }
    if (url.pathname === '/geometry') {
      return reply(response, 200, { 'content-type': 'text/html; charset=utf-8' }, geometryHtml());
    }
    const match = url.pathname.match(new RegExp(`^/${prefix}/project/([^/]+)$`));
    if (match) {
      return reply(response, 200, { 'content-type': 'text/html; charset=utf-8' },
        projectHtml(match[1], url.searchParams.get('models') === 'removed'));
    }
    if (request.method === 'POST' && url.pathname === `/${prefix}/files/upload`) {
      if (url.searchParams.get('conflict') === '1') {
        return reply(response, 200, { 'content-type': 'text/html; charset=utf-8' },
          `<!doctype html><title>上传确认（测试夹具）</title><p>上传已接收，等待确认。</p>
           <form id="upload-confirm" action="/${prefix}/files/confirm" method="post"><button>确认上传</button></form>`);
      }
      return reply(response, 303, { location: `${projectPath('project-one')}?uploaded_batch=${'a'.repeat(24)}` });
    }
    if (request.method === 'POST' && url.pathname === `/${prefix}/files/confirm`) {
      return reply(response, 303, { location: projectPath('project-one') });
    }
    if (request.method === 'POST' && url.pathname === `/${prefix}/conversation/new`) {
      return reply(response, 303, { location: `${projectPath('project-one')}?started=1` });
    }
    return reply(response, 404, { 'content-type': 'text/plain' }, 'fixture route not found');
  });
}

async function select(page, selector, value) {
  await page.locator(selector).evaluate((element, next) => {
    element.value = next;
    element.dispatchEvent(new Event('change', { bubbles: true }));
  }, value);
}

async function savedSettings(page, id) {
  return page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), settingsKey(id));
}

(async () => {
  const server = fixtureServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const origin = `http://127.0.0.1:${port}`;
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox'],
  });
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    await page.goto(origin + projectPath('project-one'), { waitUntil: 'networkidle' });
    await select(page, '#project-conversation-model', 'gpt-5.6-sol');
    await select(page, '#project-conversation-effort', 'ultra');
    await select(page, '#project-conversation-tier', 'priority');
    await select(page, '#project-conversation-permission', 'read-only');
    await page.fill('#project-send textarea', '上传前仍要保留的文字草稿');

    // The upload form is separate from the start form.  A new file returns
    // directly with 303; real conflicts retain the server-side confirmation path.
    await page.locator('.attach-button').click();
    await page.locator('#project-upload form input[type=file]').setInputFiles({
      name: 'fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('fixture'),
    });
    await Promise.all([
      page.waitForURL(origin + projectPath('project-one') + '?uploaded_batch=' + 'a'.repeat(24)),
      page.locator('#project-upload form button:not(.secondary)').click(),
    ]);
    const returned = await page.evaluate(() => ({
      model: document.querySelector('#project-conversation-model').value,
      effort: document.querySelector('#project-conversation-effort').value,
      tier: document.querySelector('#project-conversation-tier').value,
      permission: document.querySelector('#project-conversation-permission').value,
      draft: document.querySelector('#project-send textarea').value,
      modelSegment: document.querySelector('.setting-segments-model .selected')?.dataset.value,
      effortSegment: document.querySelector('.setting-segments-effort .selected')?.dataset.value,
      goal_objective: document.querySelector('#project-goal-objective').value,
      goal_status: document.querySelector('#project-goal-status').value,
    }));
    assert.deepEqual(returned, {
      model: 'gpt-5.6-sol', effort: 'ultra', tier: 'priority', permission: 'read-only',
      draft: '上传前仍要保留的文字草稿', modelSegment: 'gpt-5.6-sol', effortSegment: 'ultra',
      goal_objective: '', goal_status: 'active',
    }, `upload return did not restore this tab's draft configuration: ${JSON.stringify(returned)}`);

    // A conflicting destination still retains the explicit confirmation page.
    await page.locator('.attach-button').click();
    await page.locator('#project-upload form').evaluate(form => { form.action += '?conflict=1'; });
    await page.locator('#project-upload form input[type=file]').setInputFiles({
      name: 'conflict.txt', mimeType: 'text/plain', buffer: Buffer.from('fixture'),
    });
    await Promise.all([
      page.waitForURL(/\/files\/upload\?conflict=1$/),
      page.locator('#project-upload form button:not(.secondary)').click(),
    ]);
    assert.match(await page.title(), /上传确认（测试夹具）/);
    await Promise.all([
      page.waitForURL(origin + projectPath('project-one')),
      page.locator('#upload-confirm button').click(),
    ]);

    // A separate project and a separate browser tab must not share tab-scoped settings.
    const projectTwo = await context.newPage();
    await projectTwo.goto(origin + projectPath('project-two'), { waitUntil: 'networkidle' });
    assert.equal(await projectTwo.inputValue('#project-conversation-effort'), 'high');
    await select(projectTwo, '#project-conversation-model', 'gpt-6-luna');
    await select(projectTwo, '#project-conversation-permission', 'workspace-write');
    assert.equal((await savedSettings(page, 'project-one')).permission, 'read-only');
    const anotherTab = await context.newPage();
    await anotherTab.goto(origin + projectPath('project-one'), { waitUntil: 'networkidle' });
    assert.equal(await anotherTab.inputValue('#project-conversation-effort'), 'high',
      'a new tab must not inherit sessionStorage from the upload tab');
    await anotherTab.close();
    await projectTwo.close();

    // A later validation listener can stop submission; that must leave the selected configuration intact.
    await page.evaluate(() => document.querySelector('#project-send').addEventListener('submit', event => event.preventDefault()));
    await page.locator('.send-button').click();
    await page.waitForTimeout(40);
    assert.equal(await page.url(), origin + projectPath('project-one'));
    assert.deepEqual(await savedSettings(page, 'project-one'), {
      model: 'gpt-5.6-sol', effort: 'ultra', tier: 'priority', collaboration_mode: 'default', permission: 'read-only',
      goal_objective: '', goal_status: 'active',
    }, 'prevented start submission must retain the upload-tab configuration');

    // A normal navigation submits and clears the one-time settings.
    await page.reload({ waitUntil: 'networkidle' });
    await page.fill('#project-send textarea', '开始这段对话');
    await page.locator('.send-button').click();
    await page.waitForURL(`${origin}${projectPath('project-one')}?started=1`);
    assert.equal(await page.inputValue('#project-conversation-effort'), 'high');
    assert.equal(await page.evaluate(key => sessionStorage.getItem(key), settingsKey('project-one')), null);
    await context.close();

    const fallbackContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const fallback = await fallbackContext.newPage();
    await fallback.addInitScript(({ key }) => sessionStorage.setItem(key, JSON.stringify({
      model: 'gpt-6-luna', effort: 'ultra', tier: 'priority', permission: 'read-only',
    })), { key: settingsKey('fallback') });
    await fallback.goto(origin + projectPath('fallback'), { waitUntil: 'networkidle' });
    assert.deepEqual(await fallback.evaluate(() => ({
      model: document.querySelector('#project-conversation-model').value,
      effort: document.querySelector('#project-conversation-effort').value,
      tier: document.querySelector('#project-conversation-tier').value,
      permission: document.querySelector('#project-conversation-permission').value,
    })), { model: 'gpt-6-luna', effort: 'medium', tier: '', permission: 'read-only' },
    'unsupported effort/tier must use the selected model defaults');
    await fallbackContext.close();

    const corruptContext = await browser.newContext();
    const corrupt = await corruptContext.newPage();
    await corrupt.addInitScript(({ key }) => sessionStorage.setItem(key, '{not json'), { key: settingsKey('corrupt') });
    await corrupt.goto(origin + projectPath('corrupt'), { waitUntil: 'networkidle' });
    assert.equal(await corrupt.inputValue('#project-conversation-model'), 'gpt-6-sol');
    await corruptContext.close();

    const removedContext = await browser.newContext();
    const removed = await removedContext.newPage();
    await removed.addInitScript(({ key }) => sessionStorage.setItem(key, JSON.stringify({
      model: 'gpt-6-sol', effort: 'ultra', tier: 'priority', permission: 'read-only',
    })), { key: settingsKey('removed') });
    await removed.goto(origin + projectPath('removed') + '?models=removed', { waitUntil: 'networkidle' });
    assert.notEqual(await removed.inputValue('#project-conversation-model'), 'gpt-6-sol',
      'a removed saved model must leave rendered server defaults usable');
    await removedContext.close();

    const storageContext = await browser.newContext();
    const storage = await storageContext.newPage();
    await storage.addInitScript(() => {
      Object.defineProperty(window, 'sessionStorage', { configurable: true, get() { throw new Error('disabled'); } });
      Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw new Error('disabled'); } });
    });
    await storage.goto(origin + projectPath('storage-disabled'), { waitUntil: 'networkidle' });
    await select(storage, '#project-conversation-model', 'gpt-6-sol');
    await select(storage, '#project-conversation-effort', 'ultra');
    assert.equal(await storage.inputValue('#project-conversation-effort'), 'ultra');
    await storageContext.close();

    const geometry = await browser.newPage({ viewport: { width: 320, height: 844 } });
    for (const width of [320, 360, 390, 412]) {
      await geometry.setViewportSize({ width, height: 844 });
      await geometry.goto(origin + '/geometry', { waitUntil: 'networkidle' });
      const facts = await geometry.evaluate(() => {
        const boxes = Array.from(document.querySelectorAll('.native-bubble')).map(node => {
          const rect = node.getBoundingClientRect();
          return { left: rect.left, right: rect.right, width: rect.width };
        });
        return { overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, boxes };
      });
      const [progressOne, progressTwo, final] = facts.boxes;
      const inGutter = box => box.left >= 7.5 && box.right <= width - 7.5;
      assert(facts.overflow <= 1 && [progressOne, progressTwo, final].every(inGutter) &&
        Math.abs(progressOne.left - 8) <= .5 && Math.abs(final.left - 8) <= .5 &&
        Math.abs(final.right - (width - 18)) <= .5 && Math.abs(progressOne.right - (width - 26)) <= .5 &&
        Math.abs(progressOne.width - progressTwo.width) <= .5,
      `conversation mobile geometry failed at ${width}px: ${JSON.stringify(facts)}`);
    }
    await geometry.close();
    console.log('project_upload_settings=passed upload_direct_303=true tabs_and_projects_isolated=true storage_fallbacks=true geometry=320,360,390,412');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
