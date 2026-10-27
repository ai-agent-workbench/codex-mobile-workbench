const { chromium } = require('playwright');
const crypto = require('crypto');
const childProcess = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const css = fs.readFileSync(path.join(root, 'static/css/app.css'));
const js = fs.readFileSync(path.join(root, 'static/conversation.js'));
const reconnectCoordinatorJs = fs.readFileSync(path.join(root, 'static/reconnect-coordinator.js'));
const lightDismissJs = fs.readFileSync(path.join(root, 'static/light-dismiss.js'));
const heartbeatTestJs = Buffer.from(js.toString()
  .replace('var socketHeartbeatInterval = 15000;', 'var socketHeartbeatInterval = 250;')
  .replace('var socketStaleAfter = 45000;', 'var socketStaleAfter = 3000;'));
if (heartbeatTestJs.equals(js)) throw new Error('heartbeat test constants were not replaced');
const enhancementsJs = fs.readFileSync(path.join(root, 'static/conversation-enhancements.js'));
const roundNavigationJs = fs.readFileSync(path.join(root, 'static/conversation-round-navigation.js'));
const ttsJs = fs.readFileSync(path.join(root, 'static/tts.js'));
const conversationTtsJs = fs.readFileSync(path.join(root, 'static/conversation-tts.js'));
const ttsSettingsJs = fs.readFileSync(path.join(root, 'static/tts-settings.js'));
const completionSoundJs = fs.readFileSync(path.join(root, 'static/completion-sound.js'));
const completionSoundSettingsJs = fs.readFileSync(path.join(root, 'static/completion-sound-settings.js'));
const draftJs = fs.readFileSync(path.join(root, 'static/draft.js'));
const projectJs = fs.readFileSync(path.join(root, 'static/project-conversation.js'));
const messageAttachmentsJs = fs.readFileSync(path.join(root, 'static/message-attachments.js'));
const settingSegmentsJs = fs.readFileSync(path.join(root, 'static/setting-segments.js'));
const image = fs.readFileSync(path.join(root, 'static/icon-512.png'));
const rendered = JSON.parse(childProcess.execFileSync(
  (process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')),
  [path.join(__dirname, 'render_conversation_ui_fixtures.py')],
  { cwd: root, encoding: 'utf8' },
));
const htmlArtifactViewer = rendered.html_artifact_viewer;
let scene = 'artifacts';
// Scenario changes model newer durable state, never a revision rollback.
let fixtureRevision = 7;
let responseMode = 'success';
let snapshotFailures = 0;
let abortedSnapshotRequests = 0;
let enhancementFailures = 0;
let silenceNextSocket = false;
let projectSubmission = null;
const messageSubmissions = [];
let currentServiceTier = 'priority';
const serviceTierSubmissions = [];
const settingsSubmissions = [];
const delayedSettingsResponses = [];
const delayedMessageResponses = [];
const receiptOverrides = Object.create(null);
let timelineBound = false;
const entryPrefix = 'a'.repeat(40);
const seenPaths = [];
const stopSubmissions = [];
const enhancementRequests = [];
const websocketPaths = [];
const websocketClients = new Set();
const websocketClientMessages = [];
const runningStartedAt = new Date(Date.now() - 65000).toISOString();

function websocketFrame(value) {
  const payload = Buffer.from(value);
  if (payload.length < 126) {
    return Buffer.concat([Buffer.from([0x81, payload.length]), payload]);
  }
  if (payload.length < 65536) {
    const header = Buffer.alloc(4);
    header[0] = 0x81; header[1] = 126; header.writeUInt16BE(payload.length, 2);
    return Buffer.concat([header, payload]);
  }
  throw new Error('fixture websocket payload is too large');
}

function websocketCloseFrame(code) {
  const payload = Buffer.alloc(2);
  payload.writeUInt16BE(code);
  return Buffer.concat([Buffer.from([0x88, payload.length]), payload]);
}

function consumeClientFrames(socket, chunk) {
  socket.clientFrameBuffer = Buffer.concat([
    socket.clientFrameBuffer || Buffer.alloc(0), chunk,
  ]);
  while (socket.clientFrameBuffer.length >= 2) {
    const first = socket.clientFrameBuffer[0];
    const second = socket.clientFrameBuffer[1];
    let length = second & 0x7f;
    let offset = 2;
    if (length === 126) {
      if (socket.clientFrameBuffer.length < 4) return;
      length = socket.clientFrameBuffer.readUInt16BE(2); offset = 4;
    } else if (length === 127) {
      throw new Error('fixture does not accept 64-bit websocket frames');
    }
    const masked = Boolean(second & 0x80);
    if (!masked || socket.clientFrameBuffer.length < offset + 4 + length) return;
    const mask = socket.clientFrameBuffer.subarray(offset, offset + 4);
    offset += 4;
    const payload = Buffer.from(socket.clientFrameBuffer.subarray(offset, offset + length));
    for (let index = 0; index < payload.length; index += 1) payload[index] ^= mask[index % 4];
    socket.clientFrameBuffer = socket.clientFrameBuffer.subarray(offset + length);
    const opcode = first & 0x0f;
    if (opcode === 1) {
      const value = payload.toString();
      websocketClientMessages.push(value);
      if (value === 'ping' && !socket.ignoreHeartbeatPongs &&
          !socket.destroyed && !socket.writableEnded) {
        socket.write(websocketFrame(JSON.stringify({ type: 'pong' })));
      }
    } else if (opcode === 8) {
      socket.end();
      return;
    }
  }
}

function conversation(state, threadId, activeTurnId) {
  const value = {
    id: 'conversation-ui', thread_id: threadId, title: '手机产物与 App Server 对话',
    project_id: null, state, active_turn_id: activeTurnId,
    thread_status: { type: state === 'active' ? 'active' : 'idle' },
    settings: { model: 'gpt-5.6-sol', effort: 'ultra', permission: 'danger-full-access', service_tier: currentServiceTier },
    recovery_state: null, connection_state: 'connected', completion_notification: true,
    pinned: false, client_origin: 'mobile_web', revision: fixtureRevision,
    context_compaction_completed_count: 2, context_compaction_manual_completed_count: 1,
    context_compaction_other_completed_count: 1, context_compaction_state: 'idle',
    context_switch_count: 2,
    context_switches: [{ observed_at: '2026-08-24T12:00:00Z', state: 'output_seen' }],
    activity_at: new Date().toISOString(), terminal_generation: state === 'idle' ? 1 : 0,
    last_terminal_turn_id: state === 'idle' ? 'turn-1' : null,
    last_terminal_at: state === 'idle' ? '2026-08-24T12:01:00Z' : null,
    usage: {
      context_used: 42000, context_limit: 100000, context_percent: 42,
      input_tokens: 51000, cached_input_tokens: 36000,
      output_tokens: 4200, reasoning_output_tokens: 1600,
      updated_at: new Date().toISOString(),
    },
    created_at: '2026-08-24T11:59:55Z', updated_at: new Date().toISOString(),
  };
  return value;
}

function artifact(id, name, previewKind, size, mimeType) {
  const value = {
    id, turn_id: 'turn-1', name, preview_kind: previewKind, size, mime_type: mimeType,
    content_url: `/conversation/conversation-ui/artifact/${id}`,
    download_url: `/conversation/conversation-ui/artifact/${id}?download=1`,
    created_at: '2026-08-24T12:00:00Z',
  };
  if (previewKind === 'html') value.preview_url = value.content_url + '/preview';
  return value;
}

function timelineSnapshot() {
  const queued = {
    id: 'timeline-followup', kind: 'steer', state: 'queued', text: '14:19 排队消息',
    created_at: '2026-09-04T06:19:37Z', updated_at: '2026-09-04T06:19:37Z',
  };
  const bound = { ...queued, state: 'accepted', result_turn_id: 'timeline-next' };
  const value = {
    schema_version: 4,
    conversation: { ...conversation('active', 'thread-timeline', 'timeline-active'),
      id: 'conversation-timeline', revision: timelineBound ? 2 : 1 },
    turns: [{
      id: 'timeline-active', ordinal: 2, status: 'inProgress',
      started_at: '2026-09-04T06:18:00Z', completed_at: null, final_text: null, error: null,
      commands: [{ id: 'timeline-start', kind: 'start', state: 'accepted', text: '14:18 首条消息',
        created_at: '2026-09-04T06:18:00Z' }],
      items: [{ native_item_id: 'timeline-commentary-1', kind: 'agent_message', status: 'completed',
        phase: 'commentary', text: '14:21 过程消息', created_at: '2026-09-04T06:21:48Z', updated_at: '2026-09-04T06:21:48Z' },
      { native_item_id: 'timeline-commentary-2', kind: 'agent_message', status: 'completed',
        phase: 'commentary', text: '14:25 过程消息', created_at: '2026-09-04T06:25:00Z', updated_at: '2026-09-04T06:25:00Z' }],
      artifacts: [],
    }].concat(timelineBound ? [{
      id: 'timeline-next', ordinal: 3, status: 'inProgress', started_at: '2026-09-04T06:55:56Z',
      completed_at: null, final_text: null, error: null, commands: [bound], items: [], artifacts: [],
    }] : []),
    messages: timelineBound ? [bound] : [queued], queue: timelineBound ? [] : [queued], artifacts: [],
  };
  return value;
}

function snapshot() {
  const value = rawSnapshot();
  value.capabilities = {conversation_tree_stop: scene !== 'legacy-running'};
  if (scene === 'sync-disconnected') value.conversation.connection_state = 'disconnected';
  if (scene === 'sync-needs-user') value.conversation.state = 'needs_user';
  if (scene.startsWith('artifact-failure')) {
    value.turns[0].artifact_publish_failed = true;
    if (scene === 'artifact-failure-only') value.turns[0].artifacts = [];
  }
  if (scene === 'unsafe-content') {
    const unsafe = '<img src="x" onerror="window.__unsafeMessageExecuted=true"> <script>window.__unsafeMessageExecuted=true</script> [恶意链接](javascript:alert(1))';
    value.turns[0].final_text = unsafe;
    value.turns[0].items.filter(item => item.phase === 'final_answer').forEach(item => { item.text = unsafe; });
    value.turns[0].commands[0].text = unsafe;
    value.turns[0].artifacts[0].source_path = '/private/fixture-secret-source';
    value.turns[0].artifacts[0].storage_relpath = 'fixture-secret-storage';
  }
  return value;
}

function rawSnapshot() {
  if (scene === 'plan-result' || scene === 'plan-streaming') {
    const running = scene === 'plan-streaming';
    return {
      schema_version: 4,
      conversation: conversation(running ? 'active' : 'idle', 'thread-plan', running ? 'turn-plan-result' : null),
      turns: [{ id: 'turn-plan-result', ordinal: 1, status: running ? 'inProgress' : 'completed',
        started_at: '2026-10-03T18:09:37Z', completed_at: running ? null : '2026-10-03T18:24:01Z',
        final_text: null, error: null, artifacts: [],
        commands: [{ id: 'plan-command', kind: 'start', state: 'completed', text: '那你制定计划吧',
          created_at: '2026-10-03T18:09:37Z' }],
        items: [
          { native_item_id: 'msg_plan_progress', kind: 'agent_message', phase: 'commentary',
            text: '我再核一下完整命令清单，避免漏掉后续组。', status: 'completed', created_at: '2026-10-03T18:19:26Z' },
          { native_item_id: 'native-plan-result', kind: 'plan', phase: null,
            text: '# 项目体验、维护性与性能治理计划\n\n全部72个现行命令纳入。\n\n' + '每组保留范围与验收标准。\n'.repeat(90) + '\n总任务完成标准：全部组验收。',
            status: running ? 'inProgress' : 'completed', created_at: '2026-10-03T18:22:34Z', updated_at: '2026-10-03T18:24:01Z' },
          { native_item_id: 'turn-plan', kind: 'plan', text: '内部执行清单不公开', status: 'inProgress' },
          { native_item_id: 'obsolete-plan', kind: 'plan', text: '已纠正旧计划不公开', is_current_source: false },
          { native_item_id: 'rs_plan', kind: 'reasoning_summary', text: '内部推理不公开' },
          { native_item_id: 'exec_plan', kind: 'command_execution', detail: '内部命令不公开' },
        ],
      }], messages: [], queue: [], artifacts: [],
    };
  }
  if (scene === 'new') return {
    schema_version: 4, conversation: conversation('new', null, null),
    turns: [], messages: [], queue: [], artifacts: [],
  };
  if (scene === 'failures') return {
    schema_version: 4, conversation: conversation('idle', 'thread-1', null),
    turns: [], messages: [
      { id: 'rejected-1', kind: 'steer', state: 'rejected', text: '明确没有发出的消息', created_at: '2026-08-24T12:02:00Z' },
      { id: 'unknown-1', kind: 'steer', state: 'delivery_unknown', text: '需要核对投递的消息', created_at: '2026-08-24T12:03:00Z' },
    ], queue: [], artifacts: [],
  };
  if (scene === 'ambiguous-stop' || scene === 'unconfirmed-stop' || scene === 'idle-unconfirmed-stop') {
    const value = conversation(scene === 'idle-unconfirmed-stop' ? 'idle' : 'needs_user', 'thread-1', null);
    value.recovery_state = scene === 'ambiguous-stop' ? 'ambiguous_native_state' : 'tree_stop_unconfirmed';
    value.thread_status = {type:'active'};
    return {schema_version:4, conversation:value, turns:[], messages:[], queue:[], artifacts:[],
      stop: scene !== 'ambiguous-stop' ? {status:'unconfirmed',threads:3,active_threads:1} : null};
  }
  if (scene === 'verified-stop-background') return {
    schema_version:4,conversation:conversation('idle','thread-1',null),
    turns:[],messages:[],queue:[],artifacts:[],
    stop:{status:'verified',threads:3,terminals:1,background_errors:0},
  };
  if (scene === 'interrupted') return {
    schema_version: 4, conversation: conversation('idle', 'thread-1', null),
    turns: [{
      id: 'turn-stopped', ordinal: 2, status: 'interrupted',
      started_at: '2026-08-24T12:04:00Z', completed_at: '2026-08-24T12:04:02Z',
      final_text: null, error: null,
      commands: [{ id: 'message-stopped', kind: 'start', state: 'completed',
        text: '继续处理这项任务', created_at: '2026-08-24T12:04:00Z' }],
      items: [{
        native_item_id: 'commentary-stopped', kind: 'agent_message', status: 'completed',
        phase: 'commentary', text: '停止前已经完成这一项核对。',
        created_at: '2026-08-24T12:04:01Z', updated_at: '2026-08-24T12:04:01Z',
      }], artifacts: [],
    }], messages: [], queue: [], artifacts: [],
  };
  if (scene === 'terminal-failed') return {
    schema_version: 4, conversation: conversation('idle', 'thread-1', null),
    turns: [{
      id: 'turn-1', ordinal: 2, status: 'failed',
      started_at: '2026-08-24T12:05:00Z', completed_at: '2026-08-24T12:06:00Z',
      final_text: null,
      error: {
        codexErrorInfo: 'serverOverloaded',
        message: 'Selected model is at capacity. Please try a different model.',
      },
      commands: [{ id: 'message-failed', kind: 'start', state: 'completed',
        text: '继续完成任务', created_at: '2026-08-24T12:05:00Z' }],
      items: [{
        native_item_id: 'commentary-failed', kind: 'agent_message', status: 'completed',
        phase: 'commentary', text: '已完成部分核对。',
        created_at: '2026-08-24T12:05:30Z', updated_at: '2026-08-24T12:05:30Z',
      }], artifacts: [],
    }], messages: [], queue: [], artifacts: [],
  };
  if (scene.startsWith('long-history')) {
    const oldText = (scene === 'long-history-redraw' ? '重绘新增正文。\n\n' : '') + Array.from({ length: 260 }, (_, index) =>
      `历史第 ${index + 1} 行：这段较长的答复用于验证手机端按实际高度保留一屏预览。`).join('\n') +
      '\n\n```log\n' + Array.from({ length: 90 }, (_, index) => `log ${index + 1}: retained full source`).join('\n') + '\n```';
    const newText = Array.from({ length: 140 }, (_, index) => `最新第 ${index + 1} 行：默认完整，仍可从顶部或底部手动收起。`).join('\n') + '\n\n```log\n' + 'latest log line\n'.repeat(90) + '```';
    return {
      schema_version: 4, conversation: conversation(scene === 'long-history-running' ? 'active' : 'idle', 'thread-long', scene === 'long-history-running' ? 'turn-long-new' : null),
      turns: [{ id: 'turn-long-old', ordinal: 1, status: 'completed',
        started_at: '2026-08-24T10:00:00Z', completed_at: '2026-08-24T10:02:00Z',
        final_text: oldText, error: null, commands: [{ id: 'long-user-old', kind: 'start', state: 'completed', text: '很长的历史提问。\n'.repeat(180), created_at: '2026-08-24T10:00:00Z' }], artifacts: [],
        items: [{ native_item_id: 'long-old-final', kind: 'agent_message', status: 'completed',
          phase: 'final_answer', text: oldText, created_at: '2026-08-24T10:02:00Z', updated_at: '2026-08-24T10:02:00Z' }] },
      { id: 'turn-long-new', ordinal: 2, status: scene === 'long-history-running' ? 'inProgress' : 'completed',
        started_at: '2026-08-24T10:03:00Z', completed_at: '2026-08-24T10:04:00Z',
        final_text: newText, error: null, commands: [{ id: 'long-user-new', kind: 'start', state: 'completed', text: '继续', created_at: '2026-08-24T10:03:00Z' }], artifacts: [],
        items: [{ native_item_id: 'long-new-final', kind: 'agent_message', status: 'completed',
          phase: 'final_answer', text: newText, created_at: '2026-08-24T10:04:00Z', updated_at: '2026-08-24T10:04:00Z' }] }],
      messages: [], queue: [], artifacts: [],
    };
  }
  const files = [
    artifact('image-1', '许昌十日温度趋势.png', 'image', 643210, 'image/png'),
    { ...artifact('text-1', '分析笔记.md', 'text', 4280, 'text/plain'),
      preview_text: '# 判断\n\n- 最低温呈下降趋势，早晚体感会先转凉\n- 远期预报仍有波动，建议第 7 天复查' },
    artifact('pdf-1', '许昌十日天气报告.pdf', 'pdf', 1840000, 'application/pdf'),
    artifact('word-1', '天气分析说明.docx', 'download', 286000, 'application/octet-stream'),
    artifact('excel-1', '逐日温度数据.xlsx', 'download', 92000, 'application/octet-stream'),
    artifact('html-1', '统一经营门户与首期看板_验收交互草图_v0.8_20260902.html', 'html', 4800, 'text/html'),
  ];
  const running = scene === 'running' || scene === 'legacy-running';
  const value = {
    schema_version: 4,
    conversation: conversation(running ? 'active' : 'idle', 'thread-1', running ? 'turn-1' : null),
    turns: [{
      id: 'turn-1', ordinal: 1, status: running ? 'inProgress' : 'completed',
      started_at: running ? runningStartedAt : '2026-08-24T12:00:00Z', completed_at: running ? null : '2026-08-24T12:01:00Z',
      final_text: running ? null : '结论是早晚转凉、白天仍热。产物已经放在这条回复下面。', error: null,
      commands: [
        { id: 'message-1', kind: 'start', state: running ? 'accepted' : 'completed',
          text: '分析未来十日天气，并生成图片和报告。', created_at: '2026-08-24T12:00:00Z' },
        { id: 'message-2', kind: 'steer', state: running ? 'accepted' : 'completed',
          text: '追问：请把清晨体感也说清楚。', created_at: '2026-08-24T12:00:25Z' },
      ],
      items: running ? [{
        native_item_id: 'reasoning-1', kind: 'reasoning_summary', status: 'inProgress',
        phase: null, text: '我先核对温度趋势，再把结论和产物整理成简洁答复。',
        created_at: '2026-08-24T12:00:10Z', updated_at: '2026-08-24T12:00:20Z',
      }, {
        native_item_id: 'commentary-1', kind: 'agent_message', status: 'inProgress',
        phase: 'commentary', text: '我先核对温度趋势。', created_at: '2026-08-24T12:00:20Z', updated_at: '2026-08-24T12:00:20Z',
      }, {
        native_item_id: 'commentary-2', kind: 'agent_message', status: 'inProgress',
        phase: 'commentary', text: '正在补充清晨体感并整理报告。', created_at: '2026-08-24T12:00:30Z', updated_at: '2026-08-24T12:00:30Z',
      }, {
        native_item_id: 'turn-plan', kind: 'plan', status: 'inProgress', phase: null,
        text: 'completed: 收集数据\ninProgress: 生成报告', updated_at: '2026-08-24T12:00:31Z',
      }, {
        native_item_id: 'tool-1', kind: 'command_execution', status: 'inProgress', phase: null,
        detail: 'python generate_report.py', updated_at: '2026-08-24T12:00:32Z',
      }] : [{
        native_item_id: 'commentary-1', kind: 'agent_message', status: 'completed',
        phase: 'commentary', text: '我先核对温度趋势。', created_at: '2026-08-24T12:00:20Z', updated_at: '2026-08-24T12:00:20Z',
      }, {
        native_item_id: 'reasoning-1', kind: 'reasoning_summary', status: 'completed',
        phase: null, text: '我先核对温度趋势，再把结论和产物整理成简洁答复。',
        updated_at: '2026-08-24T12:00:45Z',
      }, {
        native_item_id: 'tool-1', kind: 'command_execution', status: 'completed', phase: null,
        detail: 'python generate_report.py', updated_at: '2026-08-24T12:00:50Z',
      }, {
        native_item_id: 'commentary-2', kind: 'agent_message', status: 'completed',
        phase: 'commentary', text: '正在补充清晨体感并整理报告。', created_at: '2026-08-24T12:00:30Z', updated_at: '2026-08-24T12:00:30Z',
      }, {
        native_item_id: 'item-9', kind: 'agent_message', status: 'completed',
        phase: 'final_answer', text: '结论是早晚转凉、白天仍热。产物已经放在这条回复下面。\n\n| 功能 | 建议 |\n|---|---|\n| 跨对话提醒 | 实现 |\n\n```js\nalert("不应朗读")\n```\n\n代码后的正文应正常朗读。',
        created_at: '2026-08-24T12:01:00Z', updated_at: '2026-08-24T12:01:00Z',
      }, {
        native_item_id: 'msg_final', kind: 'agent_message', status: 'completed',
        phase: 'final_answer', text: '结论是早晚转凉、白天仍热。产物已经放在这条回复下面。\n\n| 功能 | 建议 |\n|---|---|\n| 跨对话提醒 | 实现 |\n\n```js\nalert("不应朗读")\n```\n\n代码后的正文应正常朗读。',
        created_at: '2026-08-24T12:00:59Z', updated_at: '2026-08-24T12:00:59Z',
      }],
      artifacts: running ? files.slice(0, 1) : files,
    }],
    messages: [], queue: [], artifacts: [],
  };
  const receiptState = {
    'receipt-accepted': 'accepted', 'receipt-completed': 'completed',
    'receipt-queued': 'queued', 'receipt-submitted': 'submitted',
    'receipt-unknown': 'delivery_unknown', 'receipt-rejected': 'rejected',
  }[scene];
  if (receiptState) value.messages.push({
    id: 'receipt-command', kind: 'steer', state: receiptState,
    text: '待核对消息', created_at: '2026-10-03T00:00:00Z',
  });
  Object.entries(receiptOverrides).filter(([, state]) => state !== 'missing').forEach(([id, state]) => value.messages.push({
    id, kind: 'steer', state, text: 'fixture delayed receipt', created_at: '2026-10-03T00:00:00Z',
  }));
  return value;
}

function snapshotWithReceipt(commandId, state) {
  const value = snapshot();
  value.messages = (value.messages || []).concat([{
    id: commandId, kind: 'steer', state, text: 'fixture receipt',
    created_at: '2026-10-03T00:00:00Z',
  }]);
  return value;
}

const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/app.css"><script src="/static/reconnect-coordinator.js" defer></script><script src="/static/completion-sound.js" defer></script></head><body><main>${rendered.conversation}</main><script src="/static/draft.js" defer></script><script src="/static/setting-segments.js" defer></script></body></html>`;
function htmlWithSnapshot(value) {
  return html.replace(
    /(<script id="conversation-initial-snapshot" type="application\/json">)[\s\S]*?(<\/script>)/,
    '$1' + JSON.stringify(value) + '$2',
  );
}
const heartbeatHtml = html.replace(
  '/static/conversation.js', '/static/conversation-heartbeat-test.js'
);
if (heartbeatHtml === html) throw new Error('heartbeat test page did not replace its script');

const projectHtml = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/app.css"></head><body><main>${rendered.project}</main><script src="/static/draft.js" defer></script><script src="/static/setting-segments.js" defer></script></body></html>`;

const projectCatalogErrorHtml = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/app.css"></head><body><main>${rendered.project_catalog_error}</main><script src="/static/setting-segments.js" defer></script></body></html>`;

const soundSettingsHtml = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/app.css"><script src="/static/reconnect-coordinator.js" defer></script><script src="/static/completion-sound.js" defer></script></head><body><main>${rendered.sound_settings}</main></body></html>`;

const ttsSettingsHtml = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/app.css"></head><body><main>${rendered.tts_settings}</main></body></html>`;

const timelineHtml = html.replaceAll('conversation-ui', 'conversation-timeline');
const deepLinkHtml = html.replace(
  /(<script id="conversation-initial-snapshot" type="application\/json">)[\s\S]*?(<\/script>)/,
  '$1not-valid-json$2',
);
const ttsMissingHtml = html.replace('/static/conversation-tts.js', '/static/missing-conversation-tts.js');
const navigationMissingHtml = html.replace('/static/conversation-round-navigation.js', '/static/missing-conversation-round-navigation.js');
const enhancementMissingHtml = html.replace('/static/conversation-enhancements.js', '/static/missing-conversation-enhancements.js');
if (deepLinkHtml === html || ttsMissingHtml === html || navigationMissingHtml === html || enhancementMissingHtml === html) {
  throw new Error('module isolation fixture did not replace its script');
}

const mismatchedHtml = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script src="/static/light-dismiss.js" defer></script>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/app.css"></head><body><main>
<section id="native-conversation" data-conversation-id="conversation-ui">
  <p id="conversation-note" hidden></p><section id="conversation-messages"></section>
</section><form id="conversation-composer"><textarea name="text">未发送内容</textarea></form>
<script src="/static/conversation.js" defer></script></main></body></html>`;

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  seenPaths.push(url.pathname);
  const routePath = url.pathname.replace(new RegExp('^/' + entryPrefix + '(?=/)'), '');
  if (routePath.startsWith('/scenario/')) {
    scene = routePath.split('/').pop(); fixtureRevision += 1;
    response.writeHead(204); response.end(); return;
  }
  if (routePath === '/timeline/bind') {
    timelineBound = true; response.writeHead(204); response.end(); return;
  }
  if (routePath === '/response/settingsRelease') {
    responseMode = 'success';
    delayedSettingsResponses.splice(0).forEach(function (respond) { respond(); });
    response.writeHead(204); response.end(); return;
  }
  if (routePath === '/response/messagesRelease') {
    delayedMessageResponses.splice(0).forEach(function (respond) { respond(); });
    response.writeHead(204); response.end(); return;
  }
  if (routePath.startsWith('/response/')) {
    responseMode = routePath.split('/').pop();
    if (responseMode === 'snapshot429') snapshotFailures = 1;
    response.writeHead(204); response.end(); return;
  }
  if (routePath === '/module/enhancement-fail-once') {
    enhancementFailures = 1; response.writeHead(204); response.end(); return;
  }
  if (routePath === '/conversation/conversation-ui') {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end(scene.startsWith('receipt-') ? htmlWithSnapshot(snapshot()) : html); return;
  }
  if (routePath === '/conversation/conversation-deep-link') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(deepLinkHtml); return; }
  if (routePath === '/conversation/conversation-timeline') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(timelineHtml); return; }
  if (routePath === '/conversation-tts-missing') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(ttsMissingHtml); return; }
  if (routePath === '/conversation-navigation-missing') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(navigationMissingHtml); return; }
  if (routePath === '/conversation-enhancement-missing') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(enhancementMissingHtml); return; }
  if (routePath === '/conversation-heartbeat') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(heartbeatHtml); return; }
  if (routePath === '/conversation-mismatch') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(mismatchedHtml); return; }
  if (routePath === '/project/project-one') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(projectHtml); return; }
  if (routePath === '/project/project-catalog-error') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(projectCatalogErrorHtml); return; }
  if (routePath === '/settings/completion-sound') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(soundSettingsHtml); return; }
  if (routePath === '/settings/tts') { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(ttsSettingsHtml); return; }
  if (routePath === '/static/css/app.css') { response.writeHead(200, { 'Content-Type': 'text/css' }); response.end(css); return; }
  if (routePath === '/static/light-dismiss.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(lightDismissJs); return; }
  if (routePath === '/static/reconnect-coordinator.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(reconnectCoordinatorJs); return; }
  if (routePath === '/static/conversation-interactions.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(fs.readFileSync(path.join(root, 'static/conversation-interactions.js'))); return; }
  if (routePath === '/static/conversation.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(js); return; }
  if (routePath === '/static/conversation-heartbeat-test.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(heartbeatTestJs); return; }
  if (routePath === '/static/conversation-enhancements.js') {
    enhancementRequests.push(url.search);
    if (enhancementFailures > 0) {
      enhancementFailures -= 1;
      response.writeHead(503, { 'Content-Type': 'application/javascript' });
      response.end('/* temporarily unavailable */');
      return;
    }
    response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(enhancementsJs); return;
  }
  if (routePath === '/static/missing-conversation-enhancements.js') {
    enhancementRequests.push('missing:' + url.search);
    response.writeHead(404, { 'Content-Type': 'application/javascript' }); response.end('not found'); return;
  }
  if (routePath === '/static/conversation-round-navigation.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(roundNavigationJs); return; }
  if (routePath === '/static/tts.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(ttsJs); return; }
  if (routePath === '/static/conversation-tts.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(conversationTtsJs); return; }
  if (routePath === '/static/tts-settings.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(ttsSettingsJs); return; }
  if (routePath === '/static/completion-sound.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(completionSoundJs); return; }
  if (routePath === '/static/completion-sound-settings.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(completionSoundSettingsJs); return; }
  if (routePath === '/static/draft.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(draftJs); return; }
  if (routePath === '/static/project-conversation.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(projectJs); return; }
  if (routePath === '/static/message-attachments.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(messageAttachmentsJs); return; }
  if (routePath === '/static/setting-segments.js') { response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(settingSegmentsJs); return; }
  if (routePath === '/conversation/conversation-timeline/snapshot') {
    response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(timelineSnapshot())); return;
  }
  if (routePath === '/conversation/conversation-ui/snapshot') {
    if (responseMode === 'snapshotStallOnce') {
      responseMode = 'success';
      response.on('close', () => {
        if (!response.writableEnded) abortedSnapshotRequests += 1;
      });
      return;
    }
    if (responseMode === 'snapshotUnavailable') {
      response.writeHead(503, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: '实时同步暂时不可用。' }));
      return;
    }
    if (responseMode === 'snapshotAuth') {
      response.writeHead(401, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: { code: 'authentication_required', message: '登录已失效，请重新登录；输入仍会保留。' } }));
      return;
    }
    if (responseMode === 'snapshotForbidden') {
      response.writeHead(403, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: { code: 'authorization_denied', message: '当前账号无权执行此操作。' } }));
      return;
    }
    if (responseMode === 'snapshotMalformed') {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end('{}');
      return;
    }
    if (snapshotFailures > 0) {
      snapshotFailures -= 1;
      response.writeHead(429, { 'Content-Type': 'application/json', 'Retry-After': '1' });
      response.end(JSON.stringify({ error: '请求太频繁，请稍后再试。' }));
      return;
    }
    const respond = () => {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(snapshot()));
    };
    if (String(request.headers.referer || '').includes('/conversation/conversation-deep-link')) {
      setTimeout(respond, 250); return;
    }
    respond(); return;
  }
  if (routePath === '/conversation/conversation-ui/export') {
    response.writeHead(200, {
      'Content-Type': 'text/markdown; charset=utf-8',
      'Content-Disposition': "attachment; filename=\"conversation-export.md\"; filename*=UTF-8''%E6%89%8B%E6%9C%BA%E5%AF%B9%E8%AF%9D-%E5%AE%8C%E6%95%B4%E5%AF%B9%E8%AF%9D.md",
      'Cache-Control': 'private, no-store',
    });
    response.end('# 手机产物与 App Server 对话\n\n- 所属项目：Codex Web 工作台维护\n\n### 你\n\n首屏历史记录\n\n### Codex\n\n首屏最近记录仍然可见。\n');
    return;
  }
  if (routePath.endsWith('/artifact/html-1/preview') && url.searchParams.get('viewer') === '1') {
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src 'self'; child-src 'self'; img-src data:; object-src 'none'; connect-src 'none'; worker-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; navigate-to 'self'",
      'Cache-Control': 'private, no-store',
    });
    response.end(htmlArtifactViewer);
    return;
  }
  if (routePath.endsWith('/artifact/html-1/preview')) {
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; sandbox allow-scripts",
      'Cache-Control': 'private, no-store',
    });
    response.end('<!doctype html><button id="inside">点按草图</button><output id="result">等待</output><script>inside.onclick=()=>result.textContent="交互成功"</script>');
    return;
  }
  if (routePath.includes('/artifact/image-1')) { response.writeHead(200, { 'Content-Type': 'image/png' }); response.end(image); return; }
  if (routePath.includes('/artifact/')) { response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment' }); response.end('fixture'); return; }
  if (request.method === 'POST') {
    if (routePath === '/conversation/new') {
      const chunks = [];
      request.on('data', chunk => chunks.push(chunk));
      request.on('end', () => {
        projectSubmission = {
          path: url.pathname,
          values: Object.fromEntries(new URLSearchParams(Buffer.concat(chunks).toString())),
        };
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end('<!doctype html><title>已开始</title>');
      });
      return;
    }
    if (responseMode === 'settingsRejected' && routePath.endsWith('/settings')) {
      response.writeHead(409, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({
        error: { code: 'command_rejected', message: '设置没有保存。' },
        command: { id: 'settings-command', state: 'rejected' }, snapshot: snapshot(),
      }));
      return;
    }
    if (responseMode === 'authSettings' && routePath.endsWith('/settings') ||
        responseMode === 'authMessages' && routePath.endsWith('/messages')) {
      response.writeHead(401, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({
        error: { code: 'authentication_required', message: '登录已失效，请重新登录；输入仍会保留。' },
      }));
      return;
    }
    if (responseMode === 'htmlMessages' && routePath.endsWith('/messages')) {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      response.end('<!doctype html><title>登录</title>');
      return;
    }
    if (responseMode === 'malformedMessages' && routePath.endsWith('/messages')) {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end('{}');
      return;
    }
    if (responseMode === 'redirectLogin' && routePath.endsWith('/messages')) {
      response.writeHead(303, { Location: `/${entryPrefix}/login` });
      response.end();
      return;
    }
    if (routePath.endsWith('/messages')) {
      const chunks = [];
      request.on('data', chunk => chunks.push(chunk));
      request.on('end', () => {
        const body = Buffer.concat(chunks).toString();
        const fields = Object.fromEntries(Array.from(body.matchAll(
          /name="([^"]+)"\r\n\r\n([\s\S]*?)\r\n--/g,
        )).map(match => [match[1], match[2]]));
        messageSubmissions.push({ path: url.pathname, values: fields });
        if (responseMode === 'html500Messages') {
          // Simulate a restart proxy page after accepting the TCP request:
          // there is no receipt, so a later read must remain non-mutating.
          receiptOverrides[fields.command_id] = 'missing';
          setTimeout(() => {
            response.writeHead(500, { 'Content-Type': 'text/html; charset=utf-8' });
            response.end('<!doctype html><title>temporarily unavailable</title>');
          }, 80);
          return;
        }
        if (responseMode === 'messagesDelayedRejected') {
          receiptOverrides[fields.command_id] = 'accepted';
          delayedMessageResponses.push(function () {
            const stale = snapshot();
            stale.conversation.state = 'needs_user';
            stale.messages.forEach(command => { if (command.id === fields.command_id) command.state = 'delivery_unknown'; });
            response.writeHead(409, { 'Content-Type': 'application/json' });
            response.end(JSON.stringify({
              error: { code: 'command_rejected', message: '迟到的拒绝结果。' },
              command: { id: fields.command_id, state: 'rejected' }, snapshot: stale,
            }));
          });
          return;
        }
        if (responseMode === 'rejected' || responseMode === 'malformedRejected') {
          response.writeHead(409, { 'Content-Type': 'application/json' });
          response.end(JSON.stringify({
            error: { code: 'command_rejected', message: '这条消息未发送，输入已保留。' },
            command: { id: fields.command_id, state: 'rejected' },
            snapshot: responseMode === 'malformedRejected' ? {} : snapshotWithReceipt(fields.command_id, 'rejected'),
          }));
          return;
        }
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify(snapshotWithReceipt(fields.command_id, 'accepted')));
      });
      return;
    }
    if (routePath.endsWith('/interrupt')) {
      const chunks = [];
      request.on('data', chunk => chunks.push(chunk));
      request.on('end', () => {
        const fields = Object.fromEntries(Array.from(Buffer.concat(chunks).toString().matchAll(
          /name="([^"]+)"\r\n\r\n([^\r\n]*)/g,
        )).map(match => [match[1], match[2]]));
        stopSubmissions.push(fields);
        response.writeHead(200, {'Content-Type':'application/json'});
        response.end(JSON.stringify(snapshot()));
      });
      return;
    }
    if (routePath.endsWith('/settings')) {
      const chunks = [];
      request.on('data', chunk => chunks.push(chunk));
      request.on('end', () => {
        const body = Buffer.concat(chunks).toString();
        const fields = Object.fromEntries(Array.from(body.matchAll(
          /name="([^"]+)"\r\n\r\n([^\r\n]*)/g,
        )).map(match => [match[1], match[2]]));
        settingsSubmissions.push(fields);
        if (Object.hasOwn(fields, 'service_tier')) {
          currentServiceTier = fields.service_tier || null;
          serviceTierSubmissions.push(fields.service_tier);
        }
        const respond = function () {
          response.writeHead(200, { 'Content-Type': 'application/json' });
          response.end(JSON.stringify(snapshot()));
        };
        if (responseMode === 'settingsDelayed') {
          delayedSettingsResponses.push(respond);
          return;
        }
        respond();
      });
      return;
    }
    response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(snapshot())); return;
  }
  if (routePath === '/login') {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end('<!doctype html><title>登录</title>');
    return;
  }
  if (routePath === '/socket/forbidden') {
    websocketClients.forEach(socket => {
      if (!socket.destroyed && !socket.writableEnded) {
        socket.write(websocketCloseFrame(4403));
        socket.end();
      }
    });
    response.writeHead(204); response.end(); return;
  }
  if (routePath === '/socket/silent-next') {
    silenceNextSocket = true;
    response.writeHead(204); response.end(); return;
  }
  response.writeHead(404); response.end('not found');
});

server.on('upgrade', (request, socket) => {
  const key = request.headers['sec-websocket-key'];
  if (!key || request.url !== `/${entryPrefix}/ws`) {
    socket.destroy(); return;
  }
  const accept = crypto.createHash('sha1')
    .update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11')
    .digest('base64');
  socket.write(
    'HTTP/1.1 101 Switching Protocols\r\n'
    + 'Upgrade: websocket\r\nConnection: Upgrade\r\n'
    + `Sec-WebSocket-Accept: ${accept}\r\n\r\n`
  );
  websocketPaths.push(request.url);
  websocketClients.add(socket);
  socket.ignoreHeartbeatPongs = silenceNextSocket;
  silenceNextSocket = false;
  socket.on('data', chunk => consumeClientFrames(socket, chunk));
  socket.on('error', () => websocketClients.delete(socket));
  if (responseMode === 'snapshotAuth') {
    socket.write(websocketCloseFrame(4401));
    socket.end();
  }
  socket.on('close', () => websocketClients.delete(socket));
});

async function assertStopAppearance(page, quiet, label) {
  const appearance = await page.locator('#stop-conversation').evaluate(button => {
    const style = getComputedStyle(button);
    const rect = button.getBoundingClientRect();
    return {
      hidden: button.hidden || style.display === 'none', disabled: button.disabled,
      background: style.backgroundColor, border: parseFloat(style.borderTopWidth),
      width: rect.width, height: rect.height,
    };
  });
  const channels = appearance.background.match(/[\d.]+/g).map(Number);
  const transparent = channels.length === 4 && channels[3] === 0;
  const red = !transparent && channels[0] > channels[1] * 1.5 && channels[0] > channels[2];
  if (appearance.hidden || appearance.disabled || appearance.width !== 46 || appearance.height !== 40 ||
      (quiet ? !transparent || appearance.border < 1 : !red)) {
    throw new Error(`stop appearance failed (${label}, quiet=${quiet}): ${JSON.stringify(appearance)}`);
  }
}

let browser;
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox'],
  });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
  page.setDefaultTimeout(7000);
  await page.addInitScript(() => {
    const baker = { name: 'Baker', lang: 'zh-CN', voiceURI: 'baker-local', localService: true };
    const synth = {
      spoken: [], utterances: [], getVoices() { return [baker]; }, addEventListener() {}, cancel() {},
      speak(value) { this.spoken.push(value.text); this.utterances.push(value); },
    };
    Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
    window.SpeechSynthesisUtterance = function (text) { this.text = text; };
    window.__completionSoundPlays = 0;
    function audioParam() {
      return { setValueAtTime() {}, exponentialRampToValueAtTime() {} };
    }
    window.AudioContext = function () {
      this.currentTime = 0;
      this.destination = {};
      this.resume = () => Promise.resolve();
      this.createGain = () => {
        window.__completionSoundPlays += 1;
        return { gain: audioParam(), connect() {} };
      };
      this.createOscillator = () => ({
        type: 'sine', frequency: audioParam(), connect() {}, start() {}, stop() {},
      });
    };
  });
  const selectHidden = (selector, value) => page.evaluate(({ selector, value }) => {
    const control = document.querySelector(selector);
    control.value = value;
    control.dispatchEvent(new Event('change', { bubbles: true }));
  }, { selector, value });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));

  const mismatchPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const mismatchErrors = [];
  mismatchPage.on('pageerror', error => mismatchErrors.push(error.message));
  await mismatchPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation-mismatch`, { waitUntil: 'networkidle' });
  const mismatchNote = await mismatchPage.textContent('#conversation-note');
  if (mismatchErrors.length || !mismatchNote.includes('页面资源正在更新')) {
    throw new Error(`mixed template/script crashed instead of degrading safely: ${mismatchErrors.join('; ')}`);
  }
  await mismatchPage.close();

  for (const planScene of ['plan-streaming', 'plan-result']) {
    await page.request.get(`http://127.0.0.1:${port}/scenario/${planScene}`);
    for (const width of [360, 390, 412]) {
      const planPage = await browser.newPage({ viewport: { width, height: 844 } });
      await planPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
      await planPage.waitForSelector('.native-plan .native-message-body[data-native-enhanced]');
      for (const theme of ['dark', 'light']) {
        await planPage.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
        await assertStopAppearance(planPage, planScene === 'plan-result', `${planScene}/${theme}/${width}`);
      }
      const planState = await planPage.evaluate(() => ({
        count: document.querySelectorAll('.native-plan').length,
        label: document.querySelector('.native-plan .native-final-mark').textContent,
        source: document.querySelector('.native-plan .native-message-body').dataset.messageSource,
        text: document.querySelector('#conversation-messages').textContent,
        state: document.querySelector('#conversation-state').textContent,
        folded: document.querySelector('.native-plan').closest('article').classList.contains('is-auto-folded'),
        overflow: document.documentElement.scrollWidth > innerWidth + 1,
      }));
      if (planState.count !== 1 || planState.label !== '计划' ||
          !planState.source.endsWith('总任务完成标准：全部组验收。') || planState.folded || planState.overflow ||
          ['内部执行清单不公开', '已纠正旧计划不公开', '内部推理不公开', '内部命令不公开'].some(value => planState.text.includes(value)) ||
          (planScene === 'plan-result' && planState.state !== '本轮结束') ||
          (planScene === 'plan-streaming' && planState.state === '本轮结束')) {
        throw new Error(`native plan deliverable failed (${planScene}, ${width}): ${JSON.stringify(planState)}`);
      }
      await planPage.locator('.native-plan .native-message-more').click();
      await planPage.locator('.native-plan [data-action="quote"]').click();
      if (!(await planPage.inputValue('#conversation-composer textarea')).includes('全部72个现行命令纳入。')) {
        throw new Error('native plan cannot be quoted for a reply');
      }
      await planPage.close();
    }
  }

  // A stale session pixel must not strand a normal history entry at the top.
  // The older long answer folds by measured height while the newest answer
  // stays complete; expansion must survive the snapshot's replaceChildren.
  await page.request.get(`http://127.0.0.1:${port}/scenario/long-history`);
  const longHistoryPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const longErrors = [];
  longHistoryPage.on('pageerror', error => longErrors.push(error.message));
  await longHistoryPage.addInitScript(() => {
    sessionStorage.setItem('codex-conversation-scroll:conversation-ui', '0');
  });
  await longHistoryPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await longHistoryPage.waitForSelector('[data-message-key="final:turn-long-old:long-old-final"].is-auto-folded');
  const longHistory = await longHistoryPage.evaluate(() => {
    const oldMessage = document.querySelector('[data-message-key="final:turn-long-old:long-old-final"]');
    const newMessage = document.querySelector('[data-message-key="final:turn-long-new:long-new-final"]');
    return {
      userFolded: document.querySelector('[data-message-key="command:long-user-old"]').classList.contains('is-auto-folded'),
      oldFolded: oldMessage.classList.contains('is-auto-folded'),
      controls: oldMessage.querySelectorAll('[data-auto-fold-action]').length,
      codeFolded: oldMessage.querySelector('.native-code-card').classList.contains('auto-folded'),
      latestFolded: newMessage.classList.contains('is-auto-folded'),
      latestControls: newMessage.querySelectorAll('[data-auto-fold-action]').length,
      bottomGap: document.documentElement.scrollHeight - (innerHeight + scrollY),
    };
  });
  if (!longHistory.userFolded || !longHistory.oldFolded || longHistory.controls !== 2 || !longHistory.codeFolded ||
      longHistory.latestFolded || longHistory.latestControls !== 2 || longHistory.bottomGap > 3) {
    throw new Error(`long history folding or initial bottom placement failed: ${JSON.stringify(longHistory)}`);
  }
  await longHistoryPage.locator('[data-message-key="final:turn-long-old:long-old-final"] [data-auto-fold-action]').first().click();
  await longHistoryPage.waitForFunction(() => !document.querySelector(
    '[data-message-key="final:turn-long-old:long-old-final"]').classList.contains('is-auto-folded'));
  const lowerFold = longHistoryPage.locator('[data-message-key="final:turn-long-old:long-old-final"] [data-auto-fold-action]').last();
  await lowerFold.scrollIntoViewIfNeeded();
  const lowerTop = await lowerFold.evaluate(node => node.getBoundingClientRect().top);
  await lowerFold.click();
  await longHistoryPage.waitForFunction(() => document.querySelector(
    '[data-message-key="final:turn-long-old:long-old-final"]').classList.contains('is-auto-folded'));
  await longHistoryPage.waitForTimeout(130);
  const collapsedLowerTop = await lowerFold.evaluate(node => node.getBoundingClientRect().top);
  if (Math.abs(collapsedLowerTop - lowerTop) > 3) {
    throw new Error(`lower fold left its control out of view: before=${lowerTop}, after=${collapsedLowerTop}`);
  }
  await longHistoryPage.locator('[data-message-key="final:turn-long-old:long-old-final"] [data-auto-fold-action]').first().click();
  await longHistoryPage.waitForFunction(() => !document.querySelector(
    '[data-message-key="final:turn-long-old:long-old-final"]').classList.contains('is-auto-folded'));
  await longHistoryPage.locator('[data-message-key="final:turn-long-old:long-old-final"] .native-code-fold').click();
  await page.request.get(`http://127.0.0.1:${port}/scenario/long-history-redraw`);
  await longHistoryPage.click('#reconcile-conversation');
  await longHistoryPage.waitForFunction(() => !document.querySelector(
    '[data-message-key="final:turn-long-old:long-old-final"] .native-code-card').classList.contains('auto-folded'));
  await longHistoryPage.waitForFunction(() => document.querySelector('[data-message-key="final:turn-long-old:long-old-final"] .native-message-body').dataset.messageSource.includes('重绘新增正文'));
  const persistedFold = await longHistoryPage.locator('[data-message-key="final:turn-long-old:long-old-final"]').evaluate(node => ({
    folded: node.classList.contains('is-auto-folded'),
    source: node.querySelector('.native-message-body').dataset.speechSource,
    codeFolded: node.querySelector('.native-code-card').classList.contains('auto-folded'),
  }));
  if (persistedFold.folded || persistedFold.codeFolded ||
      !persistedFold.source.includes('log 90: retained full source')) {
    throw new Error(`expanded long history did not survive redraw or preserve full source: ${JSON.stringify(persistedFold)}`);
  }
  // Reading an older position must survive a true full snapshot replacement.
  const readingTop = await longHistoryPage.evaluate(() => {
    const message = document.querySelector('[data-message-key="final:turn-long-new:long-new-final"]');
    scrollTo(0, scrollY + message.getBoundingClientRect().top + 250);
    return message.getBoundingClientRect().top;
  });
  await page.request.get(`http://127.0.0.1:${port}/scenario/long-history`);
  await longHistoryPage.click('#reconcile-conversation');
  await longHistoryPage.waitForFunction(() => !document.querySelector('[data-message-key="final:turn-long-old:long-old-final"] .native-message-body').dataset.messageSource.includes('重绘新增正文'));
  await longHistoryPage.waitForTimeout(80);
  const readingAfter = await longHistoryPage.locator('[data-message-key="final:turn-long-new:long-new-final"]').evaluate(node => node.getBoundingClientRect().top);
  if (Math.abs(readingAfter - readingTop) > 3) throw new Error(`reading anchor changed: ${readingTop} -> ${readingAfter}`);
  const mutationCount = await longHistoryPage.evaluate(() => new Promise(resolve => {
    let count = 0;
    const observer = new MutationObserver(records => { count += records.length; });
    observer.observe(document.querySelector('#conversation-messages'), { childList: true, subtree: true });
    setTimeout(() => { observer.disconnect(); resolve(count); }, 140);
  }));
  if (mutationCount !== 0) throw new Error(`automatic folding kept mutating after settling: ${mutationCount}`);
  // The same live final may grow, but must not gain automatic folds.
  await page.request.get(`http://127.0.0.1:${port}/scenario/long-history-running`);
  await longHistoryPage.click('#reconcile-conversation');
  await longHistoryPage.waitForSelector('[data-auto-fold-live="1"]');
  if (await longHistoryPage.locator('[data-auto-fold-live="1"]').evaluate(node =>
      node.classList.contains('is-auto-folded') || !!node.querySelector('.auto-folded'))) {
    throw new Error('live final was automatically folded');
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/long-history`);
  await longHistoryPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui?turn=turn-long-old`);
  await longHistoryPage.waitForTimeout(2100);
  const deepGap = await longHistoryPage.evaluate(() => document.documentElement.scrollHeight - innerHeight - scrollY);
  if (deepGap < 500) throw new Error('long-history turn deep link was overridden by initial bottom');
  if (longErrors.length) throw new Error(`long history script errors: ${longErrors.join('; ')}`);
  await longHistoryPage.close();
  await page.request.get(`http://127.0.0.1:${port}/scenario/artifacts`);

  await page.request.get(`http://127.0.0.1:${port}/response/snapshotUnavailable`);
  const initialFailurePage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const initialFailureErrors = [];
  initialFailurePage.on('pageerror', error => initialFailureErrors.push(error.message));
  await initialFailurePage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`,
    { waitUntil: 'networkidle' },
  );
  await initialFailurePage.waitForFunction(() =>
    document.querySelector('#conversation-note').textContent.includes('历史记录会保留'));
  const retainedInitialHistory = await initialFailurePage.evaluate(() => ({
    text: document.querySelector('#conversation-messages').textContent,
    note: document.querySelector('#conversation-note').textContent,
    state: document.querySelector('#conversation-state').textContent,
  }));
  if (!retainedInitialHistory.text.includes('首屏最近记录仍然可见') ||
      !retainedInitialHistory.note.includes('点 ↻ 可重试') || initialFailureErrors.length) {
    throw new Error(`failed first sync cleared embedded history: ${JSON.stringify(retainedInitialHistory)} ${initialFailureErrors.join('; ')}`);
  }
  await initialFailurePage.request.get(`http://127.0.0.1:${port}/response/success`);
  await initialFailurePage.click('#reconcile-conversation');
  await initialFailurePage.waitForFunction(() =>
    document.querySelector('#conversation-messages').textContent.includes('结论是早晚转凉'));
  await initialFailurePage.close();

  // A pending command is only settled by its own definitive receipt. These
  // pages exercise SSR, ordinary sync/reconcile, and websocket-triggered
  // sync with the real deferred draft.js ordering used by the mobile page.
  async function openReceiptPage(state, setup, suffix) {
    await page.request.get(`http://127.0.0.1:${port}/scenario/${state}`);
    const receiptContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const receiptPage = await receiptContext.newPage();
    await receiptPage.addInitScript(setup || (() => {}));
    await receiptPage.goto(
      `http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui${suffix || ''}`,
      { waitUntil: 'domcontentloaded' },
    );
    return receiptPage;
  }
  const pendingSetup = () => {
    if (sessionStorage.getItem('receipt-fixture-seeded')) return;
    sessionStorage.setItem('receipt-fixture-seeded', '1');
    localStorage.setItem('codex-conversation-pending:conversation-ui', JSON.stringify({
      id: 'receipt-command', text: '待核对消息', action: 'messages',
    }));
    localStorage.setItem('codex-conversation-draft:conversation-ui', '待核对消息');
  };
  for (const state of ['receipt-accepted', 'receipt-completed', 'receipt-queued']) {
    const receiptPage = await openReceiptPage(state, pendingSetup);
    await receiptPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
    const settled = await receiptPage.evaluate(() => ({
      value: document.querySelector('#conversation-composer textarea').value,
      draft: localStorage.getItem('codex-conversation-draft:conversation-ui'),
      note: document.querySelector('#conversation-note').textContent,
    }));
    if (settled.value || settled.draft || settled.note.includes('已恢复上次输入')) {
      throw new Error(`${state} did not clear the matching recovered draft: ${JSON.stringify(settled)}`);
    }
    await receiptPage.close();
  }
  const unknownPage = await openReceiptPage('receipt-unknown', pendingSetup);
  await unknownPage.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '待核对消息');
  await unknownPage.waitForTimeout(80); // draft.js boot input must not invalidate ownership.
  await unknownPage.click('#reconcile-conversation');
  await unknownPage.waitForTimeout(120);
  if (!await unknownPage.evaluate(() => localStorage.getItem('codex-conversation-pending:conversation-ui'))) {
    throw new Error('submitted/delivery_unknown receipt was incorrectly settled by reconcile');
  }
  const otherTab = await unknownPage.context().newPage();
  await otherTab.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await otherTab.evaluate(() => {
    localStorage.setItem('codex-conversation-pending:conversation-ui', JSON.stringify({
      id: 'other-tab-command', text: '另一标签待核对', action: 'messages',
    }));
    localStorage.setItem('codex-conversation-draft:conversation-ui', '另一标签新草稿');
  });
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await unknownPage.click('#reconcile-conversation');
  await unknownPage.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  const otherTabStorage = await unknownPage.evaluate(() => ({
    pending: JSON.parse(localStorage.getItem('codex-conversation-pending:conversation-ui') || 'null'),
    draft: localStorage.getItem('codex-conversation-draft:conversation-ui'),
  }));
  if (!otherTabStorage.pending || otherTabStorage.pending.id !== 'other-tab-command' ||
      otherTabStorage.draft !== '另一标签新草稿') {
    throw new Error(`accepted receipt clobbered another tab: ${JSON.stringify(otherTabStorage)}`);
  }
  await otherTab.close();
  await unknownPage.close();

  const rejectedPage = await openReceiptPage('receipt-rejected', pendingSetup);
  await rejectedPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (await rejectedPage.inputValue('#conversation-composer textarea') !== '待核对消息' ||
      !await rejectedPage.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui'))) {
    throw new Error('rejected receipt did not retain an editable ordinary draft');
  }
  await rejectedPage.close();

  const changedPage = await openReceiptPage('receipt-unknown', pendingSetup);
  await changedPage.fill('#conversation-composer textarea', '新的草稿');
  await changedPage.fill('#conversation-composer textarea', '待核对消息');
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await changedPage.click('#reconcile-conversation');
  await changedPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (await changedPage.inputValue('#conversation-composer textarea') !== '待核对消息') {
    throw new Error('an edit changed back to matching text was incorrectly cleared');
  }
  await changedPage.close();

  const reloadedDraftPage = await openReceiptPage('receipt-unknown', pendingSetup);
  await reloadedDraftPage.fill('#conversation-composer textarea', '修改后的新草稿');
  await reloadedDraftPage.fill('#conversation-composer textarea', '待核对消息');
  await reloadedDraftPage.reload({ waitUntil: 'networkidle' });
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await reloadedDraftPage.click('#reconcile-conversation');
  await reloadedDraftPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (await reloadedDraftPage.inputValue('#conversation-composer textarea') !== '待核对消息' ||
      await reloadedDraftPage.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui')) !== '待核对消息') {
    throw new Error('same-text fresh draft was cleared after navigation/reload');
  }
  await reloadedDraftPage.close();

  const sameTextTabPage = await openReceiptPage('receipt-unknown', pendingSetup);
  const sameTextOtherTab = await sameTextTabPage.context().newPage();
  await sameTextOtherTab.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await sameTextOtherTab.fill('#conversation-composer textarea', '其他标签新编辑');
  await sameTextOtherTab.fill('#conversation-composer textarea', '待核对消息');
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await sameTextTabPage.click('#reconcile-conversation');
  await sameTextTabPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (await sameTextTabPage.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui')) !== '待核对消息') {
    throw new Error('same-text draft edited in another tab was deleted by a receipt');
  }
  await sameTextOtherTab.close();
  await sameTextTabPage.close();

  const pastedSamePage = await openReceiptPage('receipt-unknown', pendingSetup);
  await pastedSamePage.locator('#conversation-composer textarea').press('ControlOrMeta+A');
  await pastedSamePage.keyboard.insertText('待核对消息');
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await pastedSamePage.click('#reconcile-conversation');
  await pastedSamePage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (await pastedSamePage.inputValue('#conversation-composer textarea') !== '待核对消息') {
    throw new Error('a real same-text paste was mistaken for boot restoration');
  }
  await pastedSamePage.close();

  const modernPendingSetup = () => {
    if (sessionStorage.getItem('receipt-modern-seeded')) return;
    sessionStorage.setItem('receipt-modern-seeded', '1');
    localStorage.setItem('codex-conversation-draft:conversation-ui:revision', 'original-revision');
    localStorage.setItem('codex-conversation-draft:conversation-ui', '待核对消息');
    localStorage.setItem('codex-conversation-pending:conversation-ui', JSON.stringify({
      id: 'receipt-command', text: '待核对消息', action: 'messages',
      composer_value: '待核对消息', draft_revision: 'original-revision',
    }));
  };
  const twoReceiptPage = await openReceiptPage('receipt-unknown', modernPendingSetup);
  const twoReceiptOther = await twoReceiptPage.context().newPage();
  await twoReceiptOther.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await twoReceiptPage.click('#reconcile-conversation');
  await twoReceiptPage.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  await twoReceiptOther.click('#reconcile-conversation');
  await twoReceiptOther.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  await twoReceiptOther.close();
  await twoReceiptPage.close();

  const alreadyConfirmedPage = await openReceiptPage('receipt-accepted', modernPendingSetup);
  await alreadyConfirmedPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  await alreadyConfirmedPage.fill('#conversation-composer textarea', '待核对消息');
  await alreadyConfirmedPage.evaluate(() => localStorage.setItem('codex-conversation-pending:conversation-ui', JSON.stringify({
    id: 'receipt-command', text: '待核对消息', action: 'messages',
  })));
  const confirmedSubmissions = messageSubmissions.length;
  await alreadyConfirmedPage.click('.native-send');
  await alreadyConfirmedPage.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  if (messageSubmissions.length !== confirmedSubmissions ||
      (await alreadyConfirmedPage.textContent('#conversation-note')).includes('正在发送')) {
    throw new Error('known definitive receipt unnecessarily sent again or left a sending warning');
  }
  await alreadyConfirmedPage.close();

  const explicitReloadPage = await openReceiptPage('receipt-unknown', modernPendingSetup,
    '?prefill=%E5%BE%85%E6%A0%B8%E5%AF%B9%E6%B6%88%E6%81%AF');
  await explicitReloadPage.waitForLoadState('networkidle');
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await explicitReloadPage.reload({ waitUntil: 'networkidle' });
  if (await explicitReloadPage.inputValue('#conversation-composer textarea') !== '待核对消息' ||
      await explicitReloadPage.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui')) !== '待核对消息') {
    throw new Error('explicit same-text prefill lost its new revision after reload');
  }
  await explicitReloadPage.close();

  const quotedReloadPage = await openReceiptPage('receipt-unknown', () => {
    if (sessionStorage.getItem('receipt-quoted-seeded')) return;
    sessionStorage.setItem('receipt-quoted-seeded', '1');
    const raw = '引用：\n待核对消息\n\n';
    localStorage.setItem('codex-conversation-draft:conversation-ui:revision', 'quoted-original-revision');
    localStorage.setItem('codex-conversation-draft:conversation-ui', raw);
    localStorage.setItem('codex-conversation-pending:conversation-ui', JSON.stringify({
      id: 'receipt-command', text: raw.trim(), action: 'messages',
      composer_value: raw, draft_revision: 'quoted-original-revision',
    }));
    localStorage.setItem('codex-new-quote', '待核对消息');
  }, '?quoted=1');
  await quotedReloadPage.waitForLoadState('networkidle');
  await page.request.get(`http://127.0.0.1:${port}/scenario/receipt-accepted`);
  await quotedReloadPage.reload({ waitUntil: 'networkidle' });
  if (await quotedReloadPage.inputValue('#conversation-composer textarea') !== '引用：\n待核对消息\n\n' ||
      !await quotedReloadPage.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui'))) {
    throw new Error('explicit same-text quote was deleted after reload');
  }
  await quotedReloadPage.close();

  const differentDraftPage = await openReceiptPage('receipt-accepted', () => {
    localStorage.setItem('codex-conversation-pending:conversation-ui', JSON.stringify({ id: 'receipt-command', text: '待核对消息' }));
    localStorage.setItem('codex-conversation-draft:conversation-ui', '另一份新草稿');
  });
  await differentDraftPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (await differentDraftPage.inputValue('#conversation-composer textarea') !== '另一份新草稿') {
    throw new Error('accepted receipt overwrote a different ordinary draft');
  }
  await differentDraftPage.close();

  const prefillPage = await openReceiptPage('receipt-accepted', pendingSetup, '?prefill=%E5%BE%85%E6%A0%B8%E5%AF%B9%E6%B6%88%E6%81%AF');
  await prefillPage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (await prefillPage.inputValue('#conversation-composer textarea') !== '待核对消息') {
    throw new Error('accepted receipt cleared an explicit prefill');
  }
  await prefillPage.close();
  const quotePage = await openReceiptPage('receipt-accepted', () => {
    localStorage.setItem('codex-conversation-pending:conversation-ui', JSON.stringify({ id: 'receipt-command', text: '待核对消息' }));
    localStorage.setItem('codex-new-quote', '待核对消息');
  }, '?quoted=1');
  await quotePage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'));
  if (!(await quotePage.inputValue('#conversation-composer textarea')).startsWith('引用：')) {
    throw new Error('accepted receipt cleared an explicit quote');
  }
  await quotePage.close();

  await page.request.get(`http://127.0.0.1:${port}/scenario/artifacts`);
  await page.request.get(`http://127.0.0.1:${port}/response/messagesDelayedRejected`);
  const lateFailurePage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await lateFailurePage.addInitScript(() => {
    window.settledSendCommands = [];
    window.addEventListener('codex:conversation-send-settled', event => {
      window.settledSendCommands.push(event.detail.command_id);
    });
  });
  await lateFailurePage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await lateFailurePage.fill('#conversation-composer textarea', '先确认再迟到失败');
  await lateFailurePage.click('.native-send');
  await lateFailurePage.waitForFunction(() => document.querySelector('.native-send').disabled);
  await lateFailurePage.waitForTimeout(80);
  const delayedReceiptId = await lateFailurePage.evaluate(() =>
    JSON.parse(localStorage.getItem('codex-conversation-pending:conversation-ui')).id);
  const heldSubmissions = messageSubmissions.length;
  await lateFailurePage.fill('#conversation-composer textarea', '发送中另写的新草稿');
  await lateFailurePage.locator('#conversation-composer').evaluate(form => form.requestSubmit());
  const heldDraft = await lateFailurePage.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui'));
  if (messageSubmissions.length !== heldSubmissions || heldDraft !== '发送中另写的新草稿') {
    throw new Error('blocked duplicate submission sent twice or deleted the newly edited draft');
  }
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.updated', data: { conversation_id: 'conversation-ui' },
  }))));
  await lateFailurePage.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui'), null, { timeout: 7000 });
  await lateFailurePage.waitForFunction(() => !document.querySelector('.native-send').disabled);
  if (await lateFailurePage.inputValue('#conversation-composer textarea') !== '发送中另写的新草稿' ||
      await lateFailurePage.evaluate(() => localStorage.getItem('codex-conversation-draft:conversation-ui')) !== '发送中另写的新草稿') {
    throw new Error('receipt did not preserve the newly edited flight draft');
  }
  // The first POST remains held: a definitive WS receipt must release the
  // logical lock so a different second command can actually be sent.
  await page.request.get(`http://127.0.0.1:${port}/response/success`);
  await lateFailurePage.fill('#conversation-composer textarea', '确定收据后发送下一条');
  await lateFailurePage.click('.native-send');
  await lateFailurePage.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  if (messageSubmissions.length !== heldSubmissions + 1) throw new Error('confirmed delivery remained blocked by the hanging old POST');
  await page.request.get(`http://127.0.0.1:${port}/response/messagesRelease`);
  await lateFailurePage.waitForFunction(id => window.settledSendCommands.includes(id), delayedReceiptId);
  const lateFailure = await lateFailurePage.evaluate(() => ({
    pending: localStorage.getItem('codex-conversation-pending:conversation-ui'),
    note: document.querySelector('#conversation-note').textContent,
    value: document.querySelector('#conversation-composer textarea').value,
    draft: localStorage.getItem('codex-conversation-draft:conversation-ui'),
  }));
  if (lateFailure.value || lateFailure.draft) {
    throw new Error(`late HTTP failure corrupted the newly confirmed command: ${JSON.stringify(lateFailure)}`);
  }
  if (lateFailure.pending || lateFailure.note.includes('迟到的拒绝结果') || lateFailure.note.includes('投递结果需要同步核对')) {
    throw new Error(`late rejected HTTP result resurrected a confirmed pending command: ${JSON.stringify(lateFailure)}`);
  }
  await lateFailurePage.close();
  await page.request.get(`http://127.0.0.1:${port}/response/success`);
  await page.request.get(`http://127.0.0.1:${port}/scenario/artifacts`);

  await page.request.get(`http://127.0.0.1:${port}/module/enhancement-fail-once`);
  const recoveryRequestsBefore = enhancementRequests.length;
  const recoveryPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const recoveryErrors = [];
  recoveryPage.on('pageerror', error => recoveryErrors.push(error.message));
  await recoveryPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`,
    { waitUntil: 'domcontentloaded' },
  );
  await recoveryPage.waitForFunction(() =>
    window.codexConversationModules &&
    window.codexConversationModules.markdown === 'ready' &&
    document.querySelector('.native-final .native-message-body[data-native-enhanced]'));
  await recoveryPage.focus('#conversation-composer textarea');
  const recoveredEnhancement = await recoveryPage.evaluate(() => {
    const original = window.visualViewport;
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: {
      offsetTop: 0, offsetLeft: 0, width: 390, height: 544, addEventListener() {},
    }});
    original.dispatchEvent(new Event('resize'));
    const composer = document.querySelector('#conversation-composer').getBoundingClientRect();
    return {
      tables: document.querySelectorAll('.native-table-wrap table').length,
      codeCards: document.querySelectorAll('.native-code-card').length,
      composerBottom: Math.round(composer.bottom),
      navDisplay: getComputedStyle(document.querySelector('.nav')).display,
      headerTop: Math.round(document.querySelector('.native-conversation-head').getBoundingClientRect().top),
    };
  });
  const recoveryRequests = enhancementRequests.slice(recoveryRequestsBefore);
  if (recoveryErrors.length || recoveredEnhancement.tables < 1 ||
      recoveredEnhancement.codeCards < 1 || recoveredEnhancement.composerBottom !== 544 ||
      recoveredEnhancement.navDisplay !== 'none' || recoveredEnhancement.headerTop !== 0 ||
      recoveryRequests.length < 2 || !recoveryRequests.some(value => value.includes('retry=1'))) {
    throw new Error(`failed Markdown asset did not recover independently: ${JSON.stringify({ recoveredEnhancement, recoveryRequests, recoveryErrors })}`);
  }
  await recoveryPage.close();

  const fallbackRequestsBefore = enhancementRequests.length;
  const enhancementMissingPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const enhancementMissingErrors = [];
  enhancementMissingPage.on('pageerror', error => enhancementMissingErrors.push(error.message));
  await enhancementMissingPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation-enhancement-missing`,
    { waitUntil: 'domcontentloaded' },
  );
  await enhancementMissingPage.waitForSelector('.native-final .native-message-body');
  await enhancementMissingPage.waitForTimeout(3300);
  const rawFallback = await enhancementMissingPage.evaluate(() => {
    const body = document.querySelector('.native-final .native-message-body');
    return {
      enhanced: body.hasAttribute('data-native-enhanced'),
      whiteSpace: getComputedStyle(body).whiteSpace,
      lineBreaks: (body.textContent.match(/\n/g) || []).length,
      renderedBlocks: body.querySelectorAll('p,table,pre').length,
    };
  });
  await enhancementMissingPage.focus('#conversation-composer textarea');
  const isolatedKeyboard = await enhancementMissingPage.evaluate(() => {
    const original = window.visualViewport;
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: {
      offsetTop: 0, offsetLeft: 0, width: 390, height: 544, addEventListener() {},
    }});
    original.dispatchEvent(new Event('resize'));
    const composer = document.querySelector('#conversation-composer').getBoundingClientRect();
    return {
      composerBottom: Math.round(composer.bottom),
      headerTop: Math.round(document.querySelector('.native-conversation-head').getBoundingClientRect().top),
      navDisplay: getComputedStyle(document.querySelector('.nav')).display,
      inputMode: document.body.classList.contains('native-input-mode'),
    };
  });
  const fallbackRequests = enhancementRequests.slice(fallbackRequestsBefore);
  if (enhancementMissingErrors.length || rawFallback.enhanced ||
      rawFallback.whiteSpace !== 'pre-wrap' || rawFallback.lineBreaks < 4 ||
      rawFallback.renderedBlocks !== 0 || isolatedKeyboard.composerBottom !== 544 ||
      isolatedKeyboard.headerTop !== 0 || isolatedKeyboard.navDisplay !== 'none' ||
      !isolatedKeyboard.inputMode ||
      fallbackRequests.filter(value => value.startsWith('missing:')).length < 3 ||
      !fallbackRequests.some(value => value.includes('retry=1')) ||
      !fallbackRequests.some(value => value.includes('retry=2'))) {
    throw new Error(`Markdown failure still broke raw layout or keyboard: ${JSON.stringify({ rawFallback, isolatedKeyboard, fallbackRequests, enhancementMissingErrors })}`);
  }
  await enhancementMissingPage.close();

  const ttsMissingPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await ttsMissingPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation-tts-missing`,
    { waitUntil: 'networkidle' },
  );
  await ttsMissingPage.waitForSelector('.native-message.user');
  await ttsMissingPage.evaluate(() => {
    const messages = document.querySelector('#conversation-messages');
    const first = messages.querySelector('.native-message.user');
    const firstMeta = first.querySelector('.native-message-meta');
    firstMeta.textContent = '你 · 第 1 轮';
    const second = first.cloneNode(true);
    second.querySelector('.native-message-meta').textContent = '你 · 第 2 轮';
    messages.appendChild(second);
  });
  await ttsMissingPage.waitForFunction(() => !document.querySelector('#conversation-round-nav').hidden);
  const dragHandle = ttsMissingPage.locator('#conversation-round-nav .native-drag-handle');
  const beforeDrag = await ttsMissingPage.locator('#conversation-round-nav').evaluate(node => node.getBoundingClientRect().left);
  const handleBox = await dragHandle.boundingBox();
  await ttsMissingPage.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
  await ttsMissingPage.mouse.down();
  await ttsMissingPage.mouse.move(Math.max(30, handleBox.x - 70), handleBox.y + 45, { steps: 4 });
  await ttsMissingPage.mouse.up();
  const isolatedNavigation = await ttsMissingPage.evaluate(before => {
    const nav = document.querySelector('#conversation-round-nav');
    const enabled = Array.from(nav.querySelectorAll('#conversation-round-up,#conversation-round-down'))
      .filter(button => !button.disabled);
    if (enabled[0]) enabled[0].click();
    return {
      left: nav.getBoundingClientRect().left,
      stored: localStorage.getItem('codex-round-nav-position'),
      enabled: enabled.length,
      before,
    };
  }, beforeDrag);
  if (!isolatedNavigation.stored || isolatedNavigation.enabled < 1 ||
      Math.abs(isolatedNavigation.left - isolatedNavigation.before) < 10) {
    throw new Error(`round navigation failed when TTS module was missing: ${JSON.stringify(isolatedNavigation)}`);
  }
  await ttsMissingPage.close();

  const navigationMissingPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await navigationMissingPage.addInitScript(() => {
    const synth = {
      utterances: [], getVoices() { return []; }, addEventListener() {}, cancel() {},
      speak(value) { this.utterances.push(value); },
    };
    Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
    window.SpeechSynthesisUtterance = function (text) { this.text = text; };
  });
  await navigationMissingPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation-navigation-missing`,
    { waitUntil: 'networkidle' },
  );
  await navigationMissingPage.waitForSelector('.native-final .native-message-speak');
  await navigationMissingPage.click('.native-final .native-message-speak');
  const isolatedSpeech = await navigationMissingPage.evaluate(() => ({
    count: speechSynthesis.utterances.length,
    text: speechSynthesis.utterances[0] && speechSynthesis.utterances[0].text,
    voice: speechSynthesis.utterances[0] && speechSynthesis.utterances[0].voice,
  }));
  if (isolatedSpeech.count !== 1 || !isolatedSpeech.text || isolatedSpeech.voice !== undefined) {
    throw new Error(`TTS failed or overrode Android default when navigation module was missing: ${JSON.stringify(isolatedSpeech)}`);
  }
  await navigationMissingPage.close();

  await page.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.native-image-artifact img');
  const completedProcess = await page.evaluate(() => {
    const messages = document.querySelector('#conversation-messages');
    const text = messages.textContent;
    const group = document.querySelector('.native-commentary-group');
    const order = [
      '分析未来十日天气，并生成图片和报告。',
      '我先核对温度趋势。',
      '追问：请把清晨体感也说清楚。',
      '正在补充清晨体感并整理报告。',
      '结论是早晚转凉、白天仍热。产物已经放在这条回复下面。',
    ].map(value => text.indexOf(value));
    return {
      thinkingCards: document.querySelectorAll('.native-thinking-card').length,
      finalCards: document.querySelectorAll('.native-final-mark').length,
      text, order,
      commentaryGroups: document.querySelectorAll('.native-commentary-group').length,
      commentaryFolded: group && group.classList.contains('is-group-folded'),
      commentarySummary: group && group.querySelector('.native-commentary-group-head').textContent,
      commentaryItemsHidden: Array.from(document.querySelectorAll('[data-commentary-group-member="turn-1"]'))
        .every(node => getComputedStyle(node).display === 'none'),
      header: document.querySelector('.native-conversation-head').getBoundingClientRect().height,
      context: document.querySelector('#conversation-context-toggle').textContent,
      meta: Array.from(document.querySelectorAll('.native-message-meta')).map(node => node.textContent),
      stopHidden: document.querySelector('#stop-conversation').hidden &&
        getComputedStyle(document.querySelector('#stop-conversation')).display === 'none',
      newContentHidden: document.querySelector('#conversation-new-content').hidden &&
        getComputedStyle(document.querySelector('#conversation-new-content')).display === 'none',
    };
  });
  if (completedProcess.thinkingCards !== 0 || completedProcess.finalCards !== 1 ||
      completedProcess.text.includes('generate_report.py') ||
      completedProcess.text.includes('我先核对温度趋势，再把结论和产物整理成简洁答复。') ||
      completedProcess.order.some(value => value < 0) ||
      !completedProcess.order.every((value, index, values) => index === 0 || value > values[index - 1]) ||
      completedProcess.commentaryGroups !== 1 || !completedProcess.commentaryFolded ||
      !completedProcess.commentaryItemsHidden || !completedProcess.commentarySummary.includes('我先核对温度趋势。') ||
      completedProcess.header > 44.5 || completedProcess.context.trim() !== '42%' ||
      !completedProcess.meta.every(value => value.includes('字')) ||
      completedProcess.stopHidden || !completedProcess.newContentHidden) {
    throw new Error(`natural completed timeline failed: ${JSON.stringify(completedProcess)}`);
  }
  const finalTextMatches = completedProcess.text.split('结论是早晚转凉、白天仍热。产物已经放在这条回复下面。').length - 1;
  if (finalTextMatches !== 1) throw new Error(`duplicate final answer remained: ${finalTextMatches}`);
  await page.click('[data-commentary-group-action]');
  const expandedCommentary = await page.evaluate(() => ({
    folded: document.querySelector('.native-commentary-group').classList.contains('is-group-folded'),
    count: document.querySelectorAll('[data-commentary-group-member="turn-1"]').length,
    hasCharacterCounts: Array.from(document.querySelectorAll('[data-commentary-group-member="turn-1"] .native-message-meta'))
      .every(node => node.textContent.includes('字')),
  }));
  if (expandedCommentary.folded || expandedCommentary.count !== 2 || !expandedCommentary.hasCharacterCounts) {
    throw new Error(`completed commentary group did not expand with per-message metadata: ${JSON.stringify(expandedCommentary)}`);
  }
  await page.click('[data-commentary-group-member="turn-1"] .native-message-more');
  await page.click('[data-commentary-group-member="turn-1"] [data-action="fold"]');
  if (!await page.locator('[data-commentary-group-member="turn-1"]').first().evaluate(node => node.classList.contains('is-folded'))) {
    throw new Error('single commentary did not retain its independent manual fold');
  }
  const enhancedContent = await page.evaluate(() => ({
    tables: document.querySelectorAll('.native-final .native-table-wrap table').length,
    codeCards: document.querySelectorAll('.native-final .native-code-card').length,
    postCode: document.querySelector('.native-final .native-message-body').textContent.includes('代码后的正文应正常朗读'),
    speakButtons: document.querySelectorAll('.native-message.agent .native-message-speak').length,
    modelSegments: document.querySelectorAll('.native-settings-panel .setting-segments-model .setting-segment').length,
    effortSegments: document.querySelectorAll('.native-settings-panel .setting-segments-effort .setting-segment').length,
  }));
  if (enhancedContent.tables !== 1 || enhancedContent.codeCards !== 1 || !enhancedContent.postCode ||
      enhancedContent.speakButtons < 1 || enhancedContent.modelSegments < 2 || enhancedContent.effortSegments < 2) {
    throw new Error(`safe markdown, speech, or segmented settings missing: ${JSON.stringify(enhancedContent)}`);
  }
  const spacing = await page.evaluate(() => {
    const final = document.querySelector('.native-final');
    const bubble = final.querySelector('.native-bubble') || final;
    const style = getComputedStyle(bubble);
    const buttons = [...final.querySelectorAll('.native-message-more,.native-message-speak')]
      .map(node => { const rect = node.getBoundingClientRect(); return {width: rect.width, height: rect.height}; });
    return {gap: parseFloat(getComputedStyle(document.querySelector('.native-messages')).rowGap),
      top: parseFloat(style.paddingTop), right: parseFloat(style.paddingRight),
      bottom: parseFloat(style.paddingBottom), buttons};
  });
  if (Math.abs(spacing.gap - 9) > 1 || spacing.top !== 9 || spacing.right !== 13 || spacing.bottom !== 2 ||
      !spacing.buttons.length || spacing.buttons.some(button => button.width !== 32 || button.height !== 25)) {
    throw new Error(`compact message geometry regressed: ${JSON.stringify(spacing)}`);
  }
  await page.evaluate(() => {
    window.__localCopies = [];
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText(value) { window.__localCopies.push(value); return Promise.resolve(); },
    }});
  });
  await page.click('.native-final .native-code-card .native-local-copy');
  await page.waitForFunction(() => window.__localCopies.includes('alert("不应朗读")'));
  for (const format of ['table', 'markdown', 'plain']) {
    await page.click('.native-final .native-table-wrap .native-local-copy');
    await page.waitForSelector('#native-table-copy-sheet:not([hidden])');
    await page.click(`[data-table-copy-format="${format}"]`);
  }
  const copiedTables = await page.evaluate(() => window.__localCopies.slice(1));
  if (copiedTables[0] !== '功能\t建议\n跨对话提醒\t实现' ||
      copiedTables[1] !== '| 功能 | 建议 |\n| --- | --- |\n| 跨对话提醒 | 实现 |' ||
      copiedTables[2] !== '功能：跨对话提醒\n建议：实现') {
    throw new Error(`local table copy formats failed: ${JSON.stringify(copiedTables)}`);
  }
  await page.fill('#conversation-composer textarea', '');
  await page.click('.native-final .native-code-card .native-local-quote');
  const quotedCode = await page.inputValue('#conversation-composer textarea');
  if (quotedCode !== '```js\nalert("不应朗读")\n```\n\n') throw new Error(`local code quote failed: ${JSON.stringify(quotedCode)}`);
  await page.fill('#conversation-composer textarea', '');
  await page.click('.native-final .native-table-wrap .native-local-quote');
  const quotedTable = await page.inputValue('#conversation-composer textarea');
  if (quotedTable !== '| 功能 | 建议 |\n| --- | --- |\n| 跨对话提醒 | 实现 |\n\n') {
    throw new Error(`local table quote failed: ${JSON.stringify(quotedTable)}`);
  }
  await page.evaluate(() => {
    const message = document.createElement('article'); message.className = 'native-message agent';
    const bubble = document.createElement('div'); bubble.className = 'native-bubble';
    const body = document.createElement('div'); body.className = 'native-message-body'; body.textContent = '> 局部引用示例';
    bubble.appendChild(body); message.appendChild(bubble); document.querySelector('#conversation-messages').appendChild(message);
  });
  await page.waitForSelector('.native-quote-card .native-local-quote');
  await page.fill('#conversation-composer textarea', '');
  await page.click('.native-quote-card .native-local-quote');
  const quotedBlock = await page.inputValue('#conversation-composer textarea');
  if (quotedBlock !== '> 局部引用示例\n\n') throw new Error(`local quote block quote failed: ${JSON.stringify(quotedBlock)}`);
  await page.click('.native-quote-card .native-local-copy');
  await page.waitForFunction(() => window.__localCopies.includes('局部引用示例'));
  await page.evaluate(() => {
    const message = document.createElement('article'); message.className = 'native-message agent';
    message.dataset.numberingFixture = '1';
    const bubble = document.createElement('div'); bubble.className = 'native-bubble';
    const body = document.createElement('div'); body.className = 'native-message-body';
    body.textContent = [
      '1. 第一项', '   第一项说明', '',
      '2. 第二项', '   第二项说明', '',
      '3. 第三项', '   第三项说明', '',
      '4. 第四项', '5. 第五项', '',
      '独立列表应重新开始：', '', '1. 新的第一项',
    ].join('\n');
    bubble.appendChild(body); message.appendChild(bubble);
    document.querySelector('#conversation-messages').appendChild(message);
  });
  await page.waitForSelector('[data-numbering-fixture="1"] .native-message-body[data-native-enhanced]');
  const orderedListNumbering = await page.locator('[data-numbering-fixture="1"]').evaluate(message => ({
    starts: Array.from(message.querySelectorAll('ol')).map(list => list.start),
    itemCounts: Array.from(message.querySelectorAll('ol')).map(list => list.children.length),
    source: message.querySelector('.native-message-body').dataset.speechSource,
  }));
  if (JSON.stringify(orderedListNumbering.starts) !== JSON.stringify([1, 2, 3, 4, 1]) ||
      JSON.stringify(orderedListNumbering.itemCounts) !== JSON.stringify([1, 1, 1, 2, 1]) ||
      !orderedListNumbering.source.includes('2. 第二项') ||
      !orderedListNumbering.source.includes('3. 第三项')) {
    throw new Error(`ordered Markdown numbering was not preserved: ${JSON.stringify(orderedListNumbering)}`);
  }
  await page.fill('#conversation-composer textarea', '');
  await page.click('.native-final .native-message-more');
  await page.click('.native-final [data-action="fold"]');
  const foldedMessage = await page.evaluate(() => {
    const message = document.querySelector('.native-final').closest('.native-message');
    const body = message.querySelector('.native-message-body');
    const preview = message.querySelector('.native-message-preview');
    return {
      folded: message.classList.contains('is-folded'),
      bodyDisplay: getComputedStyle(body).display,
      previewDisplay: getComputedStyle(preview).display,
      preview: preview.textContent,
      fullSpeech: body.dataset.speechSource,
      fullSource: body.dataset.messageSource,
    };
  });
  if (!foldedMessage.folded || foldedMessage.bodyDisplay !== 'none' ||
      foldedMessage.previewDisplay === 'none' ||
      !foldedMessage.preview.startsWith('结论是早晚转凉、白天仍热。') ||
      !foldedMessage.fullSpeech.includes('代码后的正文应正常朗读。') ||
      !foldedMessage.fullSource.includes('```js')) {
    throw new Error(`folded message did not preserve a first-sentence preview and full source: ${JSON.stringify(foldedMessage)}`);
  }
  await page.click('.native-final .native-message-more');
  await page.click('.native-final [data-action="fold"]');
  if (await page.locator('.native-final').evaluate(node =>
    node.closest('.native-message').classList.contains('is-folded'))) {
    throw new Error('message did not expand after folded preview');
  }
  await page.click('.native-final .native-code-fold');
  if (!await page.locator('.native-final .native-code-card').evaluate(node => node.classList.contains('folded'))) {
    throw new Error('code block did not fold');
  }
  await page.click('.native-final .native-message-speak');
  for (let sentence = 0; sentence < 4; sentence += 1) {
    if (await page.locator('#conversation-audio-next').isDisabled()) break;
    await page.click('#conversation-audio-next');
    await page.waitForTimeout(170);
  }
  const spoken = await page.evaluate(() => speechSynthesis.spoken.join(' '));
  if (!spoken.includes('表格内容请在页面查看') || !spoken.includes('代码示例已省略') ||
      !spoken.includes('代码后的正文应正常朗读') || spoken.includes('跨对话提醒') || spoken.includes('alert')) {
    throw new Error(`speech text included controls/table/code or lost later prose: ${spoken}`);
  }
  await page.click('#conversation-audio-stop');

  await page.click('.native-final .native-message-speak');
  await page.click('#conversation-audio-settings');
  await page.click('#conversation-audio-more-settings');
  await page.waitForSelector('#tts-voice');
  if (new URL(page.url()).pathname !== `/${entryPrefix}/settings/tts`) {
    throw new Error(`conversation TTS settings link lost the public prefix: ${page.url()}`);
  }
  await page.selectOption('#tts-voice', 'baker-local');
  await page.click('#tts-save');
  await page.goBack({ waitUntil: 'networkidle' });
  await page.waitForSelector('.native-final .native-message-speak');
  await page.evaluate(() => {
    const dialog = document.querySelector('#conversation-audio-settings-dialog');
    if (dialog.open) dialog.close();
  });
  await page.click('.native-final .native-message-speak');
  const returnedTtsVoice = await page.evaluate(() => {
    const utterance = speechSynthesis.utterances.at(-1);
    return utterance && utterance.voice && utterance.voice.voiceURI;
  });
  if (returnedTtsVoice !== 'baker-local') {
    throw new Error(`conversation did not reload device TTS settings after returning: ${returnedTtsVoice}`);
  }
  await page.click('#conversation-audio-settings');
  await page.selectOption('#conversation-audio-voice', '');
  await page.click('[data-close-audio-settings]');
  await page.click('#conversation-audio-stop');
  if (process.env.CODEX_UI_COMPLETE_SCREENSHOT) {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: process.env.CODEX_UI_COMPLETE_SCREENSHOT });
  }
  await page.click('#conversation-context-toggle');
  await page.waitForSelector('#conversation-context[open]');
  const contextGeometry = await page.locator('#conversation-context').evaluate(node => ({
    top: node.getBoundingClientRect().top, text: node.textContent,
  }));
  if (contextGeometry.top > 45 || !contextGeometry.text.includes('42%') ||
      !contextGeometry.text.includes('压缩上下文') ||
      !contextGeometry.text.includes('已记录 2 次') ||
      !contextGeometry.text.includes('内部换窗')) {
    throw new Error(`context panel did not open from the top: ${JSON.stringify(contextGeometry)}`);
  }
  if (await page.locator('#context-details').evaluate(node => node.open) ||
      await page.locator('#context-switch-details').isVisible()) {
    throw new Error('context details must be collapsed by default');
  }
  for (const tokenId of ['context-input', 'context-cached', 'context-output']) {
    if (!await page.locator(`#${tokenId}`).isVisible() ||
        await page.locator(`#${tokenId}`).evaluate(node => Boolean(node.closest('details')))) {
      throw new Error(`${tokenId} must be visible on the main context panel`);
    }
  }
  await page.click('#context-details > summary');
  if (!await page.locator('#context-switch-details').isVisible() ||
      !await page.locator('#context-switch-details').textContent().then(value => value.includes('已收到返回')) ||
      !await page.locator('.native-context-scope').textContent().then(value => value.includes('记录可能重叠，不相加')) ||
      await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
    throw new Error('context switch details were not usable on mobile');
  }
  await page.click('[data-close-context]');
  await page.click('.native-head-menu > summary');
  const menuText = await page.textContent('.native-head-menu-panel');
  if (menuText.includes('历史对话') || menuText.includes('置顶对话') ||
      menuText.includes('只导出你的全部提问') || !menuText.includes('连接与排查') ||
      !menuText.includes('重命名对话') || !menuText.includes('导出对话')) {
    throw new Error(`top menu contains duplicate or missing actions: ${menuText}`);
  }
  await page.click('.native-head-menu > summary');

  const settingsBeforeNotify = seenPaths.filter(value => value.endsWith('/settings')).length;
  await page.click('#conversation-notify');
  for (let attempt = 0; attempt < 30 && seenPaths.filter(value => value.endsWith('/settings')).length === settingsBeforeNotify; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (seenPaths.filter(value => value.endsWith('/settings')).length <= settingsBeforeNotify) {
    throw new Error('ntfy switch did not persist immediately');
  }
  const manualSnapshotsBefore = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  await page.click('#reconcile-conversation');
  for (let attempt = 0; attempt < 30 && seenPaths.filter(value => value.endsWith('/snapshot')).length === manualSnapshotsBefore; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (seenPaths.filter(value => value.endsWith('/snapshot')).length <= manualSnapshotsBefore) {
    throw new Error('visible refresh button did not reload the snapshot');
  }

  await page.fill('#conversation-composer textarea', '卡顿刷新仍要保留的草稿');
  await page.request.get(`http://127.0.0.1:${port}/response/snapshotStallOnce`);
  const stalledSnapshotsBefore = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.updated', data: { conversation_id: 'conversation-ui', revision: 700 },
  }))));
  for (let attempt = 0; attempt < 80 &&
       seenPaths.filter(value => value.endsWith('/snapshot')).length <= stalledSnapshotsBefore; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (seenPaths.filter(value => value.endsWith('/snapshot')).length <= stalledSnapshotsBefore) {
    throw new Error('fixture did not start the stalled automatic snapshot');
  }
  await page.click('#reconcile-conversation');
  for (let attempt = 0; attempt < 40 &&
       seenPaths.filter(value => value.endsWith('/snapshot')).length < stalledSnapshotsBefore + 2; attempt += 1) {
    await page.waitForTimeout(50);
  }
  await page.waitForFunction(() =>
    !document.getElementById('reconcile-conversation').classList.contains('saving'));
  if (seenPaths.filter(value => value.endsWith('/snapshot')).length < stalledSnapshotsBefore + 2 ||
      abortedSnapshotRequests < 1 ||
      await page.inputValue('#conversation-composer textarea') !== '卡顿刷新仍要保留的草稿') {
    throw new Error('manual refresh did not replace a stalled snapshot while preserving the draft');
  }

  await page.fill('#conversation-composer textarea', '切换历史后仍应存在的草稿');
  await page.reload({ waitUntil: 'networkidle' });
  if (await page.inputValue('#conversation-composer textarea') !== '切换历史后仍应存在的草稿') {
    throw new Error('unsent local draft did not survive navigation/reload');
  }
  const draftTypography = await page.evaluate(() => ({
    draft: getComputedStyle(document.querySelector('#conversation-composer textarea')).fontSize,
    message: getComputedStyle(document.querySelector('.native-message-body')).fontSize,
  }));
  if (draftTypography.draft !== draftTypography.message) {
    throw new Error(`draft typography diverged from the conversation: ${JSON.stringify(draftTypography)}`);
  }
  await page.fill('#conversation-composer textarea', '');
  const formFacts = await page.evaluate(() => {
    const composer = document.querySelector('#conversation-composer');
    const upload = document.querySelector('#conversation-upload form');
    const file = upload.querySelector('input[type="file"]');
    return {
      forms: document.forms.length,
      composerFileInputs: composer.querySelectorAll('input[type="file"]').length,
      sendForm: document.querySelector('.native-send').form.id,
      fileBelongsToUpload: file.form === upload,
      uploadPath: new URL(upload.action).pathname,
      returnTo: upload.querySelector('input[name="return_to"]').value,
    };
  });
  if (formFacts.forms !== 2 || formFacts.composerFileInputs !== 0 ||
      formFacts.sendForm !== 'conversation-composer' || !formFacts.fileBelongsToUpload) {
    throw new Error(`upload/composer forms are not independent: ${JSON.stringify(formFacts)}`);
  }
  if (formFacts.uploadPath !== `/${entryPrefix}/files/upload` ||
      formFacts.returnTo !== `/${entryPrefix}/conversation/conversation-ui`) {
    throw new Error(`upload form did not preserve prefix: ${JSON.stringify(formFacts)}`);
  }
  const mobileModels = await page.locator('#conversation-model option').evaluateAll(
    options => options.map(option => option.value));
  const expectedModelOrder = [
    'gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
    'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
    'gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.3-codex-spark',
  ];
  if (mobileModels.join(',') !== expectedModelOrder.join(',')) {
    throw new Error(`mobile model catalog was not fully exposed: ${JSON.stringify(mobileModels)}`);
  }
  await page.click('#conversation-settings > summary');
  for (const width of [320, 360, 390, 412]) {
    await page.setViewportSize({ width, height: 844 });
    const responsive = await page.evaluate(() => {
      const group = document.querySelector('.native-settings-panel .setting-segments-model');
      const buttons = Array.from(group.querySelectorAll('.setting-segment'));
      const groupRect = group.getBoundingClientRect();
      const buttonRects = buttons.map(button => button.getBoundingClientRect());
      return {
        pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        groupOverflow: group.scrollWidth - group.clientWidth,
        labels: buttons.map(button => button.textContent.trim()),
        display: getComputedStyle(group).display,
        flexWrap: getComputedStyle(group).flexWrap,
        compact: buttons.every(button => {
          const style = getComputedStyle(button);
          return style.flexGrow === '0' && style.flexShrink === '0' && parseFloat(style.minWidth) >= 44;
        }),
        allVisible: buttonRects.every(rect => rect.left >= groupRect.left - .5 && rect.right <= groupRect.right + .5),
        touchTargets: buttons.every(button => button.getBoundingClientRect().height >= 44),
        namesVisible: buttons.every(button => getComputedStyle(button).textOverflow !== 'ellipsis' &&
          button.scrollHeight <= button.clientHeight + 1),
      };
    });
    if (responsive.pageOverflow > 1 || responsive.groupOverflow <= 1 ||
        responsive.labels.join(',') !== '6.1 Sol,6 Astra,6 Sol,6 Luna,5.6 Sol,5.6 Terra,5.6 Luna,5.5,5.4,5.4 mini,5.3 codex spark' ||
        responsive.display !== 'flex' || responsive.flexWrap !== 'nowrap' || !responsive.compact ||
        !responsive.touchTargets || !responsive.namesVisible) {
      throw new Error(`conversation model grid failed at ${width}px: ${JSON.stringify(responsive)}`);
    }
  }
  await page.locator('.native-settings-panel .setting-segments-model [data-value="gpt-5.6-luna"]').click();
  if (await page.inputValue('#conversation-model') !== 'gpt-5.6-luna') {
    throw new Error('conversation last model segment was not clickable');
  }
  const conversationSettingsBefore = settingsSubmissions.length;
  await page.click('[data-close-settings]');
  for (let attempt = 0; attempt < 30 && settingsSubmissions.length === conversationSettingsBefore; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (settingsSubmissions.at(-1)?.model !== 'gpt-5.6-luna') {
    throw new Error(`conversation model segment was not submitted: ${JSON.stringify(settingsSubmissions.at(-1))}`);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await selectHidden('#conversation-model', 'gpt-6-astra');
  const astraEfforts = await page.locator('#conversation-effort option').evaluateAll(
    options => options.map(option => option.value));
  if (astraEfforts.join(',') !== 'low,medium,high,xhigh,max,ultra') {
    throw new Error(`Astra efforts mismatch: ${JSON.stringify(astraEfforts)}`);
  }
  await selectHidden('#conversation-model', 'gpt-5.6-luna');
  if (await page.inputValue('#conversation-effort') !== 'medium') {
    throw new Error('model switch did not select the model default reasoning effort');
  }
  await selectHidden('#conversation-model', 'gpt-5.6-sol');
  await page.click('#conversation-settings > summary');
  await selectHidden('#conversation-collaboration-mode', 'plan');
  const modeSettingsBefore = settingsSubmissions.length;
  await page.click('[data-close-settings]');
  for (let attempt = 0; attempt < 30 && settingsSubmissions.length === modeSettingsBefore; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (settingsSubmissions.at(-1)?.collaboration_mode !== 'plan') {
    throw new Error(`Plan mode was not submitted for the next turn: ${JSON.stringify(settingsSubmissions.at(-1))}`);
  }
  if (!seenPaths.includes(`/${entryPrefix}/conversation/conversation-ui/snapshot`)) {
    throw new Error('conversation API did not preserve the public entry prefix');
  }
  for (let attempt = 0; attempt < 30 && websocketPaths.length === 0; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (!websocketPaths.includes(`/${entryPrefix}/ws`)) {
    throw new Error('websocket did not preserve the public entry prefix');
  }
  for (let attempt = 0; attempt < 30 && !websocketClientMessages.includes('ping'); attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (!websocketClientMessages.includes('ping')) {
    throw new Error('conversation websocket did not send an application heartbeat');
  }
  const socketsBeforeSilentHeartbeat = websocketPaths.length;
  const clientsBeforeSilentHeartbeat = websocketClients.size;
  await page.request.get(`http://127.0.0.1:${port}/socket/silent-next`);
  const heartbeatPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await heartbeatPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation-heartbeat`,
    { waitUntil: 'networkidle' },
  );
  for (let attempt = 0; attempt < 80 && websocketPaths.length < socketsBeforeSilentHeartbeat + 2; attempt += 1) {
    await page.waitForTimeout(100);
  }
  if (websocketPaths.length < socketsBeforeSilentHeartbeat + 2) {
    throw new Error('silent half-open websocket was not discarded and reconnected');
  }
  await heartbeatPage.close();
  for (let attempt = 0; attempt < 30 && websocketClients.size > clientsBeforeSilentHeartbeat; attempt += 1) {
    await page.waitForTimeout(50);
  }
  const snapshotsBeforeEvent = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.updated', data: { conversation_id: 'conversation-ui', revision: 8 },
  }))));
  for (let attempt = 0; attempt < 70; attempt += 1) {
    if (seenPaths.filter(value => value.endsWith('/snapshot')).length > snapshotsBeforeEvent) break;
    await page.waitForTimeout(50);
  }
  if (seenPaths.filter(value => value.endsWith('/snapshot')).length <= snapshotsBeforeEvent) {
    throw new Error('websocket update did not trigger a prefixed snapshot refresh');
  }
  const soundsBeforeOther = await page.evaluate(() => window.__completionSoundPlays);
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.terminal', data: {
      conversation_id: 'other', title: 'Other', turn_id: 'turn-other',
      generation: 1, status: 'completed',
    },
  }))));
  await page.waitForFunction(() => !document.querySelector('#conversation-cross-toast').hidden);
  const crossNotice = await page.evaluate(() => ({
    text: document.querySelector('#conversation-cross-toast').textContent,
    href: new URL(document.querySelector('#conversation-cross-open').href).pathname,
    sounds: window.__completionSoundPlays,
  }));
  if (!crossNotice.text.includes('Other') || crossNotice.href !== `/${entryPrefix}/conversation/other` ||
      crossNotice.sounds !== soundsBeforeOther + 1) {
    throw new Error(`cross-conversation completion notice failed: ${JSON.stringify(crossNotice)}`);
  }
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.terminal', data: {
      conversation_id: 'other', title: 'Other', turn_id: 'turn-other',
      generation: 1, status: 'completed',
    },
  }))));
  await page.waitForTimeout(100);
  if (await page.evaluate(() => window.__completionSoundPlays) !== crossNotice.sounds) {
    throw new Error('duplicate terminal event replayed its foreground sound');
  }
  await page.evaluate(() => { document.querySelector('#conversation-cross-toast').hidden = true; });
  const readsBeforeCurrentTerminal = seenPaths.filter(value => value.endsWith('/read')).length;
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.terminal', data: {
      conversation_id: 'conversation-ui', title: 'Current', turn_id: 'turn-current',
      generation: 2, status: 'completed',
    },
  }))));
  for (let attempt = 0; attempt < 30 && seenPaths.filter(value => value.endsWith('/read')).length === readsBeforeCurrentTerminal; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (seenPaths.filter(value => value.endsWith('/read')).length <= readsBeforeCurrentTerminal ||
      await page.evaluate(() => window.__completionSoundPlays) !== crossNotice.sounds + 1) {
    throw new Error('current foreground terminal did not sound once and mark itself read');
  }
  const snapshotsBeforeBurst = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  for (let index = 0; index < 40; index += 1) {
    websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
      type: 'conversation.updated', data: { conversation_id: 'conversation-ui', revision: 9 + index },
    }))));
  }
  await page.waitForTimeout(3200);
  const burstSnapshots = seenPaths.filter(value => value.endsWith('/snapshot')).length - snapshotsBeforeBurst;
  if (burstSnapshots !== 1) {
    throw new Error(`websocket burst was not coalesced: snapshots=${burstSnapshots}`);
  }
  // visibility/online/pageshow arrive together on mobile restore.  The shared
  // coordinator must merge them into one read-only sync, retain the healthy
  // socket, and never replay a user message.
  const recoverySnapshotsBefore = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  const recoverySocketsBefore = websocketPaths.length;
  const recoveryMessagesBefore = messageSubmissions.length;
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'));
    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
  });
  // The fixture server owns request observation outside the page, so allow
  // its one merged read to settle before asserting the shared counters below.
  await page.waitForTimeout(250);
  const recoverySnapshots = seenPaths.filter(value => value.endsWith('/snapshot')).length - recoverySnapshotsBefore;
  if (recoverySnapshots !== 1 || websocketPaths.length !== recoverySocketsBefore ||
      messageSubmissions.length !== recoveryMessagesBefore) {
    throw new Error(`foreground recovery was not one read/no replay: snapshots=${recoverySnapshots} sockets=${websocketPaths.length - recoverySocketsBefore} messages=${messageSubmissions.length - recoveryMessagesBefore}`);
  }
  await page.request.get(`http://127.0.0.1:${port}/response/snapshot429`);
  const snapshotsBefore429 = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.updated', data: { conversation_id: 'conversation-ui', revision: 9 },
  }))));
  await page.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('频繁'));
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (seenPaths.filter(value => value.endsWith('/snapshot')).length >= snapshotsBefore429 + 2) break;
    await page.waitForTimeout(100);
  }
  if (seenPaths.filter(value => value.endsWith('/snapshot')).length < snapshotsBefore429 + 2) {
    throw new Error('snapshot polling did not recover after 429');
  }
  await page.evaluate(() => { document.documentElement.style.fontSize = '20px'; });
  const geometry = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    header: document.querySelector('.native-conversation-head').getBoundingClientRect().height,
    headerScrollHeight: document.querySelector('.native-conversation-head').scrollHeight,
    titleLines: Math.round(document.querySelector('.native-conversation-title').getBoundingClientRect().height /
      parseFloat(getComputedStyle(document.querySelector('.native-conversation-title')).lineHeight || 1)),
    composerWidth: document.querySelector('.native-composer').getBoundingClientRect().width,
    toolsOverflow: document.querySelector('.native-compose-tools').scrollWidth -
      document.querySelector('.native-compose-tools').clientWidth,
    sendRight: document.querySelector('.native-send').getBoundingClientRect().right,
    imageWidth: document.querySelector('.native-image-artifact img').getBoundingClientRect().width,
    downloads: document.querySelectorAll('#conversation-messages a[data-download]').length,
    textExpanded: document.querySelector('.native-text-artifact').open,
  }));
  if (geometry.overflow > 1 || geometry.header > 44.5 || geometry.headerScrollHeight > 44 ||
      geometry.composerWidth > 390 || geometry.toolsOverflow > 1 || geometry.sendRight > 390.5 ||
      geometry.imageWidth < 330) {
    throw new Error(`mobile geometry failed: ${JSON.stringify(geometry)}`);
  }
  if (geometry.downloads !== 6 || !geometry.textExpanded) throw new Error(`artifact cards failed: ${JSON.stringify(geometry)}`);
  for (const width of [280, 320, 360, 390, 412]) {
    await page.setViewportSize({ width, height: 740 });
    const artifactGeometry = await page.evaluate(() => {
      const section = document.querySelector('.native-artifacts');
      const card = document.querySelector('.native-html-artifact');
      return {
        sectionOverflow: section.scrollWidth - section.clientWidth,
        cardRight: card.getBoundingClientRect().right,
      };
    });
    if (artifactGeometry.sectionOverflow > 1 || artifactGeometry.cardRight > width + 1) {
      throw new Error(`long artifact name overflowed ${width}px viewport: ${JSON.stringify(artifactGeometry)}`);
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.fill('#conversation-composer textarea', '继续分析');
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  if (!seenPaths.includes(`/${entryPrefix}/conversation/conversation-ui/messages`)) {
    throw new Error('send did not preserve the public entry prefix');
  }
  const longMessage = '长'.repeat(60001);
  const lengthPrompt = await page.evaluate(longMessage => {
    const input = document.querySelector('#conversation-composer textarea');
    input.value = longMessage;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const counter = document.querySelector('#conversation-message-length');
    return {
      hasMaxLength: input.hasAttribute('maxlength'),
      counter: counter.textContent,
      hidden: counter.hidden,
    };
  }, longMessage);
  if (lengthPrompt.hasMaxLength || lengthPrompt.hidden ||
      lengthPrompt.counter !== '已输入 60,001 字符') {
    throw new Error(`long-message count was not rendered without a limit: ${JSON.stringify(lengthPrompt)}`);
  }
  const messagesBeforeLongPrompt = messageSubmissions.length;
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  if (messageSubmissions.length !== messagesBeforeLongPrompt + 1 ||
      messageSubmissions.at(-1).path !== `/${entryPrefix}/conversation/conversation-ui/messages` ||
      messageSubmissions.at(-1).values.text !== longMessage) {
    throw new Error('long conversation message was not sent as its complete Form payload');
  }
  await page.fill('#conversation-composer textarea', '');
  await page.click('.native-head-menu > summary');
  await page.click('#diagnose-conversation');
  await page.waitForSelector('#conversation-diagnostics[open]');
  const diagnosticText = await page.textContent('#conversation-diagnostics');
  if (!diagnosticText.includes('对话编号') ||
      !diagnosticText.includes('所属项目') ||
      !diagnosticText.includes('Codex Web 工作台维护') || diagnosticText.includes('/home/workbench')) {
    throw new Error('safe diagnostics were not available');
  }
  await page.evaluate(() => {
    window.__copiedDiagnostics = '';
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText(value) {
          window.__copiedDiagnostics = value;
          return Promise.resolve();
        },
      },
    });
  });
  await page.click('#copy-conversation-diagnostics');
  await page.waitForFunction(() => window.__copiedDiagnostics !== '');
  const copiedDiagnostics = await page.evaluate(() => window.__copiedDiagnostics);
  if (!copiedDiagnostics.includes('对话编号：conversation-ui') ||
      !copiedDiagnostics.includes('所属项目：Codex Web 工作台维护') ||
      copiedDiagnostics.includes('对话编号：…') || copiedDiagnostics.includes('/home/workbench')) {
    throw new Error(`copied diagnostics did not preserve the full conversation id: ${copiedDiagnostics}`);
  }
  await page.click('[data-close-diagnostics]');
  await page.click('.native-head-menu > summary');
  await page.click('#export-conversation');
  await page.waitForSelector('#conversation-export-dialog[open]');
  await page.click('#copy-conversation-export');
  await page.waitForFunction(() => window.__copiedDiagnostics.startsWith('# 手机产物'));
  const copiedExport = await page.evaluate(() => window.__copiedDiagnostics);
  if (!copiedExport.includes('所属项目：Codex Web 工作台维护') ||
      !copiedExport.includes('首屏历史记录') ||
      !copiedExport.includes('首屏最近记录仍然可见。')) {
    throw new Error(`copied export was incomplete: ${copiedExport}`);
  }
  if (!seenPaths.includes(`/${entryPrefix}/conversation/conversation-ui/export`)) {
    throw new Error('copy export did not preserve the public entry prefix');
  }
  const transcriptDownloadPromise = page.waitForEvent('download');
  await page.click('#download-conversation-export');
  const exportDownload = await transcriptDownloadPromise;
  if (!exportDownload.suggestedFilename().endsWith('.md')) {
    throw new Error(`export download filename was not markdown: ${exportDownload.suggestedFilename()}`);
  }
  await page.click('[data-close-export]');

  await page.request.get(`http://127.0.0.1:${port}/response/rejected`);
  await page.fill('#conversation-composer textarea', '这条消息必须保留');
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('输入已保留'));
  if (await page.inputValue('#conversation-composer textarea') !== '这条消息必须保留') {
    throw new Error('rejected send cleared the draft');
  }
  await page.request.get(`http://127.0.0.1:${port}/response/success`);

  await page.fill('#conversation-composer textarea', '损坏快照也要保留明确拒绝结果');
  await page.request.get(`http://127.0.0.1:${port}/response/malformedRejected`);
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('数据不完整'));
  if (await page.evaluate(() => localStorage.getItem('codex-conversation-pending:conversation-ui'))) {
    throw new Error('malformed rejected snapshot skipped the definitive pending cleanup');
  }
  if (await page.locator('[data-message-key^="pending:"]').count()) {
    throw new Error('malformed rejected receipt left a sending placeholder');
  }
  await page.request.get(`http://127.0.0.1:${port}/response/success`);

  await selectHidden('#conversation-permission', 'workspace-write');
  await page.fill('#conversation-composer textarea', '设置失败时消息不能被吃掉');
  const messagesBeforeSettingsFailure = seenPaths.filter(value => value.endsWith('/messages')).length;
  await page.request.get(`http://127.0.0.1:${port}/response/settingsRejected`);
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('设置没有保存'));
  const settingsRejectedPending = await page.locator('[data-message-key^="pending:"]').allTextContents();
  if (seenPaths.filter(value => value.endsWith('/messages')).length !== messagesBeforeSettingsFailure ||
      await page.inputValue('#conversation-composer textarea') !== '设置失败时消息不能被吃掉' ||
      !await page.evaluate(() => localStorage.getItem('codex-conversation-pending:conversation-ui')) ||
      settingsRejectedPending.length !== 1 || !settingsRejectedPending[0].includes('你 · 未发送') ||
      settingsRejectedPending[0].includes('发送中')) {
    throw new Error('settings rejection was misclassified as a rejected message');
  }
  await page.request.get(`http://127.0.0.1:${port}/response/success`);
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');

  await selectHidden('#conversation-permission', 'workspace-write');
  await page.fill('#conversation-composer textarea', '登录过期时保留设置后的消息');
  const messagesBeforeSettingsAuth = seenPaths.filter(value => value.endsWith('/messages')).length;
  await page.request.get(`http://127.0.0.1:${port}/response/authSettings`);
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('登录已失效'));
  if (seenPaths.filter(value => value.endsWith('/messages')).length !== messagesBeforeSettingsAuth ||
      await page.inputValue('#conversation-composer textarea') !== '登录过期时保留设置后的消息') {
    throw new Error('settings auth failure sent or cleared the pending message');
  }
  await page.request.get(`http://127.0.0.1:${port}/response/success`);
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');

  for (const mode of ['authMessages', 'htmlMessages', 'redirectLogin', 'malformedMessages']) {
    const text = `异常响应仍保留输入-${mode}`;
    const expectedNote = {
      authMessages: '登录已失效',
      htmlMessages: '无法识别',
      redirectLogin: '登录状态可能已失效',
      malformedMessages: '数据不完整',
    }[mode];
    await page.fill('#conversation-composer textarea', text);
    await page.request.get(`http://127.0.0.1:${port}/response/${mode}`);
    await page.click('.native-send');
    await page.waitForFunction(({ text, expectedNote }) =>
      document.querySelector('#conversation-composer textarea').value === text &&
      document.querySelector('#conversation-note').textContent.includes(expectedNote) &&
      !document.querySelector('.native-send').disabled,
      { text, expectedNote });
    if (!await page.evaluate(() => localStorage.getItem('codex-conversation-pending:conversation-ui'))) {
      throw new Error(`${mode} cleared pending local storage`);
    }
    await page.request.get(`http://127.0.0.1:${port}/response/success`);
    await page.click('.native-send');
    await page.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  }

  // A restart page after POST leaves delivery ambiguous.  It must visibly
  // leave "sending", survive a reload and its read-only 404 receipt probe,
  // and reuse the durable command ID only when the user explicitly retries.
  const restartText = '重启期间的消息须待核对';
  const restartPostsBefore = messageSubmissions.length;
  await page.fill('#conversation-composer textarea', restartText);
  await page.request.get(`http://127.0.0.1:${port}/response/html500Messages`);
  await page.click('.native-send');
  await page.waitForFunction(() => document.querySelector('[data-message-key^="pending:"]').textContent.includes('发送中'));
  await page.waitForFunction(() =>
    document.querySelector('#conversation-note').textContent.includes('工作台暂时不可用') &&
    document.querySelector('[data-message-key^="pending:"]').textContent.includes('发送结果待核对') &&
    !document.querySelector('.native-send').disabled);
  const restartUncertain = await page.evaluate(() => ({
    value: document.querySelector('#conversation-composer textarea').value,
    pending: JSON.parse(localStorage.getItem('codex-conversation-pending:conversation-ui') || 'null'),
    bubble: document.querySelector('[data-message-key^="pending:"]').textContent,
  }));
  if (restartUncertain.value !== restartText || !restartUncertain.pending ||
      restartUncertain.bubble.includes('发送中') || !restartUncertain.bubble.includes('发送结果待核对') ||
      messageSubmissions.length !== restartPostsBefore + 1 ||
      receiptOverrides[restartUncertain.pending.id] !== 'missing') {
    throw new Error(`500 HTML response did not retain an unconfirmed pending command: ${JSON.stringify(restartUncertain)}`);
  }
  const restartId = restartUncertain.pending.id;
  const restartReceiptPath = `/${entryPrefix}/conversation/conversation-ui/actions/${encodeURIComponent(restartId)}`;
  const receiptReadsBeforeReload = seenPaths.filter(value => value === restartReceiptPath).length;
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(text =>
    document.querySelector('#conversation-composer textarea').value === text &&
    document.querySelector('[data-message-key^="pending:"]').textContent.includes('发送结果待核对'), restartText);
  await page.waitForFunction(path => window.performance.getEntriesByType('resource').some(entry =>
    new URL(entry.name).pathname === path), restartReceiptPath);
  const restartAfterReload = await page.evaluate(() => ({
    value: document.querySelector('#conversation-composer textarea').value,
    pending: JSON.parse(localStorage.getItem('codex-conversation-pending:conversation-ui') || 'null'),
    bubble: document.querySelector('[data-message-key^="pending:"]').textContent,
  }));
  if (restartAfterReload.value !== restartText || !restartAfterReload.pending ||
      restartAfterReload.pending.id !== restartId || restartAfterReload.bubble.includes('发送中') ||
      seenPaths.filter(value => value === restartReceiptPath).length <= receiptReadsBeforeReload ||
      messageSubmissions.filter(submission => submission.values.command_id === restartId).length !== 1) {
    throw new Error(`reload replayed or relabeled the 500 pending command: ${JSON.stringify(restartAfterReload)}`);
  }
  await page.request.get(`http://127.0.0.1:${port}/response/success`);
  await page.click('.native-send');
  await page.waitForFunction(() => !localStorage.getItem('codex-conversation-pending:conversation-ui') &&
    document.querySelector('#conversation-composer textarea').value === '' &&
    !document.querySelector('[data-message-key^="pending:"]'));
  const restartPosts = messageSubmissions.filter(submission => submission.values.command_id === restartId);
  if (restartPosts.length !== 2 || restartPosts.some(submission => submission.values.text !== restartText)) {
    throw new Error(`explicit retry did not reuse and settle the original command: ${JSON.stringify(restartPosts)}`);
  }

  await page.setViewportSize({ width: 320, height: 740 });
  await page.click('#settings-summary');
  const settingsRect = await page.locator('.native-settings-panel').evaluate(node => {
    const rect = node.getBoundingClientRect();
    return { left: rect.left, right: rect.right, width: rect.width };
  });
  if (settingsRect.left < -1 || settingsRect.right > 321) {
    throw new Error(`settings overflowed narrow viewport: ${JSON.stringify(settingsRect)}`);
  }
  await page.click('[data-close-settings]');
  await page.setViewportSize({ width: 390, height: 844 });
  currentServiceTier = 'priority';
  await page.reload({ waitUntil: 'networkidle' });
  if ((await page.textContent('#conversation-tier-toggle')).trim() !== '⚡Fast') {
    throw new Error('Fast tier did not render as enabled before toggle regression');
  }
  const tierSubmissionsBefore = serviceTierSubmissions.length;
  await page.click('#conversation-tier-toggle');
  await page.waitForFunction(() => document.querySelector('#conversation-tier-toggle').textContent.trim() === '标准');
  for (let attempt = 0; attempt < 30 && serviceTierSubmissions.length === tierSubmissionsBefore; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (serviceTierSubmissions.at(-1) !== '') {
    throw new Error(`Fast tier did not submit an explicit Standard value: ${JSON.stringify(serviceTierSubmissions)}`);
  }
  await page.click('#conversation-tier-toggle');
  await page.waitForFunction(() => document.querySelector('#conversation-tier-toggle').textContent.trim() === '⚡Fast');
  for (let attempt = 0; attempt < 30 && serviceTierSubmissions.length === tierSubmissionsBefore + 1; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (serviceTierSubmissions.at(-1) !== 'priority') {
    throw new Error(`Standard tier did not switch back to Fast: ${JSON.stringify(serviceTierSubmissions)}`);
  }
  const fastSubmission = settingsSubmissions.at(-1);
  if (!fastSubmission || Object.keys(fastSubmission).sort().join(',') !== 'command_id,service_tier') {
    throw new Error(`Fast submitted unchanged turn settings: ${JSON.stringify(fastSubmission)}`);
  }

  const delayedTierSubmissions = serviceTierSubmissions.length;
  await page.request.get(`http://127.0.0.1:${port}/response/settingsDelayed`);
  await page.click('#conversation-tier-toggle');
  await page.waitForFunction(() => document.querySelector('#conversation-tier-toggle').textContent.trim() === '标准');
  for (let attempt = 0; attempt < 30 && serviceTierSubmissions.length === delayedTierSubmissions; attempt += 1) {
    await page.waitForTimeout(50);
  }
  await page.click('#conversation-tier-toggle');
  await page.waitForFunction(() => document.querySelector('#conversation-tier-toggle').textContent.trim() === '⚡Fast');
  await page.request.get(`http://127.0.0.1:${port}/response/settingsRelease`);
  for (let attempt = 0; attempt < 40 && serviceTierSubmissions.length < delayedTierSubmissions + 2; attempt += 1) {
    await page.waitForTimeout(50);
  }
  const delayedTierResult = await page.evaluate(() => ({
    tier: document.querySelector('#conversation-tier').value,
    button: document.querySelector('#conversation-tier-toggle').textContent.trim(),
    toast: document.querySelector('#conversation-toast').textContent,
  }));
  if (serviceTierSubmissions.length !== delayedTierSubmissions + 2 ||
      serviceTierSubmissions.at(-1) !== 'priority' || delayedTierResult.tier !== 'priority' ||
      delayedTierResult.button !== '⚡Fast' || !delayedTierResult.toast.includes('Fast')) {
    throw new Error(`rapid Fast/Standard edits lost intent or reported stale state: ${JSON.stringify({ serviceTierSubmissions, delayedTierResult })}`);
  }

  await page.request.get(`http://127.0.0.1:${port}/response/settingsRejected`);
  await page.click('#conversation-tier-toggle');
  await page.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('设置没有保存'));
  const rejectedTierResult = await page.evaluate(() => ({
    tier: document.querySelector('#conversation-tier').value,
    button: document.querySelector('#conversation-tier-toggle').textContent.trim(),
  }));
  if (rejectedTierResult.tier !== 'priority' || rejectedTierResult.button !== '⚡Fast') {
    throw new Error(`rejected tier update left a false saved value: ${JSON.stringify(rejectedTierResult)}`);
  }
  await page.request.get(`http://127.0.0.1:${port}/response/success`);

  const timelinePage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await timelinePage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-timeline`,
    { waitUntil: 'networkidle' },
  );
  async function timelineOrder() {
    return timelinePage.locator('#conversation-messages .native-message-body').allTextContents();
  }
  const expectedTimeline = ['14:18 首条消息', '14:19 排队消息', '14:21 过程消息', '14:25 过程消息'];
  if (JSON.stringify(await timelineOrder()) !== JSON.stringify(expectedTimeline)) {
    throw new Error(`queued message was not shown at its submission time: ${JSON.stringify(await timelineOrder())}`);
  }
  await timelinePage.request.get(`http://127.0.0.1:${port}/timeline/bind`);
  await timelinePage.reload({ waitUntil: 'networkidle' });
  if (JSON.stringify(await timelineOrder()) !== JSON.stringify(expectedTimeline)) {
    throw new Error(`bound queued message moved after refresh: ${JSON.stringify(await timelineOrder())}`);
  }
  await timelinePage.close();
  await page.request.get(`http://127.0.0.1:${port}/scenario/artifacts`);
  const deepLinkPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await deepLinkPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-deep-link?turn=turn-1`,
    { waitUntil: 'domcontentloaded' },
  );
  await deepLinkPage.waitForSelector('[data-turn-id="turn-1"].native-turn-highlight');
  if (!deepLinkPage.url().includes(`/${entryPrefix}/conversation/conversation-deep-link?turn=turn-1`)) {
    throw new Error(`turn deep link lost its public entry prefix: ${deepLinkPage.url()}`);
  }
  await deepLinkPage.close();
  if (process.env.CODEX_UI_ARTIFACTS_SCREENSHOT) {
    await page.locator('.native-artifacts').first().scrollIntoViewIfNeeded();
    await page.screenshot({ path: process.env.CODEX_UI_ARTIFACTS_SCREENSHOT });
  }
  if (process.env.CODEX_UI_DOWNLOADS_SCREENSHOT) {
    await page.locator('.native-file-card').last().evaluate(node => {
      node.scrollIntoView({ block: 'center' });
    });
    await page.screenshot({ path: process.env.CODEX_UI_DOWNLOADS_SCREENSHOT });
  }

  const pagesBeforePreview = browser.contexts()[0].pages().length;
  const conversationUrl = page.url();
  await page.click('.native-image-artifact .native-artifact-preview');
  await page.waitForSelector('#conversation-artifact-viewer:not([hidden])');
  const viewerFacts = await page.evaluate(() => ({
    source: document.querySelector('#artifact-viewer-image').src,
    cardSource: document.querySelector('.native-image-artifact img').src,
    naturalWidth: document.querySelector('#artifact-viewer-image').naturalWidth,
  }));
  if (browser.contexts()[0].pages().length !== pagesBeforePreview ||
      viewerFacts.source !== viewerFacts.cardSource || viewerFacts.naturalWidth < 1) {
    throw new Error(`image did not use the same-page original viewer: ${JSON.stringify(viewerFacts)}`);
  }
  if (process.env.CODEX_UI_PREVIEW_SCREENSHOT) await page.screenshot({ path: process.env.CODEX_UI_PREVIEW_SCREENSHOT });
  await page.goBack();
  await page.waitForFunction(() => document.querySelector('#conversation-artifact-viewer').hidden);
  if (page.url() !== conversationUrl) throw new Error(`Android Back left the conversation: ${page.url()}`);

  const pagesBeforeHtmlPreview = browser.contexts()[0].pages().length;
  const conversationScale = await page.evaluate(() => visualViewport.scale);
  await page.click('.native-html-artifact .native-file-open');
  await page.waitForURL(`**/${entryPrefix}/conversation/conversation-ui/artifact/html-1/preview?viewer=1`);
  const htmlViewerFacts = await page.evaluate(() => {
    const frame = document.querySelector('#artifact-viewer-html-frame');
    const viewer = document.querySelector('.artifact-viewer');
    const head = document.querySelector('.artifact-viewer-head');
    const download = document.querySelector('#artifact-viewer-download');
    const title = document.querySelector('#artifact-viewer-title');
    return {
      conversationVisible: Boolean(document.querySelector('#native-conversation')),
      sandbox: frame.getAttribute('sandbox'),
      source: new URL(frame.src, location.href).pathname,
      notice: document.querySelector('.artifact-viewer-notice').textContent,
      download: new URL(download.href, location.href).pathname,
      viewport: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      viewerRight: viewer.getBoundingClientRect().right,
      headRight: head.getBoundingClientRect().right,
      frameRight: frame.getBoundingClientRect().right,
      downloadRight: download.getBoundingClientRect().right,
      titleOverflow: title.scrollWidth > title.clientWidth,
    };
  });
  if (browser.contexts()[0].pages().length !== pagesBeforeHtmlPreview ||
      htmlViewerFacts.conversationVisible ||
      htmlViewerFacts.sandbox !== 'allow-scripts' ||
      htmlViewerFacts.source !== `/${entryPrefix}/conversation/conversation-ui/artifact/html-1/preview` ||
      htmlViewerFacts.download !== `/${entryPrefix}/conversation/conversation-ui/artifact/html-1` ||
      htmlViewerFacts.scrollWidth > htmlViewerFacts.viewport + 1 ||
      Math.max(htmlViewerFacts.viewerRight, htmlViewerFacts.headRight,
        htmlViewerFacts.frameRight, htmlViewerFacts.downloadRight) > htmlViewerFacts.viewport + 1 ||
      !htmlViewerFacts.titleOverflow ||
      !htmlViewerFacts.notice.includes('已隔离工作台数据并限制外部资源')) {
    throw new Error(`HTML artifact did not open in a restricted dedicated viewer: ${JSON.stringify(htmlViewerFacts)}`);
  }
  for (const width of [280, 300, 320, 375]) {
    await page.setViewportSize({ width, height: 740 });
    const narrow = await page.evaluate(() => {
      const rightEdges = [
        '.artifact-viewer', '.artifact-viewer-head',
        '#artifact-viewer-html-frame', '#artifact-viewer-download',
      ].map(selector => document.querySelector(selector).getBoundingClientRect().right);
      const title = document.querySelector('#artifact-viewer-title');
      return {
        viewport: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        maxRight: Math.max(...rightEdges),
        titleOverflow: title.scrollWidth > title.clientWidth,
      };
    });
    if (narrow.scrollWidth > narrow.viewport + 1 ||
        narrow.maxRight > narrow.viewport + 1 || !narrow.titleOverflow) {
      throw new Error(`HTML viewer overflowed ${width}px viewport: ${JSON.stringify(narrow)}`);
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  const htmlFrame = page.frameLocator('#artifact-viewer-html-frame');
  await htmlFrame.locator('#inside').click();
  if (await htmlFrame.locator('#result').textContent() !== '交互成功') {
    throw new Error('restricted HTML preview lost its self-contained interaction');
  }
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setPageScaleFactor', { pageScaleFactor: 1.5 });
  const previewScale = await page.evaluate(() => visualViewport.scale);
  if (previewScale < 1.4) {
    throw new Error(`CDP could not set dedicated preview scale: ${previewScale}`);
  }
  await page.click('#artifact-viewer-back');
  await page.waitForURL(`**/${entryPrefix}/conversation/conversation-ui`);
  await page.waitForSelector('#native-conversation');
  const restoredConversationScale = await page.evaluate(() => visualViewport.scale);
  if (Math.abs(restoredConversationScale - conversationScale) > 0.05) {
    throw new Error(`conversation scale was not restored after preview: before=${conversationScale}, after=${restoredConversationScale}`);
  }

  await page.click('.native-html-artifact .native-file-open');
  await page.waitForURL(`**/${entryPrefix}/conversation/conversation-ui/artifact/html-1/preview?viewer=1`);
  await cdp.send('Emulation.setPageScaleFactor', { pageScaleFactor: 1.5 });
  await page.goBack();
  await page.waitForURL(`**/${entryPrefix}/conversation/conversation-ui`);
  const systemBackScale = await page.evaluate(() => visualViewport.scale);
  if (Math.abs(systemBackScale - conversationScale) > 0.05 ||
      await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)) {
    throw new Error(`Android Back polluted conversation scale or width: ${systemBackScale}`);
  }

  const htmlDownloadPromise = page.waitForEvent('download');
  await page.click('.native-html-artifact > a[data-download]');
  const htmlDownload = await htmlDownloadPromise;
  await htmlDownload.cancel();
  if (!await page.locator('#conversation-artifact-viewer').evaluate(node => node.hidden)) {
    throw new Error('explicit HTML download unexpectedly opened the preview viewer');
  }

  const downloadPromise = page.waitForEvent('download');
  await page.click('.native-file-card .native-download');
  const download = await downloadPromise;
  await page.waitForFunction(() => !document.querySelector('#conversation-toast').hidden &&
    document.querySelector('#conversation-toast').textContent.includes('浏览器和系统决定'));
  await download.cancel();
  if (!seenPaths.some(value => value === `/${entryPrefix}/conversation/conversation-ui/artifact/word-1`)) {
    throw new Error('artifact download did not preserve the public entry prefix');
  }

  await page.request.get(`http://127.0.0.1:${port}/scenario/running`);
  await page.evaluate(() => localStorage.setItem(
    'codex-round-nav-position', JSON.stringify({ x: 9999, y: 160 })
  ));
  await page.evaluate(() => localStorage.removeItem('codex-conversation-commentary-group:conversation-ui:turn-1'));
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.native-state.state-active');
  await assertStopAppearance(page, false, 'running');
  const runningProcess = await page.evaluate(() => ({
    cards: document.querySelectorAll('.native-live-commentary').length,
    text: document.querySelector('#conversation-messages').textContent,
    serverActivity: document.querySelector('.native-server-activity') &&
      document.querySelector('.native-server-activity').textContent,
    state: document.querySelector('#conversation-state').textContent,
    roundRight: document.querySelector('.native-round-nav').getBoundingClientRect().right,
    groups: document.querySelectorAll('.native-commentary-group').length,
    folded: document.querySelector('.native-commentary-group').classList.contains('is-group-folded'),
  }));
  const runningOrder = [
    '分析未来十日天气，并生成图片和报告。', '我先核对温度趋势。',
    '追问：请把清晨体感也说清楚。', '正在补充清晨体感并整理报告。',
  ].map(value => runningProcess.text.indexOf(value));
  if (runningProcess.cards !== 1 || runningProcess.roundRight > 390.5 ||
      runningProcess.groups !== 1 || runningProcess.folded ||
      !runningProcess.serverActivity || !runningProcess.serverActivity.includes('仍在执行') ||
      !runningProcess.serverActivity.includes('实时连接正常') ||
      runningOrder.some(value => value < 0) ||
      !runningOrder.every((value, index, values) => index === 0 || value > values[index - 1]) ||
      runningProcess.text.includes('我先核对温度趋势，再把结论和产物整理成简洁答复。') ||
      runningProcess.text.includes('completed: 收集数据') ||
      runningProcess.text.includes('generate_report.py')) {
    throw new Error(`running natural timeline/noise filtering failed: ${JSON.stringify(runningProcess)}`);
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/artifacts`);
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.updated', data: { conversation_id: 'conversation-ui', revision: 10 },
  }))));
  await page.waitForFunction(() =>
    document.querySelector('.native-state').classList.contains('state-idle'));
  await assertStopAppearance(page, true, 'completed after running');
  const stopsBeforeIdle = stopSubmissions.length;
  page.once('dialog', dialog => dialog.accept());
  await page.click('#stop-conversation');
  for (let i = 0; i < 30 && stopSubmissions.length === stopsBeforeIdle; i++) await page.waitForTimeout(50);
  if (stopSubmissions.length !== stopsBeforeIdle + 1 ||
      stopSubmissions[stopsBeforeIdle].scope !== 'conversation_tree') {
    throw new Error('quiet completed stop lost its whole-conversation action');
  }
  await page.waitForFunction(() => !document.querySelector('#stop-conversation').disabled);
  const completedAfterRunning = await page.evaluate(() => ({
    folded: document.querySelector('.native-commentary-group').classList.contains('is-group-folded'),
    summary: document.querySelector('.native-commentary-group-head').textContent,
    serverActivity: Boolean(document.querySelector('.native-server-activity')),
  }));
  if (!completedAfterRunning.folded || completedAfterRunning.serverActivity ||
      !completedAfterRunning.summary.includes('我先核对温度趋势。')) {
    throw new Error(`completed turn did not automatically collect its commentary: ${JSON.stringify(completedAfterRunning)}`);
  }
  await page.click('[data-commentary-group-action]');
  const socketsBeforeCommentaryReload = websocketPaths.length;
  await page.reload({ waitUntil: 'networkidle' });
  for (let attempt = 0; attempt < 30 && websocketPaths.length <= socketsBeforeCommentaryReload; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (websocketPaths.length <= socketsBeforeCommentaryReload) {
    throw new Error('conversation websocket did not reconnect after commentary preference reload');
  }
  if (await page.locator('.native-commentary-group').evaluate(node => node.classList.contains('is-group-folded'))) {
    throw new Error('expanded commentary group did not retain its browser-local choice');
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/ambiguous-stop`);
  await page.reload({waitUntil:'networkidle'});
  if (await page.locator('.native-stop').isHidden()) throw new Error('ambiguous state has no whole-conversation stop');
  await page.waitForFunction(() => document.body.textContent.includes('多个未结束轮次'));
  await assertStopAppearance(page, false, 'ambiguous stop');
  const stopsBeforeAmbiguous = stopSubmissions.length;
  page.once('dialog', dialog => dialog.accept());
  await page.click('.native-stop');
  for (let i=0;i<30 && stopSubmissions.length === stopsBeforeAmbiguous;i++) await page.waitForTimeout(50);
  const ambiguousStop = stopSubmissions[stopSubmissions.length-1];
  if (!ambiguousStop || ambiguousStop.scope !== 'conversation_tree' || ambiguousStop.expected_turn_id) {
    throw new Error('ambiguous stop guessed current turn or omitted tree scope');
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/unconfirmed-stop`);
  await page.reload({waitUntil:'networkidle'});
  await page.waitForFunction(() => document.body.textContent.includes('停止结果待核对'));
  await assertStopAppearance(page, false, 'unconfirmed stop');
  if (await page.locator('.native-stop').isHidden() || (await page.textContent('.native-state')).includes('已停止')) {
    throw new Error('partial stop was falsely reported complete or cannot be retried');
  }
  const reconcilesBefore = seenPaths.filter(value => value.endsWith('/reconcile')).length;
  const stopsBeforeRefresh = stopSubmissions.length;
  await page.click('#reconcile-conversation');
  for (let i=0;i<30 && seenPaths.filter(value => value.endsWith('/reconcile')).length === reconcilesBefore;i++) await page.waitForTimeout(50);
  if (seenPaths.filter(value => value.endsWith('/reconcile')).length === reconcilesBefore || stopSubmissions.length !== stopsBeforeRefresh) {
    throw new Error('refresh did not verify pending stop readonly, or resent interrupt');
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/idle-unconfirmed-stop`);
  await page.reload({waitUntil:'networkidle'});
  await assertStopAppearance(page, false, 'idle root with unconfirmed agents');
  await page.request.get(`http://127.0.0.1:${port}/scenario/verified-stop-background`);
  await page.reload({waitUntil:'networkidle'});
  await assertStopAppearance(page, true, 'verified agent stop with background command');
  const agentStopState=await page.textContent('.native-state');
  const backgroundNote=await page.textContent('#conversation-note');
  if (!agentStopState.includes('代理已停止') || agentStopState.includes('全部') ||
      !backgroundNote.includes('1 个原生后台命令') || !backgroundNote.includes('未主动终止')) {
    throw new Error('agent stop hid the continuing background test or claimed all execution stopped');
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/legacy-running`);
  await page.reload({waitUntil:'networkidle'});
  const legacyStopsBefore = stopSubmissions.length;
  page.once('dialog', dialog => dialog.accept());
  await page.click('.native-stop');
  for (let i=0;i<30 && stopSubmissions.length === legacyStopsBefore;i++) await page.waitForTimeout(50);
  const legacyStop = stopSubmissions[stopSubmissions.length-1];
  if (!legacyStop || legacyStop.scope !== 'root_turn' || !legacyStop.expected_turn_id) {
    throw new Error('supported old protocol lost its exact current-turn stop');
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/running`);
  websocketClients.forEach(socket => socket.write(websocketFrame(JSON.stringify({
    type: 'conversation.updated', data: { conversation_id: 'conversation-ui', revision: 12 },
  }))));
  await page.waitForSelector('.native-state.state-active');
  if (await page.locator('.native-stop').isHidden()) throw new Error('active turn did not expose stop');
  await assertStopAppearance(page, false, 'running after idle');
  const interruptsBefore = seenPaths.filter(value => value.endsWith('/interrupt')).length;
  page.once('dialog', dialog => dialog.dismiss());
  await page.click('.native-stop');
  await page.waitForTimeout(150);
  if (seenPaths.filter(value => value.endsWith('/interrupt')).length !== interruptsBefore) {
    throw new Error('dismissed stop confirmation still interrupted the turn');
  }
  page.once('dialog', dialog => dialog.accept());
  await page.click('.native-stop');
  for (let attempt = 0; attempt < 30 && seenPaths.filter(value => value.endsWith('/interrupt')).length === interruptsBefore; attempt += 1) {
    await page.waitForTimeout(50);
  }
  if (!seenPaths.includes(`/${entryPrefix}/conversation/conversation-ui/interrupt`) ||
      seenPaths.filter(value => value.endsWith('/interrupt')).length <= interruptsBefore) {
    throw new Error('interrupt did not preserve the public entry prefix');
  }
  if (process.env.CODEX_UI_RUNNING_SCREENSHOT) await page.screenshot({ path: process.env.CODEX_UI_RUNNING_SCREENSHOT });

  await page.request.get(`http://127.0.0.1:${port}/scenario/interrupted`);
  await page.reload({ waitUntil: 'networkidle' });
  await assertStopAppearance(page, true, 'interrupted');
  const interruptedText = await page.textContent('#conversation-messages');
  if (!interruptedText.includes('本轮已停止') || !interruptedText.includes('重新发送')) {
    throw new Error('interrupted turn was still visually silent');
  }
  if (!await page.locator('[data-turn-id="turn-stopped"]').count()) {
    throw new Error('interrupted turn cannot be targeted by a notification deep link');
  }
  if (await page.locator('.native-commentary-group').count() ||
      !await page.locator('.native-message.agent .native-message-meta').first().textContent().then(value => value.includes('字'))) {
    throw new Error('single commentary gained a group entry or lost its existing character count');
  }

  await page.request.get(`http://127.0.0.1:${port}/scenario/terminal-failed`);
  await page.reload({ waitUntil: 'networkidle' });
  const terminalFailure = await page.evaluate(() => ({
    state: document.querySelector('#conversation-state').textContent,
    note: document.querySelector('#conversation-note').textContent,
    messages: document.querySelector('#conversation-messages').textContent,
  }));
  await page.click('.native-head-menu > summary');
  await page.click('#diagnose-conversation');
  const failedDiagnostics = await page.evaluate(() => {
    const labels = Array.from(document.querySelectorAll('#conversation-diagnostics dt'));
    const label = labels.find(node => node.textContent === '页面状态');
    return label && label.nextElementSibling && label.nextElementSibling.textContent;
  });
  if (terminalFailure.state !== '执行失败' ||
      !terminalFailure.note.includes('未生成最终答复') ||
      !terminalFailure.messages.includes('模型服务暂时繁忙') ||
      terminalFailure.messages.includes('Selected model is at capacity') ||
      failedDiagnostics !== '执行失败') {
    throw new Error(`failed terminal state was misreported: ${JSON.stringify({ terminalFailure, failedDiagnostics })}`);
  }
  if (!await page.locator('[data-turn-id="turn-1"]').count()) {
    throw new Error('failed turn cannot be targeted by a notification deep link');
  }

  await page.request.get(`http://127.0.0.1:${port}/scenario/new`);
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.native-empty');
  if ((await page.textContent('body')).includes('执行后端')) throw new Error('backend selector leaked into new conversation');
  if (process.env.CODEX_UI_NEW_SCREENSHOT) await page.screenshot({ path: process.env.CODEX_UI_NEW_SCREENSHOT });

  await page.request.get(`http://127.0.0.1:${port}/scenario/failures`);
  await page.reload({ waitUntil: 'networkidle' });
  const failureText = await page.textContent('#conversation-messages');
  if (!failureText.includes('明确没有发出的消息') || !failureText.includes('未发送') ||
      !failureText.includes('需要核对投递的消息') || !failureText.includes('投递待核对') ||
      await page.locator('.native-empty').count()) {
    throw new Error('unbound failed messages were hidden from the conversation');
  }

  // Validate visible failure/safety contracts, not implementation token spelling.
  for (const [failureScene, label] of [['artifact-failure-only', '失败的文件未发送'], ['artifact-failure-partial', '部分未发送']]) {
    await page.request.get(`http://127.0.0.1:${port}/scenario/${failureScene}`);
    await page.reload({waitUntil: 'networkidle'});
    await page.waitForFunction(label => document.querySelector('#conversation-messages').textContent.includes(label), label);
  }
  for (const [syncScene, operation] of [['sync-disconnected', '同步未完成'], ['sync-needs-user', '仍需核对']]) {
    await page.request.get(`http://127.0.0.1:${port}/scenario/${syncScene}`);
    await page.reload({waitUntil: 'networkidle'});
    await page.click('.native-head-menu > summary');
    await page.click('#diagnose-conversation');
    await page.click('#diagnostics-reconcile');
    await page.waitForFunction(operation => {
      const label = [...document.querySelectorAll('#conversation-diagnostics dt')].find(node => node.textContent === '最近操作');
      return label && label.nextElementSibling.textContent === operation;
    }, operation);
    if ((await page.textContent('#conversation-note')).includes('同步完成')) throw new Error('uncertain native sync claimed success');
  }
  await page.request.get(`http://127.0.0.1:${port}/scenario/unsafe-content`);
  await page.reload({waitUntil: 'networkidle'});
  await page.waitForSelector('.native-final .native-message-body[data-native-enhanced]');
  const safety = await page.evaluate(() => ({
    executed: Boolean(window.__unsafeMessageExecuted),
    injected: document.querySelectorAll('#conversation-messages img[src="x"],#conversation-messages script,#conversation-messages a[href^="javascript:"]').length,
    text: document.querySelector('#conversation-messages').textContent,
  }));
  if (safety.executed || safety.injected || !safety.text.includes('<img') || safety.text.includes('fixture-secret-')) {
    throw new Error(`untrusted message or private artifact path escaped its boundary: ${JSON.stringify(safety)}`);
  }
  const draftPage = await browser.newPage();
  await draftPage.goto(`http://127.0.0.1:${port}/draft-contract`);
  await draftPage.setContent('<form id="thread-send"></form><form id="actual-owner"><textarea data-draft-key="fixture-owning-form"></textarea></form>');
  await draftPage.evaluate(() => document.querySelectorAll('form').forEach(form => form.addEventListener('submit', event => event.preventDefault())));
  await draftPage.addScriptTag({content: draftJs.toString()});
  await draftPage.fill('textarea', '独立表单草稿');
  await draftPage.dispatchEvent('#thread-send', 'submit');
  if (await draftPage.evaluate(() => localStorage.getItem('fixture-owning-form')) !== '独立表单草稿') throw new Error('unrelated form cleared composer draft');
  await draftPage.dispatchEvent('#actual-owner', 'submit');
  if (await draftPage.evaluate(() => localStorage.getItem('fixture-owning-form')) !== null) throw new Error('owning form did not clear its sent draft');
  await draftPage.close();
  await page.request.get(`http://127.0.0.1:${port}/scenario/failures`);
  await page.reload({waitUntil: 'networkidle'});
  await page.request.get(`http://127.0.0.1:${port}/response/snapshotAuth`);
  const authSnapshotsBefore = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  const authSocketsBefore = websocketPaths.length;
  const authPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const authErrors = [];
  authPage.on('pageerror', error => authErrors.push(error.message));
  await authPage.addInitScript(() => {
    if (window !== window.top) return;
    localStorage.setItem(
      'codex-conversation-pending:conversation-ui',
      JSON.stringify({ id: 'm-auth', text: '登录过期后恢复的输入', action: 'messages' })
    );
  });
  await authPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await authPage.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('登录已失效'));
  if (await authPage.inputValue('#conversation-composer textarea') !== '登录过期后恢复的输入' || authErrors.length) {
    throw new Error(`snapshot auth failure lost input or crashed: ${authErrors.join('; ')}`);
  }
  await authPage.waitForTimeout(5200);
  const authSnapshotCount = seenPaths.filter(value => value.endsWith('/snapshot')).length - authSnapshotsBefore;
  const authSocketCount = websocketPaths.length - authSocketsBefore;
  if (authSnapshotCount !== 1 || authSocketCount > 1) {
    throw new Error(`auth failure kept polling or reconnecting: snapshots=${authSnapshotCount} sockets=${authSocketCount}`);
  }
  await authPage.close();
  await page.request.get(`http://127.0.0.1:${port}/response/success`);

  await page.request.get(`http://127.0.0.1:${port}/response/snapshotForbidden`);
  const forbiddenHttpSnapshotsBefore = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  const forbiddenHttpSocketsBefore = websocketPaths.length;
  const forbiddenHttpPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await forbiddenHttpPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await forbiddenHttpPage.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('无权'));
  await forbiddenHttpPage.waitForTimeout(5200);
  const forbiddenHttpSnapshotCount = seenPaths.filter(value => value.endsWith('/snapshot')).length - forbiddenHttpSnapshotsBefore;
  const forbiddenHttpSocketCount = websocketPaths.length - forbiddenHttpSocketsBefore;
  if (forbiddenHttpSnapshotCount !== 1 || forbiddenHttpSocketCount > 1) {
    throw new Error(`HTTP 403 kept polling or reconnecting: snapshots=${forbiddenHttpSnapshotCount} sockets=${forbiddenHttpSocketCount}`);
  }
  await forbiddenHttpPage.close();
  await page.request.get(`http://127.0.0.1:${port}/response/success`);

  await page.request.get(`http://127.0.0.1:${port}/response/snapshotMalformed`);
  const malformedSnapshotsBefore = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  const malformedSocketsBefore = websocketPaths.length;
  const malformedPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await malformedPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await malformedPage.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('数据不完整'));
  await malformedPage.waitForTimeout(5200);
  const malformedSnapshotCount = seenPaths.filter(value => value.endsWith('/snapshot')).length - malformedSnapshotsBefore;
  const malformedSocketCount = websocketPaths.length - malformedSocketsBefore;
  if (malformedSnapshotCount !== 1 || malformedSocketCount > 1) {
    throw new Error(`malformed snapshot kept polling or reconnecting: snapshots=${malformedSnapshotCount} sockets=${malformedSocketCount}`);
  }
  await malformedPage.close();
  await page.request.get(`http://127.0.0.1:${port}/response/success`);

  await page.request.get(`http://127.0.0.1:${port}/scenario/running`);
  const forbiddenPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await forbiddenPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await forbiddenPage.waitForSelector('.native-state.state-active');
  await forbiddenPage.request.get(`http://127.0.0.1:${port}/socket/forbidden`);
  await forbiddenPage.waitForFunction(() => document.querySelector('#conversation-note').textContent.includes('无权访问'));
  const forbiddenSnapshotsBefore = seenPaths.filter(value => value.endsWith('/snapshot')).length;
  const forbiddenSocketsBefore = websocketPaths.length;
  await forbiddenPage.waitForTimeout(5200);
  if (seenPaths.filter(value => value.endsWith('/snapshot')).length !== forbiddenSnapshotsBefore ||
      websocketPaths.length !== forbiddenSocketsBefore) {
    throw new Error('forbidden websocket did not stop scheduled polling and reconnects');
  }
  await forbiddenPage.close();
  await page.request.get(`http://127.0.0.1:${port}/scenario/artifacts`);
  const storagePage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await storagePage.addInitScript(() => {
    Storage.prototype.setItem = function () { throw new DOMException('storage unavailable'); };
  });
  const sendsBeforeStorageFailure = seenPaths.filter(value => value.endsWith('/messages')).length;
  await storagePage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await storagePage.fill('#conversation-composer textarea', '本地存储不可用也要发送');
  await storagePage.click('.native-send');
  await storagePage.waitForFunction(() => document.querySelector('#conversation-composer textarea').value === '');
  if (seenPaths.filter(value => value.endsWith('/messages')).length <= sendsBeforeStorageFailure) {
    throw new Error('localStorage failure prevented the send request');
  }
  await storagePage.close();

  const projectPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await projectPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/project/project-one`, { waitUntil: 'networkidle' });
  const projectFacts = await projectPage.evaluate(() => {
    const start = document.querySelector('#project-send');
    const upload = document.querySelector('#project-upload form');
    return {
      forms: document.forms.length,
      startFileInputs: start.querySelectorAll('input[type="file"]').length,
      uploadPath: new URL(upload.action).pathname,
      returnTo: upload.querySelector('input[name="return_to"]').value,
      modelSegments: document.querySelectorAll('.project-start-settings .setting-segments-model .setting-segment').length,
      effortSegments: document.querySelectorAll('.project-start-settings .setting-segments-effort .setting-segment').length,
      settingsColumns: getComputedStyle(document.querySelector('.project-start-settings')).gridTemplateColumns.split(' ').length,
    };
  });
  if (projectFacts.forms !== 2 || projectFacts.startFileInputs !== 0 ||
      projectFacts.uploadPath !== `/${entryPrefix}/files/upload` ||
      projectFacts.returnTo !== `/${entryPrefix}/project/project-one` ||
      projectFacts.modelSegments < 2 || projectFacts.effortSegments < 2 || projectFacts.settingsColumns !== 2) {
    throw new Error(`project start/upload forms failed: ${JSON.stringify(projectFacts)}`);
  }
  const projectLengthPrompt = await projectPage.evaluate(() => {
    const input = document.querySelector('#project-send textarea');
    input.value = '长'.repeat(60001);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const counter = document.querySelector('#project-message-length');
    return {
      hasMaxLength: input.hasAttribute('maxlength'),
      counter: counter.textContent,
      hidden: counter.hidden,
    };
  });
  if (projectLengthPrompt.hasMaxLength || projectLengthPrompt.hidden ||
      projectLengthPrompt.counter !== '已输入 60,001 字符') {
    throw new Error(`project long-message count was not rendered without a limit: ${JSON.stringify(projectLengthPrompt)}`);
  }
  await projectPage.fill('#project-send textarea', '');
  await projectPage.fill('#project-send textarea', '上传图片前已经输入的文字');
  await projectPage.reload({ waitUntil: 'networkidle' });
  if (await projectPage.inputValue('#project-send textarea') !== '上传图片前已经输入的文字') {
    throw new Error('project start draft did not survive the upload-style page reload');
  }
  for (const width of [360, 390, 412]) {
    await projectPage.setViewportSize({ width, height: 844 });
    const responsive = await projectPage.evaluate(() => {
      const effort = document.querySelector('.setting-segments-effort');
      const composer = document.querySelector('.project-start-composer');
      return {
        pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        effortOverflow: effort.scrollWidth - effort.clientWidth,
        composerRight: composer.getBoundingClientRect().right,
      };
    });
    if (responsive.pageOverflow > 1 || responsive.effortOverflow > 1 || responsive.composerRight > width + .5) {
      throw new Error(`project mobile geometry failed at ${width}px: ${JSON.stringify(responsive)}`);
    }
  }
  await projectPage.setViewportSize({ width: 390, height: 844 });
  if (await projectPage.inputValue('#project-conversation-model') !== 'gpt-6-sol' ||
      await projectPage.inputValue('#project-conversation-effort') !== 'high') {
    throw new Error('project conversation did not default to GPT-6 Sol with high reasoning');
  }
  await projectPage.selectOption('#project-conversation-effort', 'ultra');
  const projectModels = await projectPage.locator('#project-conversation-model option').evaluateAll(
    options => options.map(option => option.value));
  if (projectModels.join(',') !== expectedModelOrder.join(',')) {
    throw new Error(`project model catalog was not fully exposed: ${JSON.stringify(projectModels)}`);
  }
  for (const width of [320, 360, 390, 412]) {
    await projectPage.setViewportSize({ width, height: 844 });
    const responsive = await projectPage.evaluate(() => {
      const group = document.querySelector('.project-start-settings .setting-segments-model');
      const buttons = Array.from(group.querySelectorAll('.setting-segment'));
      const groupRect = group.getBoundingClientRect();
      const buttonRects = buttons.map(button => button.getBoundingClientRect());
      return {
        pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        groupOverflow: group.scrollWidth - group.clientWidth,
        labels: buttons.map(button => button.textContent.trim()),
        display: getComputedStyle(group).display,
        flexWrap: getComputedStyle(group).flexWrap,
        compact: buttons.every(button => {
          const style = getComputedStyle(button);
          return style.flexGrow === '0' && style.flexShrink === '0' && parseFloat(style.minWidth) >= 44;
        }),
        allVisible: buttonRects.every(rect => rect.left >= groupRect.left - .5 && rect.right <= groupRect.right + .5),
        touchTargets: buttons.every(button => button.getBoundingClientRect().height >= 44),
        namesVisible: buttons.every(button => getComputedStyle(button).textOverflow !== 'ellipsis' &&
          button.scrollHeight <= button.clientHeight + 1),
      };
    });
    if (responsive.pageOverflow > 1 || responsive.groupOverflow <= 1 ||
        responsive.labels.join(',') !== '6.1 Sol,6 Astra,6 Sol,6 Luna,5.6 Sol,5.6 Terra,5.6 Luna,5.5,5.4,5.4 mini,5.3 codex spark' ||
        responsive.display !== 'flex' || responsive.flexWrap !== 'nowrap' || !responsive.compact ||
        !responsive.touchTargets || !responsive.namesVisible) {
      throw new Error(`project model grid failed at ${width}px: ${JSON.stringify(responsive)}`);
    }
  }
  await projectPage.locator('.project-start-settings .setting-segments-model [data-value="gpt-5.6-luna"]').click();
  if (await projectPage.inputValue('#project-conversation-model') !== 'gpt-5.6-luna') {
    throw new Error('project last model segment was not clickable');
  }
  await projectPage.setViewportSize({ width: 390, height: 844 });
  await projectPage.selectOption('#project-conversation-model', 'gpt-5.6-sol');
  await projectPage.selectOption('#project-conversation-effort', 'ultra');
  await projectPage.selectOption('#project-conversation-model', 'gpt-6-astra');
  if (await projectPage.inputValue('#project-conversation-effort') !== 'ultra') {
    throw new Error('project Astra switch lost the still-supported saved effort');
  }
  await projectPage.selectOption('#project-conversation-model', 'gpt-5.6-luna');
  if (await projectPage.inputValue('#project-conversation-effort') !== 'medium') {
    throw new Error('project model switch did not use the supported default effort');
  }
  await projectPage.selectOption('#project-conversation-model', 'gpt-5.6-sol');
  await projectPage.selectOption('#project-conversation-effort', 'ultra');
  await projectPage.locator('[data-project-control="speed"]').click();
  await projectPage.locator('#project-option-content input[value="priority"]').check();
  await projectPage.locator('#project-option-done').click();
  await projectPage.locator('[data-project-control="mode"]').click();
  await projectPage.locator('#project-option-content input[value="plan"]').check();
  await projectPage.locator('#project-option-done').click();
  await projectPage.locator('[data-project-control="permission"]').click();
  await projectPage.locator('#project-option-content input[value="danger-full-access"]').check();
  await projectPage.locator('#project-option-done').click();
  const longProjectMessage = '长'.repeat(60001);
  await projectPage.fill('#project-send textarea', longProjectMessage);
  await projectPage.click('.send-button');
  for (let attempt = 0; attempt < 30 && projectSubmission === null; attempt += 1) {
    await projectPage.waitForTimeout(50);
  }
  if (!projectSubmission || projectSubmission.path !== `/${entryPrefix}/conversation/new` ||
      projectSubmission.values.model !== 'gpt-5.6-sol' ||
      projectSubmission.values.effort !== 'ultra' ||
      projectSubmission.values.service_tier !== 'priority' ||
      projectSubmission.values.permission !== 'danger-full-access' ||
      projectSubmission.values.collaboration_mode !== 'plan' ||
      projectSubmission.values.prompt !== longProjectMessage) {
    throw new Error(`project settings were not submitted: ${JSON.stringify(projectSubmission)}`);
  }
  await projectPage.close();

  const projectInitializerFailurePage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await projectInitializerFailurePage.route('**/static/project-conversation.js*', route => route.abort());
  await projectInitializerFailurePage.goto(`http://127.0.0.1:${port}/${entryPrefix}/project/project-one`, { waitUntil: 'networkidle' });
  const initializerFailureFallback = await projectInitializerFailurePage.evaluate(() => ({
    model: document.querySelector('#project-conversation-model').value,
    effort: document.querySelector('#project-conversation-effort').value,
    effortOptions: document.querySelector('#project-conversation-effort').options.length,
    effortSegments: document.querySelectorAll('.project-start-settings .setting-segments-effort .setting-segment').length,
    tier: document.querySelector('#project-conversation-tier').value,
    tierOptions: document.querySelector('#project-conversation-tier').options.length,
    permission: document.querySelector('#project-conversation-permission').value,
  }));
  if (initializerFailureFallback.model !== 'gpt-6-sol' ||
      initializerFailureFallback.effort !== 'high' ||
      initializerFailureFallback.effortOptions < 2 || initializerFailureFallback.effortSegments < 2 ||
      initializerFailureFallback.tier !== '' || initializerFailureFallback.tierOptions < 2 ||
      initializerFailureFallback.permission !== 'danger-full-access') {
    throw new Error(`project initializer failure left unusable settings: ${JSON.stringify(initializerFailureFallback)}`);
  }
  await projectInitializerFailurePage.close();

  const wideConversationPage = await browser.newPage({ viewport: { width: 684, height: 439 } });
  await wideConversationPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  const wideHeaderTop = await wideConversationPage.locator('.native-conversation-head').evaluate(
    node => Math.round(node.getBoundingClientRect().top));
  if (wideHeaderTop !== 0) {
    throw new Error(`wide mobile conversation header was not flush to the top: ${wideHeaderTop}`);
  }
  await wideConversationPage.close();

  const catalogErrorPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await catalogErrorPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/project/project-catalog-error`, { waitUntil: 'networkidle' });
  const catalogFallback = await catalogErrorPage.evaluate(() => ({
    warning: document.body.textContent.includes('模型目录暂时不可用'),
    model: document.querySelector('#project-conversation-model').value,
    effort: document.querySelector('#project-conversation-effort').value,
    tier: document.querySelector('#project-conversation-tier').value,
    permission: document.querySelector('#project-conversation-permission').value,
  }));
  if (!catalogFallback.warning || catalogFallback.model !== 'gpt-5.6-sol' ||
      catalogFallback.effort !== 'medium' || catalogFallback.tier !== '' ||
      catalogFallback.permission !== 'danger-full-access') {
    throw new Error(`catalog fallback was not safely usable: ${JSON.stringify(catalogFallback)}`);
  }
  await catalogErrorPage.close();

  const soundSettingsPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await soundSettingsPage.addInitScript(() => {
    window.__settingsSoundPlays = 0;
    function audioParam() {
      return { setValueAtTime() {}, exponentialRampToValueAtTime() {} };
    }
    window.AudioContext = function () {
      this.currentTime = 0; this.destination = {};
      this.resume = () => Promise.resolve();
      this.createGain = () => {
        window.__settingsSoundPlays += 1;
        return { gain: audioParam(), connect() {} };
      };
      this.createOscillator = () => ({
        type: 'sine', frequency: audioParam(), connect() {}, start() {}, stop() {},
      });
    };
  });
  await soundSettingsPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/settings/completion-sound`,
    { waitUntil: 'networkidle' },
  );
  const settingsLayout = await soundSettingsPage.evaluate(() => ({
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    tones: document.querySelectorAll('[data-completion-tone]').length,
    enabled: document.querySelector('#completion-sound-enabled').checked,
    helper: Boolean(window.codexCompletionSound),
  }));
  if (settingsLayout.overflow !== 0 || settingsLayout.tones !== 3 ||
      !settingsLayout.enabled || !settingsLayout.helper) {
    throw new Error(`completion sound settings layout failed: ${JSON.stringify(settingsLayout)}`);
  }
  await soundSettingsPage.click('[data-completion-tone="strong"]');
  await soundSettingsPage.locator('#completion-sound-volume').fill('80');
  await soundSettingsPage.click('#completion-sound-save');
  const savedSound = await soundSettingsPage.evaluate(() => ({
    tone: localStorage.getItem('codex-completion-sound-tone'),
    volume: localStorage.getItem('codex-completion-sound-volume'),
    status: document.querySelector('#completion-sound-saved').textContent,
  }));
  if (savedSound.tone !== 'strong' || savedSound.volume !== '0.8' ||
      !savedSound.status.includes('已保存')) {
    throw new Error(`completion sound preferences did not persist: ${JSON.stringify(savedSound)}`);
  }
  await soundSettingsPage.click('#completion-sound-test');
  await soundSettingsPage.waitForFunction(() =>
    document.querySelector('#completion-sound-saved').textContent.includes('已播放试听音'));
  if (await soundSettingsPage.evaluate(() => window.__settingsSoundPlays) !== 1) {
    throw new Error('completion sound test did not synthesize exactly once');
  }
  await soundSettingsPage.click('.completion-sound-switch');
  const disabledSound = await soundSettingsPage.evaluate(() => ({
    enabled: document.querySelector('#completion-sound-enabled').checked,
    stored: localStorage.getItem('codex-completion-sound-enabled'),
    disabledClass: document.querySelector('#completion-sound-panel').classList.contains('disabled'),
  }));
  if (disabledSound.enabled || disabledSound.stored !== '0' || !disabledSound.disabledClass) {
    throw new Error(`completion sound master switch failed: ${JSON.stringify(disabledSound)}`);
  }
  await soundSettingsPage.reload({ waitUntil: 'networkidle' });
  if (await soundSettingsPage.locator('#completion-sound-enabled').isChecked() ||
      !await soundSettingsPage.locator('[data-completion-tone="strong"]').evaluate(
        node => node.classList.contains('selected'))) {
    throw new Error('completion sound preferences did not survive reload');
  }
  await soundSettingsPage.close();

  const ttsSettingsPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await ttsSettingsPage.addInitScript(() => {
    const baker = { name: 'Baker', lang: 'zh-CN', voiceURI: 'baker-local', localService: true };
    const synth = {
      utterances: [], getVoices() { return [baker]; }, addEventListener() {}, cancel() {},
      speak(value) { this.utterances.push(value); },
    };
    Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
    window.SpeechSynthesisUtterance = function (text) { this.text = text; };
    localStorage.removeItem('codex-tts-voice');
  });
  await ttsSettingsPage.goto(
    `http://127.0.0.1:${port}/${entryPrefix}/settings/tts`,
    { waitUntil: 'networkidle' },
  );
  const ttsLayout = await ttsSettingsPage.evaluate(() => ({
    helper: Boolean(window.codexTts),
    selected: document.querySelector('#tts-voice').value,
    options: document.querySelectorAll('#tts-voice option').length,
    mode: document.querySelector('#tts-diagnostic-mode').textContent,
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  if (!ttsLayout.helper || ttsLayout.selected !== '' || ttsLayout.options !== 2 ||
      !ttsLayout.mode.includes('Android 系统默认') || ttsLayout.overflow > 1) {
    throw new Error(`TTS settings default mode/layout failed: ${JSON.stringify(ttsLayout)}`);
  }
  await ttsSettingsPage.click('#tts-test');
  const defaultUtterance = await ttsSettingsPage.evaluate(() => {
    const utterance = speechSynthesis.utterances.at(-1);
    return { count: speechSynthesis.utterances.length, voice: utterance && utterance.voice, lang: utterance && utterance.lang };
  });
  if (defaultUtterance.count !== 1 || defaultUtterance.voice !== undefined || defaultUtterance.lang !== 'zh-CN') {
    throw new Error(`system-default TTS assigned a browser voice: ${JSON.stringify(defaultUtterance)}`);
  }
  await ttsSettingsPage.selectOption('#tts-voice', 'baker-local');
  await ttsSettingsPage.click('#tts-save');
  await ttsSettingsPage.click('#tts-test');
  const explicitUtterance = await ttsSettingsPage.evaluate(() => {
    const utterance = speechSynthesis.utterances.at(-1);
    return {
      stored: localStorage.getItem('codex-tts-voice'),
      voice: utterance && utterance.voice && utterance.voice.voiceURI,
      result: document.querySelector('#tts-diagnostic-result').textContent,
    };
  });
  if (explicitUtterance.stored !== 'baker-local' || explicitUtterance.voice !== 'baker-local' ||
      !explicitUtterance.result.includes('指定音色')) {
    throw new Error(`explicit TTS voice was not persisted/applied: ${JSON.stringify(explicitUtterance)}`);
  }
  for (const width of [360, 390, 412]) {
    await ttsSettingsPage.setViewportSize({ width, height: 844 });
    const overflow = await ttsSettingsPage.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 1) throw new Error(`TTS settings overflowed at ${width}px: ${overflow}`);
  }
  await ttsSettingsPage.selectOption('#tts-voice', '');
  await ttsSettingsPage.click('#tts-save');
  await ttsSettingsPage.close();

  const keyboardPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await keyboardPage.goto(`http://127.0.0.1:${port}/${entryPrefix}/conversation/conversation-ui`, { waitUntil: 'networkidle' });
  await keyboardPage.focus('#conversation-composer textarea');
  const keyboardGeometry = await keyboardPage.evaluate(() => {
    const original = window.visualViewport;
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: {
      offsetTop: 0, offsetLeft: 0, width: 390, height: 544, addEventListener() {},
    }});
    original.dispatchEvent(new Event('resize'));
    const composer = document.querySelector('#conversation-composer').getBoundingClientRect();
    return {
      composerBottom: Math.round(composer.bottom), composerTop: Math.round(composer.top),
      headerTop: Math.round(document.querySelector('.native-conversation-head').getBoundingClientRect().top),
      navDisplay: getComputedStyle(document.querySelector('.nav')).display,
    };
  });
  if (keyboardGeometry.composerBottom !== 544 || keyboardGeometry.composerTop >= 544 ||
      keyboardGeometry.headerTop !== 0 || keyboardGeometry.navDisplay !== 'none') {
    throw new Error(`keyboard did not pin composer to visible viewport: ${JSON.stringify(keyboardGeometry)}`);
  }
  await keyboardPage.close();
  if (errors.length) throw new Error(`page errors: ${errors.join('; ')}`);
  websocketClients.forEach(socket => socket.destroy());
  await browser.close();
  server.close();
  process.stdout.write(JSON.stringify({ ok: true, geometry }) + '\n');
})().catch(async error => {
  websocketClients.forEach(socket => socket.destroy());
  if (browser) await browser.close();
  server.close();
  console.error(error);
  process.exitCode = 1;
});
