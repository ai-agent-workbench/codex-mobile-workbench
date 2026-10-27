/* Real rendered composers and assets against local HTTP doubles. No production
 * access or model calls: test attachment selection, receipts and recovery. */
const assert = require('assert');
const childProcess = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const rendered = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')),
  [path.join(__dirname, 'render_conversation_ui_fixtures.py')], { cwd: root, encoding: 'utf8' }));
const prefix = '/' + 'a'.repeat(40);
const record = { id: 'a'.repeat(36), name: '很长很长的手机附件名称.txt', size: 12,
  mime_type: 'text/plain', preview_kind: 'text', created_at: '2026-10-04T00:00:00Z' };
const imageRecord = { ...record, id: 'b'.repeat(36), name: '这是一个非常长的手机截图附件文件名_完整名称仍用于预览与下载.png', mime_type: 'image/png', preview_kind: 'image' };
const available = new Map([[record.id, record], [imageRecord.id, imageRecord]]);
const baseSnapshot = JSON.parse(rendered.conversation.match(/id="conversation-initial-snapshot"[^>]*>([\s\S]*?)<\/script>/)[1]);
let commands = [], submissions = [], validationRequests = [], sendState = 'accepted';
let held = null, validationMode = 'normal', projectInitial = [record], conversationInitial = [record];
function snapshot() {
  const value = JSON.parse(JSON.stringify(baseSnapshot));
  value.conversation.project_id = 'project-one'; value.conversation.revision += commands.length;
  value.turns = commands.filter(item => ['accepted', 'completed'].includes(item.state)).map((command, index) => ({
    id: 'turn-fixture-' + command.id, ordinal: index + 1, status: 'completed',
    started_at: command.created_at, completed_at: command.created_at, final_text: null, error: null,
    commands: [command], items: [], artifacts: []
  }));
  value.messages = commands; value.queue = commands.filter(item => item.state === 'queued');
  return value;
}
function html(content, initial) {
  content = content.replace(/data-owner=""/g, 'data-owner="owner-a"')
    .replace(/data-project-id=""/g, 'data-project-id="project-one"')
    .replace(/data-initial='[^']*'/g, `data-initial='${JSON.stringify(initial)}'`)
    .replace(/(<script id="conversation-initial-snapshot"[^>]*>)[\s\S]*?(<\/script>)/,
      (_, a, b) => a + JSON.stringify(snapshot()) + b);
  // Include the actual base scripts: behavior.js must not lock a cancelled
  // attachment validation as if a native first-turn POST had started.
  return rendered.page_shell.replace('<!--fixture-content-->', content);
}
function fields(body, contentType) {
  if (!contentType.includes('multipart/form-data')) return new URLSearchParams(body);
  const result = new URLSearchParams();
  for (const match of body.matchAll(/name="([^"]+)"\r\n\r\n([\s\S]*?)\r\n--/g)) result.append(match[1], match[2]);
  return result;
}
function reply(res, body, status = 200, type = 'application/json') {
  res.writeHead(status, { 'content-type': type }); res.end(body);
}
(async () => {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://fixture');
    const route = url.pathname.startsWith(prefix + '/') ? url.pathname.slice(prefix.length) : url.pathname;
    if (route.startsWith('/static/')) {
      const file = path.join(root, route.slice(1));
      if (fs.existsSync(file) && fs.statSync(file).isFile()) {
        return reply(res, fs.readFileSync(file), 200, route.endsWith('.css') ? 'text/css' : 'application/javascript');
      }
      return reply(res, '', 404);
    }
    // Model the deployed gateway boundary, not an unrestricted backend.
    if (route === '/message-attachments' || route.startsWith('/message-attachments/')) return reply(res, '', 404);
    if (route === '/conversation/attachments/list') {
      const requested = url.searchParams.getAll('ids'); validationRequests.push(requested);
      if (validationMode === 'login') return reply(res, '{}', 401);
      if (validationMode === 'forbidden') return reply(res, '{}', 403);
      if (validationMode === 'invalid') return reply(res, '<html>login</html>');
      const found = validationMode === 'missing' ? [] : requested.map(id => available.get(id)).filter(Boolean);
      return reply(res, JSON.stringify({ attachments: found, missing_ids: requested.filter(id => !found.some(item => item.id === id)) }));
    }
    if (route.startsWith('/conversation/attachments/content/')) {
      if (route.endsWith(imageRecord.id)) return reply(res, fs.readFileSync(path.join(root, 'static/icon-512.png')), 200, 'image/png');
      return reply(res, '<b>安全文本，不作为 HTML 插入。</b>', 200, 'text/plain');
    }
    if (req.method === 'GET' && route === '/project/project-one') return reply(res, html(rendered.project, projectInitial), 200, 'text/html; charset=utf-8');
    if (req.method === 'GET' && route === '/conversation/conversation-ui') return reply(res, html(rendered.conversation, url.searchParams.has('attachment_batch') ? conversationInitial : []), 200, 'text/html; charset=utf-8');
    if (req.method === 'GET' && route === '/conversation/conversation-ui/snapshot') return reply(res, JSON.stringify(snapshot()));
    if (req.method === 'POST' && (route === '/conversation/new' || route.endsWith('/messages'))) {
      const chunks = []; req.on('data', chunk => chunks.push(chunk)); req.on('end', () => {
        const data = fields(Buffer.concat(chunks).toString(), req.headers['content-type'] || '');
        const isStart = route === '/conversation/new';
        const submission = { id: data.get(isStart ? 'client_command_id' : 'command_id'),
          kind: isStart ? 'start' : 'steer', state: sendState,
          text: (data.get(isStart ? 'prompt' : 'text') || '').trim(),
          attachments: data.getAll('attachment_ids').map(id => available.get(id)),
          created_at: new Date().toISOString() };
        submissions.push(submission);
        function respond() {
          const prior = commands.find(item => item.id === submission.id);
          if (!prior) commands.push(submission);
          if (isStart) { res.writeHead(303, { location: prefix + '/conversation/conversation-ui' }); res.end(); }
          else reply(res, JSON.stringify(snapshot()));
        }
        if (sendState === 'held') { held = () => { submission.state = 'accepted'; respond(); }; return; }
        respond();
      }); return;
    }
    return reply(res, '{}', 404);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  async function open(context, route) {
    const page = await context.newPage(); await page.goto(origin + prefix + route, { waitUntil: 'networkidle' });
    return page;
  }
  async function capture(page, form = '#conversation-composer') {
    return page.evaluate(selector => CodexMessageAttachments.forForm(document.querySelector(selector)).capture(), form);
  }
  try {
    // Deliberately ignore AbortSignal: a timed-out/abandoned response must be
    // harmless even if the network layer delivers it after cancellation.
    async function heldValidationPage(project = false) {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
      const page = await context.newPage();
      await page.clock.install();
      await page.addInitScript(record => {
        const realFetch = window.fetch.bind(window);
        window.validationHeld = true; window.validationReplies = [];
        window.fetch = function(url, options) {
          if (String(url).includes('/conversation/attachments/list') && window.validationHeld) {
            return new Promise(resolve => window.validationReplies.push(() => resolve(new Response(
              JSON.stringify({ attachments: [record], missing_ids: [] }),
              { status: 200, headers: { 'content-type': 'application/json' } }))));
          }
          return realFetch(url, options);
        };
      }, record);
      const form = project ? '#project-send' : '#conversation-composer';
      await page.goto(origin + prefix + (project ? '/project/project-one' : '/conversation/conversation-ui?attachment_batch=held'), { waitUntil: 'networkidle' });
      await page.fill(form + ' textarea', '核验失败必须保留的草稿');
      await page.locator(form + (project ? ' .send-button' : ' button[type=submit]')).click();
      return { context, page };
    }
    const timeoutCase = await heldValidationPage();
    const timeoutPage = timeoutCase.page, beforeTimeout = submissions.length;
    assert((await timeoutPage.locator('.message-attachments-status').textContent()).includes('正在核验附件'), '悬挂核验必须有明确进度');
    assert(await timeoutPage.locator('#conversation-composer button[type=submit]').isDisabled(), '核验中不能重复点击发送');
    await timeoutPage.evaluate(() => {
      const form = document.querySelector('#conversation-composer');
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    assert.equal(await timeoutPage.evaluate(() => validationReplies.length), 1, '同一核验必须单飞');
    await timeoutPage.clock.fastForward(15010);
    await timeoutPage.waitForFunction(() => document.querySelector('.message-attachments-status').textContent.includes('超时'));
    assert.equal(submissions.length, beforeTimeout);
    assert(await timeoutPage.locator('#conversation-composer button[type=submit]').isEnabled());
    assert.equal(await timeoutPage.inputValue('#conversation-composer textarea'), '核验失败必须保留的草稿');
    assert.deepEqual((await capture(timeoutPage)).ids, [record.id]);
    await timeoutPage.evaluate(() => validationReplies[0]());
    await timeoutPage.waitForTimeout(40);
    assert.equal(submissions.length, beforeTimeout, '超时后的迟到成功不得自动发送');
    await timeoutPage.evaluate(() => { validationHeld = false; });
    await timeoutPage.locator('#conversation-composer button[type=submit]').click();
    await timeoutPage.waitForFunction(() => !document.querySelector('[data-message-attachment-id]'));
    assert.equal(submissions.length, beforeTimeout + 1, '明确重试且核验成功后仅投递一次');
    await timeoutCase.context.close(); commands = []; submissions = [];

    const cancelCase = await heldValidationPage();
    await cancelCase.page.locator('.message-attachments-cancel').click();
    await cancelCase.page.waitForFunction(() => !document.querySelector('#conversation-composer button[type=submit]').disabled);
    assert((await cancelCase.page.locator('.message-attachments-status').textContent()).includes('取消'));
    await cancelCase.page.locator('#conversation-composer button[type=submit]').click();
    await cancelCase.page.evaluate(() => validationReplies[0]());
    await cancelCase.page.waitForTimeout(40);
    assert(await cancelCase.page.locator('#conversation-composer button[type=submit]').isDisabled(), '旧回调不得清除新核验');
    assert.equal(submissions.length, 0);
    await cancelCase.page.evaluate(() => validationReplies[1]());
    await cancelCase.page.waitForFunction(() => !document.querySelector('[data-message-attachment-id]'));
    assert.equal(submissions.length, 1);
    await cancelCase.context.close(); commands = []; submissions = [];

    for (const change of ['remove', 'remote', 'text']) {
      const changed = await heldValidationPage();
      if (change === 'remove') await changed.page.locator('#conversation-composer .message-attachment-remove').click();
      else if (change === 'text') await changed.page.fill('#conversation-composer textarea', '新草稿不能被旧核验发送');
      else await changed.page.evaluate(record => {
        const key = 'codex-message-attachments:owner-a:conversation:conversation-ui:codex-conversation-draft:conversation-ui';
        const next = JSON.stringify({ records: [record], revision: 'remote-during-preflight' });
        localStorage.setItem(key, next); window.dispatchEvent(new StorageEvent('storage', { key, newValue: next }));
      }, imageRecord);
      await changed.page.waitForFunction(() => !document.querySelector('#conversation-composer button[type=submit]').disabled);
      await changed.page.evaluate(() => validationReplies[0]());
      await changed.page.waitForTimeout(40);
      assert.equal(submissions.length, 0, change + ': 旧核验不得发送变更后的草稿');
      await changed.context.close();
    }
    // The project first-turn form has its own requestSubmit/receipt path.
    const firstQuestion = await heldValidationPage(true), projectPage = firstQuestion.page;
    await projectPage.locator('#project-send .send-button').click();
    assert.equal(await projectPage.evaluate(() => validationReplies.length), 1);
    await projectPage.clock.fastForward(15010);
    await projectPage.waitForFunction(() => document.querySelector('.message-attachments-status').textContent.includes('超时'));
    await projectPage.evaluate(() => validationReplies[0]());
    await projectPage.waitForTimeout(40); assert.equal(submissions.length, 0);
    assert.equal(await projectPage.inputValue('#project-send textarea'), '核验失败必须保留的草稿');
    await projectPage.locator('#project-send .send-button').click();
    await projectPage.locator('.message-attachments-cancel').click();
    await projectPage.locator('#project-send .send-button').click();
    await projectPage.evaluate(() => validationReplies[1]());
    await projectPage.waitForTimeout(40); assert.equal(submissions.length, 0);
    await projectPage.fill('#project-send textarea', '首问的新草稿');
    await projectPage.evaluate(() => validationReplies[2]());
    await projectPage.waitForTimeout(40); assert.equal(submissions.length, 0);
    await projectPage.evaluate(() => { validationHeld = false; });
    await projectPage.locator('#project-send .send-button').click();
    await projectPage.waitForURL(origin + prefix + '/conversation/conversation-ui');
    assert.equal(submissions.length, 1);
    assert.equal(submissions[0].text, '首问的新草稿');
    await firstQuestion.context.close(); commands = []; submissions = []; validationRequests = [];

    // Image and ordinary files use the same compact row, including both
    // actions. Long names yield to 44px touch targets instead of adding rows.
    projectInitial = [record, imageRecord]; conversationInitial = [record, imageRecord];
    for (const width of [320, 360, 390, 412]) {
      const layoutContext = await browser.newContext({ viewport: { width, height: 844 } });
      for (const [route, form] of [['/project/project-one', '#project-send'],
        ['/conversation/conversation-ui?attachment_batch=geometry', '#conversation-composer']]) {
        const layout = await open(layoutContext, route);
        for (const theme of ['dark', 'light']) {
          await layout.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
          const geometry = await layout.locator(form + ' .message-attachment-card').evaluateAll(cards => cards.map(card => {
            const preview = card.querySelector('.message-attachment-preview').getBoundingClientRect();
            const remove = card.querySelector('.message-attachment-remove').getBoundingClientRect();
            const name = card.querySelector('strong'), title = name.getBoundingClientRect();
            return { height: card.getBoundingClientRect().height, previewY: preview.top, removeY: remove.top,
              previewW: preview.width, previewH: preview.height, removeW: remove.width, removeH: remove.height,
              separated: preview.right <= remove.left && title.right <= preview.left,
              ellipsis: getComputedStyle(name).textOverflow, truncated: name.scrollWidth > name.clientWidth };
          }));
          assert.equal(geometry.length, 2);
          for (const card of geometry) {
            assert(Math.abs(card.previewY - card.removeY) <= 1, `${width}/${theme}: 预览与移除必须并排`);
            assert(card.height <= 60, `${width}/${theme}: 卡片不得堆叠成两行高按钮`);
            assert(card.previewW >= 44 && card.previewH >= 44 && card.removeW >= 44 && card.removeH >= 44,
              `${width}/${theme}: 紧凑布局仍须保留触控面积`);
            assert(card.separated, `${width}/${theme}: 文件名不能挤压或覆盖操作按钮`);
            assert.equal(card.ellipsis, 'ellipsis');
          }
          assert(geometry[1].truncated, `${width}/${theme}: 长图片文件名应省略显示`);
          assert.equal(await layout.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
          if (width === 390 && theme === 'dark' && process.env.ATTACHMENT_COMPOSER_SCREENSHOT) {
            await layout.screenshot({ path: process.env.ATTACHMENT_COMPOSER_SCREENSHOT });
          }
        }
        await layout.close();
      }
      await layoutContext.close();
    }
    projectInitial = [record]; conversationInitial = [record]; validationRequests = [];
    // A rejected first-turn preflight preserves the upload and text and lets
    // the user retry; there must be no POST or permanent "sending" state.
    const retryContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const retryProject = await open(retryContext, '/project/project-one');
    await retryProject.fill('#project-send textarea', '保留已上传附件的首问');
    const beforeRetry = submissions.length;
    for (const mode of ['missing', 'invalid', 'login', 'forbidden']) {
      validationMode = mode;
      await retryProject.locator('#project-send .send-button').click();
      await retryProject.waitForFunction(() => document.querySelector('.message-attachments-status').textContent.includes('未发送') ||
        document.querySelector('.message-attachments-status').textContent.includes('不可用'));
      assert.equal(submissions.length, beforeRetry, '核验失败不得投递首问');
      assert.equal(await retryProject.locator('#project-send .send-button').isEnabled(), true, '取消的提交不得锁成发送中');
      assert.equal(await retryProject.locator('#project-send .send-button').textContent(), '开始');
      assert.equal(await retryProject.inputValue('#project-send textarea'), '保留已上传附件的首问');
      assert.deepEqual((await capture(retryProject, '#project-send')).ids, [record.id]);
    }
    validationMode = 'normal';
    await retryProject.locator('#project-send .send-button').click();
    await retryProject.waitForURL(origin + prefix + '/conversation/conversation-ui');
    assert.equal(submissions.length, beforeRetry + 1);
    assert.deepEqual(submissions.at(-1).attachments.map(item => item.id), [record.id]);
    await retryContext.close(); commands = []; submissions = []; validationRequests = [];
    const context = await browser.newContext({ viewport: { width: 320, height: 640 } });
    const page = await open(context, '/conversation/conversation-ui?attachment_batch=batch1');
    assert(!page.url().includes('attachment_batch'));
    assert.deepEqual(validationRequests[0], [record.id], '真实核验请求必须传精确 ID，不能是 undefined');
    assert.equal(await page.locator('[data-message-attachment-id]').count(), 1);
    assert.equal(await page.locator('#conversation-composer textarea').evaluate(node => node.required), false);
    await page.locator('.message-attachment-preview').click();
    await page.waitForSelector('.message-attachment-viewer pre');
    await page.waitForFunction(() => document.querySelector('.message-attachment-viewer pre').textContent.includes('安全文本'));
    assert.equal(await page.locator('.message-attachment-viewer pre b').count(), 0);
    await page.locator('.message-attachment-viewer header button').click();
    await page.waitForTimeout(60);
    // Missing, invalid JSON, login and offline keep files and block any POST.
    for (const mode of ['missing', 'invalid', 'login', 'forbidden']) {
      validationMode = mode;
      const rejected = await page.evaluate(() => CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).ready().then(() => false, () => true));
      assert(rejected, mode + ' validation must reject'); assert.equal((await capture(page)).ids.length, 1);
    }
    validationMode = 'normal'; await context.setOffline(true);
    await page.fill('#conversation-composer textarea', '离线草稿'); await page.locator('#conversation-composer button[type=submit]').click();
    await page.waitForTimeout(150); assert.equal(submissions.length, 0);
    assert.equal(await page.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui')), '离线草稿');
    await context.setOffline(false);
    // A pure attachment is a real ordinary form request with no invented text.
    await page.fill('#conversation-composer textarea', ''); await page.locator('#conversation-composer button[type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('[data-message-attachment-id]'));
    assert.equal(submissions.length, 1); assert.equal(submissions[0].text, '');
    assert.deepEqual(submissions[0].attachments.map(item => item.id), [record.id]);
    assert.equal(await page.locator('.message-attachments-history .message-attachment-card').count(), 1);
    conversationInitial = []; await page.reload({ waitUntil: 'networkidle' });
    assert.equal(submissions.length, 1, '刷新不能自动重放');
    assert.equal(await page.locator('.message-attachments-history .message-attachment-card').count(), 1);
    // Storage updates and old receipts cannot remove a newer revision (including ABA).
    const second = await open(context, '/conversation/conversation-ui');
    await page.evaluate(record => {
      const host = document.querySelector('[data-message-attachments]');
      const key = 'codex-message-attachments:owner-a:conversation:conversation-ui:codex-conversation-draft:conversation-ui';
      localStorage.setItem(key, JSON.stringify({ records: [record], revision: 'revision-old' }));
      window.dispatchEvent(new StorageEvent('storage', { key, newValue: localStorage.getItem(key) }));
    }, record);
    const old = await capture(page);
    await page.locator('#conversation-composer .message-attachment-remove').click();
    await second.waitForFunction(() => !document.querySelector('[data-message-attachment-id]'));
    await second.evaluate(record => {
      const key = 'codex-message-attachments:owner-a:conversation:conversation-ui:codex-conversation-draft:conversation-ui';
      const data = JSON.stringify({ records: [record], revision: 'revision-new' });
      localStorage.setItem(key, data); window.dispatchEvent(new StorageEvent('storage', { key, newValue: data }));
    }, record);
    await page.waitForFunction(() => !!document.querySelector('[data-message-attachment-id]'));
    assert.equal(await page.evaluate(sent => CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).settle(sent), old), false);
    assert.equal((await capture(page)).ids.length, 1);
    // A late accepted HTTP receipt cannot clear files/text edited during its flight.
    await page.evaluate(() => CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).ready());
    sendState = 'held'; await page.fill('#conversation-composer textarea', '旧消息');
    await page.locator('#conversation-composer button[type=submit]').click();
    while (!held) await page.waitForTimeout(20);
    await page.fill('#conversation-composer textarea', '发送后新草稿');
    await second.evaluate(records => {
      const key = 'codex-message-attachments:owner-a:conversation:conversation-ui:codex-conversation-draft:conversation-ui';
      const data = JSON.stringify({ records, revision: 'after-send' });
      localStorage.setItem(key, data); window.dispatchEvent(new StorageEvent('storage', { key, newValue: data }));
    }, [record, imageRecord]);
    await page.waitForFunction(() => document.querySelectorAll('[data-message-attachment-id]').length === 2);
    held(); held = null;
    await page.waitForFunction(() => !document.querySelector('#conversation-composer button[type=submit]').disabled);
    assert.equal(await page.inputValue('#conversation-composer textarea'), '发送后新草稿');
    assert.deepEqual((await capture(page)).ids, [record.id, imageRecord.id]);
    sendState = 'queued'; await page.locator('#conversation-composer button[type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('[data-message-attachment-id]'));
    assert.equal(submissions.at(-1).state, 'queued');
    assert.equal(submissions.at(-1).attachments.length, 2);
    await context.close();
    // Project first question: exact command claim, whitespace, unknown and retry.
    commands = []; submissions = []; conversationInitial = []; projectInitial = [record];
    const firstContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const project = await open(firstContext, '/project/project-one');
    await project.fill('#project-send textarea', '  首问有前后空白  ');
    const identities = await project.evaluate(() => {
      const form = document.querySelector('#project-send'), api = CodexMessageAttachments.forForm(form);
      const captured = api.capture(), a = new FormData(form), b = new FormData(form);
      b.set('prompt', '另一个标签页的不同首问');
      const first = api.prepareTransfer(captured, a, '  首问有前后空白  ');
      const other = api.prepareTransfer(captured, b, '另一个标签页的不同首问');
      const retry = api.prepareTransfer(captured, a, '  首问有前后空白  ');
      return [first.command_id, other.command_id, retry.command_id];
    });
    assert.notEqual(identities[0], identities[1]);
    assert.equal(identities[0], identities[2], 'A→B→A 不得覆盖未解决首问的投递身份');
    sendState = 'delivery_unknown'; await project.locator('#project-send .send-button').click();
    await project.waitForURL(origin + prefix + '/conversation/conversation-ui');
    assert.equal(submissions.length, 1);
    const firstId = submissions[0].id;
    const markerKey = 'codex-message-attachment-transfer:owner-a:project-one:' + firstId;
    assert(await project.evaluate(key => !!localStorage.getItem(key), markerKey), 'unknown保留首问状态');
    await project.reload({ waitUntil: 'networkidle' }); assert.equal(submissions.length, 1);
    const otherMarkerKey = 'codex-message-attachment-transfer:owner-a:project-one:' + identities[1];
    const protectedSource = await project.evaluate(({ otherMarkerKey, markerKey }) => {
      const other = JSON.parse(localStorage.getItem(otherMarkerKey));
      const original = JSON.parse(localStorage.getItem(markerKey));
      CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).claim({
        conversation: { project_id: 'project-one' },
        messages: [{ id: other.command_id, kind: 'start', state: 'accepted', text: other.text, attachments: other.records }]
      });
      return { source: JSON.parse(localStorage.getItem(original.source_key)), original,
        unknown: !!localStorage.getItem(markerKey), other: JSON.parse(localStorage.getItem(otherMarkerKey)) };
    }, { otherMarkerKey, markerKey });
    assert.deepEqual(protectedSource.source.records.map(item => item.id), [record.id]);
    assert.equal(protectedSource.source.revision, protectedSource.original.source_revision);
    assert(protectedSource.unknown, 'B 的回执不得消费 A 未决首问的共享附件');
    assert.equal(protectedSource.other.receipt_state, 'accepted', '共享源仍有未决首问时保留 B 身份');
    projectInitial = []; await project.goto(origin + prefix + '/project/project-one', { waitUntil: 'networkidle' });
    assert.equal(await project.inputValue('#project-send textarea'), '  首问有前后空白  ');
    await project.locator('#project-send .send-button').click();
    await project.waitForURL(origin + prefix + '/conversation/conversation-ui');
    assert.equal(submissions[1].id, firstId, '不确定首问手动重试仍用原command ID');
    // Later accepted receipt clears only that exact source revision; whitespace
    // matches normalized server text while source cleanup matches original text.
    commands[0].state = 'accepted'; await project.reload({ waitUntil: 'networkidle' });
    assert.equal(await project.evaluate(key => localStorage.getItem(key), markerKey), null);
    assert.equal(await project.evaluate(key => localStorage.getItem(key), otherMarkerKey), null);
    assert.equal(await project.evaluate(() => localStorage.getItem('codex-project-conversation-draft:project-one')), null);
    assert.equal(submissions.length, 2);
    // Queued history still displays its file; no upload directory inference.
    commands.push({ ...commands[0], id: 'queue-1', kind: 'steer', state: 'queued', text: '下一轮', attachments: [imageRecord] });
    await project.reload({ waitUntil: 'networkidle' });
    assert.equal(await project.locator('.message-attachments-history .message-attachment-card').count(), 2);
    await project.locator('.message-attachments-history .message-attachment-preview').last().click();
    await project.waitForSelector('.message-attachment-viewer img');
    assert(await project.locator('.message-attachment-viewer img').evaluate(node => node.complete && node.naturalWidth > 0));
    await project.goBack(); await project.waitForFunction(() => !document.querySelector('.message-attachment-viewer'));
    const overflow = await project.locator('.native-conversation').evaluate(node => node.scrollWidth > window.innerWidth + 1);
    assert.equal(overflow, false, '移动宽度不能横向溢出');
    if (process.env.ATTACHMENT_SCREENSHOT) await project.screenshot({ path: process.env.ATTACHMENT_SCREENSHOT });
    await firstContext.close();
    // First-turn cleanup is a separate transaction: failed writes must use
    // owned removal or retain the marker, including an attachment-only start.
    for (const failRemoval of [false, true]) {
      commands = []; projectInitial = [record]; conversationInitial = [];
      const receiptContext = await browser.newContext();
      const sourcePage = await open(receiptContext, '/project/project-one');
      const transfer = await sourcePage.evaluate(() => {
        const form = document.querySelector('#project-send'), api = CodexMessageAttachments.forForm(form);
        return api.prepareTransfer(api.capture(), new FormData(form), '');
      });
      const destination = await open(receiptContext, '/conversation/conversation-ui');
      const result = await destination.evaluate(({ transfer, failRemoval }) => {
        const originalSet = Storage.prototype.setItem, originalRemove = Storage.prototype.removeItem;
        Storage.prototype.setItem = function (key, value) {
          if (key.startsWith('codex-message-attachment')) throw new DOMException('quota', 'QuotaExceededError');
          return originalSet.call(this, key, value);
        };
        Storage.prototype.removeItem = function (key) {
          if (failRemoval && key.startsWith('codex-message-attachment')) throw Error('remove blocked');
          return originalRemove.call(this, key);
        };
        CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).claim({
          conversation: { project_id: 'project-one' },
          messages: [{ id: transfer.command_id, kind: 'start', state: 'accepted', text: '', attachments: transfer.records }]
        });
        Storage.prototype.setItem = originalSet; Storage.prototype.removeItem = originalRemove;
        return { source: localStorage.getItem(transfer.source_key),
          marker: localStorage.getItem('codex-message-attachment-transfer:owner-a:project-one:' + transfer.command_id) };
      }, { transfer, failRemoval });
      if (failRemoval) {
        assert(result.source && result.marker, '源未消费必须保留已投递身份');
        projectInitial = []; await sourcePage.reload({ waitUntil: 'networkidle' });
        const retryId = await sourcePage.evaluate(() => {
          const form = document.querySelector('#project-send'), api = CodexMessageAttachments.forForm(form);
          return api.prepareTransfer(api.capture(), new FormData(form), '').command_id;
        });
        assert.equal(retryId, transfer.command_id, '清理失败后的纯附件重试必须使用原身份');
      } else {
        assert.equal(result.source, null); assert.equal(result.marker, null);
        projectInitial = []; await sourcePage.reload({ waitUntil: 'networkidle' });
        assert.equal((await capture(sourcePage, '#project-send')).ids.length, 0, '已投递附件不能被存储旧值恢复');
      }
      await receiptContext.close();
    }
    // Readable but quota-full storage: consume the in-page receipt and only
    // its stale owned value; never delete another tab's newer same-file draft.
    const quotaKey = 'codex-message-attachments:owner-a:conversation:conversation-ui:codex-conversation-draft:conversation-ui';
    async function quotaContext() {
      const context = await browser.newContext({ viewport: { width: 320, height: 640 } });
      await context.addInitScript(({ key, record }) => {
        const original = Storage.prototype.setItem;
        original.call(localStorage, key, JSON.stringify({ records: [record], revision: 'quota-old' }));
        window.fixtureForceStorage = (key, value) => original.call(localStorage, key, value);
        Storage.prototype.setItem = function (key, value) {
          if (key.startsWith('codex-message-attachments:')) throw new DOMException('quota', 'QuotaExceededError');
          return original.call(this, key, value);
        };
      }, { key: quotaKey, record });
      return context;
    }
    conversationInitial = [imageRecord];
    const quota = await quotaContext(), quotaPage = await open(quota, '/conversation/conversation-ui?attachment_batch=quota-batch');
    const quotaSent = await capture(quotaPage); assert.deepEqual(quotaSent.ids, [record.id, imageRecord.id]);
    assert.equal(await quotaPage.evaluate(sent => CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).settle(sent), quotaSent), true);
    assert.equal((await capture(quotaPage)).ids.length, 0);
    assert.equal(await quotaPage.evaluate(key => localStorage.getItem(key), quotaKey), null);
    assert(!quotaPage.url().includes('attachment_batch'));
    await quota.close();
    const remoteQuota = await quotaContext(), remoteQuotaPage = await open(remoteQuota, '/conversation/conversation-ui?attachment_batch=quota-batch');
    const oldQuotaSent = await capture(remoteQuotaPage);
    const newer = JSON.stringify({ records: [imageRecord], revision: 'newer-other-tab' });
    await remoteQuotaPage.evaluate(({ key, value }) => fixtureForceStorage(key, value), { key: quotaKey, value: newer });
    assert.equal(await remoteQuotaPage.evaluate(sent => CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).settle(sent), oldQuotaSent), true);
    assert.equal((await capture(remoteQuotaPage)).ids.length, 0);
    assert.equal(await remoteQuotaPage.evaluate(key => localStorage.getItem(key), quotaKey), newer);
    await remoteQuota.close();
    // A failed pending-ID write must stop before any POST (no unknown resend
    // after refresh). Existing text-only storage fallback stays unchanged.
    const failedPending = await browser.newContext();
    await failedPending.addInitScript(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key.startsWith('codex-conversation-pending:')) throw new DOMException('quota', 'QuotaExceededError');
        return original.call(this, key, value);
      };
    });
    conversationInitial = [record]; const blocked = await open(failedPending, '/conversation/conversation-ui?attachment_batch=pending-batch');
    const beforeBlocked = submissions.length;
    await blocked.locator('#conversation-composer button[type=submit]').click();
    await blocked.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('无法保存附件投递状态'));
    assert.equal(submissions.length, beforeBlocked); assert.equal((await capture(blocked)).ids.length, 1);
    await failedPending.close();
    // Disabled storage is truthful and doesn't crash or erase the upload batch
    // that can restore this page on refresh.
    const disabledContext = await browser.newContext();
    await disabledContext.addInitScript(() => Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw Error('disabled'); } }));
    conversationInitial = [record]; const disabled = await open(disabledContext, '/conversation/conversation-ui?attachment_batch=batch1');
    assert(disabled.url().includes('attachment_batch'));
    const sent = await capture(disabled);
    assert.equal(await disabled.evaluate(sent => CodexMessageAttachments.forForm(document.querySelector('#conversation-composer')).settle(sent), sent), true);
    assert.equal((await capture(disabled)).ids.length, 0);
    assert(!disabled.url().includes('attachment_batch'), '已确认附件即使无存储也必须移除批次URL');
    await disabled.reload({ waitUntil: 'networkidle' });
    assert.equal((await capture(disabled)).ids.length, 0, '刷新不能恢复已消费的批次');
    await disabledContext.close();
    console.log('message attachment real-composer browser checks passed');
  } finally {
    if (held) held(); await browser.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
