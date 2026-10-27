/* Browser drafts contain public metadata only. Reconstruct URLs from the current
 * authenticated scope; storage is never trusted as a path or URL source. */
(function () {
  'use strict';
  var instances = new WeakMap();
  function token() {
    return window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() :
      Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }
  function safeGet(key) {
    try { return { ok: true, value: localStorage.getItem(key) }; }
    catch (_) { return { ok: false, value: null }; }
  }
  function safeSet(key, value) {
    try { localStorage.setItem(key, value); return true; } catch (_) { return false; }
  }
  function safeRemove(key) {
    try { localStorage.removeItem(key); return true; } catch (_) { return false; }
  }
  function valid(value) {
    return value && typeof value.id === 'string' && value.id && typeof value.name === 'string';
  }
  function records(value) { return Array.isArray(value) ? value.filter(valid) : []; }
  function saved(value) {
    try {
      var data = JSON.parse(value || '{}');
      return Array.isArray(data) ? { records: records(data), revision: '' } :
        { records: records(data.records), revision: typeof data.revision === 'string' ? data.revision : '' };
    } catch (_) { return { records: [], revision: '' }; }
  }
  function ids(values) { return values.map(function (item) { return item.id; }); }
  function sameIds(left, right) {
    return Array.isArray(left) && Array.isArray(right) && left.join(',') === right.join(',');
  }
  function prefix() {
    var found = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
    return found ? '/' + found[1] : '';
  }
  function size(value) {
    var n = Number(value) || 0, units = ['B', 'KB', 'MB', 'GB'], index = 0;
    while (n >= 1024 && index < 3) { n /= 1024; index++; }
    return (index ? n.toFixed(n >= 10 ? 0 : 1) : Math.round(n)) + ' ' + units[index];
  }
  function publicUrl(id, kind, scope, download) {
    var query = new URLSearchParams();
    query.set(kind === 'project' ? 'project_id' : 'conversation_id', scope);
    if (download) query.set('download', '1');
    return prefix() + '/conversation/attachments/content/' + encodeURIComponent(id) + '?' + query.toString();
  }
  function normalize(value, kind, scope, verified) {
    if (!valid(value)) return null;
    var preview = ['image', 'pdf', 'text', 'download'].includes(value.preview_kind) ? value.preview_kind : 'download';
    return {
      id: value.id, name: value.name, size: Number(value.size) || 0,
      mime_type: String(value.mime_type || ''), preview_kind: preview, created_at: value.created_at || '',
      content_url: publicUrl(value.id, kind, scope, false),
      download_url: publicUrl(value.id, kind, scope, true), verified: Boolean(verified)
    };
  }
  function init(form) {
    if (!form || instances.has(form)) return instances.get(form);
    var host = form.querySelector('[data-message-attachments]');
    if (!host) return null;
    var kind = host.dataset.kind, scope = host.dataset.id, owner = host.dataset.owner || '';
    var draftKey = host.dataset.draftKey || ((form.querySelector('textarea') || {}).dataset || {}).draftKey || '';
    var key = 'codex-message-attachments:' + owner + ':' + kind + ':' + scope + ':' + draftKey;
    var storage = safeGet(key), storageFailed = !storage.ok, restored = saved(storage.value);
    var lastDurableValue = storage.value;
    var revision = restored.revision || token(), checking = null, statusText = "";
    var values = restored.records.map(function (item) { return normalize(item, kind, scope, false); }).filter(Boolean);
    var initial = records((function () {
      try { return JSON.parse(host.dataset.initial || '[]'); } catch (_) { return []; }
    }())).map(function (item) { return normalize(item, kind, scope, true); }).filter(Boolean);
    function merge(next) {
      var map = Object.create(null), before = ids(values);
      values.concat(next).forEach(function (item) { if (item) map[item.id] = item; });
      values = Object.keys(map).map(function (id) { return map[id]; });
      if (!sameIds(before, ids(values))) revision = token();
    }
    if (initial.length) merge(initial);
    function status(text) {
      statusText = text || "";
      var note = host.querySelector('.message-attachments-status');
      if (note) { note.textContent = text || ''; note.hidden = !text; }
    }
    function persist() {
      var serialized = JSON.stringify({
        records: values.map(function (item) {
          return { id: item.id, name: item.name, size: item.size, mime_type: item.mime_type,
            preview_kind: item.preview_kind, created_at: item.created_at };
        }), revision: revision
      });
      storageFailed = !safeSet(key, serialized);
      if (!storageFailed) lastDurableValue = serialized;
      return !storageFailed;
    }
    function capture() {
      return Object.freeze({ ids: ids(values), records: values.map(function (item) {
        return Object.assign({}, item);
      }), revision: revision });
    }
    function card(item, removable) {
      var section = document.createElement('section');
      section.className = 'message-attachment-card'; section.dataset.attachmentId = item.id;
      if (item.verified && item.preview_kind === 'image') {
        var image = document.createElement('img');
        image.src = item.content_url; image.alt = ''; image.loading = 'lazy'; section.appendChild(image);
      }
      var info = document.createElement('div'); info.className = 'message-attachment-info';
      var title = document.createElement('strong'); title.textContent = item.name; title.title = item.name;
      var meta = document.createElement('small');
      meta.textContent = [size(item.size), item.mime_type].filter(Boolean).join(' · ');
      info.append(title, meta); section.appendChild(info);
      var open = document.createElement('button');
      open.type = 'button'; open.className = 'message-attachment-preview';
      open.textContent = item.verified ? (item.preview_kind === 'download' ? '保存' : '预览') : '待核验';
      open.disabled = !item.verified; open.addEventListener('click', function () { openRecord(item); });
      section.appendChild(open);
      if (removable) {
        var remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'message-attachment-remove'; remove.textContent = '移除';
        remove.addEventListener('click', function () {
          cancel('附件已移除，未发送；请确认草稿后重新发送。');
          values = values.filter(function (current) { return current.id !== item.id; });
          revision = token(); persist(); render();
        });
        section.appendChild(remove);
      }
      return section;
    }
    function render() {
      host.replaceChildren();
      var list = document.createElement('div'); list.className = 'message-attachments-list';
      values.forEach(function (item) { list.appendChild(card(item, true)); }); host.appendChild(list);
      var note = document.createElement('p'); note.className = 'message-attachments-status';
      note.setAttribute('role', 'status'); note.hidden = true; host.appendChild(note);
      if (statusText) status(statusText);
      else if (storageFailed) status('浏览器无法保存附件草稿；刷新可能丢失，本页附件仍可使用。');
      host.setAttribute('aria-busy', checking ? 'true' : 'false');
      if (checking) {
        var stop = document.createElement('button');
        stop.type = 'button'; stop.className = 'message-attachments-cancel'; stop.textContent = '取消核验';
        stop.addEventListener('click', function () { cancel(); }); host.appendChild(stop);
      }
      form.querySelectorAll('input[data-message-attachment-id]').forEach(function (input) { input.remove(); });
      values.forEach(function (item) {
        var input = document.createElement('input'); input.type = 'hidden'; input.name = 'attachment_ids';
        input.value = item.id; input.dataset.messageAttachmentId = ''; form.appendChild(input);
      });
      var area = form.querySelector('textarea'); if (area) area.required = !values.length;
      form.dispatchEvent(new CustomEvent('codex:attachments-changed', { bubbles: true, detail: capture() }));
    }
    function listUrl(currentIds) {
      var query = new URLSearchParams();
      currentIds.forEach(function (id) { query.append('ids', id); });
      query.set(kind === 'project' ? 'project_id' : 'conversation_id', scope);
      return prefix() + '/conversation/attachments/list?' + query;
    }
    function cancel(message) {
      if (!checking) return false;
      checking.finish(new Error(message || '已取消附件核验，未发送；草稿已保留。'));
      return true;
    }
    function ready() {
      var current = capture();
      if (checking && (checking.revision !== current.revision || !sameIds(checking.ids, current.ids))) {
        cancel('附件草稿已变更，未发送；请重新核验后再试。');
      }
      if (!current.ids.length) return Promise.resolve(current);
      if (checking) return checking.promise;
      // Each generation owns its deadline and settlement, including body reads.
      // Abort alone is insufficient: a late/uncancellable fetch must be inert.
      var attempt = { controller: new AbortController(), revision: current.revision,
        ids: current.ids, timer: null, promise: null, finish: null };
      attempt.promise = new Promise(function (resolve, reject) {
        attempt.finish = function (error, data) {
          if (checking !== attempt) return;
          checking = null; clearTimeout(attempt.timer);
          if (error) attempt.controller.abort();
          else values = data.attachments.map(function (item) { return normalize(item, kind, scope, true); });
          statusText = error ? error.message : '';
          render();
          if (error) reject(error); else resolve(capture());
        };
      });
      checking = attempt;
      statusText = '正在核验附件，尚未发送…'; render();
      attempt.timer = setTimeout(function () {
        attempt.finish(new Error('附件核验超时（15 秒），未发送；草稿已保留，请检查网络后重试。'));
      }, 15000);
      // Revalidate every send: fixed attachment IDs, scope and revision retain
      // their existing ownership checks, independent of receipt/command IDs.
      Promise.resolve().then(function () {
        return fetch(listUrl(current.ids), {
          credentials: 'same-origin', cache: 'no-store', signal: attempt.controller.signal,
          headers: { Accept: 'application/json' }
        });
      }).then(function (response) {
        if (checking !== attempt) return;
        if (!response.ok) throw new Error('附件核验未完成（登录、权限或网络异常），未发送。');
        return response.json();
      }).then(function (data) {
        if (checking !== attempt) return;
        if (!data || !Array.isArray(data.attachments) || !Array.isArray(data.missing_ids)) {
          throw new Error('附件核验返回无效，未发送。');
        }
        if (data.missing_ids.length || data.attachments.length !== current.ids.length ||
            !sameIds(ids(records(data.attachments)), current.ids)) {
          throw new Error('有附件已不可用，草稿已保留；请移除或重新上传后再发送。');
        }
        if (revision !== current.revision || !sameIds(ids(values), current.ids)) {
          throw new Error('附件草稿已变更，未发送；请重新核验后再试。');
        }
        attempt.finish(null, data);
      }).catch(function (error) {
        if (checking !== attempt) return;
        attempt.finish(new Error(error instanceof TypeError || error instanceof SyntaxError ?
          '附件核验失败（网络异常或返回无效），未发送；草稿已保留，请重试。' :
          error && error.message || '附件核验未完成，未发送。'));
      });
      return attempt.promise;
    }
    function matches(sent) {
      return Boolean(sent && sent.revision === revision && sameIds(sent.ids, ids(values)));
    }
    function clear(sent) {
      // Receipt ownership in this page is separate from storage ownership.
      // Quota failure must not leave a known accepted selection in the composer.
      if (sent && Array.isArray(sent.ids) && sent.ids.some(function (id) { return ids(initial).includes(id); })) removeBatchQuery();
      if (!matches(sent)) return false;
      var now = safeGet(key), latest = saved(now.value);
      var ownsStorage = !now.ok || !now.value || now.value === lastDurableValue ||
        latest.revision === sent.revision && sameIds(ids(latest.records), sent.ids);
      values = []; revision = token();
      if (ownsStorage) {
        if (!persist() && now.ok && now.value) {
          // removeItem still works when setItem runs out of quota. Remove only
          // the last owned value, never a newer draft written by another tab.
          var recheck = safeGet(key);
          if (recheck.ok && recheck.value === now.value && safeRemove(key)) lastDurableValue = null;
        }
      }
      render(); removeBatchQuery();
      if (!ownsStorage) status('本页已确认附件已清除；另一个标签页的新草稿仍保留。');
      return true;
    }
    function openRecord(item) {
      if (!item || !item.verified) return;
      if (item.preview_kind === 'download') {
        var link = document.createElement('a'); link.href = item.download_url; link.download = ''; link.click(); return;
      }
      var dialog = document.createElement('dialog'); dialog.className = 'message-attachment-viewer';
      var head = document.createElement('header'), close = document.createElement('button');
      var title = document.createElement('strong'), download = document.createElement('a');
      close.type = 'button'; close.textContent = '返回'; title.textContent = item.name;
      download.href = item.download_url; download.download = ''; download.textContent = '保存';
      head.append(close, title, download); dialog.appendChild(head);
      function dispose(back) {
        window.removeEventListener('popstate', pop);
        if (dialog.open) dialog.close(); dialog.remove();
        if (back && history.state && history.state.codexAttachmentPreview) history.back();
      }
      function pop() { dispose(false); }
      history.pushState({ codexAttachmentPreview: true }, ''); window.addEventListener('popstate', pop);
      close.addEventListener('click', function () { dispose(true); });
      dialog.addEventListener('cancel', function (event) { event.preventDefault(); dispose(true); });
      if (item.preview_kind === 'image') {
        var image = document.createElement('img'); image.src = item.content_url; image.alt = item.name;
        dialog.appendChild(image);
      } else if (item.preview_kind === 'pdf') {
        var frame = document.createElement('iframe'); frame.src = item.content_url; frame.title = item.name;
        dialog.appendChild(frame);
      } else {
        var pre = document.createElement('pre'); pre.textContent = '正在读取…'; dialog.appendChild(pre);
        fetch(item.content_url, { credentials: 'same-origin' }).then(function (response) {
          if (!response.ok) throw new Error(); return response.text();
        }).then(function (text) { pre.textContent = text; }).catch(function () {
          pre.textContent = '无法读取预览，请保存后查看。';
        });
      }
      document.body.appendChild(dialog); dialog.showModal();
    }
    function historyCards(items) {
      var list = document.createElement('div'); list.className = 'message-attachments-list message-attachments-history';
      records(items).map(function (item) { return normalize(item, kind, scope, true); }).filter(Boolean).forEach(function (item) {
        list.appendChild(card(item, false));
      });
      return list.childElementCount ? list : null;
    }
    function transferPrefix(projectId) {
      return 'codex-message-attachment-transfer:' + owner + ':' + projectId;
    }
    function transfers(projectId) {
      var base = transferPrefix(projectId), found = [];
      try {
        for (var i = 0; i < localStorage.length; i++) {
          var candidate = localStorage.key(i);
          if (typeof candidate !== 'string' || candidate !== base && !candidate.startsWith(base + ':')) continue;
          try {
            var value = JSON.parse(localStorage.getItem(candidate));
            if (value && value.command_id) found.push({ key: candidate, value: value });
          } catch (_) {}
        }
      } catch (_) {}
      return found;
    }
    function signature(formData) {
      var rows = [];
      formData.forEach(function (value, name) {
        if (name !== 'client_command_id') rows.push([name, typeof value === 'string' ? value : value.name || '']);
      });
      rows.sort(function (a, b) { return a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]); });
      return JSON.stringify(rows);
    }
    function prepareTransfer(sent, formData, text) {
      var sig = signature(formData);
      var match = transfers(scope).find(function (entry) {
        return entry.value.signature === sig && entry.value.source_revision === sent.revision && sameIds(entry.value.ids, sent.ids);
      });
      var prior = match && match.value;
      var commandId = prior && prior.signature === sig && prior.source_revision === sent.revision &&
        sameIds(prior.ids, sent.ids) ? prior.command_id : token();
      var sourceSettingsKey = 'codex-project-settings:' + prefix() + ':' + draftKey;
      var sourceSettings = null;
      try { sourceSettings = sessionStorage.getItem(sourceSettingsKey); } catch (_) {}
      var payload = {
        command_id: commandId, signature: sig, source_key: key, source_revision: sent.revision,
        ids: sent.ids.slice(), records: sent.records, text: String(text || '').trim(),
        composer_value: String(text || ''), text_revision: safeGet(draftKey + ':revision').value || '',
        source_settings_key: sourceSettingsKey, source_settings: sourceSettings
      };
      if (prior && ['accepted', 'completed', 'queued'].includes(prior.receipt_state)) payload.receipt_state = prior.receipt_state;
      if (!safeSet(transferPrefix(scope) + ':' + commandId, JSON.stringify(payload)) && sent.ids.length) {
        throw new Error('浏览器无法保存首问投递状态，未发送；请启用浏览器存储后再试。');
      }
      return payload;
    }
    function claim(snapshot) {
      var projectId = host.dataset.projectId;
      if (kind !== 'conversation' || !projectId || !snapshot || !snapshot.conversation ||
          String(snapshot.conversation.project_id || '') !== projectId) return;
      transfers(projectId).forEach(function (entry) { claimTransfer(snapshot, projectId, entry); });
    }
    function claimTransfer(snapshot, projectId, entry) {
      var markerKey = entry.key, transfer = entry.value;
      var commands = (snapshot.messages || []).concat(snapshot.queue || []);
      (snapshot.turns || []).forEach(function (turn) { commands = commands.concat(turn.commands || []); });
      var command = commands.find(function (item) {
        return item && item.id === transfer.command_id && item.kind === 'start' &&
          String(item.text || '') === transfer.text && sameIds(ids(records(item.attachments)), transfer.ids);
      });
      if (!command) return;
      var state = String(command.state || '').toLowerCase();
      if (state === 'rejected') {
        merge(records(transfer.records).map(function (item) { return normalize(item, kind, scope, false); }).filter(Boolean));
        var area = form.querySelector('textarea');
        if (area && !area.value && transfer.composer_value) {
          area.value = transfer.composer_value; area.dispatchEvent(new Event('input', { bubbles: true }));
        }
        persist(); render(); status('首问未送达，附件和文字已保留到当前输入框。');
        safeRemove(markerKey); return;
      }
      if (!['accepted', 'completed', 'queued'].includes(state)) return;
      // A shared project selection may have several distinct first requests.
      // Keep each identity until every request using this selection has a
      // receipt; another tab's acceptance cannot consume an unresolved one.
      transfer.receipt_state = state;
      safeSet(markerKey, JSON.stringify(transfer));
      var group = transfers(projectId).filter(function (other) {
        return other.value.source_key === transfer.source_key &&
          other.value.source_revision === transfer.source_revision && sameIds(other.value.ids, transfer.ids);
      });
      var own = group.find(function (other) { return other.key === markerKey; });
      if (own) own.value = transfer; else group.push({ key: markerKey, value: transfer });
      if (group.some(function (other) { return !['accepted', 'completed', 'queued'].includes(other.value.receipt_state); })) return;
      var source = safeGet(transfer.source_key);
      if (!source.ok) return; // Retry cleanup from a later receipt, never lose the identity.
      var sourceSaved = saved(source.value);
      if (sourceSaved.revision === transfer.source_revision && sameIds(ids(sourceSaved.records), transfer.ids)) {
        // Prefer a tombstone for other tabs; quota-full storage can still
        // remove the exact owned value. A newer selection is never removed.
        if (!safeSet(transfer.source_key, JSON.stringify({ records: [], revision: token() }))) {
          var recheck = safeGet(transfer.source_key);
          if (!recheck.ok || recheck.value === source.value && !safeRemove(transfer.source_key)) return;
        }
      }
      group.forEach(function (other) {
        var receipt = other.value;
        var sourceDraftKey = 'codex-project-conversation-draft:' + projectId;
        var sourceText = safeGet(sourceDraftKey), sourceTextRevision = safeGet(sourceDraftKey + ':revision');
        if (sourceText.ok && sourceText.value === receipt.composer_value && sourceTextRevision.value === receipt.text_revision) {
          if (!safeRemove(sourceDraftKey)) return;
          safeRemove(sourceDraftKey + ':revision');
        }
        try {
          if (receipt.source_settings && sessionStorage.getItem(receipt.source_settings_key) === receipt.source_settings) {
            sessionStorage.removeItem(receipt.source_settings_key);
          }
        } catch (_) {}
        safeRemove(other.key);
      });
    }
    window.addEventListener('storage', function (event) {
      if (event.key !== key) return;
      var remote = saved(event.newValue);
      if (!event.newValue || remote.revision && remote.revision !== revision) {
        cancel('附件草稿已在另一个标签页更新，未发送；请重新核验。');
        values = remote.records.map(function (item) { return normalize(item, kind, scope, false); }).filter(Boolean);
        revision = remote.revision || token(); render();
        status('附件草稿已在另一个标签页更新；请重新核验后发送。');
      }
    });
    function removeBatchQuery() {
      var url = new URL(location.href);
      if (!url.searchParams.has('attachment_batch')) return;
      url.searchParams.delete('attachment_batch');
      history.replaceState(history.state, '', url.pathname + url.search + url.hash);
    }
    window.addEventListener('pagehide', function () { cancel(); });
    var api = { capture: capture, ready: ready, cancel: cancel, clear: clear, settle: clear, open: openRecord,
      historyCards: historyCards, prepareTransfer: prepareTransfer, claim: claim };
    instances.set(form, api);
    var durable = persist(); render();
    if (durable) removeBatchQuery();
    ready().catch(function () {}); return api;
  }
  window.CodexMessageAttachments = { forForm: init };
  document.addEventListener('DOMContentLoaded', function () { document.querySelectorAll('form').forEach(init); });
}());
