/* Native approvals and MCP elicitation. No timeouts, auto-approval or localStorage. */
(function () {
  'use strict';
  var root = document.getElementById('native-conversation');
  var host = document.getElementById('conversation-native-interactions');
  if (!root || !host || root.dataset.conversationKind === 'dot') return;
  var cid = root.dataset.conversationId;
  var marker = '/conversation/' + encodeURIComponent(cid);
  var index = location.pathname.lastIndexOf(marker);
  var prefix = index >= 0 ? location.pathname.slice(0, index) : '';
  var cards = new Map();
  var states = {
    awaiting: '等待你的原生授权或回答', claimed: '回答已记录，正在发送',
    response_sent: '回答已发送，等待原生确认（不代表任务完成）',
    resolved: '原生请求已确认或清理（不代表任务完成）',
    awaiting_reissue: '连接已断开，等待原生重新提出请求；不会自动批准',
    delivery_unknown: '回答是否送达待核对；不会自动重发', closed: '本请求已关闭', cancelled: '本请求已取消'
  };
  function element(tag, text) {
    var value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    return value;
  }
  function showJSON(parent, title, value) {
    if (value === undefined || value === null) return;
    var details = element('details'); details.append(element('summary', title));
    details.append(element('pre', JSON.stringify(value, null, 2))); parent.append(details);
  }
  function labelFor(choice) {
    if (choice === 'accept') return '仅批准这次请求';
    if (choice === 'acceptForSession') return '批准本会话内同类请求';
    if (choice === 'decline') return '拒绝';
    if (choice === 'cancel') return '取消本次请求';
    if (choice.acceptWithExecpolicyAmendment) return '批准并保存原生命令规则（影响后续执行）';
    if (choice.applyNetworkPolicyAmendment) return '应用原生网络规则（影响后续执行）';
    return JSON.stringify(choice);
  }
  function radio(form, group, label, value, choices) {
    var row = element('label'); var input = element('input'); input.type = 'radio';
    input.name = group; input.value = String(choices.length); choices.push(value);
    row.append(input, element('span', label)); form.append(row);
    return input;
  }
  function jsonEditor(parent, label, value) {
    var row = element('label', label); var input = element('textarea'); input.rows = 5;
    input.value = JSON.stringify(value, null, 2); input.autocomplete = 'off';
    input.spellcheck = false; row.append(input); parent.append(row); return input;
  }
  function mcpFields(parent, schema) {
    var controls = [];
    Object.keys(schema.properties || {}).forEach(function (key) {
      var field = schema.properties[key]; var row = element('label', field.title || key);
      if (field.description) row.append(element('small', field.description));
      var input; var enumeration = field.enum;
      if (field.type === 'boolean') {
        input = element('select'); input.append(new Option('未提供', ''));
        input.append(new Option('是', 'true'), new Option('否', 'false'));
      } else if (field.type === 'string' && Array.isArray(enumeration)) {
        input = element('select'); input.append(new Option('未提供', ''));
        enumeration.forEach(function (value, i) { input.append(new Option((field.enumNames || [])[i] || value, String(value))); });
      } else {
        input = element(field.type === 'array' ? 'textarea' : 'input');
        if (field.type === 'array') input.rows = 2;
        else input.type = /password|secret|token/i.test(key) ? 'password' :
          field.type === 'number' || field.type === 'integer' ? 'number' : 'text';
        if (field.type === 'integer') input.step = '1';
        if (field.type === 'number') input.step = 'any';
        if (field.type === 'array') input.placeholder = 'JSON 数组，例如 ["选项"]';
      }
      input.autocomplete = 'off'; row.append(input); parent.append(row);
      // Optional empty string can still be supplied with the raw-content editor.
      controls.push({key:key, field:field, input:input});
    });
    return function () {
      var content = {};
      controls.forEach(function (item) {
        var value = item.input.value;
        if (value === '') return;
        if (item.field.type === 'boolean') value = value === 'true';
        else if (item.field.type === 'number' || item.field.type === 'integer') {
          value = Number(value); if (!Number.isFinite(value)) throw new Error('数字格式无效。');
        } else if (item.field.type === 'array') value = JSON.parse(value);
        content[item.key] = value;
      });
      return content;
    };
  }
  function createCard(record) {
    var card = element('article'); card.className = 'native-interaction-card';
    card.dataset.interactionId = record.id; card.dataset.attempt = String(record.attempt);
    var display = record.display || {};
    var titles = {command:'原生命令审批', fileChange:'原生文件修改审批', permissions:'原生权限申请', mcpElicitation:'MCP 原生交互'};
    card.append(element('h3', titles[display.kind] || '原生请求'));
    var status = element('p'); status.className = 'native-interaction-status'; card.append(status);
    var form = element('form'); form.autocomplete = 'off'; card.append(form);
    if (display.reason) form.append(element('p', display.reason));
    if (display.cwd) form.append(element('p', '工作目录：' + display.cwd));
    if (display.command) form.append(element('pre', display.command));
    if (display.serverName) form.append(element('p', 'MCP 服务：' + display.serverName));
    if (display.message) form.append(element('p', display.message));
    if (display.kind === 'fileChange') {
      form.append(element('p', '原生文件修改对象：' + record.native_item_id + (display.grantRoot ? '\n授权根：' + display.grantRoot : '')));
      showJSON(form, '原生文件修改详情', display.changes);
      form.append(element('small', display.changesUnavailable ? '暂未取得原生修改详情；批准前请自行核对文件，不确定可拒绝。' : '请核对上述文件修改；会话批准范围由原生 Codex 管理。'));
    }
    showJSON(form, '完整原生请求范围', display);
    var offered = []; var group = 'native-choice-' + record.id;
    var permissionsEditor, scope, strict, contentReader, contentEditor, useJSON;
    if (display.kind === 'command' || display.kind === 'fileChange') {
      (display.availableDecisions || []).forEach(function (choice) {
        radio(form, group, labelFor(choice), choice, offered);
      });
    } else if (display.kind === 'permissions') {
      radio(form, group, '批准下方权限（可删减，不能扩展请求范围）', 'grant', offered);
      radio(form, group, '不批准额外权限', 'decline', offered);
      permissionsEditor = jsonEditor(form, '权限 JSON（原生请求范围）', display.requested || {});
      var scopeRow = element('label', '授权持续范围'); scope = element('select');
      scope.append(new Option('仅本轮', 'turn'), new Option('本会话', 'session')); scopeRow.append(scope); form.append(scopeRow);
      var strictRow = element('label'); strict = element('input'); strict.type = 'checkbox';
      strictRow.append(strict, element('span', '本轮后续每条命令交由原生再次检查')); form.append(strictRow);
    } else {
      if (display.mode === 'url') {
        try {
          var url = new URL(display.url);
          if (url.protocol === 'https:' || url.protocol === 'http:') {
            var link = element('a', '打开 MCP 验证页面'); link.href = url.href;
            link.target = '_blank'; link.rel = 'noopener noreferrer'; form.append(link);
          } else form.append(element('p', '此验证地址不是 HTTP(S)，请自行核对；工作台不会自动打开。'));
        } catch (_) { form.append(element('p', '验证地址无法打开，请核对原生请求。')); }
        form.append(element('p', '完成外部验证后，才选择接受并提交。打开链接不等于已授权。'));
      }
      if (display.mode === 'openai/userVerification') {
        form.append(element('p', '此请求需要可信设备证明，当前工作台未接入该证明提供方；不能伪造验证成功。你可拒绝或取消。'));
      } else radio(form, group, display.mode === 'url' ? '我已完成外部验证，通知原生继续' : '接受并提交填写内容', 'accept', offered);
      radio(form, group, '拒绝', 'decline', offered); radio(form, group, '取消', 'cancel', offered);
      if (display.requestedSchema) {
        contentReader = mcpFields(form, display.requestedSchema);
        var advanced = element('details'); advanced.append(element('summary', '改用完整内容 JSON（支持原生全部字段）'));
        var useRow = element('label'); useJSON = element('input'); useJSON.type = 'checkbox';
        useRow.append(useJSON, element('span', '提交下方 JSON 而非上方表单')); advanced.append(useRow);
        contentEditor = jsonEditor(advanced, '内容 JSON（不保存到浏览器或日志）', {}); form.append(advanced);
      }
    }
    var submit = element('button', '提交原生决定'); submit.type = 'submit'; submit.disabled = true;
    var note = element('p'); note.role = 'status'; form.append(submit, note);
    var delivery = null; var sending = false;
    function response() {
      var selected = form.querySelector('input[name="' + group + '"]:checked');
      if (!selected) throw new Error('请先选择你的决定。');
      var decision = offered[Number(selected.value)];
      if (display.kind === 'command' || display.kind === 'fileChange') return {decision:decision};
      if (display.kind === 'permissions') return decision === 'decline' ? {permissions:{}} :
        {permissions:JSON.parse(permissionsEditor.value), scope:scope.value, strictAutoReview:strict.checked};
      return {action:decision, content:decision === 'accept' && contentReader ?
        (useJSON.checked ? JSON.parse(contentEditor.value) : contentReader()) : null};
    }
    form.addEventListener('change', function () { if (!sending) submit.disabled = !form.querySelector('input[type="radio"]:checked'); });
    form.addEventListener('submit', async function (event) {
      event.preventDefault(); if (sending || record.state !== 'awaiting') return;
      try {
        var wire = JSON.stringify(response());
        // Retrying an uncertain HTTP result reuses its exact id/response. It
        // cannot approve twice, change a decision silently, or send on reopen.
        if (delivery && delivery.wire !== wire) throw new Error('上次决定是否送达未知，请先刷新核对；不能改成另一个决定重发。');
        if (!delivery) delivery = {id:'ni-' + crypto.randomUUID(), wire:wire};
        sending = true; submit.disabled = true; note.textContent = '正在发送你的决定…';
        var body = new FormData(); body.set('command_id', delivery.id); body.set('attempt', record.attempt);
        body.set('fingerprint', record.fingerprint); body.set('response_json', delivery.wire);
        var result = await fetch(prefix + marker + '/native-interactions/' + encodeURIComponent(record.id) + '/answer',
          {method:'POST', body:body, credentials:'same-origin', cache:'no-store'});
        var data = await result.json();
        if (!result.ok) {
          if (result.status === 400 || result.status === 409) delivery = null;
          throw new Error(result.status === 401 ? '登录已失效，请重新登录；不会自动授权。' :
            result.status === 403 ? '当前账号无权授权。' : (typeof data.error === 'string' ? data.error : '决定未确认送达，请刷新核对。'));
        }
        // Immediately erase response/proof material once acknowledged by Web.
        delivery = null; contentReader = null; contentEditor = null;
        form.replaceChildren(element('p', '决定已记录；执行结果仍以原生 Turn 为准。'));
        window.dispatchEvent(new CustomEvent('codex:conversation-action-result', {detail:data}));
      } catch (error) { note.textContent = error instanceof SyntaxError ? 'JSON 格式无效，请核对。' : error.message; }
      finally { sending = false; if (form.contains(submit)) submit.disabled = record.state !== 'awaiting'; }
    });
    return {node:card, record:record, status:status, form:form, erase:function () {
      form.querySelectorAll('input,textarea').forEach(function (input) { input.value = ''; });
      delivery = null; contentReader = null; contentEditor = null; form.replaceChildren();
    }};
  }
  function render(snapshot) {
    if (!snapshot || !snapshot.conversation || snapshot.conversation.id !== cid) return;
    var records = snapshot.native_interactions || []; var present = new Set();
    records.forEach(function (record) {
      present.add(record.id); var previous = cards.get(record.id);
      var identity = record.attempt + ':' + record.fingerprint;
      if (!previous || previous.identity !== identity) {
        var next = createCard(record); next.identity = identity;
        if (previous) previous.node.replaceWith(next.node); else host.append(next.node);
        cards.set(record.id, next); previous = next;
      }
      previous.record.state = record.state;
      previous.status.textContent = states[record.state] || '原生状态待核对';
      if (record.state !== 'awaiting') {
        // Do not leave secret inputs or original decisions in a completed DOM.
        previous.erase();
        if (record.state === 'resolved' || record.state === 'closed' || record.state === 'cancelled') {
          previous.node.hidden = true;
        }
      }
    });
    cards.forEach(function (card, id) { if (!present.has(id)) {card.node.remove(); cards.delete(id);} });
    host.hidden = !records.some(function (item) { return !['resolved','closed','cancelled'].includes(item.state); });
  }
  window.addEventListener('codex:conversation-snapshot', function (event) { render(event.detail); });
  var initial = document.getElementById('conversation-initial-snapshot');
  if (initial) { try { render(JSON.parse(initial.textContent)); } catch (_) {} }
})();
