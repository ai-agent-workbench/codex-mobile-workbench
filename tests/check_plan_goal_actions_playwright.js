const assert = require('assert/strict');
const { chromium } = require('playwright');
const childProcess = require('child_process');
const crypto = require('crypto');
const http = require('http');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');
const rendered = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv', 'bin', 'python')), [path.join(__dirname, 'render_conversation_ui_fixtures.py')], { cwd: root, encoding: 'utf8' }));
const snapshot = JSON.parse(rendered.conversation.match(/id="conversation-initial-snapshot"[^>]*>([\s\S]*?)<\/script>/)[1]);
snapshot.conversation.settings.collaboration_mode = 'plan';
const turn = snapshot.turns[0];
const plan = { native_item_id: 'plan-ui', kind: 'plan', status: 'completed', text: '修改界面并完成定向验证。', plan_digest: crypto.createHash('sha256').update('修改界面并完成定向验证。').digest('hex'), created_at: '2026-08-24T11:58:30Z', updated_at: '2026-08-24T11:58:30Z' };
turn.items.unshift(plan);
snapshot.goal = { objective: '这是一个用于窄屏验证的很长目标，确保摘要不会横向溢出并且只贴在最新回复下方。', status: 'active', version: '1', timeUsedSeconds: 61, tokensUsed: 1234, tokenBudget: 8000 };
snapshot.goal_sync_state = 'synced';
snapshot.turns.push({ id: 'auto-ui', ordinal: 2, status: 'inProgress', items: [
  { native_item_id: 'turn-plan', kind: 'plan', status: 'completed', text: '内部进度步骤', plan_digest: '0'.repeat(64) },
  { native_item_id: 'old-auto-plan', kind: 'plan', status: 'completed', text: '已经过期的来源', is_current_source: false, plan_digest: '1'.repeat(64) }
], commands: [{ kind: 'goal_continue', origin: 'system' }] });
snapshot.conversation.state = 'active'; snapshot.conversation.active_turn_id = 'auto-ui';
let planPosts = 0, startPosts = 0, goalPosts = 0, actionGets = 0, startMode = 'accepted';
let settingsPosts = 0, settingsFail = false, reconcileFail = false;
const receipts = new Map();
function response(res, value, status = 200) { res.statusCode = status; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); }
function form(req) { return new Promise(resolve => { let raw = ''; req.on('data', c => raw += c); req.on('end', async () => resolve(await new Response(raw, { headers: { 'content-type': req.headers['content-type'] } }).formData())); }); }
function document(body) { return '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/css/app.css"><main>' + body + '</main><script src="/static/draft.js" defer></script><script src="/static/setting-segments.js" defer></script><script src="/static/light-dismiss.js" defer></script>'; }
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/conversation' || url.pathname === '/legacy') {
    let body = rendered.conversation.replace(/(id="conversation-initial-snapshot"[^>]*>)[\s\S]*?(<\/script>)/, '$1' + JSON.stringify(snapshot) + '$2');
    if (url.pathname === '/legacy') body = body.replace(/<dialog[^>]*id="conversation-option-sheet"[\s\S]*?<\/dialog>/, '');
    return res.end(document(body));
  }
  if (url.pathname === '/conversation/conversation-ui/snapshot' && req.method === 'GET') return response(res, snapshot);
  if (url.pathname === '/conversation/conversation-ui/goal' && req.method === 'GET') return response(res, { snapshot });
  if (url.pathname === '/conversation/conversation-ui/reconcile' && req.method === 'POST') { if (reconcileFail) return response(res, { error: '暂时无法核对' }, 503); snapshot.conversation.state = 'idle'; return response(res, snapshot); }
  if (url.pathname === '/conversation/conversation-ui/settings' && req.method === 'POST') {
    settingsPosts++; const fields = await form(req); await new Promise(resolve => setTimeout(resolve, 500));
    if (settingsFail) return response(res, { error: '设置保存失败' }, 409);
    for (const key of ['permission', 'collaboration_mode']) if (fields.has(key)) snapshot.conversation.settings[key] = fields.get(key);
    return response(res, snapshot);
  }
  if (url.pathname.startsWith('/conversation/conversation-ui/actions/') && req.method === 'GET') { actionGets++; const command = receipts.get(url.pathname.split('/').pop()); return command ? response(res, { snapshot, command }) : response(res, { error: '收据不存在' }, 404); }
  if (url.pathname === '/conversation/conversation-ui/plan/implement' && req.method === 'POST') {
    planPosts++; const fields = await form(req); assert.equal(fields.get('turn_id'), turn.id); assert.equal(fields.get('item_id'), plan.native_item_id); assert.equal(fields.get('plan_digest'), plan.plan_digest); const command = { id: fields.get('command_id'), state: 'accepted' }; receipts.set(command.id, command); snapshot.conversation.settings.collaboration_mode = 'default'; return response(res, { snapshot, command });
  }
  if (url.pathname === '/conversation/conversation-ui/goal/start' && req.method === 'POST') {
    startPosts++; const fields = await form(req);
    if (startMode === 'missing') return response(res, { error: '目标启动请求未写入收据' }, 400);
    const command = { id: fields.get('command_id'), state: startMode === 'unknown' ? 'delivery_unknown' : startMode === 'activation_rejected' ? 'rejected' : 'accepted' };
    receipts.set(command.id, command);
    if (startMode === 'unknown') return response(res, { snapshot, command }, 503);
    if (startMode === 'activation_rejected') { command.message_state = 'accepted'; command.kind = 'goal_start'; snapshot.goal = null; snapshot.goal_draft = { objective: fields.get('objective'), status: 'paused', revision: String(startPosts) }; snapshot.goal_sync_state = 'unknown'; return response(res, { snapshot, command, error: '首轮消息已送达，但目标启用被拒绝' }, 409); }
    snapshot.goal = { objective: fields.get('objective'), status: 'active', version: String(startPosts) }; snapshot.goal_draft = null; snapshot.goal_sync_state = 'synced'; return response(res, { snapshot, command });
  }
  if (url.pathname === '/conversation/conversation-ui/goal' && req.method === 'POST') {
    goalPosts++; const fields = await form(req); const command = { id: fields.get('command_id'), state: 'accepted' }; receipts.set(command.id, command); const action = fields.get('action'); if (action === 'clear') snapshot.goal = null; else if (snapshot.goal) snapshot.goal.status = action === 'pause' ? 'paused' : action === 'resume' ? 'active' : (fields.get('status') || 'active'); return response(res, { snapshot, command });
  }
  if (url.pathname.startsWith('/static/')) { const file = path.join(root, url.pathname); if (file.startsWith(path.join(root, 'static')) && fs.existsSync(file)) return res.end(fs.readFileSync(file)); }
  res.statusCode = 404; res.end();
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 320, height: 720 } });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.goto(`http://127.0.0.1:${server.address().port}/conversation`, { waitUntil: 'domcontentloaded' }); await page.waitForTimeout(500);
    assert.equal(await page.locator('.native-plan-implement').count(), 1, 'Goal 已自动开始下一轮，上一份有效 Plan 仍可确认');
    assert.equal(await page.locator('.native-plan-implement').textContent(), '确认并实施', 'UTF-8 按钮文案');
    assert.equal(await page.locator('.native-goal-status').count(), 1, 'Goal 仅出现一个');
    assert.equal(await page.locator('.native-goal-status').evaluate(e => e.scrollWidth <= e.clientWidth + 1), true, '长目标卡片窄屏不横溢');
    assert.match(await page.locator('.native-goal-status > .native-goal-usage').textContent(), /累计 1分1秒.*1.23K tokens.*8K tokens/, '收起时直接可见真实累计耗时/用量');
    await page.locator('.native-goal-summary').click();
    assert.equal(await page.locator('.native-goal-usage-exact').textContent(), '累计 61秒 · 已用 1,234 tokens · 预算 8,000 tokens');
    const originalGoal = { ...snapshot.goal };
    const usageCases = [
      [0, 0, '0秒', '0'], [59, 999, '59秒', '999'],
      [60, 1000, '1分0秒', '1K'], [3599, 999999, '59分59秒', '1M'],
      [3600, 1000000, '1小时0分', '1M'], [23633, 2590087, '6小时33分', '2.59M'],
      [86399, 999999999, '23小时59分', '1B'], [86400, 1000000000, '1天0小时', '1B'],
      [90061, 1234567890, '1天1小时', '1.23B']
    ];
    for (const [seconds, tokens, timeLabel, tokenLabel] of usageCases) {
      snapshot.goal = { ...originalGoal, timeUsedSeconds: seconds, tokensUsed: tokens, tokenBudget: tokens };
      await page.reload({ waitUntil: 'domcontentloaded' });
      assert.equal(await page.locator('.native-goal-status > .native-goal-usage').textContent(), `累计 ${timeLabel} · 已用 ${tokenLabel} tokens · 预算 ${tokenLabel} tokens`);
      await page.locator('.native-goal-summary').click();
      assert.equal(await page.locator('.native-goal-usage-exact').textContent(), `累计 ${seconds.toLocaleString()}秒 · 已用 ${tokens.toLocaleString()} tokens · 预算 ${tokens.toLocaleString()} tokens`);
      assert.equal(await page.locator('.native-goal-status').evaluate(e => e.scrollWidth <= e.clientWidth + 1), true, '计量及精确值在320px窄屏不横溢');
    }
    for (const value of [null, undefined, -1, 'invalid']) {
      snapshot.goal = { ...originalGoal, timeUsedSeconds: value, tokensUsed: value, tokenBudget: value };
      await page.reload({ waitUntil: 'domcontentloaded' });
      assert.equal(await page.locator('.native-goal-usage, .native-goal-usage-exact').count(), 0, '缺失或无效计量不伪造为零');
    }
    snapshot.goal = originalGoal;
    await page.reload({ waitUntil: 'domcontentloaded' });
    snapshot.goal = { ...originalGoal, timeUsedSeconds: 23633, tokensUsed: 2590087 };
    await page.locator('#reconcile-conversation').click();
    await page.waitForTimeout(80);
    assert.match(await page.locator('.native-goal-status > .native-goal-usage').textContent(), /累计 6小时33分 · 已用 2.59M tokens/, '仅用量改变时摘要也实时更新单位');
    assert.equal(await page.locator('.native-goal-usage-exact').isVisible(), false, '精确值默认折叠');
    snapshot.goal = originalGoal;
    await page.locator('#reconcile-conversation').click();
    await page.waitForTimeout(80);
    // A settings-only snapshot must invalidate Plan controls even when all
    // timeline, Goal and state fields stay byte-for-byte unchanged.
    snapshot.conversation.settings.collaboration_mode = 'default';
    await page.locator('#reconcile-conversation').click(); await page.waitForTimeout(80);
    assert.equal(await page.locator('.native-plan-implement').count(), 0, '切到 Default 立即移除旧确认按钮');
    snapshot.conversation.settings.collaboration_mode = 'plan';
    await page.locator('#reconcile-conversation').click(); await page.waitForTimeout(80);
    assert.equal(await page.locator('.native-plan-implement').count(), 1, '切回 Plan 立即恢复有效计划确认按钮');
    await page.locator('#conversation-permission').evaluate(e => { e.value = 'read-only'; e.dispatchEvent(new Event('change', { bubbles: true })); });
    await page.locator('[data-close-settings]').evaluate(e => e.click()); await page.waitForTimeout(70);
    assert.equal(settingsPosts, 1);
    await page.locator('.native-plan-implement').evaluate(e => { e.click(); e.click(); }); await page.waitForTimeout(100);
    assert.equal(planPosts, 0, '设置仍在保存时不能用旧高权限确认计划'); await page.waitForTimeout(500);
    assert.equal(planPosts, 1, '确认 Plan 不得双 POST'); assert.equal(snapshot.conversation.settings.collaboration_mode, 'default');
    assert.equal(snapshot.conversation.settings.permission, 'read-only', '确认实施沿用已保存的新权限');
    assert.equal(await page.locator('.native-plan-implement').count(), 0, '实施后旧 Plan 不再可确认');
    snapshot.turns.pop(); snapshot.conversation.state = 'idle'; snapshot.conversation.active_turn_id = null;
    // Goal-only 更新不依赖正文变动，且卡片迁移到新的正式回复。
    snapshot.conversation.settings.collaboration_mode = 'default'; snapshot.goal.status = 'paused'; snapshot.conversation.revision++;
    await page.locator('#reconcile-conversation').click(); await page.waitForTimeout(80);
    assert.equal(await page.locator('.native-goal-status').getAttribute('data-goal-status'), 'paused', 'Goal-only 状态更新');
    const second = JSON.parse(JSON.stringify(turn)); second.id = 'turn-new'; second.ordinal = 2; second.items = [{ native_item_id: 'msg-new', kind: 'agent_message', status: 'completed', phase: 'final_answer', text: '新的正式回复。', created_at: '2026-08-24T12:00:00Z', updated_at: '2026-08-24T12:00:00Z' }]; second.final_text = '新的正式回复。'; snapshot.turns.push(second); snapshot.conversation.revision++;
    await page.locator('#reconcile-conversation').click(); await page.waitForTimeout(80);
    assert.equal(await page.locator('.native-goal-status').count(), 1, '新回复出现时旧 Goal 条移除');
    assert.equal(await page.locator('.native-message').filter({ hasText: '新的正式回复。' }).locator('.native-goal-status').count(), 1, 'Goal 贴在最新正式回复下');
    const failed = { id: 'turn-failed', ordinal: 3, status: 'failed', items: [], commands: [], error: { message: '测试失败状态' }, completed_at: '2026-08-24T12:01:00Z' };
    snapshot.turns.push(failed); snapshot.conversation.revision++;
    await page.locator('#reconcile-conversation').click(); await page.waitForTimeout(80);
    assert.equal(await page.locator('.native-message.agent').last().locator('.native-goal-status').count(), 1, '失败也是最新状态，目标条不留在历史回复');
    assert.equal(await page.locator('.native-goal-status').count(), 1, '失败状态也仅显示一个目标条');
    await page.locator('.native-goal-summary').click(); await page.locator('[data-goal-quick=resume]').click(); await page.waitForTimeout(60);
    assert.equal(snapshot.goal.status, 'active', '卡片直接继续目标');
    assert.equal(await page.locator('.native-goal-summary').getAttribute('aria-expanded'), 'true', 'Goal 更新保留已展开详情');
    assert.equal(await page.locator('.native-goal-details').isVisible(), true);
    await page.locator('[data-goal-quick=pause]').click(); await page.waitForTimeout(60);
    assert.equal(snapshot.goal.status, 'paused', '卡片直接暂停目标');
    assert.equal(await page.locator('.native-goal-summary').getAttribute('aria-expanded'), 'true', 'Goal 更新保留已展开详情');
    assert.equal(await page.locator('.native-goal-details').isVisible(), true);
    await page.locator('[data-goal-quick=clear]').click(); await page.waitForTimeout(60);
    assert.equal(await page.locator('.native-goal-status').count(), 0, '清除后状态条消失');
    settingsFail = true;
    await page.locator('#conversation-permission').evaluate(e => { e.value = 'danger-full-access'; e.dispatchEvent(new Event('change', { bubbles: true })); });
    await page.locator('[data-close-settings]').evaluate(e => e.click()); await page.waitForTimeout(60);
    await page.locator('textarea[name="text"]').fill('/goal 设置失败不得启动'); await page.locator('#conversation-composer').evaluate(e => e.requestSubmit()); await page.waitForTimeout(600);
    assert.equal(startPosts, 0, '等待中的设置保存失败不能发送 Goal 动作');
    assert.equal(await page.inputValue('textarea[name="text"]'), '/goal 设置失败不得启动'); settingsFail = false;
    await page.locator('textarea[name="text"]').fill('/goal 新的明确目标'); await page.locator('#conversation-composer').evaluate(e => e.requestSubmit()); await page.waitForTimeout(70);
    assert.equal(startPosts, 1, '/goal <objective> 只启动一次'); assert.equal(await page.inputValue('textarea[name="text"]'), '', '已确认目标命令清理自身草稿');
    startMode = 'unknown'; await page.locator('textarea[name="text"]').fill('/goal 不确定目标'); await page.locator('#conversation-composer').evaluate(e => e.requestSubmit()); await page.waitForTimeout(120);
    assert.equal(startPosts, 2, '不确定投递也只发一次 POST'); assert.equal(actionGets >= 1, true, '不确定投递只读核对'); assert.equal(await page.inputValue('textarea[name="text"]'), '/goal 不确定目标', '未知投递保留输入');
    await page.reload({ waitUntil: 'domcontentloaded' }); await page.waitForTimeout(180);
    assert.equal(startPosts, 2, '刷新不重发未知 Goal 请求');
    snapshot.conversation.state = 'needs_user'; snapshot.conversation.revision++;
    await page.locator('#reconcile-conversation').click(); await page.waitForTimeout(100);
    assert.equal(await page.locator('.native-action-check').count(), 1, 'needs_user 状态刷新不能删除未知操作恢复入口');
    reconcileFail = true; await page.locator('.native-action-check').click(); await page.waitForTimeout(100);
    assert.equal(await page.locator('.native-action-check').count(), 1, '核对失败后入口仍在');
    assert.equal(await page.locator('.native-action-check').isEnabled(), true);
    reconcileFail = false;
    await page.locator('.native-action-check').click(); await page.waitForTimeout(120);
    startMode = 'missing'; await page.locator('textarea[name="text"]').fill('/goal 未建收据目标'); await page.locator('#conversation-composer').evaluate(e => e.requestSubmit());
    await page.locator('.native-action-check').waitFor({ state: 'visible' });
    assert.equal(startPosts, 3, '请求未建收据也不自动重试');
    assert.equal(await page.inputValue('textarea[name="text"]'), '/goal 未建收据目标', '404 收据保留输入');
    await page.locator('.native-action-check').click(); await page.waitForTimeout(120);
    startMode = 'activation_rejected'; await page.locator('textarea[name="text"]').fill('/goal 首轮成功但目标失败'); await page.locator('#conversation-composer').evaluate(e => e.requestSubmit()); await page.waitForTimeout(120);
    assert.equal(startPosts, 4); assert.equal(await page.inputValue('textarea[name="text"]'), '/goal 首轮成功但目标失败', '首轮成功不能误清目标失败输入');
    startMode = 'accepted'; await page.locator('.native-goal-summary').click();
    await page.locator('[data-goal-quick=resume]').click(); await page.waitForTimeout(100);
    assert.equal(startPosts, 5, '启用失败的目标草稿，明确继续时携带完整目标而非恢复不存在的原生Goal');
    assert.equal(snapshot.goal.objective, '首轮成功但目标失败');
    assert.equal(await page.inputValue('textarea[name="text"]'), '/goal 首轮成功但目标失败', '按钮恢复不能清理用户仍保留的输入');
    const legacy = await browser.newPage({ viewport: { width: 360, height: 720 } }); legacy.on('pageerror', error => errors.push(error.message));
    await legacy.goto(`http://127.0.0.1:${server.address().port}/legacy`, { waitUntil: 'domcontentloaded' }); await legacy.waitForTimeout(200);
    await legacy.locator('.native-goal-summary').click();
    assert.equal(await legacy.locator('[data-goal-quick=edit]').count(), 0, '没有选项面板的兼容页面不提供损坏编辑按钮');
    await legacy.close(); assert.deepEqual(errors, [], '页面无 JS 异常');
    console.log('plan_goal_actions=passed plan_single_post=true goal_latest_only=true goal_unknown_no_replay=true receipt404_recoverable=true first_message_not_goal_success=true legacy_goal_safe=true');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
