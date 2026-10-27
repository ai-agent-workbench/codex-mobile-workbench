/* Native AgentMessage delivery:"async" fixture: no real App Server turn. */
const {chromium} = require('playwright');
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const http = require('http');
const cp = require('child_process');

const root = path.resolve(__dirname, '..');
const rendered = JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [
  path.join(__dirname, 'render_conversation_ui_fixtures.py')
], {cwd: root, encoding: 'utf8'}));
const base = JSON.parse(rendered.conversation.match(
  /<script id="conversation-initial-snapshot"[^>]*>([\s\S]*?)<\/script>/
)[1]);
let snapshot, posts = [];

function scene(questions) {
  const data = structuredClone(base), now = Date.now();
  data.conversation.state = 'active'; data.conversation.active_turn_id = 'turn-async';
  data.conversation.revision = 20; data.conversation.terminal_generation = 0;
  data.turns = [{
    id: 'turn-async', ordinal: 2, status: 'inProgress',
    started_at: new Date(now - 2000).toISOString(), completed_at: null,
    final_text: null, error: null,
    commands: [{id: 'start-async', kind: 'start', state: 'accepted', text: '继续核对记录', created_at: new Date(now - 2000).toISOString()}],
    items: [{
      native_item_id: 'call-async', kind: 'agent_message', status: 'completed',
      phase: 'final_answer', delivery: 'async', questions: questions.map((question, index) => ({
        ...question,
        request: {
          id: 'async-request-' + index, state: 'awaiting_answer',
          deadline_at: Array.isArray(question.options) ? new Date(now + 60000).toISOString() : null,
          answer_source: null, command_state: null, resolved_at: null
        }
      })),
      text: '是否继续？\n- 继续\n- 暂停\n\n请补充说明',
      created_at: new Date(now - 1000).toISOString(), updated_at: new Date(now - 1000).toISOString()
    }], artifacts: []
  }];
  data.messages = []; data.queue = []; data.artifacts = []; data.questions = [];
  snapshot = data;
}
function publicData() { return {...snapshot, server_now: new Date().toISOString()}; }
function pageHtml() {
  const body = rendered.conversation.replace(
    /(<script id="conversation-initial-snapshot"[^>]*>)[\s\S]*?(<\/script>)/,
    (_, start, end) => start + JSON.stringify(publicData()).replace(/</g, '\\u003c') + end
  );
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><link rel="stylesheet" href="/static/css/app.css"></head><body><main>${body}</main></body></html>`;
}
function reply(res, data, status=200) {
  res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
  res.end(JSON.stringify(data));
}
const server = http.createServer(async (req, res) => { try {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  if (pathname.startsWith('/static/')) {
    const file = path.join(root, pathname);
    if (!file.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(file)) {
      res.writeHead(404); res.end(); return;
    }
    res.writeHead(200, {'Content-Type': pathname.endsWith('.js') ? 'application/javascript' : 'text/css'});
    res.end(fs.readFileSync(file)); return;
  }
  if (pathname.endsWith('/snapshot')) { reply(res, publicData()); return; }
  if (pathname.includes('/async-questions/') && pathname.endsWith('/answer')) {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const form = await new Request('http://localhost', {method: 'POST', headers: req.headers, body: Buffer.concat(chunks)}).formData();
    const requestId = pathname.split('/').slice(-2)[0];
    posts.push({command: form.get('command_id'), text: form.get('text'), fields: [...form.keys()], requestId});
    snapshot.conversation.revision += 1;
    snapshot.turns[0].commands.push({
      id: form.get('command_id'), kind: 'steer', state: 'accepted', text: form.get('text'), origin: 'user', created_at: new Date().toISOString()
    });
    snapshot.turns[0].items[0].questions.find(question => question.request.id === requestId).request = {
      id: requestId, state: 'answered', deadline_at: null, answer_source: 'user', command_state: 'accepted', resolved_at: new Date().toISOString()
    };
    reply(res, publicData()); return;
  }
  if (pathname.endsWith('/messages')) {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const form = await new Request('http://localhost', {method: 'POST', headers: req.headers, body: Buffer.concat(chunks)}).formData();
    posts.push({command: form.get('command_id'), text: form.get('text'), fields: [...form.keys()]});
    snapshot.conversation.revision += 1;
    snapshot.turns[0].commands.push({
      id: form.get('command_id'), kind: 'steer', state: 'accepted', text: form.get('text'), created_at: new Date().toISOString()
    });
    reply(res, publicData()); return;
  }
  if (req.method === 'POST') { reply(res, publicData()); return; }
  res.writeHead(200, {'Content-Type': 'text/html', 'Cache-Control': 'no-store'}); res.end(pageHtml());
} catch (error) { res.writeHead(500); res.end(String(error)); } });

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/conversation/conversation-ui`;
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 390, height: 844}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    class TestSocket {
      constructor() { this.readyState = 0; window.testSocket = this; setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 0); }
      send(text) { if (text === 'ping' && this.onmessage) this.onmessage({data: '{"type":"pong"}'}); }
      close() { this.readyState = 3; }
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    }
    window.WebSocket = TestSocket;
  });
  async function load(questions) {
    scene(questions); await page.goto(url);
    await page.locator('.native-async-question-card').waitFor();
  }
  async function notifyUpdate() {
    snapshot.conversation.revision += 1;
    const marker = '异步问题同步检查' + snapshot.conversation.revision;
    snapshot.turns[0].items = snapshot.turns[0].items.filter(item => item.native_item_id !== 'async-sync-marker');
    snapshot.turns[0].items.push({
      native_item_id: 'async-sync-marker', kind: 'agent_message', status: 'inProgress',
      phase: 'commentary', text: marker,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString()
    });
    await page.evaluate(() => window.testSocket.onmessage({data: JSON.stringify({type: 'conversation.updated', data: {conversation_id: 'conversation-ui'}})}));
    await page.waitForFunction(value => document.getElementById('conversation-messages').textContent.includes(value), marker, {timeout: 15000});
  }
  try {
    const longOption = '全月，包含月底补录、跨月签收、需要逐项回查的供货单，以及存在退货或作废情形的异常记录';
    await load([
      {title: '8 月加工间有没有给江碗碗送过货？', options: ['没有', longOption, '我不确定']},
      {title: '请补充需要重点核对的记录', options: null}
    ]);
    assert.equal(await page.locator('.native-async-question-card').count(), 1);
    assert.equal(await page.locator('.native-final-mark').count(), 0, 'async event must not masquerade as final');
    assert.equal(await page.locator('.native-async-question-field').count(), 2);
    assert.equal(await page.locator('.native-async-question-option').count(), 3);
    assert.equal(await page.locator('.native-async-question-option[aria-pressed="true"]').count(), 1, 'first suggestion is only preselected');
    assert.match(await page.textContent('.native-async-question-countdown'), /剩余/);
    assert.equal(await page.textContent('.native-async-question-hint'), '超时按推荐项自动继续');
    assert.equal(posts.length, 0, 'preselection must never submit');
    const optionMetrics = await page.locator('.native-async-question-option').evaluateAll(values => values.map(value => {
      const rect = value.getBoundingClientRect(); return {left: rect.left, right: rect.right, height: rect.height, width: rect.width, scrollWidth: value.scrollWidth, clientWidth: value.clientWidth, text: value.innerText};
    }));
    assert.ok(optionMetrics.every(value => Math.abs(value.left - optionMetrics[0].left) < 1 && Math.abs(value.right - optionMetrics[0].right) < 1), 'each option must use one full line');
    assert.ok(optionMetrics.every(value => value.scrollWidth <= value.clientWidth), 'options must not overflow horizontally');
    assert.ok(optionMetrics.find(value => value.text.includes('跨月签收')).height > optionMetrics[0].height, 'long option must wrap');
    await page.locator('.native-async-question-option').nth(2).click();
    assert.equal(posts.length, 0, 'selection alone must not submit');
    assert.match(await page.locator('.native-async-question-option[aria-pressed="true"]').textContent(), /我不确定/);
    await page.locator('.native-async-question-other').click();
    const firstFree = page.locator('[data-async-question-field]').first();
    await firstFree.fill('改为人工复核后再决定');
    await firstFree.focus(); await page.evaluate(() => { const control = document.activeElement; control.setSelectionRange(2, 4); });
    snapshot.turns[0].items.push({native_item_id: 'commentary-sync', kind: 'agent_message', status: 'inProgress', phase: 'commentary', text: '仍在核对记录', created_at: new Date().toISOString(), updated_at: new Date().toISOString()});
    await notifyUpdate();
    assert.equal(await page.locator('[data-async-question-field]').first().inputValue(), '改为人工复核后再决定');
    assert.deepEqual(await page.evaluate(() => ({field: document.activeElement.dataset.asyncQuestionField, start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd})), {field: await page.locator('[data-async-question-field]').first().getAttribute('data-async-question-field'), start: 2, end: 4});
    const answerRoute = '**/async-questions/*/answer';
    await page.route(answerRoute, route => route.abort());
    await page.locator('.native-async-question-send').first().click();
    await page.waitForFunction(() => {
      const button = document.querySelector('.native-async-question-send');
      return button && !button.disabled && document.querySelector('.native-async-question-error')?.textContent;
    });
    assert.equal(await firstFree.inputValue(), '改为人工复核后再决定', 'failed async answer keeps its editable reply');
    assert.equal(posts.length, 0, 'transport failure does not reach the answer endpoint');
    await page.unroute(answerRoute);
    await page.locator('.native-async-question-send').first().click();
    await page.waitForFunction(() => document.querySelector('.native-async-question-receipt'));
    assert.equal(posts.length, 1);
    assert.equal(posts[0].text, '改为人工复核后再决定');
    assert.deepEqual(posts[0].fields.sort(), ['command_id', 'text'], 'async reply uses the durable answer endpoint fields');
    assert.equal(posts[0].requestId, 'async-request-0');
    assert.match(await page.textContent('#conversation-messages'), /已作为普通消息发送/);
    const freeOnly = page.locator('[data-async-question-field]').first();
    assert.equal(await freeOnly.isVisible(), true, 'free-text-only question opens its field');
    await freeOnly.fill('重点核对 8 月 15 日');
    await page.locator('.native-async-question-send').first().click();
    await page.waitForFunction(() => document.querySelectorAll('.native-async-question-receipt').length >= 2);
    assert.equal(posts.length, 2);
    assert.equal(posts[1].text, '重点核对 8 月 15 日');
    assert.equal(posts[1].requestId, 'async-request-1');
    await load([{title: '自动继续测试', options: ['推荐方案', '备用方案']}]);
    // The page's normal sync debounce is 2.5 s; keep the fixture deadline
    // beyond that so this verifies a displayed countdown rather than a race.
    snapshot.turns[0].items[0].questions[0].request.deadline_at = new Date(Date.now() + 5000).toISOString();
    await notifyUpdate();
    await page.waitForFunction(() => /剩余 [1-5] 秒/.test(document.querySelector('.native-async-question-countdown').textContent));
    const beforeTimeoutPosts = posts.length;
    await page.waitForFunction(() => /后台/.test(document.querySelector('.native-async-question-countdown').textContent), null, {timeout: 9000});
    assert.equal(posts.length, beforeTimeoutPosts, 'browser countdown must never submit an answer');
    const timed = snapshot.turns[0].items[0].questions[0].request;
    timed.state = 'answered'; timed.answer_source = 'deadline'; timed.deadline_at = null;
    snapshot.turns[0].commands.push({id: 'async-auto', kind: 'steer', state: 'accepted', origin: 'system', text: '推荐方案', created_at: new Date().toISOString()});
    await notifyUpdate();
    assert.match(await page.textContent('.native-async-question-receipt'), /已按推荐项自动继续/);
    assert.match(await page.textContent('#conversation-messages'), /系统/);
    for (const width of [360, 390, 430]) {
      await page.setViewportSize({width, height: 844});
      for (const theme of ['dark', 'light']) {
        await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `horizontal overflow ${width}/${theme}`);
      }
    }
    snapshot.turns[0].items = [Object.assign({}, snapshot.turns[0].items[0], {questions: null, text: 'Codex 正在继续核对两份记录。'})];
    await notifyUpdate();
    await page.locator('.native-async-message').waitFor();
    assert.equal(await page.locator('.native-async-question-card').count(), 0, 'missing structured field must not invent a question card');
    assert.match(await page.textContent('.native-async-message'), /继续核对/);
    assert.equal(await page.locator('.native-final-mark').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: async native questions preserve layout, server-owned deadline display, manual durable reply, system auto receipt, focus, fallback, themes and narrow screens.');
  } finally {
    await browser.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
