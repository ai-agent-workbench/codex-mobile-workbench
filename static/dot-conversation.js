/* Pull-only dot adapter for the real conversation template. No native API. */
(function () {
  'use strict';
  var root = document.getElementById('native-conversation');
  if (!root || root.dataset.conversationKind !== 'dot') return;
  var cid = root.dataset.conversationId;
  var prefix = (location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/) || [])[1];
  var base = (prefix ? '/' + prefix : '') + '/settings/dot/' + encodeURIComponent(cid);
  var form = document.getElementById('conversation-composer');
  var textarea = form.querySelector('textarea');
  var sendButton = form.querySelector('[type="submit"]');
  var messages = document.getElementById('conversation-messages');
  var state = document.getElementById('conversation-state');
  var note = document.getElementById('conversation-note');
  var snapshot = JSON.parse(document.getElementById('conversation-initial-snapshot').textContent);
  var pendingKey = 'dot-pending:' + cid;
  var pending = null;
  var sending = false;
  var stopped = false;
  var timer;
  try { pending = JSON.parse(localStorage.getItem(pendingKey) || 'null'); } catch (_) {}
  if (pending && (typeof pending.id !== 'string' || typeof pending.text !== 'string')) pending = null;

  // Match the native composer: retain its position through blur/click and
  // follow the visible viewport while the phone keyboard is open.
  function updateKeyboardPosition() {
    if (!document.body.classList.contains('native-input-mode')) return;
    var vv = window.visualViewport;
    var top = vv ? vv.offsetTop : 0;
    var visibleBottom = vv ? vv.offsetTop + vv.height : window.innerHeight;
    var keyboard = navigator.virtualKeyboard && navigator.virtualKeyboard.boundingRect;
    if (keyboard && keyboard.height > 0) visibleBottom = Math.min(visibleBottom, keyboard.y);
    document.documentElement.style.setProperty('--native-vv-top', Math.round(top) + 'px');
    document.documentElement.style.setProperty(
      '--native-composer-top',
      Math.max(top + 44, Math.round(visibleBottom - form.offsetHeight)) + 'px'
    );
  }

  function inputMode(enabled) {
    document.body.classList.toggle('native-input-mode', enabled);
    if (enabled) {
      requestAnimationFrame(updateKeyboardPosition);
      [120, 320, 650].forEach(function (delay) {
        setTimeout(updateKeyboardPosition, delay);
      });
    } else {
      document.documentElement.style.removeProperty('--native-composer-top');
      document.documentElement.style.setProperty('--native-vv-top', '0px');
    }
  }

  textarea.addEventListener('focus', function () { inputMode(true); });
  textarea.addEventListener('blur', function () {
    setTimeout(function () {
      if (document.activeElement !== textarea) inputMode(false);
    }, 180);
  });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', updateKeyboardPosition);
    window.visualViewport.addEventListener('scroll', updateKeyboardPosition);
  }
  if (navigator.virtualKeyboard &&
      typeof navigator.virtualKeyboard.addEventListener === 'function') {
    navigator.virtualKeyboard.addEventListener('geometrychange', updateKeyboardPosition);
  }

  function showNote(text) { note.textContent = text; note.hidden = !text; }
  function node(tag, className, text) {
    var item = document.createElement(tag);
    item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function bubble(role, text, id, label) {
    var article = node('article', 'native-message ' + role);
    article.dataset.messageKey = id;
    var body = node('div', 'native-message-body', text);
    body.dataset.messageSource = text;
    var wrap = node('div', 'native-bubble' + (role === 'agent' ? ' native-final' : ''));
    wrap.appendChild(body);
    var footer = node('div', 'native-message-footer');
    footer.appendChild(node('span', 'native-message-meta', label));
    footer.appendChild(node('span', 'native-message-footer-spacer'));
    var more = node('button', 'native-message-more', '⋯');
    more.type = 'button'; more.setAttribute('aria-label', '消息操作');
    footer.appendChild(more);
    var menu = node('div', 'native-message-menu');
    menu.hidden = true; menu.dataset.lightDismiss = 'menu';
    menu.dataset.lightDismissTrigger = '.native-message-more';
    [['copy', '复制全文'], ['quote', '引用到输入框']].forEach(function (value) {
      var button = node('button', '', value[1]); button.type = 'button';
      button.dataset.dotAction = value[0]; menu.appendChild(button);
    });
    wrap.append(footer, menu);
    article.appendChild(wrap);
    return article;
  }
  function clearPending() {
    var sentText = pending && pending.text;
    pending = null;
    try { localStorage.removeItem(pendingKey); } catch (_) {}
    if (textarea.value === sentText) {
      textarea.value = '';
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }
  var markedReadGeneration = 0;
  var pendingReadGeneration = 0;
  var readRunning = false;
  async function markDisplayedRead(generation) {
    pendingReadGeneration = Math.max(pendingReadGeneration, Number(generation) || 0);
    if (document.hidden || readRunning || pendingReadGeneration <= markedReadGeneration) return;
    var target = pendingReadGeneration;
    readRunning = true;
    var data = new URLSearchParams({generation:String(target)});
    try {
      var response = await fetch(base + '/read', {method:'POST', body:data, credentials:'same-origin', cache:'no-store'});
      if (!response.ok) return;
      markedReadGeneration = Math.max(markedReadGeneration, target);
    } catch (_) { /* Retry on the next visible refresh; never discard a newer reply. */ }
    finally { readRunning = false; }
  }
  function render(value) {
    if (!value || value.backend !== 'dot_mailbox' || value.conversation.id !== cid) {
      stopped = true;
      sendButton.disabled = true;
      throw new Error('返回的 dot 会话不匹配，已停止发送。');
    }
    if (snapshot && value.conversation.revision < snapshot.conversation.revision) return;
    var nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 130;
    snapshot = value;
    var existing = new Map(Array.from(messages.children).map(function (item) { return [item.dataset.messageKey, item]; }));
    var desired = [];
    function reuse(role, text, id, label) {
      var key = role + ':' + id;
      var item = existing.get(key);
      if (!item || item.querySelector('.native-message-body').dataset.messageSource !== text) item = bubble(role, text, key, label);
      desired.push(item);
    }
    value.messages.forEach(function (message) {
      reuse('user', message.text, message.id, '你');
      if (message.reply) reuse('agent', message.reply.text, message.reply.id, 'dot');
      if (pending && message.id === pending.id) {
        if (message.text !== pending.text) throw new Error('消息 ID 已关联不同内容，输入仍保留。');
        clearPending();
      }
    });
    // Unchanged polling results keep the same nodes: selection, code folds,
    // open menus and search focus survive the five-second refresh.
    desired.forEach(function (item, index) {
      if (messages.children[index] !== item) messages.insertBefore(item, messages.children[index] || null);
    });
    Array.from(messages.children).forEach(function (item) { if (!desired.includes(item)) item.remove(); });
    var waiting = value.messages.some(function (message) { return !message.reply; });
    state.textContent = waiting ? '等待 dot' : '可发消息';
    showNote(waiting ? '消息已保存，等待 dot 回复。' : '');
    if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
    var displayedGeneration = value.conversation.reply_generation || 0;
    requestAnimationFrame(function () { markDisplayedRead(displayedGeneration); });
  }
  async function request(path, options) {
    var response = await fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options));
    var data = await response.json();
    if (!response.ok) {
      var code = data.error && data.error.code;
      if (response.status === 401 || response.status === 403 || response.status === 503) stopped = true;
      var text = code === 'mailbox_conflict' ? '会话已经更新，输入保留。请刷新后再次发送。' :
        (response.status === 401 ? '登录已失效，请重新登录；输入保留。' :
        response.status === 403 ? '当前账号无权访问这条 dot 对话；输入保留。' :
        response.status === 503 ? 'dot 通道尚未启用或暂时不可用；输入保留。' : '发送未成功，输入保留。');
      throw new Error(text);
    }
    return data;
  }
  async function refresh() {
    if (stopped || sending) return;
    try { render(await request(base + '/snapshot')); }
    catch (error) { showNote(error.message || '无法刷新，输入保留。'); }
  }
  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (stopped || sending || !textarea.value.trim()) return;
    if (pending && pending.text !== textarea.value) {
      showNote('上一次消息仍待核对。请先刷新，或恢复上一次文字后重试。');
      return;
    }
    if (!pending) pending = { id: 'dm_' + crypto.randomUUID(), text: textarea.value };
    try { localStorage.setItem(pendingKey, JSON.stringify(pending)); }
    catch (_) { showNote('浏览器无法保存发送标记，已暂停发送；输入保留。'); return; }
    sending = true;
    sendButton.disabled = true;
    try {
      var payload = new URLSearchParams({ message_id: pending.id, text: pending.text,
                                         expected_revision: String(snapshot.conversation.revision) });
      render(await request(base + '/messages', { method: 'POST', body: payload }));
    } catch (error) { showNote(error.message || '送达结果待核对。请刷新后重试，消息 ID 会保持不变。'); }
    finally { sending = false; sendButton.disabled = stopped; }
  });
  document.getElementById('reconcile-conversation').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  textarea.addEventListener('input', function () {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(180, textarea.scrollHeight) + 'px';
    updateKeyboardPosition();
  });
  var searchDialog = document.getElementById('conversation-search-dialog');
  if (searchDialog) {
    var searchInput = document.getElementById('conversation-search-input');
    var searchStatus = document.getElementById('conversation-search-status');
    var searchIndex = -1;
    function clearSearch() {
      messages.querySelectorAll('.is-search-hit').forEach(function (item) { item.classList.remove('is-search-hit'); });
    }
    function updateSearch(move) {
      clearSearch();
      var query = searchInput.value.trim().toLowerCase();
      if (!query) { searchIndex = -1; searchStatus.textContent = '输入关键词开始查找'; return; }
      var hits = Array.from(messages.querySelectorAll('.native-message')).filter(function (item) {
        var body = item.querySelector('.native-message-body');
        return (body.dataset.messageSource || body.textContent).toLowerCase().includes(query);
      });
      if (!hits.length) { searchIndex = -1; searchStatus.textContent = '没有找到'; return; }
      if (move === 'next') searchIndex = (searchIndex + 1 + hits.length) % hits.length;
      else if (move === 'prev') searchIndex = (searchIndex - 1 + hits.length) % hits.length;
      else searchIndex = Math.min(Math.max(searchIndex, 0), hits.length - 1);
      hits[searchIndex].classList.add('is-search-hit');
      searchStatus.textContent = (searchIndex + 1) + ' / ' + hits.length;
      if (move) hits[searchIndex].scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    document.getElementById('search-conversation').addEventListener('click', function () {
      var menu = root.querySelector('.native-head-menu'); if (menu) menu.open = false;
      searchDialog.showModal(); searchInput.focus(); updateSearch(false);
    });
    searchInput.addEventListener('input', function () { searchIndex = -1; updateSearch(false); });
    document.getElementById('conversation-search-next').addEventListener('click', function () { updateSearch('next'); });
    document.getElementById('conversation-search-prev').addEventListener('click', function () { updateSearch('prev'); });
    searchDialog.querySelector('[data-close-search]').addEventListener('click', function () { searchDialog.close(); });
    searchDialog.addEventListener('close', clearSearch);
    window.addEventListener('codex:conversation-messages-enhanced', function () { if (searchDialog.open) updateSearch(false); });
  }
  render(snapshot);
  timer = setInterval(function () { if (!document.hidden) refresh(); }, 5000);
  window.addEventListener('pagehide', function () { clearInterval(timer); });
  window.addEventListener('pageshow', function (event) { if (event.persisted) { refresh(); timer = setInterval(function () { if (!document.hidden) refresh(); }, 5000); } });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
}());
