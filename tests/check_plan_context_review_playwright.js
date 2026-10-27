const assert = require('assert/strict');
const { chromium } = require('playwright');
const childProcess = require('child_process');
const crypto = require('crypto');
const http = require('http');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');
const rendered = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv', 'bin', 'python')), [path.join(__dirname, 'render_conversation_ui_fixtures.py')], { cwd: root, encoding: 'utf8' }));
const initial = JSON.parse(rendered.conversation.match(/id="conversation-initial-snapshot"[^>]*>([\s\S]*?)<\/script>/)[1]);
const text = '# 实施计划\n保留现有能力，完成回归，不直接上线。\n' + '完整计划正文。'.repeat(100);
const plan = { native_item_id: 'review-plan', kind: 'plan', status: 'completed', text, plan_digest: crypto.createHash('sha256').update(text).digest('hex'), created_at: '2026-08-24T11:58:30Z', updated_at: '2026-08-24T11:58:30Z' };
const supplements = [
  { id: 'go-1', text: '去做吧', attachments: [] },
  { id: 'extra-2', text: '保留原有数据，而且不直接上线。', attachments: [] },
  { id: 'question-3', text: '？ <img src=x onerror="window.unsafePlan=true">', attachments: [{ id: 'safe-file', name: '补充.txt', size: 4, preview_kind: 'text', content_url: '/conversation/attachments/content/safe-file?conversation_id=conversation-ui', download_url: '/conversation/attachments/content/safe-file?conversation_id=conversation-ui&download=1' }] }
];
let snapshot, posts = 0, gets = 0, responseMode = 'accepted', lastFields;
const receipts = new Map();
function reset() {
  snapshot = structuredClone(initial);
  snapshot.goal = null; snapshot.goal_sync_state = 'synced'; snapshot.queue = [];
  snapshot.conversation.settings.collaboration_mode = 'plan'; snapshot.conversation.state = 'idle'; snapshot.conversation.active_turn_id = null;
  snapshot.turns[0].items.unshift(plan);
  snapshot.turns.push({ id: 'ordinary-after-plan', ordinal: 2, status: 'completed', kind: 'model', started_at: '2026-08-24T12:01:00Z', completed_at: '2026-08-24T12:02:00Z', commands: supplements.map((x, i) => ({ ...x, kind: 'steer', state: 'completed', origin: 'user', result_turn_id: 'ordinary-after-plan', created_at: `2026-08-24T12:01:0${i}Z` })), items: [{ native_item_id: 'ordinary-final', kind: 'agent_message', phase: 'final_answer', status: 'completed', text: '已经授权，但仍处于 Plan。', created_at: '2026-08-24T12:02:00Z' }], artifacts: [] });
  snapshot.plan_confirmation = { state: 'review', turn_id: snapshot.turns[0].id, item_id: plan.native_item_id, plan_digest: plan.plan_digest, context_digest: 'a'.repeat(64), text, supplements: structuredClone(supplements), reason: null };
}
function json(res, value, status = 200) { res.statusCode = status; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); }
function fields(req) { return new Promise(resolve => { let raw = ''; req.on('data', c => raw += c); req.on('end', async () => resolve(await new Response(raw, { headers: { 'content-type': req.headers['content-type'] } }).formData())); }); }
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/conversation') {
    const body = rendered.conversation.replace(/(id="conversation-initial-snapshot"[^>]*>)[\s\S]*?(<\/script>)/, '$1' + JSON.stringify(snapshot) + '$2');
    return res.end('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/css/app.css"><main>' + body + '</main><script src="/static/draft.js" defer></script><script src="/static/setting-segments.js" defer></script><script src="/static/light-dismiss.js" defer></script>');
  }
  if (url.pathname.endsWith('/snapshot') || url.pathname.endsWith('/reconcile')) return json(res, snapshot);
  if (url.pathname.includes('/actions/')) { gets++; return json(res, { snapshot, command: receipts.get(url.pathname.split('/').pop()) }); }
  if (url.pathname.endsWith('/plan/implement') && req.method === 'POST') {
    posts++; lastFields = await fields(req);
    assert.equal(lastFields.get('turn_id'), snapshot.plan_confirmation.turn_id);
    assert.equal(lastFields.get('item_id'), plan.native_item_id);
    assert.equal(lastFields.get('plan_digest'), plan.plan_digest);
    assert.equal(lastFields.get('context_digest'), snapshot.plan_confirmation.context_digest);
    const command = { id: lastFields.get('command_id'), state: responseMode };
    receipts.set(command.id, command);
    await new Promise(resolve => setTimeout(resolve, 100));
    if (responseMode === 'delivery_unknown') return json(res, { snapshot, command, error: '投递结果尚待核对' }, 503);
    snapshot.conversation.settings.collaboration_mode = 'default';
    return json(res, { snapshot, command });
  }
  if (url.pathname.startsWith('/static/')) {
    const file = path.join(root, url.pathname);
    if (file.startsWith(path.join(root, 'static')) && fs.existsSync(file)) return res.end(fs.readFileSync(file));
  }
  res.statusCode = 404; res.end();
});
(async () => {
  reset(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const errors = [];
  try {
    for (const width of [320, 360, 412, 430]) {
      reset(); const context = await browser.newContext({ viewport: { width, height: 844 } }); const page = await context.newPage();
      page.on('pageerror', e => errors.push(e.message));
      await page.goto(`http://127.0.0.1:${server.address().port}/conversation`);
      assert.equal(await page.locator('.native-plan-implement').count(), 0, '旧计划不能保留无上下文的直接实施按钮');
      assert.equal(await page.locator('.native-plan-review-entry').count(), 1, '后续普通回复仍有实施入口');
      assert.equal(await page.locator('.native-message.agent').last().locator('.native-plan-review-entry').count(), 1, '入口只在最新回复下');
      await page.locator('textarea[name=text]').fill('未发送草稿要保留');
      await page.getByRole('button', { name: '核对计划并实施', exact: true }).click();
      assert.equal(await page.locator('.native-plan-review-text').first().textContent(), text, '核对完整固定版本，不截断');
      assert.equal(await page.locator('.native-plan-review-supplement').count(), 3);
      assert.equal(await page.locator('.native-plan-review-supplement').nth(1).locator('pre').textContent(), supplements[1].text);
      assert.equal(await page.locator('.native-plan-review-content img').count(), 0, '补充文本不解析为不可信HTML');
      assert.equal(await page.evaluate(() => window.unsafePlan), undefined);
      assert.equal(await page.locator('#conversation-plan-review').evaluate(e => e.scrollWidth <= e.clientWidth + 1), true, '核对面板无横向溢出');
      assert.equal(await page.locator('#conversation-plan-review-confirm').isEnabled(), true);
      if (width === 412 && process.env.CODEX_WEB_UI_OUTPUT_DIR) await page.screenshot({ path: path.join(process.env.CODEX_WEB_UI_OUTPUT_DIR, 'review-412.png') });
      if (width === 320) {
        snapshot.plan_confirmation.context_digest = 'b'.repeat(64);
        snapshot.plan_confirmation.supplements.push({ id: 'extra-4', text: '还有新的补充', attachments: [] });
        await page.locator('#reconcile-conversation').evaluate(e => e.click()); await page.waitForTimeout(100);
        assert.equal(await page.locator('#conversation-plan-review-confirm').isDisabled(), true, '核对期间消息更新立即失效');
        assert.match(await page.locator('.native-plan-review-status').textContent(), /重新核对/);
        assert.equal(posts, 0, '更新不会自动实施');
        await page.locator('.native-plan-review-head button').click();
        await page.getByRole('button', { name: '核对计划并实施', exact: true }).click();
        assert.equal(await page.locator('.native-plan-review-supplement').count(), 4);
        const permission = snapshot.conversation.settings.permission;
        await page.locator('#conversation-plan-review-confirm').evaluate(e => { e.click(); e.click(); }); await page.waitForTimeout(300);
        assert.equal(posts, 1, '双击只提交一个实施动作');
        assert.equal(lastFields.get('context_digest'), 'b'.repeat(64));
        assert.equal(snapshot.conversation.settings.collaboration_mode, 'default');
        assert.equal(snapshot.conversation.settings.permission, permission, '不改变权限');
        assert.equal(await page.locator('#conversation-plan-review').evaluate(e => e.open), false);
        assert.equal(await page.locator('.native-plan-review-entry').count(), 0);
        assert.equal(await page.inputValue('textarea[name=text]'), '未发送草稿要保留');
      }
      await context.close();
    }
    reset(); const page = await browser.newPage({ viewport: { width: 360, height: 800 } });
    page.on('pageerror', e => errors.push(e.message));
    snapshot.plan_confirmation.state = 'blocked'; snapshot.plan_confirmation.reason = 'ordinary_input_not_completed'; delete snapshot.plan_confirmation.context_digest;
    await page.goto(`http://127.0.0.1:${server.address().port}/conversation`);
    assert.match(await page.locator('.native-plan-review-entry').textContent(), /当前回复尚未结束/);
    await page.getByRole('button', { name: '查看计划', exact: true }).click();
    assert.equal(await page.locator('#conversation-plan-review-confirm').isDisabled(), true, '活动普通轮不越过补充实施');
    snapshot.conversation.settings.collaboration_mode = 'default';
    await page.locator('#reconcile-conversation').evaluate(e => e.click()); await page.waitForTimeout(100);
    assert.equal(await page.locator('.native-plan-review-entry').count(), 0, '模式更新移除入口');
    assert.equal(await page.locator('#conversation-plan-review-confirm').isDisabled(), true);
    await page.locator('.native-plan-review-head button').click();
    reset(); snapshot.turns.pop(); snapshot.plan_confirmation.state = 'direct'; snapshot.plan_confirmation.supplements = []; delete snapshot.plan_confirmation.context_digest;
    await page.reload(); assert.equal(await page.locator('.native-plan-implement').count(), 1, '服务器direct状态保留原一键实施');
    reset(); responseMode = 'delivery_unknown'; await page.reload();
    await page.getByRole('button', { name: '核对计划并实施', exact: true }).click(); await page.locator('#conversation-plan-review-confirm').click(); await page.waitForTimeout(400);
    assert.equal(posts, 2, '未知投递只提交一次'); assert.ok(gets >= 1, '未知结果只读核对');
    assert.equal(await page.locator('#conversation-plan-review-confirm').isDisabled(), true);
    await page.reload(); await page.waitForTimeout(200); assert.equal(posts, 2, '重载不重发未知实施');
    assert.deepEqual(errors, []);
    console.log('plan_context_review=passed widths=320,360,412,430 full_context=true stale_disabled=true single_post=true unknown_no_replay=true');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exitCode = 1; });
