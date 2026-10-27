(function () {
  'use strict';

  var root = document.getElementById('native-conversation');
  var composer = document.getElementById('conversation-composer');
  if (!root || !composer) return;

  var conversationId = root.dataset.conversationId;
  var messages = document.getElementById('conversation-messages');
  var stateNode = document.getElementById('conversation-state');
  var titleNode = document.getElementById('conversation-title');
  var note = document.getElementById('conversation-note');
  var stopButton = document.getElementById('stop-conversation');
  var sendButton = composer.querySelector('.native-send');
  var textarea = composer.querySelector('textarea[name="text"]');
  var messageLength = document.getElementById('conversation-message-length');
  var model = document.getElementById('conversation-model');
  var effort = document.getElementById('conversation-effort');
  var tier = document.getElementById('conversation-tier');
  var collaborationMode = document.getElementById('conversation-collaboration-mode');
  var permission = document.getElementById('conversation-permission');
  var settings = document.getElementById('conversation-settings');
  var settingsSummary = document.getElementById('settings-summary');
  var notification = composer.querySelector('input[name="completion_notification"]');
  var notificationButton = document.getElementById('conversation-notify');
  var tierButton = document.getElementById('conversation-tier-toggle');
  var contextButton = document.getElementById('conversation-context-toggle');
  var refreshButton = document.getElementById('reconcile-conversation');
  var newContentButton = document.getElementById('conversation-new-content');
  var diagnostics = document.getElementById('conversation-diagnostics');
  var diagnosticsSummary = document.getElementById('conversation-diagnostics-summary');
  var diagnosticsValues = document.getElementById('conversation-diagnostics-values');
  var copyExportButton = document.getElementById('copy-conversation-export');
  var downloadExportButton = document.getElementById('download-conversation-export');
  var contextDialog = document.getElementById('conversation-context');
  var contextSwitchCard = document.getElementById('context-switch-card');
  var contextSwitchDetails = document.getElementById('context-switch-details');
  var contextSwitchEvents = document.getElementById('context-switch-events');
  var exportDialog = document.getElementById('conversation-export-dialog');
  var searchDialog = document.getElementById('conversation-search-dialog');
  var searchInput = document.getElementById('conversation-search-input');
  var searchStatus = document.getElementById('conversation-search-status');
  var viewer = document.getElementById('conversation-artifact-viewer');
  var viewerImage = document.getElementById('artifact-viewer-image');
  var viewerFrame = document.getElementById('artifact-viewer-frame');
  var viewerTitle = document.getElementById('artifact-viewer-title');
  var viewerDownload = document.getElementById('artifact-viewer-download');
  var viewerControls = document.getElementById('artifact-viewer-controls');
  var toast = document.getElementById('conversation-toast');

  var requiredNodes = [
    messages, stateNode, titleNode, note, stopButton, sendButton,
    textarea, messageLength, model, effort, tier, permission, settings,
    settingsSummary, notification, notificationButton, tierButton,
    contextButton, refreshButton, newContentButton, diagnostics,
    diagnosticsSummary, diagnosticsValues, copyExportButton,
    downloadExportButton, contextDialog, contextSwitchCard, contextSwitchDetails,
    contextSwitchEvents, exportDialog, searchDialog,
    searchInput, searchStatus, viewer, viewerImage, viewerFrame,
    viewerTitle, viewerDownload,
    viewerControls, toast
  ];
  if (requiredNodes.some(function (value) { return !value; })) {
    var compatibilityNote = note || document.createElement('p');
    compatibilityNote.className = 'native-connection-note warning';
    compatibilityNote.textContent = '页面资源正在更新，请刷新后继续；输入内容不会被发送。';
    compatibilityNote.hidden = false;
    if (!note) root.appendChild(compatibilityNote);
    return;
  }

  // Keyboard layout is a core conversation behavior, not a Markdown
  // enhancement. Keep it here so a missing optional renderer cannot leave
  // the bottom navigation visible or the composer behind Android's keyboard.
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
      Math.max(top + 44, Math.round(visibleBottom - composer.offsetHeight)) + 'px'
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

  // Android Chrome can restore a tab whose deferred Markdown asset failed
  // without requesting that asset again. Retry it independently; raw text
  // remains readable via the CSS pre-wrap fallback if both attempts fail.
  var enhancementRetryAttempts = 0;
  var enhancementRetryTimer = null;

  function markdownEnhancementReady() {
    return Boolean(window.codexConversationModules &&
      window.codexConversationModules.markdown === 'ready');
  }

  function retryMarkdownEnhancement() {
    clearTimeout(enhancementRetryTimer);
    enhancementRetryTimer = null;
    if (markdownEnhancementReady() || enhancementRetryAttempts >= 2) return;
    var source = document.querySelector('script[data-conversation-module="markdown"]');
    if (!source || !source.src) return;
    enhancementRetryAttempts += 1;
    var url = new URL(source.src, location.href);
    url.searchParams.set('retry', String(enhancementRetryAttempts));
    var retry = document.createElement('script');
    retry.src = url.href;
    retry.async = true;
    retry.dataset.conversationModuleRetry = 'markdown';
    retry.onload = function () {
      retry.remove();
      if (!markdownEnhancementReady()) {
        enhancementRetryTimer = setTimeout(retryMarkdownEnhancement, 700);
      }
    };
    retry.onerror = function () {
      retry.remove();
      enhancementRetryTimer = setTimeout(retryMarkdownEnhancement, 1200);
    };
    document.head.appendChild(retry);
  }

  function scheduleMarkdownRecovery(delay) {
    if (markdownEnhancementReady() || enhancementRetryAttempts >= 2 ||
        enhancementRetryTimer) return;
    enhancementRetryTimer = setTimeout(retryMarkdownEnhancement, delay || 0);
  }

  window.addEventListener('load', function () { scheduleMarkdownRecovery(500); });

  var snapshot = null;
  var historyPages = new Map();
  var historyTail = null;
  var historyPaged = false;
  var historyFull = false;
  var historyLoading = false;
  var fullHistoryFlight = null;
  var historyBar = document.getElementById('conversation-history-controls');
  var historyOlder = document.getElementById('conversation-history-older');
  var historyStatus = document.getElementById('conversation-history-status');
  var historyFallback = '';
  var syncing = null;
  var settingsSaving = null;
  var settingsDirty = false;
  var pollTimer = null;
  var pollBackoff = 4000;
  var localRetryNotBefore = 0;
  var serverRetryNotBefore = 0;
  var lastSyncStartedAt = 0;
  var minimumAutomaticSyncInterval = 2500;
  var snapshotSyncTimeoutMs = 15000;
  var syncAgain = false;
  var socket = null;
  var connectionRetry = null;
  var socketHeartbeatTimer = null;
  var socketLastConfirmedAt = 0;
  var socketHeartbeatInterval = 15000;
  var socketStaleAfter = 45000;
  var terminalEventGenerations = Object.create(null);
  var markedReadGeneration = 0;
  var pendingReadGeneration = 0;
  var readRequestRunning = false;
  var pendingKey = 'codex-conversation-pending:' + conversationId;
  // Plan/Goal actions are independent of message delivery. Keep their durable
  // identity separately so an uncertain POST is verified read-only, never sent again.
  var actionPendingKey = 'codex-conversation-action:' + conversationId;
  var pendingAction = null;
  var actionFlight = null;
  // Keep receipt recovery separate from transient notes overwritten by polls.
  var actionRecovery = document.createElement('div');
  actionRecovery.className = 'native-action-recovery'; actionRecovery.hidden = true;
  note.insertAdjacentElement('afterend', actionRecovery);
  var draftKey = textarea.dataset.draftKey;
  var draftRevisionKey = draftKey + ':revision';
  var attachmentDraft = window.CodexMessageAttachments && window.CodexMessageAttachments.forForm(composer);
  var attachmentReadySubmission = false;
  var attachmentPreparation = false;
  var pendingAttachmentCapture = null;
  var messageStatePrefix = 'codex-conversation-message:' + conversationId + ':';
  var commentaryGroupStatePrefix = 'codex-conversation-commentary-group:' + conversationId + ':';
  var pendingCommand = null;
  // A durable pending ID is not evidence that its HTTP request is still in
  // flight. Restored or failed submissions remain unconfirmed, never replayed.
  var pendingMessageState = 'unconfirmed';
  var pendingReceiptFlight = null;
  // Only clear the composer if it still contains the exact text that this
  // page put there for a pending command. A later edit, prefill, or quote is
  // a new draft even when an old command is eventually acknowledged.
  var pendingTextInComposerId = null;
  var pendingComposerRevision = null;
  var pendingComposerValue = null;
  var composerInputRevision = 0;
  var lastComposerValue = textarea.value;
  var sendFlight = null;
  var receiptStates = Object.create(null);
  // Async native questions have no server-side request/response record: App
  // Server deliberately receives their replies as ordinary user messages.
  // Keep unsent UI state only in this page instance, never in browser storage.
  var asyncQuestionDrafts = Object.create(null);
  var lastOperation = '尚未发送';
  var automaticRequestsStopped = false;
  var lastPresentationSignature = null;
  var presentationNodes = new Map();
  var presentationTurns = new Map();
  var turnPresentationRevisions = new WeakMap();
  var turnTimelines = new WeakMap();
  var turnEventSignatures = new WeakMap();
  var liveDecoration = { anchor: null, signature: null, nodes: [] };
  var firstRender = true;
  // A conversation is a timeline, so a normal entry starts at its newest
  // content.  Do not reuse an arbitrary pixel from a previous visit: that
  // pixel is especially wrong after Markdown enhancement or a font change.
  var initialBottomUntil = 0;
  var initialBottomCancelled = false;
  var toastTimer = null;
  var serverActivityNode = node('p', 'native-server-activity');
  var searchHits = [];
  var searchIndex = -1;
  var zoom = 1;
  var viewerHistoryToken = null;
  var viewerFocus = null;
  var requestedTurnId = null;
  var requestedCommandId = null;
  var requestedItemId = null;
  var pendingSearchRevealKey = null;
  var searchFocusKey = null;
  var searchFocusUntil = 0;
  var searchFocusCancelled = false;
  var turnFocusScheduled = false;
  var activeStates = new Set([
    'starting_thread', 'starting_turn', 'active', 'interrupting', 'reconciling'
  ]);
  var prefixMatch = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
  var appPrefix = prefixMatch ? '/' + prefixMatch[1] : '';

  function messageCharacterCount(value) {
    return Array.from(String(value || '').trim()).length;
  }

  function renderMessageLength() {
    var count = messageCharacterCount(textarea.value);
    messageLength.hidden = !count;
    messageLength.textContent = '已输入 ' + count.toLocaleString('zh-CN') + ' 字符';
    messageLength.classList.remove('warning', 'failure');
  }

  function route(path) {
    if (!path) return path;
    if (appPrefix && path.indexOf(appPrefix + '/') === 0) return path;
    return appPrefix + path;
  }

  function node(tag, className, text) {
    var value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined && text !== null) value.textContent = String(text);
    return value;
  }

  function commandId(prefix) {
    var value = (crypto.randomUUID ? crypto.randomUUID() :
      Date.now().toString(36) + Math.random().toString(36).slice(2));
    return prefix + value;
  }

  function storageGet(key) {
    try { return localStorage.getItem(key); } catch (error) { return null; }
  }

  function storageSet(key, value) {
    try { localStorage.setItem(key, value); return true; } catch (error) { return false; }
  }

  function storageRemove(key) {
    try { localStorage.removeItem(key); } catch (error) {}
  }

  function responseError(response, data) {
    var detail = data && data.error;
    var message = typeof detail === 'string' ? detail : detail && detail.message;
    if (!message && response.status === 401) message = '登录已失效，请重新登录；输入仍保留在下方。';
    if (!message && response.status === 429) message = '请求太频繁，请稍后再试；输入仍保留在下方。';
    var error = new Error(message || '请求未完成；输入仍保留在下方。');
    error.status = data && data.reason_code === 'authentication_required' ? 401 : response.status;
    error.data = data || {};
    error.retryAfter = Number(response.headers.get('Retry-After')) || 0;
    return error;
  }

  function transportError(error) {
    // An AbortController belongs to the read lifecycle, not to the server.
    // In particular, a hidden page or a new foreground generation may retire
    // a GET while fetch is decoding JSON.  Do not turn that intentional abort
    // into a permanent protocol/auth failure.
    if (error && error.name === 'AbortError') return error;
    if (error && (error.status !== undefined || error.data || error.permanent)) return error;
    return new Error('请求未完成；请检查登录或网络状态，输入仍保留在下方。');
  }

  function stopAutomaticRequests(error) {
    if (!error || (!error.permanent && ![401, 403, 404].includes(error.status))) return;
    var firstStop = !automaticRequestsStopped;
    automaticRequestsStopped = true;
    if (error.status === 401 && firstStop) window.dispatchEvent(new CustomEvent('codex:auth-required'));
    clearTimeout(pollTimer);
    if (connectionRetry) connectionRetry.halt();
    clearInterval(socketHeartbeatTimer);
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
  }

  function jsonResponse(response) {
    var contentType = response.headers.get('Content-Type') || '';
    if (response.redirected || !contentType.toLowerCase().includes('application/json')) {
      var error = responseError(response, {});
      error.message = response.redirected || response.status === 401 ?
        '登录状态可能已失效，请刷新页面重新登录；输入仍保留在下方。' :
        '工作台返回了无法识别的页面；请刷新后重试，输入仍保留在下方。';
      error.permanent = response.redirected || (response.status >= 200 && response.status < 500);
      throw error;
    }
    return response.json().catch(function (cause) {
      if (cause && cause.name === 'AbortError') throw cause;
      var error = responseError(response, {});
      error.message = '工作台返回的数据不完整；输入仍保留在下方。';
      error.permanent = response.status >= 200 && response.status < 500;
      throw error;
    }).then(function (data) {
      if (!response.ok) throw responseError(response, data);
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        var error = new Error('工作台返回的数据不完整；输入仍保留在下方。');
        error.permanent = true;
        throw error;
      }
      return data;
    });
  }

  function jsonRequest(url, options) {
    options = options || {};
    options.credentials = 'same-origin';
    options.redirect = 'follow';
    options.headers = Object.assign({ Accept: 'application/json' }, options.headers || {});
    return fetch(url, options).then(jsonResponse).catch(function (error) {
      var normalized = transportError(error);
      if (!(options.signal && options.signal.aborted) && (!normalized || normalized.name !== 'AbortError')) stopAutomaticRequests(normalized);
      throw normalized;
    });
  }

  function formRequest(url, values) {
    var body = new FormData();
    Object.keys(values).forEach(function (key) {
      var value = values[key];
      if (Array.isArray(value)) value.forEach(function (item) { body.append(key, item); });
      else if (value !== undefined && value !== null) body.append(key, value);
    });
    return jsonRequest(url, { method: 'POST', body: body });
  }

  function friendlyState(value) {
    return {
      new: '新对话', starting_thread: '正在开始', starting_turn: '正在发送',
      active: '执行中', interrupting: '正在停止', reconciling: '正在同步',
      needs_user: '需要核对', idle: '本轮结束'
    }[value] || value || '未知';
  }

  function terminalTurn(conversation) {
    if (!snapshot || !conversation || !conversation.last_terminal_turn_id) return null;
    return (snapshot.turns || []).find(function (turn) {
      return turn.id === conversation.last_terminal_turn_id;
    }) || null;
  }

  function terminalOutcome(conversation) {
    if (!conversation || conversation.state !== 'idle') return null;
    var turn = terminalTurn(conversation);
    return turn && (turn.status === 'failed' || turn.status === 'interrupted') ? turn.status : null;
  }

  function friendlyConversationState(conversation) {
    if (conversation && conversation.state === 'idle' && snapshot && snapshot.stop && snapshot.stop.status === 'verified') return '本对话及子代理已停止';
    if (snapshot && snapshot.stop && snapshot.stop.status === 'unconfirmed') return '停止结果待核对';
    var outcome = terminalOutcome(conversation);
    if (outcome === 'failed') return '执行失败';
    if (outcome === 'interrupted') return '已停止';
    return friendlyState(conversation && conversation.state);
  }

  function friendlyTurnError(error) {
    if (!error) return '本轮回复失败。';
    if (error.codexErrorInfo === 'serverOverloaded') {
      return '模型服务暂时繁忙，本轮未生成最终答复。请稍后继续；工作台不会自动重复已经发生的操作。';
    }
    return error.message || '本轮回复失败。';
  }

  function showNote(text, warning, kind) {
    note.textContent = text || '';
    note.hidden = !text;
    note.classList.toggle('warning', Boolean(warning));
    note.dataset.kind = text ? (kind || '') : '';
  }

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 2400);
  }

  function sizeLabel(value) {
    var size = Number(value) || 0;
    var units = ['B', 'KB', 'MB', 'GB'];
    var index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return (index ? size.toFixed(size >= 10 ? 0 : 1) : Math.round(size)) + ' ' + units[index];
  }

  function tokenLabel(value) {
    var count = Number(value);
    if (!Number.isFinite(count) || count < 0) return '—';
    if (count >= 1000000) return (count / 1000000).toFixed(count >= 10000000 ? 0 : 1) + 'M';
    if (count >= 1000) return (count / 1000).toFixed(count >= 10000 ? 0 : 1) + 'K';
    return String(Math.round(count));
  }

  function timeLabel(value) {
    if (!value) return '';
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function timestamp(value) {
    var parsed = Date.parse(value || '');
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function durationLabel(started, completed) {
    var start = timestamp(started);
    var end = timestamp(completed);
    if (!start || !end || end < start) return '';
    var seconds = Math.round((end - start) / 1000);
    if (seconds < 60) return seconds + ' 秒';
    return Math.floor(seconds / 60) + '分' + String(seconds % 60).padStart(2, '0') + '秒';
  }

  function elapsedLabel(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  }

  function characterCount(text) {
    return Array.from(String(text || '')).length;
  }

  function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(value);
    return new Promise(function (resolve, reject) {
      var area = node('textarea');
      area.value = value;
      area.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
      document.body.appendChild(area);
      area.select();
      try { document.execCommand('copy') ? resolve() : reject(new Error('copy failed')); }
      catch (error) { reject(error); }
      area.remove();
    });
  }

  function readMessageState(key) {
    try { return JSON.parse(storageGet(messageStatePrefix + key) || '{}'); }
    catch (error) { return {}; }
  }

  function writeMessageState(key, value) {
    storageSet(messageStatePrefix + key, JSON.stringify(value));
  }

  function readCommentaryGroupState(turnId) {
    try { return JSON.parse(storageGet(commentaryGroupStatePrefix + turnId) || '{}'); }
    catch (error) { return {}; }
  }

  function writeCommentaryGroupState(turnId, value) {
    storageSet(commentaryGroupStatePrefix + turnId, JSON.stringify(value));
  }

  function collapsedPreview(text) {
    var value = String(text || '')
      .replace(/^\s*#{1,6}\s+/gm, '')
      .replace(/^\s*[-*+]\s+/gm, '')
      .replace(/\*\*|__|`/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    if (!value) return '这条消息没有可预览的文字';
    var sentence = value.match(/^.{1,220}?[。！？!?](?:[”’」』])?/);
    return (sentence ? sentence[0] : value.slice(0, 220)).trim();
  }

  function messageBubble(role, text, meta, extraClass, key, options) {
    options = options || {};
    var wrap = node('article', 'native-message ' + role);
    wrap.dataset.messageKey = key;
    if (key && key.indexOf('command:') === 0) wrap.dataset.commandId = key.slice(8);
    // The Markdown enhancer measures completed answers after it has rendered
    // them.  Mark complete answers and user messages; the render pass marks
    // the newest of each role so its outer body stays open by default.
    if (options.final || role === 'user') wrap.dataset.autoFoldCandidate = '1';
    var bubble = node('div', 'native-bubble' + (extraClass ? ' ' + extraClass : ''));
    if (options.final) bubble.appendChild(node('span', 'native-final-mark', options.plan ? '计划' : '最终答复'));
    var body = node('div', 'native-message-body', text || '');
    body.dataset.messageSource = text || '';
    var preview = node('div', 'native-message-preview', collapsedPreview(text));
    bubble.append(body, preview);
    if (options.menu !== false) {
      var footer = node('div', 'native-message-footer');
      var metaText = [meta, characterCount(text) + ' 字'].filter(Boolean).join(' · ');
      footer.append(node('span', 'native-message-meta', metaText), node('span', 'native-message-footer-spacer'));
      var more = node('button', 'native-message-more', '⋯');
      more.type = 'button'; more.setAttribute('aria-label', '消息操作');
      footer.appendChild(more);
      bubble.appendChild(footer);
      var menu = node('div', 'native-message-menu');
      menu.dataset.lightDismiss = 'menu';
      menu.dataset.lightDismissTrigger = '.native-message-more';
      menu.hidden = true;
      [['copy', '复制全文'], ['quote', '引用到输入框'], ['important', '标记重要'], ['fold', '折叠消息']].forEach(function (entry) {
        var button = node('button', '', entry[1]);
        button.type = 'button'; button.dataset.action = entry[0]; menu.appendChild(button);
      });
      bubble.appendChild(menu);
      var saved = readMessageState(key);
      if (saved.important) wrap.classList.add('is-important');
      if (saved.folded) {
        wrap.classList.add('is-folded');
        menu.querySelector('[data-action="fold"]').textContent = '展开消息';
      }
    }
    wrap.appendChild(bubble);
    return wrap;
  }

  function attachmentCards(records) {
    return attachmentDraft && typeof attachmentDraft.historyCards === 'function' ?
      attachmentDraft.historyCards(records) : null;
  }

  function interruptedMessage(turn) {
    var wrap = node('article', 'native-message agent');
    wrap.dataset.messageKey = 'interrupted:' + turn.id;
    wrap.dataset.turnId = turn.id;
    var bubble = node('div', 'native-bubble native-interrupted');
    bubble.append(node('strong', '', '本轮已停止'), node('div', '', '尚未生成最终答复。你可以继续处理，也可以把本轮首条消息重新放回输入框。'));
    var actions = node('div', 'native-interrupted-actions');
    var resume = node('button', '', '继续处理'); resume.type = 'button'; resume.dataset.interruptedAction = 'continue'; resume.dataset.turnId = turn.id;
    var retry = node('button', '', '重新发送'); retry.type = 'button'; retry.dataset.interruptedAction = 'retry'; retry.dataset.turnId = turn.id;
    actions.append(resume, retry); bubble.appendChild(actions); wrap.appendChild(bubble);
    return wrap;
  }

  function artifactActions(artifact, previewKind) {
    var actions = node('div', 'native-artifact-actions');
    if (previewKind) {
      var preview = node('button', '', '预览');
      preview.type = 'button';
      preview.dataset.artifactPreview = previewKind;
      preview.dataset.contentUrl = route(artifact.content_url);
      preview.dataset.downloadUrl = route(artifact.download_url);
      preview.dataset.artifactName = artifact.name;
      actions.appendChild(preview);
    }
    var download = node('a', '', '下载');
    download.href = route(artifact.download_url);
    download.download = artifact.name;
    download.dataset.download = '';
    actions.appendChild(download);
    return actions;
  }

  function artifactSection(values, label, key) {
    if (!values || !values.length) return null;
    var section = node('section', 'native-artifacts');
    section.dataset.eventKey = key;
    var head = node('div', 'native-artifacts-head');
    head.append(node('strong', '', label || '本轮产物'), node('small', '', values.length + ' 个文件'));
    section.appendChild(head);
    values.forEach(function (artifact) {
      if (artifact.preview_kind === 'image') {
        var imageCard = node('article', 'native-image-artifact');
        var previewButton = node('button', 'native-artifact-preview');
        previewButton.type = 'button';
        previewButton.dataset.artifactPreview = 'image';
        previewButton.dataset.contentUrl = route(artifact.content_url);
        previewButton.dataset.downloadUrl = route(artifact.download_url);
        previewButton.dataset.artifactName = artifact.name;
        var image = node('img');
        image.src = route(artifact.content_url); image.alt = artifact.name; image.loading = 'lazy';
        previewButton.appendChild(image); imageCard.appendChild(previewButton);
        var caption = node('div', 'native-artifact-caption');
        var info = node('div');
        info.append(node('strong', '', artifact.name), node('span', '', '图片 · ' + sizeLabel(artifact.size)));
        caption.append(info, artifactActions(artifact, 'image'));
        imageCard.appendChild(caption); section.appendChild(imageCard); return;
      }
      if (artifact.preview_kind === 'pdf') {
        var pdfCard = node('article', 'native-pdf-artifact');
        var pdfPreview = node('button', 'native-artifact-preview native-pdf-preview', 'PDF · 点按在当前页面预览');
        pdfPreview.type = 'button';
        pdfPreview.dataset.artifactPreview = 'pdf';
        pdfPreview.dataset.contentUrl = route(artifact.content_url);
        pdfPreview.dataset.downloadUrl = route(artifact.download_url);
        pdfPreview.dataset.artifactName = artifact.name;
        pdfCard.appendChild(pdfPreview);
        var pdfCaption = node('div', 'native-artifact-caption');
        var pdfInfo = node('div');
        pdfInfo.append(node('strong', '', artifact.name), node('span', '', 'PDF · ' + sizeLabel(artifact.size)));
        pdfCaption.append(pdfInfo, artifactActions(artifact, 'pdf'));
        pdfCard.appendChild(pdfCaption); section.appendChild(pdfCard); return;
      }
      if (artifact.preview_kind === 'text') {
        var textCard = node('details', 'native-text-artifact');
        textCard.open = true;
        textCard.appendChild(node('summary', '', artifact.name + ' · ' + sizeLabel(artifact.size)));
        textCard.appendChild(node('pre', '', artifact.preview_text || ''));
        var textActions = node('div', 'native-text-actions');
        var downloadText = node('a', '', '下载原文件');
        downloadText.href = route(artifact.download_url); downloadText.download = artifact.name; downloadText.dataset.download = '';
        textActions.appendChild(downloadText); textCard.appendChild(textActions); section.appendChild(textCard); return;
      }
      if (artifact.preview_kind === 'html' && artifact.preview_url) {
        var htmlCard = node('article', 'native-file-card native-html-artifact');
        var htmlOpen = node('button', 'native-file-open');
        htmlOpen.type = 'button';
        htmlOpen.dataset.artifactPreview = 'html';
        htmlOpen.dataset.contentUrl = route(artifact.preview_url);
        htmlOpen.dataset.downloadUrl = route(artifact.download_url);
        htmlOpen.dataset.artifactName = artifact.name;
        htmlOpen.appendChild(node('span', 'native-file-icon', 'HTML'));
        var htmlInfo = node('span', 'native-file-info');
        htmlInfo.append(node('strong', '', artifact.name), node('span', '', '互动网页 · 受限预览 · ' + sizeLabel(artifact.size)));
        htmlOpen.append(htmlInfo, node('span', 'native-file-open-action', '打开'));
        var htmlDownload = node('a', 'native-download', '下载');
        htmlDownload.href = route(artifact.download_url);
        htmlDownload.download = artifact.name;
        htmlDownload.dataset.download = '';
        htmlCard.append(htmlOpen, htmlDownload); section.appendChild(htmlCard); return;
      }
      var card = node('a', 'native-file-card');
      card.href = route(artifact.download_url);
      card.download = artifact.name;
      card.dataset.download = '';
      var extension = (artifact.name.split('.').pop() || '文件').toUpperCase().slice(0, 6);
      card.appendChild(node('span', 'native-file-icon', extension));
      var fileInfo = node('span', 'native-file-info');
      fileInfo.append(node('strong', '', artifact.name), node('span', '', '文件 · ' + sizeLabel(artifact.size)));
      var fileDownload = node('span', 'native-download', '下载');
      card.append(fileInfo, fileDownload); section.appendChild(card);
    });
    return section;
  }

  function normalizedText(value) {
    // Exactly Python str.split() whitespace; JS \s also includes FEFF and
    // omits the four separators, which could hide a server-visible message.
    return String(value || '').replace(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/g, ' ').replace(/^ +| +$/g, '');
  }

  function visibleAgentItems(values) {
    var visible = (values || []).filter(function (item) {
      return item.text && item.is_current_source !== false &&
        ((item.kind === 'plan' && item.native_item_id !== 'turn-plan') || (item.kind === 'agent_message' &&
          (item.phase === 'commentary' || item.phase === 'final_answer')));
    });
    var realtimeKeys = new Set(visible.filter(function (item) {
      return item.dedup_role ? item.dedup_role === 'realtime' : /^msg_/.test(String(item.native_item_id || ''));
    }).map(function (item) {
      return item.phase + '\u0000' + (item.delivery === 'async' ? 'async' : 'sync') + '\u0000' + normalizedText(item.text);
    }));
    return visible.filter(function (item) {
      var itemId = String(item.native_item_id || '');
      var copy = item.dedup_role ? item.dedup_role === 'legacy-copy' : /^item-\d+$/.test(itemId);
      if (!copy) return true;
      return !realtimeKeys.has(item.phase + '\u0000' + (item.delivery === 'async' ? 'async' : 'sync') + '\u0000' + normalizedText(item.text));
    });
  }

  function commandMeta(command, turn, first) {
    var parts = [command && command.origin === 'scheduled' ? '定时执行' : command && command.origin === 'system' ? '系统' : '你'];
    var time = timeLabel(command.created_at || command.updated_at);
    if (time) parts.push(time);
    if (first) parts.push('第 ' + turn.ordinal + ' 轮');
    else {
      parts.push({
        submitted: '发送中', accepted: '已送达当前执行', completed: '已送达',
        queued: '等待下一轮', delivery_unknown: '投递待核对', rejected: command.reason === 'maintenance_quiescing' ? '维护中 · 未发送' : '未发送'
      }[command.state] || '已送达');
    }
    return parts.join(' · ');
  }

  function modelDisplay(conversation) {
    var value = conversation && conversation.settings && conversation.settings.model;
    var option = Array.from(model.options || []).find(function (item) { return item.value === value; });
    return option ? option.textContent.replace(/-/g, ' ') : (value || 'Codex');
  }

  // Native questions are projected by the server; browser timers only display
  // an absolute deadline. They never submit answers or decide a winner.
  var questionDrafts = new Map();
  var questionClockOffset = 0;

  function currentQuestion(id) {
    return ((snapshot && snapshot.questions) || []).find(function (value) { return value.id === id; });
  }

  function questionDraft(id) {
    if (!questionDrafts.has(id)) questionDrafts.set(id, {
      selected: Object.create(null), free: Object.create(null), error: '',
      submitting: false, command: null, payload: null, originalOpen: false
    });
    return questionDrafts.get(id);
  }

  function questionRemaining(question) {
    var deadline = timestamp(question.deadline_at);
    return deadline ? Math.max(0, Math.ceil((deadline - Date.now() - questionClockOffset) / 1000)) : null;
  }

  function questionStatus(question) {
    var auto = question.answer_source === 'deadline';
    return {
      awaiting_answer: question.is_blocking ? '需要你的回答' : '等待你的回答',
      answer_claimed: auto ? '正在按推荐项自动继续…' : '回答已提交，正在回传…',
      resolved_before_send: '正在核对答案送达状态…',
      response_sent: auto ? '已按推荐项自动继续' : '回答已回传，等待任务继续',
      awaiting_activity: auto ? '已按推荐项自动继续' : '回答已回传，等待任务继续',
      answered: auto ? '已按推荐项自动继续' : '已回答，任务已继续',
      closed: '问题已关闭',
      expired: '等待已结束，尚未确认自动回传',
      awaiting_reissue: '连接恢复中，等待原生问题重新下发',
      delivery_unknown: '答案送达待核实，不会自动重复发送'
    }[question.state] || '正在同步问题状态';
  }

  function questionOptions(question) {
    return Array.isArray(question.options) ? question.options : [];
  }

  function questionAnswerRows(section, request) {
    var answers = request.answers_masked;
    if (!answers || request.answer_source !== 'user') return;
    var list = node('dl', 'native-question-answers');
    (request.questions || []).forEach(function (question) {
      if (!Object.prototype.hasOwnProperty.call(answers, question.id)) return;
      list.append(node('dt', '', question.header || question.question),
        node('dd', '', question.isSecret ? '已提交敏感回答（不回显）' : String(answers[question.id] || '')));
    });
    section.appendChild(list);
  }

  function questionHistory(request, draft) {
    var details = node('details', 'native-question-history');
    details.open = draft.originalOpen;
    details.appendChild(node('summary', '', '查看原问题与选项'));
    (request.questions || []).forEach(function (question) {
      details.appendChild(node('p', 'native-question-original', question.question));
      var list = node('ul');
      questionOptions(question).forEach(function (option) {
        list.appendChild(node('li', '', option.label + (option.description ? '：' + option.description : '')));
      });
      if (list.childNodes.length) details.appendChild(list);
    });
    details.addEventListener('toggle', function () { draft.originalOpen = details.open; });
    return details;
  }

  function questionCard(request) {
    var draft = questionDraft(request.id);
    if (draft.attempt !== (request.attempt || 1)) {
      draft.command = null; draft.payload = null; draft.submitting = false;
      draft.attempt = request.attempt || 1;
    }
    var pending = request.state === 'awaiting_answer';
    var card = node('article', 'native-question-card');
    card.dataset.questionId = request.id;
    card.dataset.eventKey = 'question:' + request.id;
    card.dataset.questionTurn = request.turn_id;
    var heading = node('div', 'native-question-heading');
    heading.appendChild(node('strong', '', '中途提问'));
    if (pending) {
      var countdown = node('span', 'native-question-countdown');
      countdown.dataset.questionCountdown = request.id;
      heading.appendChild(countdown);
    }
    card.appendChild(heading);
    if (pending) {
      var hint = request.is_blocking ? '此问题需要回答，不会自动跳过。' :
        '超时按推荐项自动继续。';
      card.appendChild(node('p', 'native-question-hint', hint));
      var fields = node('div', 'native-question-fields');
      (request.questions || []).forEach(function (question, index) {
        var fieldset = node('fieldset', 'native-question-field');
        fieldset.appendChild(node('legend', '', question.question));
        var options = questionOptions(question);
        options.forEach(function (option, optionIndex) {
          var label = node('label', 'native-question-option');
          var radio = node('input');
          radio.type = 'radio';
          radio.name = 'question-' + request.id + '-' + index;
          radio.value = option.label;
          radio.dataset.questionField = index + ':option:' + optionIndex;
          radio.checked = draft.selected[question.id] === option.label;
          radio.disabled = draft.submitting;
          radio.addEventListener('change', function () {
            draft.selected[question.id] = radio.value;
            draft.error = '';
          });
          var text = node('span', 'native-question-option-text');
          var recommended = /\s*[（(]Recommended[）)]\s*$/i.test(option.label) || /\s*[（(]推荐[）)]\s*$/.test(option.label);
          var title = recommended ? option.label.replace(/\s*[（(](?:Recommended|推荐)[）)]\s*$/i, '') : option.label;
          var line = node('span', 'native-question-option-title', title);
          if (recommended) line.appendChild(node('span', 'native-question-recommended', '推荐'));
          text.appendChild(line);
          if (option.description) text.appendChild(node('small', '', option.description));
          label.append(radio, text);
          fieldset.appendChild(label);
        });
        if (!options.length || question.isOther) {
          var fieldId = 'native-answer-' + request.id + '-' + index;
          var freeLabel = node('label', 'native-question-free-label', options.length ? '或直接填写你的答案' : '你的回答');
          freeLabel.htmlFor = fieldId;
          var free = node(question.isSecret ? 'input' : 'textarea', 'native-question-free');
          if (question.isSecret) {
            free.type = 'password';
            free.autocomplete = 'off';
            free.spellcheck = false;
          } else free.rows = 2;
          free.id = fieldId;
          free.dataset.questionField = index + ':free';
          free.value = draft.free[question.id] || '';
          free.disabled = draft.submitting;
          free.setAttribute('aria-label', question.question);
          free.addEventListener('input', function () { draft.free[question.id] = free.value; draft.error = ''; });
          fieldset.append(freeLabel, free);
        }
        fields.appendChild(fieldset);
      });
      card.appendChild(fields);
      var error = node('p', 'native-question-error', draft.error);
      error.setAttribute('role', 'status');
      error.hidden = !draft.error;
      card.appendChild(error);
      var submit = node('button', 'native-question-submit', draft.submitting ? '正在提交…' : draft.command && draft.error ? '重试提交' : '提交回答');
      submit.type = 'button';
      submit.disabled = draft.submitting;
      submit.dataset.questionSubmit = request.id;
      submit.addEventListener('click', function () { submitQuestion(request.id); });
      card.appendChild(submit);
    } else {
      var receipt = node('p', 'native-question-receipt', questionStatus(request));
      receipt.setAttribute('role', 'status');
      card.appendChild(receipt);
      questionAnswerRows(card, request);
      card.appendChild(questionHistory(request, draft));
      // Secrets and unsent form contents are never kept after authoritative
      // settlement or copied into local/session storage.
      draft.selected = Object.create(null);
      draft.free = Object.create(null);
      draft.payload = null;
      draft.command = null;
    }
    var footer = [timeLabel(request.received_at)];
    if (request.deadline_source === 'host_optional_default' && pending) footer.push('工作台等待时限');
    card.appendChild(node('small', 'native-question-meta', footer.filter(Boolean).join(' · ')));
    return card;
  }

  function refreshQuestionCard(id) {
    var request = currentQuestion(id);
    var existing = Array.from(messages.querySelectorAll('[data-question-id]')).find(function (card) { return card.dataset.questionId === id; });
    if (!request || !existing) return;
    var focus = captureQuestionFocus();
    var replacement = questionCard(request);
    existing.replaceWith(replacement);
    // Local validation/submission refreshes must update the keyed DOM cache
    // too; an unchanged streaming event may otherwise resurrect the old form.
    var cached = presentationNodes.get('question:' + id);
    if (cached) cached.nodes = cached.nodes.map(function (value) {
      return value === existing ? replacement : value;
    });
    updateQuestionCountdowns();
    updateAsyncQuestionCountdowns();
    restoreQuestionFocus(focus);
  }

  function submitQuestion(id) {
    var request = currentQuestion(id);
    var draft = questionDraft(id);
    if (!request || request.state !== 'awaiting_answer' || draft.submitting) return;
    if (questionRemaining(request) === 0) {
      sync(true).catch(function () {});
      return;
    }
    var answers = Object.create(null);
    var incomplete = false;
    (request.questions || []).forEach(function (question) {
      var free = draft.free[question.id] || '';
      var selected = draft.selected[question.id] || '';
      var value = free.trim() ? free : selected;
      if (!value.trim()) incomplete = true;
      answers[question.id] = value;
    });
    if (incomplete) {
      draft.error = '请回答全部问题后提交。';
      refreshQuestionCard(id);
      return;
    }
    var payload = JSON.stringify(answers);
    if (draft.payload !== payload || !draft.command) {
      draft.command = commandId('q');
      draft.payload = payload;
    }
    draft.submitting = true;
    draft.error = '';
    refreshQuestionCard(id);
    formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/questions/' + encodeURIComponent(id) + '/answer'), {
      command_id: draft.command, answers_json: payload
    }).then(function (data) {
      draft.submitting = false;
      draft.error = '';
      render(data);
    }).catch(function (error) {
      draft.submitting = false;
      draft.error = '提交未确认：' + error.message;
      // A response may have been lost after the server accepted the command.
      // Reconcile first; an explicit retry keeps the same command and payload.
      refreshQuestionCard(id);
      sync(true).catch(function () {});
    });
  }

  function updateQuestionCountdowns() {
    messages.querySelectorAll('[data-question-countdown]').forEach(function (value) {
      var question = currentQuestion(value.dataset.questionCountdown);
      if (!question || question.state !== 'awaiting_answer') return;
      var remaining = questionRemaining(question);
      value.textContent = remaining === null ? '需要回答' : remaining ?
        '剩余 ' + remaining + ' 秒' : '后台正在处理…';
      var card = value.closest('[data-question-id]');
      var submit = card && card.querySelector('[data-question-submit]');
      if (submit) submit.disabled = questionDraft(question.id).submitting || remaining === 0;
    });
  }

  function captureQuestionFocus() {
    var control = document.activeElement;
    var card = control && control.closest && control.closest('[data-question-id]');
    if (card && control.dataset.questionField) {
      return { kind: 'blocking', id: card.dataset.questionId, field: control.dataset.questionField,
        start: typeof control.selectionStart === 'number' ? control.selectionStart : null,
        end: typeof control.selectionEnd === 'number' ? control.selectionEnd : null,
        scrollTop: control.scrollTop };
    }
    var asyncField = control && control.closest && control.closest('[data-async-question-key]');
    if (!asyncField || !control.dataset.asyncQuestionField) return null;
    return { kind: 'async', id: asyncField.dataset.asyncQuestionKey,
      start: typeof control.selectionStart === 'number' ? control.selectionStart : null,
      end: typeof control.selectionEnd === 'number' ? control.selectionEnd : null,
      scrollTop: control.scrollTop };
  }

  function restoreQuestionFocus(saved) {
    if (!saved) return;
    if (saved.kind === 'async') {
      var asyncCard = Array.from(messages.querySelectorAll('[data-async-question-key]')).find(function (value) {
        return value.dataset.asyncQuestionKey === saved.id;
      });
      var asyncControl = asyncCard && asyncCard.querySelector('[data-async-question-field]');
      if (!asyncControl || asyncControl.disabled) return;
      asyncControl.focus({ preventScroll: true });
      if (saved.start !== null && typeof asyncControl.setSelectionRange === 'function') {
        try { asyncControl.setSelectionRange(saved.start, saved.end); } catch (error) {}
      }
      asyncControl.scrollTop = saved.scrollTop;
      return;
    }
    var card = Array.from(messages.querySelectorAll('[data-question-id]')).find(function (value) { return value.dataset.questionId === saved.id; });
    if (!card) return;
    var control = Array.from(card.querySelectorAll('[data-question-field]')).find(function (value) { return value.dataset.questionField === saved.field; });
    if (!control || control.disabled) return;
    control.focus({ preventScroll: true });
    if (saved.start !== null && typeof control.setSelectionRange === 'function') {
      try { control.setSelectionRange(saved.start, saved.end); } catch (error) {}
    }
    control.scrollTop = saved.scrollTop;
  }

  setInterval(function () {
    updateQuestionCountdowns();
    updateAsyncQuestionCountdowns();
  }, 1000);

  function nativeAsyncQuestions(item) {
    if (!item || item.delivery !== 'async' || !Array.isArray(item.questions) ||
        !item.questions.length) return null;
    var valid = item.questions.every(function (question) {
      if (!question || typeof question.title !== 'string' || !question.title.trim()) return false;
      return question.options === null || (
        Array.isArray(question.options) && question.options.length &&
        question.options.every(function (option) {
          return typeof option === 'string' && option.trim();
        })
      );
    });
    return valid ? item.questions : null;
  }

  function asyncQuestionRequest(question) {
    var request = question && question.request;
    if (!request || typeof request.id !== 'string' || !request.id ||
        typeof request.state !== 'string') return null;
    return request;
  }

  function asyncQuestionRemaining(request) {
    var deadline = timestamp(request && request.deadline_at);
    return deadline ? Math.max(0, Math.ceil((deadline - Date.now() - questionClockOffset) / 1000)) : null;
  }

  function asyncQuestionStatus(request) {
    if (!request) return '';
    var automatic = request.answer_source === 'deadline';
    return {
      awaiting_answer: '等待你的回答',
      answer_claimed: automatic ? '正在按推荐项自动继续…' : '正在发送…',
      answered: automatic ? '已按推荐项自动继续' : '已作为普通消息发送。',
      delivery_unknown: '发送结果待核对。',
      rejected: '未发送，请重新选择后发送。'
    }[request.state] || '正在同步问题状态';
  }

  function asyncQuestionKey(turn, item, index) {
    return 'async:' + turn.id + ':' + (item.native_item_id || item.revision || index) + ':' + index;
  }

  function asyncQuestionDraft(key, question) {
    var draft = asyncQuestionDrafts[key];
    if (!draft) {
      draft = {
        selected: Array.isArray(question.options) ? question.options[0] : '',
        free: '', freeOpen: !Array.isArray(question.options), command: null,
        submitting: false, sent: false, error: ''
      };
      asyncQuestionDrafts[key] = draft;
    }
    return draft;
  }

  function asyncAnswerValue(draft) {
    return draft.free.trim() || String(draft.selected || '').trim();
  }

  function asyncQuestionCard(turn, item, agentLabel) {
    var questions = nativeAsyncQuestions(item);
    if (!questions) return null;
    var card = node('article', 'native-async-question-card');
    card.dataset.asyncNativeItem = item.native_item_id || '';
    var head = node('div', 'native-async-question-head');
    head.appendChild(node('p', 'native-async-question-heading', '中途提问'));
    var countdownRequest = questions.map(asyncQuestionRequest).find(function (request) {
      return request && request.state === 'awaiting_answer' && asyncQuestionRemaining(request) !== null;
    });
    if (countdownRequest) {
      var countdown = node('span', 'native-async-question-countdown');
      countdown.dataset.asyncQuestionCountdown = countdownRequest.id;
      head.appendChild(countdown);
      card.appendChild(head);
      card.appendChild(node('p', 'native-async-question-hint', '超时按推荐项自动继续'));
    } else card.appendChild(head);
    questions.forEach(function (question, index) {
      var key = asyncQuestionKey(turn, item, index);
      var draft = asyncQuestionDraft(key, question);
      var request = asyncQuestionRequest(question);
      var pending = !request || request.state === 'awaiting_answer';
      var field = node('section', 'native-async-question-field');
      field.dataset.asyncQuestionKey = key;
      if (request) field.dataset.asyncQuestionRequest = request.id;
      field.appendChild(node('p', 'native-async-question-title', question.title));
      if (pending && Array.isArray(question.options)) {
        var options = node('div', 'native-async-question-options');
        options.setAttribute('role', 'group');
        options.setAttribute('aria-label', question.title);
        question.options.forEach(function (option) {
          var choice = node('button', 'native-async-question-option');
          choice.type = 'button';
          choice.dataset.asyncAnswerOption = option;
          choice.disabled = draft.submitting || (request && asyncQuestionRemaining(request) === 0);
          choice.setAttribute('aria-pressed', String(draft.selected === option && !draft.free.trim()));
          choice.append(node('span', 'native-async-question-dot',
            draft.selected === option && !draft.free.trim() ? '●' : ''),
          node('span', 'native-async-question-option-text', option));
          choice.addEventListener('click', function () {
            draft.selected = option; draft.free = ''; draft.freeOpen = false;
            draft.command = null; draft.sent = false; draft.error = '';
            lastPresentationSignature = null; render(snapshot);
          });
          options.appendChild(choice);
        });
        field.appendChild(options);
      }
      if (pending) {
        var free = node('div', 'native-async-question-free');
        free.hidden = !draft.freeOpen;
        var input = node('textarea');
        input.rows = 2;
        input.value = draft.free;
        input.placeholder = '自填回答';
        input.dataset.asyncQuestionField = key;
        input.setAttribute('aria-label', question.title + '的自填回答');
        input.disabled = draft.submitting || (request && asyncQuestionRemaining(request) === 0);
        input.addEventListener('input', function () {
          draft.free = input.value; draft.command = null; draft.sent = false; draft.error = '';
        });
        free.appendChild(input); field.appendChild(free);
        var actions = node('div', 'native-async-question-actions');
        if (Array.isArray(question.options)) {
          var other = node('button', 'native-async-question-other', draft.freeOpen ? '收起自填' : '自填');
          other.type = 'button'; other.disabled = draft.submitting || (request && asyncQuestionRemaining(request) === 0);
          other.addEventListener('click', function () {
            draft.freeOpen = !draft.freeOpen; draft.error = '';
            lastPresentationSignature = null; render(snapshot);
            if (draft.freeOpen) requestAnimationFrame(function () {
              var target = Array.from(messages.querySelectorAll('[data-async-question-field]')).find(function (value) {
                return value.dataset.asyncQuestionField === key;
              });
              if (target) target.focus();
            });
          });
          actions.appendChild(other);
        }
        var submit = node('button', 'native-async-question-send', draft.submitting ? '发送中…' : '发送');
        submit.type = 'button';
        submit.disabled = draft.submitting || (request && asyncQuestionRemaining(request) === 0);
        submit.addEventListener('click', function () { sendAsyncQuestionAnswer(turn, item, index); });
        actions.appendChild(submit); field.appendChild(actions);
      }
      if (draft.error && pending) {
        var error = node('p', 'native-async-question-error', draft.error);
        error.setAttribute('role', 'status'); field.appendChild(error);
      } else if (!pending && request) {
        var receipt = node('p', 'native-async-question-receipt', asyncQuestionStatus(request));
        receipt.setAttribute('role', 'status'); field.appendChild(receipt);
      } else if (draft.sent) {
        var fallbackReceipt = node('p', 'native-async-question-receipt', '已作为普通消息发送。');
        fallbackReceipt.setAttribute('role', 'status'); field.appendChild(fallbackReceipt);
      }
      card.appendChild(field);
    });
    card.appendChild(node('small', 'native-async-question-meta',
      [agentLabel, timeLabel(item.created_at || item.updated_at)].filter(Boolean).join(' · ')));
    return card;
  }

  function sendAsyncQuestionAnswer(turn, item, index) {
    var questions = nativeAsyncQuestions(item);
    if (!questions || !questions[index] || !snapshot) return;
    var question = questions[index];
    var key = asyncQuestionKey(turn, item, index);
    var draft = asyncQuestionDraft(key, question);
    var request = asyncQuestionRequest(question);
    if (request && (request.state !== 'awaiting_answer' || asyncQuestionRemaining(request) === 0)) {
      sync(true).catch(function () {}); return;
    }
    var answer = asyncAnswerValue(draft);
    if (!answer) {
      draft.error = '请先选择或填写回答。'; draft.sent = false;
      lastPresentationSignature = null; render(snapshot); return;
    }
    if (draft.submitting) return;
    if (!draft.command) draft.command = commandId('m');
    draft.submitting = true; draft.error = ''; draft.sent = false;
    lastPresentationSignature = null; render(snapshot);
    var path = request ? '/conversation/' + encodeURIComponent(conversationId) +
      '/async-questions/' + encodeURIComponent(request.id) + '/answer' :
      '/conversation/' + encodeURIComponent(conversationId) + '/messages';
    formRequest(route(path), request ? {
      command_id: draft.command, text: answer
    } : {
      command_id: draft.command, text: answer
    }).then(function (data) {
      validateSnapshot(data);
      draft.submitting = false; draft.command = null; draft.sent = !request;
      lastOperation = '已确认发送'; lastPresentationSignature = null;
      render(data); showNote('');
    }).catch(function (error) {
      draft.submitting = false;
      var command = error && error.data && error.data.command;
      if (command && command.state === 'rejected') draft.command = null;
      draft.error = (error && error.message) || '发送结果待核对；请同步后重试。';
      lastOperation = command && command.state === 'delivery_unknown' ? '是否送达待核对' : '发送失败';
      if (error && error.data && error.data.snapshot) {
        try { render(error.data.snapshot); } catch (renderError) {}
      }
      lastPresentationSignature = null;
      if (snapshot) render(snapshot);
      showNote(draft.error, true, 'send');
    });
  }

  function updateAsyncQuestionCountdowns() {
    messages.querySelectorAll('[data-async-question-countdown]').forEach(function (value) {
      var requestId = value.dataset.asyncQuestionCountdown;
      var question = Array.from(messages.querySelectorAll('[data-async-question-request]')).find(function (field) {
        return field.dataset.asyncQuestionRequest === requestId;
      });
      if (!question) return;
      var item = (snapshot.turns || []).flatMap(function (turn) { return turn.items || []; }).flatMap(function (item) {
        return item.questions || [];
      }).find(function (candidate) {
        var request = asyncQuestionRequest(candidate); return request && request.id === requestId;
      });
      var request = asyncQuestionRequest(item);
      if (!request || request.state !== 'awaiting_answer') return;
      var remaining = asyncQuestionRemaining(request);
      value.textContent = remaining ? '剩余 ' + remaining + ' 秒' : '后台正在处理…';
    });
  }

  function buildTurnEvents(turn) {
    var cached = turnTimelines.get(turn);
    if (cached) return cached;
    var events = [];
    var sequence = 0;
    var commands = (turn.commands || []).filter(function (command) {
      return command.kind === 'start' || command.kind === 'steer';
    });
    commands.forEach(function (command) {
      events.push({
        type: 'command', value: command, sequence: sequence++,
        time: timestamp(command.created_at || command.updated_at)
      });
    });
    var items = visibleAgentItems(turn.items);
    var liveCommentary = turn.status === 'inProgress' ? items.filter(function (item) {
      return item.phase === 'commentary';
    }).slice(-1)[0] : null;
    items.forEach(function (item) {
      var asyncQuestions = nativeAsyncQuestions(item);
      events.push({
        type: item.kind === 'plan' ? 'plan' : item.delivery === 'async' ?
          (asyncQuestions ? 'async-question' : 'async-message') :
          (item.phase === 'final_answer' ? 'final' : 'commentary'), value: item,
        sequence: sequence++, time: timestamp(item.created_at || item.updated_at)
      });
    });
    var hasFinal = items.some(function (item) {
      return item.kind === 'agent_message' && item.phase === 'final_answer' && item.delivery !== 'async';
    });
    if (!hasFinal && turn.final_text) {
      events.push({
        type: 'final', value: { text: turn.final_text, updated_at: turn.completed_at, native_item_id: 'fallback' },
        sequence: sequence++, time: timestamp(turn.completed_at)
      });
      hasFinal = true;
    }
    events.sort(function (left, right) {
      if (left.time && right.time && left.time !== right.time) return left.time - right.time;
      if (left.time && !right.time) return -1;
      if (!left.time && right.time) return 1;
      return left.sequence - right.sequence;
    });
    var hasPlan = items.some(function (item) {
      return item.kind === 'plan' && item.status === 'completed';
    });
    var timeline = { events: events, commands: commands, hasFinal: hasFinal,
      hasResult: hasFinal || hasPlan, liveCommentary: liveCommentary };
    turnTimelines.set(turn, timeline);
    return timeline;
  }

  function commentaryGroup(turn, commentaryEvents, collectable) {
    var group = node('section', 'native-commentary-group');
    group.dataset.commentaryGroupTurn = turn.id;
    var saved = readCommentaryGroupState(turn.id);
    var folded = collectable && saved.expanded !== true;
    group.classList.toggle('is-group-folded', folded);
    var first = commentaryEvents[0].value;
    var summary = '本轮过程 · ' + commentaryEvents.length + ' 条 · ' + collapsedPreview(first.text);
    var head = node(collectable ? 'button' : 'div', 'native-commentary-group-head');
    if (!collectable) {
      head.textContent = (turn.status === 'inProgress' ? '正在处理' : '本轮过程') +
        ' · ' + commentaryEvents.length + ' 条过程记录';
    } else {
      head.type = 'button';
      head.dataset.commentaryGroupAction = 'toggle';
      head.setAttribute('aria-expanded', String(!folded));
      head.append(node('strong', '', folded ? summary : '收起本轮过程 · ' + commentaryEvents.length + ' 条'),
        node('span', 'native-commentary-group-toggle', folded ? '展开' : '收起'));
    }
    group.appendChild(head);
    return group;
  }

  function buildPresentationEvents(data) {
    var events = [];
    var sequence = 0;
    (data.turns || []).forEach(function (turn) {
      var timeline = buildTurnEvents(turn);
      var firstCommand = timeline.commands.length ? timeline.commands[0].id : null;
      var commentaryEvents = timeline.events.filter(function (event) {
        return event.type === 'commentary';
      });
      var groupedCommentary = commentaryEvents.length > 1;
      var collectCommentary = groupedCommentary && turn.status === 'completed' && timeline.hasResult;
      timeline.events.forEach(function (event) {
        events.push({
          type: 'turn', turn: turn, timeline: timeline, event: event,
          firstCommand: firstCommand, commentaryEvents: commentaryEvents,
          groupedCommentary: groupedCommentary, collectCommentary: collectCommentary,
          firstCommentary: event === commentaryEvents[0],
          time: event.time, sequence: sequence++
        });
      });
      var artifacts = turn.artifacts || [];
      if (artifacts.length) events.push({
        type: 'artifacts', turn: turn, values: artifacts,
        time: timestamp(artifacts[0].created_at || turn.completed_at || turn.started_at),
        sequence: sequence++
      });
      if (turn.artifact_publish_failed) events.push({
        type: 'artifact-error', turn: turn,
        time: timestamp(turn.completed_at || turn.started_at), sequence: sequence++
      });
      if (turn.status === 'interrupted' && !timeline.hasFinal) events.push({
        type: 'interrupted', turn: turn,
        time: timestamp(turn.completed_at || turn.started_at), sequence: sequence++
      });
      if (turn.error) events.push({
        type: 'error', turn: turn,
        time: timestamp(turn.completed_at || turn.started_at), sequence: sequence++
      });
    });
    (data.questions || []).forEach(function (question) {
      events.push({ type: 'question', value: question,
        time: timestamp(question.received_at), sequence: sequence++ });
    });
    var detachedMessages = (data.messages || []).filter(function (command) {
      return !command.result_turn_id && !command.target_turn_id &&
        (command.state === 'rejected' || command.state === 'delivery_unknown');
    });
    detachedMessages.forEach(function (command) {
      events.push({ type: 'detached', command: command,
        time: timestamp(command.created_at || command.updated_at), sequence: sequence++ });
    });
    (data.queue || []).forEach(function (command) {
      events.push({ type: 'queue', command: command,
        time: timestamp(command.created_at || command.updated_at), sequence: sequence++ });
    });
    events.sort(function (left, right) {
      if (left.time && right.time && left.time !== right.time) return left.time - right.time;
      if (left.time && !right.time) return -1;
      if (!left.time && right.time) return 1;
      return left.sequence - right.sequence;
    });
    return { events: events, detachedMessages: detachedMessages };
  }

  function executablePlan(data, turn, item) {
    if (Object.prototype.hasOwnProperty.call(data || {}, 'plan_confirmation')) {
      var candidate = data.plan_confirmation;
      return Boolean(candidate && candidate.state === 'direct' && candidate.turn_id === turn.id &&
        candidate.item_id === item.native_item_id && candidate.plan_digest === item.plan_digest &&
        data.conversation.settings.collaboration_mode === 'plan' &&
        !(pendingAction && pendingAction.kind === 'plan_implement'));
    }
    var turns = data && data.turns || [];
    var plans = (turn.items || []).filter(function (value) { return value.kind === 'plan' && value.native_item_id !== 'turn-plan' && value.is_current_source !== false && value.status === 'completed' && value.text; });
    var newerAllowed = turns.every(function (value) {
      if (Number(value.ordinal) <= Number(turn.ordinal)) return true;
      return ['inProgress', 'completed'].includes(value.status) && (value.commands || []).length &&
        value.commands.every(function (command) { return command.kind === 'goal_continue'; }) &&
        !(value.items || []).some(function (next) { return next.kind === 'plan' && next.native_item_id !== 'turn-plan' && next.is_current_source !== false; });
    });
    return Boolean(turn.status === 'completed' && plans[plans.length - 1] === item && newerAllowed &&
      item && item.kind === 'plan' && item.status === 'completed' && item.text && item.native_item_id &&
      /^[a-f0-9]{64}$/.test(String(item.plan_digest || '')) &&
      data.conversation && data.conversation.settings && data.conversation.settings.collaboration_mode === 'plan' &&
      !(data.queue || []).length &&
      !(pendingAction && pendingAction.kind === 'plan_implement'));
  }

  function planActions(data, turn, item) {
    if (!executablePlan(data, turn, item)) return null;
    var actions = node('div', 'native-plan-actions');
    var implement = node('button', 'native-plan-implement', '确认并实施');
    implement.type = 'button';
    implement.addEventListener('click', function () {
      if (implement.disabled || actionFlight) return;
      var action = { id: commandId('p'), kind: 'plan_implement', turn_id: turn.id,
        item_id: item.native_item_id, plan_digest: item.plan_digest };
      implement.disabled = true;
      postAction(action, '/conversation/' + encodeURIComponent(conversationId) + '/plan/implement', {
        command_id: action.id, turn_id: action.turn_id, item_id: action.item_id,
        plan_digest: action.plan_digest, skills: selectedSkills(), skills_explicit: '1'
      }).then(function (data) {
        var command = actionCommand(data);
        showToast(command && command.state === 'queued' ? '已排队交给原生目标继续' : '已确认计划，正在实施');
      }).catch(function (error) {
        showNote((error && error.message) || '计划确认结果待核对；不会自动重发。', true, 'goal');
      }).finally(function () { implement.disabled = false; });
    });
    var revise = node('button', 'native-plan-revise', '继续修改计划');
    revise.type = 'button';
    revise.addEventListener('click', function () {
      // Keep the selected Plan mode and let the user explicitly write the revision.
      collaborationMode.value = 'plan';
      textarea.focus();
      showToast('已保留 Plan；请继续修改计划。');
    });
    actions.append(implement, revise);
    return actions;
  }

  // Later user messages do not silently remove the path from Plan to execution.
  // Review uses a server-issued boundary, never a browser guess about "好的".
  var planReviewDialog = null;
  var planReviewStage = null;
  var planReviewContent = null;
  var planReviewStatus = null;
  var planReviewConfirm = null;

  function planReviewCandidate(data) {
    return data && data.conversation && (data.conversation.settings || {}).collaboration_mode === 'plan' ?
      data.plan_confirmation : null;
  }

  function planReviewKey(candidate) {
    return candidate ? [candidate.state, candidate.turn_id, candidate.item_id, candidate.plan_digest,
      candidate.context_digest, candidate.reason].join(':') : '';
  }

  function planReviewReason(candidate) {
    return {
      latest_plan_not_completed: '新的计划尚未完成，完成后即可确认实施。',
      latest_plan_invalid: '最新计划暂不可确认，请继续规划后核对。',
      ordinary_input_pending: '有消息的投递结果尚待核对，不能跳过它实施旧计划。',
      new_native_turn_unattributed: '后续轮次的输入来源尚未核实，请先同步对话或继续规划。',
      subsequent_input_not_user_message: '后续有其他操作，需继续规划并核对当前实施范围。',
      ordinary_input_not_completed: '当前回复尚未结束，结束后即可核对计划并实施。',
      plan_already_confirmed: '这份计划已有实施请求，请核对原请求结果；修改计划请继续规划，不重复发起实施。',
      automatic_turn_needs_reconcile: '后续自动轮已异常结束，请先同步对话并核对。'
    }[candidate && candidate.reason] || '计划或补充要求需要重新核对，暂未提交实施。';
  }

  function closePlanReview() {
    if (planReviewStage && planReviewStage.inflight) return;
    planReviewStage = null;
    if (planReviewDialog && planReviewDialog.open) planReviewDialog.close();
  }

  function continuePlanning() {
    if (planReviewStage && planReviewStage.inflight) return;
    closePlanReview();
    collaborationMode.value = 'plan';
    collaborationMode.dispatchEvent(new Event('change', { bubbles: true }));
    textarea.focus();
    showToast('已保留 Plan；请继续补充或修改计划。');
  }

  function ensurePlanReviewDialog() {
    if (planReviewDialog) return;
    planReviewDialog = node('dialog', 'native-plan-review-sheet');
    planReviewDialog.id = 'conversation-plan-review';
    planReviewDialog.setAttribute('aria-labelledby', 'conversation-plan-review-title');
    var head = node('div', 'native-plan-review-head');
    var title = node('strong', '', '核对计划和后续补充');
    title.id = 'conversation-plan-review-title';
    var close = node('button', 'secondary', '关闭');
    close.type = 'button'; close.addEventListener('click', closePlanReview);
    head.append(title, close);
    planReviewContent = node('div', 'native-plan-review-content');
    planReviewStatus = node('p', 'native-plan-review-status');
    planReviewStatus.setAttribute('role', 'status');
    var actions = node('div', 'native-plan-review-footer');
    var revise = node('button', 'secondary', '继续修改计划');
    revise.type = 'button'; revise.addEventListener('click', continuePlanning);
    planReviewConfirm = node('button', '', '确认并实施');
    planReviewConfirm.id = 'conversation-plan-review-confirm';
    planReviewConfirm.type = 'button';
    planReviewConfirm.addEventListener('click', submitPlanReview);
    actions.append(revise, planReviewConfirm);
    planReviewDialog.append(head, planReviewContent, planReviewStatus, actions);
    planReviewDialog.addEventListener('cancel', function (event) {
      if (planReviewStage && planReviewStage.inflight) event.preventDefault(); else planReviewStage = null;
    });
    planReviewDialog.addEventListener('click', function (event) {
      if (event.target === planReviewDialog && outsideDialog(planReviewDialog, event)) closePlanReview();
    });
    document.body.appendChild(planReviewDialog);
  }

  function openPlanReview(candidate) {
    if (planReviewKey(candidate) !== planReviewKey(planReviewCandidate(snapshot))) {
      showNote('计划或补充已更新，请使用当前回复下的入口重新核对。', true, 'goal'); return;
    }
    ensurePlanReviewDialog();
    planReviewStage = { candidate: candidate, key: planReviewKey(candidate), inflight: false, failed: false };
    planReviewContent.replaceChildren();
    planReviewContent.append(node('p', 'native-plan-review-note',
      '确认后自动进入普通模式，以本计划为基础并保留下面的后续补充；有冲突或未明确处会先澄清。输入框中的未发送草稿不会被发送或清除。'),
      node('h3', '', '计划全文'), node('pre', 'native-plan-review-text', candidate.text || '最新计划尚未完成。'));
    (candidate.supplements || []).forEach(function (supplement, index) {
      var section = node('section', 'native-plan-review-supplement');
      section.append(node('h3', '', (supplement.scope === 'plan_turn' ? '计划轮输入 ' : '后续补充 ') + (index + 1)),
        node('pre', 'native-plan-review-text', supplement.text || '此消息仅附有文件。'));
      var cards = attachmentCards(supplement.attachments || []);
      if (cards) section.appendChild(cards);
      else (supplement.attachments || []).forEach(function (record) {
        section.appendChild(node('p', 'native-plan-review-note', '附件：' + (record.name || '文件')));
      });
      planReviewContent.appendChild(section);
    });
    planReviewStatus.textContent = candidate.state === 'review' ? '' : planReviewReason(candidate);
    if (pendingAction || actionFlight || pendingCommand || sendFlight) {
      planReviewStatus.textContent = '上次发送或操作尚待核对，不能重复提交实施。';
    }
    planReviewConfirm.disabled = candidate.state !== 'review' || !/^[a-f0-9]{64}$/.test(candidate.context_digest || '') ||
      Boolean(pendingAction || actionFlight || pendingCommand || sendFlight);
    if (!planReviewDialog.open) planReviewDialog.showModal();
    planReviewContent.scrollTop = 0;
  }

  function updatePlanReview(data) {
    if (!planReviewStage || planReviewStage.inflight || !planReviewDialog.open) return;
    var current = planReviewCandidate(data);
    if (planReviewKey(current) !== planReviewStage.key || pendingCommand || pendingAction || actionFlight) {
      planReviewStage.failed = true;
      planReviewConfirm.disabled = true;
      planReviewStatus.textContent = '计划、模式或补充要求已更新，请关闭后重新核对；本次没有提交实施。';
    }
  }

  function submitPlanReview() {
    var stage = planReviewStage;
    if (!stage || stage.inflight || stage.failed || planReviewConfirm.disabled) return;
    if (planReviewKey(planReviewCandidate(snapshot)) !== stage.key || pendingCommand || pendingAction || actionFlight) {
      updatePlanReview(snapshot); return;
    }
    var candidate = stage.candidate;
    var action = { id: commandId('p'), kind: 'plan_implement', turn_id: candidate.turn_id,
      item_id: candidate.item_id, plan_digest: candidate.plan_digest, context_digest: candidate.context_digest };
    stage.inflight = true; planReviewConfirm.disabled = true;
    planReviewStatus.textContent = '正在提交本次确认…';
    postAction(action, '/conversation/' + encodeURIComponent(conversationId) + '/plan/implement', {
      command_id: action.id, turn_id: action.turn_id, item_id: action.item_id,
      plan_digest: action.plan_digest, context_digest: action.context_digest,
      skills: selectedSkills(), skills_explicit: '1'
    }).then(function (data) {
      stage.inflight = false;
      if (planReviewStage === stage) closePlanReview();
      var command = actionCommand(data);
      showToast(command && command.state === 'queued' ? '已排队交给原生目标继续' : '已核对计划和补充，正在实施');
    }).catch(function (error) {
      stage.failed = true;
      planReviewStatus.textContent = (error && error.message) || '确认结果待核对；不会自动重发。';
      showNote(planReviewStatus.textContent, true, 'goal');
    }).finally(function () { stage.inflight = false; });
  }

  function planReviewEntry(data) {
    var candidate = planReviewCandidate(data);
    if (!candidate || !['review', 'blocked'].includes(candidate.state)) return null;
    var card = node('section', 'native-plan-review-entry');
    card.append(node('strong', '', candidate.reason === 'plan_already_confirmed' ? '计划已确认' : '计划待实施'), node('p', '', candidate.state === 'review' ?
      '计划有 ' + (candidate.supplements || []).length + ' 条输入及补充需要核对，之后可直接进入实施。' : planReviewReason(candidate)));
    var actions = node('div', 'native-plan-actions');
    var review = node('button', '', candidate.state === 'review' ? '核对计划并实施' : '查看计划');
    review.type = 'button'; review.addEventListener('click', function () { openPlanReview(candidate); });
    var revise = node('button', 'native-plan-revise', '继续修改计划');
    revise.type = 'button'; revise.addEventListener('click', continuePlanning);
    actions.append(review, revise); card.appendChild(actions);
    return card;
  }

  function goalElapsedLabel(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    if (seconds < 60) return seconds + '秒';
    if (seconds < 3600) return Math.floor(seconds / 60) + '分' + (seconds % 60) + '秒';
    if (seconds < 86400) return Math.floor(seconds / 3600) + '小时' + Math.floor(seconds % 3600 / 60) + '分';
    return Math.floor(seconds / 86400) + '天' + Math.floor(seconds % 86400 / 3600) + '小时';
  }

  function goalTokenLabel(count) {
    var units = ['', 'K', 'M', 'B'];
    var index = 0;
    while (count >= 1000 && index < units.length - 1) { count /= 1000; index += 1; }
    count = Number(count.toFixed(index ? 2 : 0));
    // Promote rounded boundary values instead of displaying 1000K or 1000M.
    if (count >= 1000 && index < units.length - 1) { count /= 1000; index += 1; }
    return String(count) + units[index];
  }

  function goalUsageLabel(goal, exact) {
    var parts = [];
    function valid(value) { return value !== null && value !== undefined && Number.isFinite(Number(value)) && Number(value) >= 0; }
    if (goal && valid(goal.timeUsedSeconds)) {
      var seconds = Math.floor(Number(goal.timeUsedSeconds));
      parts.push('累计 ' + (exact ? seconds.toLocaleString() + '秒' : goalElapsedLabel(seconds)));
    }
    if (goal && valid(goal.tokensUsed)) {
      parts.push('已用 ' + (exact ? Number(goal.tokensUsed).toLocaleString() : goalTokenLabel(Number(goal.tokensUsed))) + ' tokens');
    }
    if (goal && valid(goal.tokenBudget)) {
      parts.push('预算 ' + (exact ? Number(goal.tokenBudget).toLocaleString() : goalTokenLabel(Number(goal.tokenBudget))) + ' tokens');
    }
    return parts.join(' · ');
  }

  function goalStatusCard(data) {
    var goal = goalValue(data);
    if (!goal || !goal.objective) return null;
    var sync = data.goal_sync_state || 'synced';
    var card = node('section', 'native-goal-status');
    card.dataset.goalStatus = goal.status || '';
    var summary = node('button', 'native-goal-summary'); summary.type = 'button';
    summary.setAttribute('aria-expanded', 'false');
    var summaryLabel = sync === 'draft' ? '目标草稿' :
      (sync === 'unknown' || sync === 'unavailable' ? '目标待核验' : '目标' + goalStatusLabel(goal.status, data));
    summary.append(node('strong', '', summaryLabel),
      node('span', 'native-goal-objective', goal.objective));
    var details = node('div', 'native-goal-details'); details.hidden = true;
    var state = sync === 'unknown' || sync === 'unavailable' ? '状态待核验，保留已知目标。' :
      sync === 'draft' ? '将在开始新对话后生效。' : goalStatusLabel(goal.status, data);
    details.appendChild(node('p', 'native-goal-state', state));
    var usage = goalUsageLabel(goal);
    var exactUsage = goalUsageLabel(goal, true);
    if (exactUsage) details.appendChild(node('p', 'native-goal-usage-exact', exactUsage));
    var actions = node('div', 'native-goal-actions');
    if (goal.status === 'active') {
      var pause = node('button', '', '暂停'); pause.type = 'button'; pause.dataset.goalQuick = 'pause'; actions.appendChild(pause);
    } else if (goal.status === 'paused') {
      var resume = node('button', '', '继续'); resume.type = 'button'; resume.dataset.goalQuick = 'resume'; actions.appendChild(resume);
    }
    var edit = node('button', '', '编辑'); edit.type = 'button'; edit.dataset.goalQuick = 'edit';
    var clear = node('button', 'danger', '清除'); clear.type = 'button'; clear.dataset.goalQuick = 'clear';
    if (optionSheet && optionContent && optionDone) actions.appendChild(edit);
    actions.appendChild(clear); details.appendChild(actions);
    summary.addEventListener('click', function () { details.hidden = !details.hidden; summary.setAttribute('aria-expanded', String(!details.hidden)); });
    actions.addEventListener('click', function (event) {
      var button = event.target.closest('[data-goal-quick]'); if (!button || button.disabled) return;
      var action = button.dataset.goalQuick;
      if (action === 'edit') { openOptionSheet('goal'); return; }
      if (action === 'clear' && !confirm('清除这个目标？不会撤销已完成的工作。')) return;
      button.disabled = true;
      quickGoalAction(action, goal).catch(function () {}).finally(function () { button.disabled = false; });
    });
    card.appendChild(summary);
    if (usage) card.appendChild(node('p', 'native-goal-usage', usage));
    card.appendChild(details);
    return card;
  }

  function appendPresentationEvent(fragment, presentation, conversation) {
    if (presentation.type === 'question') {
      fragment.appendChild(questionCard(presentation.value));
      return;
    }
    if (presentation.type === 'queue' || presentation.type === 'detached') {
      var command = presentation.command;
      var status = presentation.type === 'queue' ? '等待下一轮' :
        command.state === 'rejected' ? (command.reason === 'maintenance_quiescing' ? '维护中 · 未发送' : '未发送') : '投递待核对';
      var detachedBubble = messageBubble('user', command.text,
        ['你', timeLabel(command.created_at || command.updated_at), status].filter(Boolean).join(' · '), '',
        'command:' + command.id);
      var detachedCards = attachmentCards(command.attachments);
      if (detachedCards) detachedBubble.querySelector('.native-bubble').insertBefore(detachedCards, detachedBubble.querySelector('.native-message-footer'));
      fragment.appendChild(detachedBubble);
      return;
    }
    if (presentation.type === 'artifacts') {
      var artifacts = artifactSection(presentation.values,
        presentation.turn.status === 'inProgress' ? '刚生成' : '本轮产物',
        'artifacts:' + presentation.turn.id);
      if (artifacts) fragment.appendChild(artifacts);
      return;
    }
    if (presentation.type === 'artifact-error') {
      fragment.appendChild(messageBubble(
        'agent', '有附件发布失败；失败的文件未发送。请以本轮产物列表为准。',
        '工作台 · 部分未发送', 'native-error', 'artifact-error:' + presentation.turn.id
      ));
      return;
    }
    if (presentation.type === 'interrupted') {
      fragment.appendChild(interruptedMessage(presentation.turn));
      return;
    }
    if (presentation.type === 'error') {
      var errorBubble = messageBubble(
        'agent', friendlyTurnError(presentation.turn.error), 'Codex · 失败', 'native-error',
        'error:' + presentation.turn.id
      );
      errorBubble.dataset.turnId = presentation.turn.id;
      fragment.appendChild(errorBubble);
      return;
    }

    var turn = presentation.turn;
    var timeline = presentation.timeline;
    var event = presentation.event;
    var agentLabel = modelDisplay(conversation);
    if (event.type === 'command') {
      var turnCommand = event.value;
      var commandBubble = messageBubble(
        'user', turnCommand.text,
        commandMeta(turnCommand, turn, turnCommand.id === presentation.firstCommand), '',
        'command:' + turnCommand.id
      );
      var commandCards = attachmentCards(turnCommand.attachments);
      if (commandCards) commandBubble.querySelector('.native-bubble').insertBefore(commandCards, commandBubble.querySelector('.native-message-footer'));
      if (turnCommand.id === presentation.firstCommand) commandBubble.id = 'turn-' + turn.id;
      // Defensive target for an unusual terminal turn that has neither a
      // final item nor an explicit error/stop block.
      if (turnCommand.id === presentation.firstCommand && !timeline.hasResult &&
          turn.status !== 'interrupted' && !turn.error) {
        commandBubble.dataset.turnId = turn.id;
      }
      fragment.appendChild(commandBubble);
      return;
    }
    if (event.type === 'async-question') {
      var asyncCard = asyncQuestionCard(turn, event.value, agentLabel);
      if (asyncCard) fragment.appendChild(asyncCard);
      return;
    }
    if (event.type === 'async-message') {
      var asyncItem = event.value;
      fragment.appendChild(messageBubble(
        'agent', asyncItem.text,
        [agentLabel, timeLabel(asyncItem.created_at || asyncItem.updated_at), '异步消息']
          .filter(Boolean).join(' · '),
        'native-async-message',
        'async:' + turn.id + ':' + (asyncItem.native_item_id || asyncItem.revision || event.sequence),
        { menu: false }
      ));
      return;
    }
    var item = event.value;
    var time = timeLabel(item.created_at || item.updated_at);
    if (event.type === 'commentary') {
      if (presentation.groupedCommentary && presentation.firstCommentary) {
        fragment.appendChild(commentaryGroup(
          turn, presentation.commentaryEvents, presentation.collectCommentary
        ));
      }
      var commentary = messageBubble(
        'agent', item.text, [agentLabel, time].filter(Boolean).join(' · '),
        item === timeline.liveCommentary ? 'native-live-commentary' : '',
        'item:' + turn.id + ':' + (item.native_item_id || item.revision || event.sequence)
      );
      if (presentation.groupedCommentary) {
        commentary.dataset.commentaryGroupMember = turn.id;
        if (presentation.collectCommentary && readCommentaryGroupState(turn.id).expanded !== true) {
          commentary.classList.add('is-group-hidden');
        }
      }
      fragment.appendChild(commentary);
      return;
    }
    var duration = durationLabel(turn.started_at, turn.completed_at || item.updated_at);
    var meta = [agentLabel, time, '第 ' + turn.ordinal + ' 轮',
      duration ? '用时 ' + duration : ''].filter(Boolean).join(' · ');
    var finalBubble = messageBubble(
      'agent', item.text, meta, event.type === 'plan' ? 'native-plan' : 'native-final',
      event.type + ':' + turn.id + ':' + (item.native_item_id || 'fallback'),
      { final: true, plan: event.type === 'plan' }
    );
    // A notification deep link targets the durable turn ID, never a mutable
    // ordinal or generated DOM key.
    finalBubble.dataset.turnId = turn.id;
    if (item.native_item_id) finalBubble.dataset.itemId = item.native_item_id;
    if (turn.status !== 'completed' || conversation.active_turn_id === turn.id) {
      finalBubble.dataset.autoFoldLive = '1';
    }
    if (event.type === 'plan') {
      var planControl = planActions(snapshot || { conversation: conversation, turns: [] }, turn, item);
      if (planControl) finalBubble.querySelector('.native-bubble').appendChild(planControl);
    }
    fragment.appendChild(finalBubble);
  }

  function snapshotCommand(data, commandIdValue) {
    if (!commandIdValue) return null;
    var commands = (data.messages || []).concat(data.queue || []);
    (data.turns || []).forEach(function (turn) {
      commands = commands.concat(turn.commands || []);
    });
    return commands.find(function (command) { return command && command.id === commandIdValue; }) || null;
  }

  function actionResponse(data) {
    return data && data.snapshot ? data.snapshot : data;
  }

  function actionCommand(data) {
    return data && data.command || null;
  }

  function actionTerminal(state) {
    return ['accepted', 'queued', 'completed', 'rejected'].includes(String(state || '').toLowerCase());
  }

  function ownsActionDraft(action) {
    return action && action.draft_revision && action.draft_revision === storageGet(draftRevisionKey) &&
      textarea.value === action.composer_value;
  }

  function settleAction(action, command) {
    if (!action || !command || !actionTerminal(command.state)) return false;
    if (pendingAction && pendingAction.id === action.id) pendingAction = null;
    if (!pendingAction) { actionRecovery.hidden = true; actionRecovery.replaceChildren(); }
    var stored = null;
    try { stored = JSON.parse(storageGet(actionPendingKey) || 'null'); } catch (error) {}
    if (stored && stored.id === action.id) storageRemove(actionPendingKey);
    if (action.clear_composer && ['accepted', 'queued', 'completed'].includes(command.state) && ownsActionDraft(action)) {
      textarea.value = '';
      storageRemove(draftKey);
      textarea.dispatchEvent(new CustomEvent('input', { bubbles: true, detail: { storageAlreadyHandled: true } }));
    }
    return true;
  }

  function renderActionResult(data, action) {
    var next = actionResponse(data);
    if (next && next.conversation) render(next);
    var command = actionCommand(data);
    if (command && settleAction(action, command)) {
      // Action settlement itself changes presentation eligibility (notably the
      // one-shot Plan button), even when the timeline body is unchanged.
      lastPresentationSignature = null;
      if (snapshot) render(snapshot);
    }
    return command;
  }

  function commandReceipt(id) {
    // A missing command is not a missing conversation. Keep authentication
    // handling, but do not disable live sync on a receipt's 404.
    return timedHistoryRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/actions/' + encodeURIComponent(id)));
  }

  function actionReceipt(action) {
    if (!action || !action.id) return Promise.resolve(null);
    return commandReceipt(action.id)
      .then(function (data) {
        var command = renderActionResult(data, action);
        if (!command || !actionTerminal(command.state)) {
          showActionRecovery(action);
        }
        return data;
      }).catch(function () { showActionRecovery(action); return null; });
  }

  function showActionRecovery(action) {
    if (!pendingAction || !action || pendingAction.id !== action.id) return;
    actionRecovery.hidden = false;
    actionRecovery.replaceChildren(node('span', '', '操作是否送达仍待核对，输入已保留；不会自动重发。'));
    var check = node('button', 'native-action-check', '核对后解除等待'); check.type = 'button';
    check.addEventListener('click', function () {
      check.disabled = true;
      formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/reconcile'), {})
        .then(function (data) { if (data && data.conversation) render(data); return refreshGoal(); })
        .then(function () { return actionReceipt(action); })
        .then(function () {
          if (pendingAction && pendingAction.id === action.id) {
            pendingAction = null; storageRemove(actionPendingKey);
            actionRecovery.hidden = true; actionRecovery.replaceChildren();
            lastPresentationSignature = null; if (snapshot) render(snapshot);
          }
          showNote('已核对当前状态；先前投递仍以原收据为准，没有重发。', false, 'goal');
        }).catch(function (error) {
          showNote((error && error.message) || '核对未完成，仍保留等待状态。', true, 'goal');
        }).finally(function () { check.disabled = false; });
    });
    actionRecovery.appendChild(check);
  }

  function postAction(action, path, values) {
    if (actionFlight || !action || pendingAction && pendingAction.id !== action.id) {
      if (pendingAction) actionReceipt(pendingAction);
      return Promise.reject(new Error('上次操作尚待核对，没有发送本次操作。'));
    }
    pendingAction = action;
    if (!storageSet(actionPendingKey, JSON.stringify(action))) {
      pendingAction = null;
      showNote('浏览器无法保存操作收据，未发送；请启用浏览器存储后重试。', true, 'goal');
      return Promise.reject(new Error('无法保存操作收据'));
    }
    actionFlight = action.id;
    var sent = false;
    return Promise.resolve().then(function () {
      return action.kind === 'goal_start' || action.kind === 'plan_implement' ? saveSettings() : null;
    }).then(function () {
      sent = true;
      return formRequest(route(path), values);
    }).then(function (data) {
      var command = renderActionResult(data, action);
      if (!command || !actionTerminal(command.state) || command.state === 'rejected') {
        throw new Error('操作结果尚未确认，请核对；不会自动重发。');
      }
      return data;
    }).catch(function (error) {
      if (error && error.data) {
        try { renderActionResult(error.data, action); } catch (ignored) {}
      }
      // A failed transport is ambiguous. Verify the one durable command with GET;
      // callers must not mint another id or replay this POST.
      if (sent) actionReceipt(action);
      else { pendingAction = null; storageRemove(actionPendingKey); }
      throw error;
    }).finally(function () {
      if (actionFlight === action.id) actionFlight = null;
    });
  }

  function receiptState(data, commandIdValue) {
    var command = snapshotCommand(data, commandIdValue);
    var state = command && String(command.state || '').toLowerCase();
    return ['accepted', 'completed', 'queued', 'rejected'].includes(state) ? state : null;
  }

  function removeStoredPending(id) {
    var stored = null;
    try { stored = JSON.parse(storageGet(pendingKey) || 'null'); } catch (error) {}
    if (stored && stored.id === id) storageRemove(pendingKey);
  }

  function ownsStoredDraft(pending) {
    return (pending.draft_revision || null) === storageGet(draftRevisionKey);
  }

  function removeOldDraft(value, pending) {
    if (ownsStoredDraft(pending) && storageGet(draftKey) === value) {
      storageRemove(draftKey);
      // Keep the revision as a tombstone until a real edit creates a new
      // one: another unedited tab still needs to recognize this receipt.
    }
  }

  function restoreRejectedDraft(value, pending) {
    var saved = storageGet(draftKey);
    if (ownsStoredDraft(pending) && (!saved || saved === value)) storageSet(draftKey, value);
  }

  function settlePendingReceipt(command, state) {
    if (!command || !state) return;
    receiptStates[command.id] = state;
    // Logical delivery is complete even when the original HTTP transport is
    // still hanging. Its captured flight cannot unlock a newer submission.
    if (sendFlight && sendFlight.id === command.id) sendFlight = null;
    removeStoredPending(command.id);
    if (!pendingCommand || pendingCommand.id !== command.id) return;
    var pending = pendingCommand;
    pendingCommand = null;
    lastPresentationSignature = null;
    var ownsOldComposerText = pendingTextInComposerId === pending.id &&
      pendingComposerRevision === composerInputRevision &&
      textarea.value === pendingComposerValue;
    if (state === 'rejected') {
      // A definitive rejection is not a delivery ambiguity. Keep the old
      // text editable, but do not restore it as a pending submission later.
      if (ownsOldComposerText) {
        restoreRejectedDraft(pendingComposerValue, pending);
      }
      lastOperation = '明确未发送';
      if (note.dataset.kind === 'send') {
        showNote(command.reason === 'maintenance_quiescing' ? '工作台正在准备维护，不接受新任务；输入已保留，恢复后请手动发送。' : '这条消息未送达，输入已保留，可修改后重新发送。', true, 'send');
      }
      return;
    }
    // The attachment capture has its own revision.  A receipt for an older
    // command must never remove files added after that command was sent.
    if (attachmentDraft && pending.attachment_capture) attachmentDraft.settle(pending.attachment_capture);
    if (ownsOldComposerText) {
      textarea.value = '';
      removeOldDraft(pendingComposerValue, pending);
      textarea.dispatchEvent(new CustomEvent('input', {
        bubbles: true, detail: { storageAlreadyHandled: true }
      }));
    }
    pendingTextInComposerId = null;
    pendingComposerRevision = null;
    pendingComposerValue = null;
    lastOperation = '已确认发送';
    if (note.dataset.kind === 'send') showNote('');
  }

  function recoverPendingReceipt() {
    if (!pendingCommand) return Promise.resolve(null);
    if (pendingReceiptFlight) return pendingReceiptFlight;
    var pending = pendingCommand;
    pendingReceiptFlight = commandReceipt(pending.id).then(function (data) {
      var next = actionResponse(data);
      // Like an older-history read, a receipt does not consume a queued WS
      // refresh. Its snapshot may predate that refresh's final event.
      if (next && next.conversation) render(next, true);
      var command = actionCommand(data);
      if (command && command.id === pending.id && actionTerminal(command.state)) {
        settlePendingReceipt(command, String(command.state).toLowerCase());
        if (snapshot) render(snapshot, true);
      }
      return data;
    }).catch(function () {
      // Unknown/failed reads preserve the original ID and draft. Only another
      // explicit refresh may retry this read; never replay the message POST.
      return null;
    }).finally(function () { pendingReceiptFlight = null; });
    return pendingReceiptFlight;
  }

  function settlePendingFromSnapshot(data) {
    if (!pendingCommand) return;
    var state = receiptState(data, pendingCommand.id);
    if (state) settlePendingReceipt(pendingCommand, state);
  }

  function internPresentationTurns(data) {
    var next = new Map();
    var turns = data.turns.map(function (turn) {
      var revision = typeof turn.presentation_revision === 'string' ? turn.presentation_revision : stableSignature(turn);
      var previous = presentationTurns.get(turn.id);
      var current = previous && previous.revision === revision ? previous.turn : turn;
      next.set(turn.id, { revision: revision, turn: current });
      turnPresentationRevisions.set(current, revision);
      return current;
    });
    presentationTurns = next;
    return Object.assign({}, data, { turns: turns });
  }

  function presentationSignature(data) {
    return JSON.stringify({
      state: data.conversation.state,
      active_turn_id: data.conversation.active_turn_id,
      model: modelDisplay(data.conversation),
      collaboration_mode: (data.conversation.settings || {}).collaboration_mode || 'default',
      plan_confirmation: data.plan_confirmation,
      turns: data.turns.map(function (turn) { return [turn.id, turnPresentationRevisions.get(turn)]; }),
      messages: data.messages || [],
      questions: data.questions || [],
      queue: data.queue,
      artifacts: data.artifacts,
      goal: data.goal,
      goal_draft: data.goal_draft,
      goal_sync_state: data.goal_sync_state,
      pending_action: pendingAction && pendingAction.id,
      pending: pendingCommand && { id: pendingCommand.id, text: pendingCommand.text, state: pendingMessageState }
    }, function (_key, value) {
      // Inline Jinja JSON sorts keys, whereas API JSON keeps insertion order.
      // Equal snapshots must not replace the timeline and erase a deep-link
      // highlight just because they arrived through different serializers.
      if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
      var ordered = Object.create(null);
      Object.keys(value).sort().forEach(function (key) { ordered[key] = value[key]; });
      return ordered;
    });
  }

  function stableSignature(value) {
    return JSON.stringify(value, function (_key, item) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return item;
      var ordered = Object.create(null);
      Object.keys(item).sort().forEach(function (key) { ordered[key] = item[key]; });
      return ordered;
    });
  }

  function presentationKey(event) {
    if (event.type === 'turn') {
      return 'turn:' + event.turn.id + ':' + event.event.type + ':' +
        (event.event.value.id || event.event.value.native_item_id || 'fallback');
    }
    return event.type + ':' + (event.turn ? event.turn.id :
      event.command ? event.command.id : event.value.id);
  }

  function presentationEventSignature(event, conversation) {
    if (event.type !== 'turn') return event.turn ? stableSignature([event.type, turnPresentationRevisions.get(event.turn), event.values]) : stableSignature(event);
    var turn = event.turn;
    var signatures = turnEventSignatures.get(turn);
    if (!signatures) { signatures = new Map(); turnEventSignatures.set(turn, signatures); }
    var key = presentationKey(event);
    var local = signatures.get(key);
    if (local === undefined) {
      local = stableSignature({
        event: event.event, turn: [turn.id, turn.ordinal, turn.status, turn.started_at,
          turn.completed_at, turn.error], firstCommand: event.firstCommand,
        grouped: event.groupedCommentary, collect: event.collectCommentary,
        firstCommentary: event.firstCommentary,
        commentary: event.firstCommentary && event.groupedCommentary ? event.commentaryEvents : null,
        liveCommentary: event.timeline.liveCommentary === event.event.value,
        hasResult: event.timeline.hasResult
      });
      signatures.set(key, local);
    }
    // Controls are independent of body identity: an unchanged historical Plan
    // can gain/lose its action when the live mode or pending action changes.
    return local + stableSignature([conversation.active_turn_id === turn.id,
      modelDisplay(conversation), event.event.type === 'plan' ? [snapshot.plan_confirmation,
        (conversation.settings || {}).collaboration_mode, pendingAction && pendingAction.id] : null]);
  }

  // Build only changed events. Cached nodes remain connected, preserving text
  // selections, native inputs, details state and already-enhanced Markdown.
  function reconcilePresentation(data, conversation) {
    var next = new Map();
    var desired = [];
    function keep(key, signature, build, force) {
      var previous = presentationNodes.get(key);
      var entry = previous && previous.signature === signature && !force ? previous : null;
      if (!entry) {
        var fragment = document.createDocumentFragment();
        build(fragment);
        entry = { signature: signature, nodes: Array.from(fragment.childNodes) };
      }
      next.set(key, entry);
      desired.push.apply(desired, entry.nodes);
    }
    var presentation = buildPresentationEvents(data);
    presentation.events.forEach(function (event) {
      keep(presentationKey(event), presentationEventSignature(event, conversation), function (fragment) {
        appendPresentationEvent(fragment, event, conversation);
      }, lastPresentationSignature === null && (event.type === 'question' ||
        (event.event && event.event.type === 'async-question')));
    });
    if (pendingCommand && !snapshotCommand(data, pendingCommand.id)) {
      keep('pending', stableSignature([pendingCommand, pendingMessageState]), function (fragment) {
        var deliveryLabel = pendingMessageState === 'sending' ? '发送中' :
          pendingMessageState === 'not_sent' ? '未发送' : '发送结果待核对';
        fragment.appendChild(messageBubble('user', pendingCommand.text, '你 · ' + deliveryLabel, '', 'pending:' + pendingCommand.id));
      });
    }
    keep('detached-artifacts', stableSignature(data.artifacts), function (fragment) {
      var detached = artifactSection(data.artifacts, '对话产物', 'artifacts:detached');
      if (detached) fragment.appendChild(detached);
    });
    if (!data.turns.length && !data.queue.length && !presentation.detachedMessages.length && !pendingCommand) {
      keep('empty', '', function (fragment) {
        var empty = node('section', 'native-empty');
        empty.append(node('h1', '', '开始新对话'), node('p', '', '直接输入你的任务；项目、上传与完成通知会继续保留在这段对话里。'));
        fragment.appendChild(empty);
      });
    }
    var cursor = messages.firstChild;
    desired.forEach(function (child) {
      if (child === cursor) cursor = cursor.nextSibling;
      else messages.insertBefore(child, cursor);
    });
    while (cursor) { var stale = cursor; cursor = cursor.nextSibling; stale.remove(); }
    presentationNodes = next;
    ['agent', 'user'].forEach(function (role) {
      var candidates = messages.querySelectorAll('.' + role + '[data-auto-fold-candidate]');
      var latest = candidates[candidates.length - 1];
      candidates.forEach(function (candidate) {
        if (candidate === latest) {
          if (candidate.dataset.autoFoldLatest !== '1') candidate.dataset.autoFoldLatest = '1';
        } else if (candidate.dataset.autoFoldLatest) delete candidate.dataset.autoFoldLatest;
      });
    });
    var goalAnchor = Array.from(messages.querySelectorAll('.native-message.agent:not(.is-group-hidden)')).pop();
    var decorationSignature = stableSignature([data.goal, data.goal_draft, data.goal_sync_state,
      data.plan_confirmation, data.conversation.state, (data.conversation.settings || {}).collaboration_mode, pendingAction && pendingAction.id]);
    if (liveDecoration.anchor !== goalAnchor || liveDecoration.signature !== decorationSignature) {
      var previousGoal = liveDecoration.nodes.find(function (child) { return child.classList.contains('native-goal-status'); });
      var expandedGoal = previousGoal && !previousGoal.querySelector('.native-goal-details').hidden;
      liveDecoration.nodes.forEach(function (child) { child.remove(); });
      var decorations = goalAnchor ? [planReviewEntry(data), goalStatusCard(data)].filter(Boolean) : [];
      decorations.forEach(function (child) {
        if (expandedGoal && child.classList.contains('native-goal-status')) {
          child.querySelector('.native-goal-details').hidden = false;
          child.querySelector('.native-goal-summary').setAttribute('aria-expanded', 'true');
        }
        goalAnchor.appendChild(child);
      });
      liveDecoration = { anchor: goalAnchor, signature: decorationSignature, nodes: decorations };
    }
  }

  function nearBottom() {
    return window.innerHeight + window.scrollY >= document.body.scrollHeight - 150;
  }

  function scrollToConversationBottom() {
    window.scrollTo(0, document.body.scrollHeight);
  }

  function keepInitialBottom() {
    if (!initialBottomUntil || initialBottomCancelled || requestedTurnId || requestedCommandId || requestedItemId ||
        Date.now() > initialBottomUntil) return;
    requestAnimationFrame(function () {
      if (!initialBottomCancelled && !requestedTurnId && !requestedCommandId && !requestedItemId && Date.now() <= initialBottomUntil) {
        scrollToConversationBottom();
      }
    });
  }

  // The optional renderer can arrive after the core script (including its
  // retry path) and web fonts may alter line wrapping.  During the short
  // initial placement window keep following the tail, but never override a
  // notification target or a person's deliberate scroll.
  window.addEventListener('codex:conversation-messages-enhanced', keepInitialBottom);
  function keepSearchFocus() {
    if (!searchFocusKey || searchFocusCancelled || Date.now() > searchFocusUntil) return;
    requestAnimationFrame(function () {
      if (searchFocusCancelled || Date.now() > searchFocusUntil) return;
      var target = Array.from(messages.querySelectorAll('[data-message-key]')).find(function (item) {
        return item.dataset.messageKey === searchFocusKey;
      });
      if (target) target.scrollIntoView({block:'start',behavior:'instant'});
    });
  }
  window.addEventListener('codex:conversation-messages-enhanced', function () {
    if (pendingSearchRevealKey) {
      var key = pendingSearchRevealKey;
      pendingSearchRevealKey = null;
      // Enhancement can fold messages above the hit and restore their old
      // viewport anchor. Place the explicit hit again after that layout work.
      if (!searchFocusCancelled) searchFocusUntil = Date.now() + 800;
      messages.dispatchEvent(new CustomEvent('codex:open-message', {detail: {key: key}}));
    }
    keepSearchFocus();
  });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(keepInitialBottom, function () {});
    document.fonts.ready.then(keepSearchFocus, function () {});
  }

  function viewportAnchor() {
    var candidates = Array.from(messages.querySelectorAll('[data-message-key],[data-event-key]'));
    var headerBottom = document.querySelector('.native-conversation-head').getBoundingClientRect().bottom;
    var target = candidates.find(function (item) { return item.getBoundingClientRect().bottom >= headerBottom + 4; });
    if (!target) return null;
    return {
      key: target.dataset.messageKey || target.dataset.eventKey,
      message: Boolean(target.dataset.messageKey),
      offset: target.getBoundingClientRect().top
    };
  }

  function restoreAnchor(anchor, fallbackY) {
    if (!anchor) { window.scrollTo(0, fallbackY); return; }
    var values = Array.from(messages.querySelectorAll(anchor.message ? '[data-message-key]' : '[data-event-key]'));
    var target = values.find(function (item) {
      return (anchor.message ? item.dataset.messageKey : item.dataset.eventKey) === anchor.key;
    });
    if (!target) { window.scrollTo(0, fallbackY); return; }
    window.scrollBy(0, target.getBoundingClientRect().top - anchor.offset);
  }

  function renderContext(conversation) {
    var usage = conversation.usage;
    var valid = usage && Number.isFinite(Number(usage.context_percent)) &&
      Number(usage.context_limit) > 0 && Number(usage.context_used) >= 0;
    var percent = valid ? Math.max(0, Math.round(Number(usage.context_percent))) : null;
    contextButton.textContent = valid ? percent + '%' : '—%';
    contextButton.classList.toggle('context-high', valid && percent >= 80);
    contextButton.title = valid ? '上下文已用 ' + percent + '%' : '尚未收到 App Server 的真实上下文用量';
    document.getElementById('context-percent').textContent = valid ? percent + '%' : '—%';
    document.getElementById('context-summary').textContent = valid ?
      '已用 ' + tokenLabel(usage.context_used) + ' / ' + tokenLabel(usage.context_limit) :
      '等待 App Server 的真实用量';
    document.getElementById('context-bar').style.width = valid ? Math.min(100, percent) + '%' : '0%';
    document.getElementById('context-input').textContent = valid ? tokenLabel(usage.input_tokens) : '—';
    document.getElementById('context-cached').textContent = valid ? tokenLabel(usage.cached_input_tokens) : '—';
    document.getElementById('context-output').textContent = valid ? tokenLabel(usage.output_tokens) : '—';
    var count = Number(conversation.context_compaction_completed_count);
    if (!Number.isInteger(count) || count < 0) count = 0;
    var manualCount = Number(conversation.context_compaction_manual_completed_count);
    if (!Number.isInteger(manualCount) || manualCount < 0 || manualCount > count) manualCount = 0;
    var otherCount = Number(conversation.context_compaction_other_completed_count);
    if (!Number.isInteger(otherCount) || otherCount < 0 || manualCount + otherCount !== count) {
      otherCount = count - manualCount;
    }
    var switches = Array.isArray(conversation.context_switches) ?
      conversation.context_switches.filter(function (item) {
        return item && typeof item.observed_at === 'string' &&
          (item.state === 'call_seen' || item.state === 'output_seen');
      }) : [];
    var switchCount = Number(conversation.context_switch_count);
    if (!Number.isInteger(switchCount) || switchCount < 0) switchCount = switches.length;
    renderContextSwitches(switches, switchCount);
    var compactionState = conversation.context_compaction_state;
    if (compactionState !== 'busy' && compactionState !== 'failed') compactionState = 'idle';
    var card = document.getElementById('context-compaction-card');
    var compactButton = document.getElementById('compact-conversation');
    var compactionHint = document.getElementById('context-compaction-hint');
    card.className = 'native-context-summary-row' +
      (compactionState === 'idle' ? '' : ' ' + compactionState);
    document.getElementById('context-compaction-breakdown').textContent =
      '手动请求 ' + manualCount + ' 次 · 其他压缩 ' + otherCount + ' 次';
    document.getElementById('context-compaction-count').textContent = count > 0 ?
      '已记录 ' + count + ' 次' : '暂无记录';
    document.getElementById('context-compaction-count').classList.toggle('is-empty', count === 0);
    compactionHint.hidden = compactionState === 'idle';
    compactionHint.className = 'native-context-status ' + compactionState;
    compactionHint.querySelector('small').textContent = compactionState === 'busy' ?
      '正在等待 App Server 返回完成事件。' : '本次手动压缩未完成；可以稍后重新尝试。';
    compactButton.disabled = compactionState === 'busy';
    compactButton.textContent = compactionState === 'busy' ? '正在压缩…' :
      compactionState === 'failed' ? '重新压缩' : '压缩上下文';
  }

  function contextSwitchTime(value, detailed) {
    var date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '时间未知';
    var clock = new Intl.DateTimeFormat('zh-CN', {
      hour: '2-digit', minute: '2-digit', hour12: false
    }).format(date);
    if (detailed) return clock;
    var now = new Date();
    return date.getFullYear() === now.getFullYear() &&
      date.getMonth() === now.getMonth() && date.getDate() === now.getDate() ?
      '今天 ' + clock : new Intl.DateTimeFormat('zh-CN', {
        month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false
      }).format(date);
  }

  function renderContextSwitches(switches, count) {
    var latest = document.getElementById('context-switch-latest');
    document.getElementById('context-switch-count').textContent = count ?
      '已记录 ' + count + ' 次' : '暂无记录';
    document.getElementById('context-switch-count').classList.toggle('is-empty', count === 0);
    latest.textContent = count && switches.length ?
      '最近：' + contextSwitchTime(switches[0].observed_at, false) :
      '自工作台开始记录后统计';
    contextSwitchEvents.replaceChildren();
    if (!count) {
      contextSwitchEvents.append(node('li', 'native-context-switch-empty', '暂无记录'));
      return;
    }
    switches.forEach(function (item) {
      var row = document.createElement('li');
      var time = document.createElement('time');
      time.textContent = contextSwitchTime(item.observed_at, true);
      var body = document.createElement('div');
      var title = document.createElement('b');
      var note = document.createElement('small');
      if (item.state === 'output_seen') {
        title.textContent = '已收到返回';
        note.textContent = '换窗工具已返回';
      } else {
        title.textContent = '已检测到调用';
        note.textContent = '等待工具返回';
      }
      body.append(title, note);
      row.append(time, body);
      contextSwitchEvents.append(row);
    });
  }

  function activeTurn(data) {
    var id = data && data.conversation && data.conversation.active_turn_id;
    return id ? (data.turns || []).find(function (turn) { return turn.id === id; }) : null;
  }

  function recentActivityLabel(value) {
    var observed = timestamp(value);
    if (!observed) return '时间未知';
    var seconds = Math.max(0, Math.floor((Date.now() - observed) / 1000));
    if (seconds < 8) return '刚刚';
    if (seconds < 60) return seconds + ' 秒前';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
    return timeLabel(value);
  }

  function socketConfirmed() {
    return socketConnected() && socketLastConfirmedAt > 0 &&
      Date.now() - socketLastConfirmedAt <= socketStaleAfter;
  }

  function updateServerActivity() {
    if (automaticRequestsStopped || !snapshot ||
        !activeStates.has(snapshot.conversation.state)) {
      serverActivityNode.remove();
      return;
    }
    var connection = socketConfirmed() ? '实时连接正常' : '正在确认实时连接';
    serverActivityNode.textContent = '仍在执行 · ' + connection + ' · 最近活动 ' +
      recentActivityLabel(snapshot.conversation.updated_at);
    serverActivityNode.title = '只表示工作台收到新的服务端事件，不展示内部推理或命令内容。';
    if (serverActivityNode.parentNode !== messages) messages.appendChild(serverActivityNode);
  }

  function updateHeaderState() {
    if (!snapshot) return;
    var conversation = snapshot.conversation;
    var label = friendlyState(conversation.state);
    if (activeStates.has(conversation.state)) {
      var turn = activeTurn(snapshot);
      var started = timestamp(turn && turn.started_at);
      if (started) label = elapsedLabel((Date.now() - started) / 1000);
    } else if (conversation.state === 'idle') {
      var outcome = terminalOutcome(conversation);
      label = outcome === 'failed' ? '执行失败' :
        outcome === 'interrupted' ? '已停止' :
        snapshot.turns.length ? '本轮结束' : '新对话';
    }
    if (snapshot.stop && snapshot.stop.status === 'unconfirmed') label = '停止结果待核对';
    else if (snapshot.stop && snapshot.stop.status === 'verified' && conversation.state === 'idle') label = '代理已停止';
    stateNode.textContent = label;
    stateNode.title = friendlyConversationState(conversation) + (activeStates.has(conversation.state) ? ' · 已运行 ' + label : '');
    stateNode.className = 'native-head-control native-state state-' + conversation.state +
      (terminalOutcome(conversation) === 'failed' ? ' outcome-failed' :
       terminalOutcome(conversation) === 'interrupted' ? ' outcome-interrupted' : '');
    updateServerActivity();
  }

  function validTurnId(turnId) {
    return Boolean(turnId && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(turnId));
  }

  function focusTurn(turnId) {
    if (!validTurnId(turnId)) return false;
    // Comparing dataset values avoids relying on CSS.escape for IDs that may
    // legally contain colons or dots.
    var target = Array.from(messages.querySelectorAll('[data-turn-id]')).find(function (item) {
      return item.dataset.turnId === turnId;
    });
    if (!target) return false;
    initialBottomCancelled = true;
    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    target.classList.add('native-turn-highlight');
    setTimeout(function () { target.classList.remove('native-turn-highlight'); }, 2200);
    return true;
  }

  function scheduleRequestedTurnFocus() {
    if ((!requestedTurnId && !requestedCommandId && !requestedItemId) || turnFocusScheduled) return;
    turnFocusScheduled = true;
    requestAnimationFrame(function () {
      turnFocusScheduled = false;
      var exact = requestedCommandId || requestedItemId;
      var target = exact && Array.from(messages.querySelectorAll('[data-command-id],[data-item-id]')).find(function (item) {
        return requestedCommandId ? item.dataset.commandId === requestedCommandId :
          item.dataset.itemId === requestedItemId && item.dataset.turnId === requestedTurnId;
      });
      if (target) {
        initialBottomCancelled = true;
        var saved = readMessageState(target.dataset.messageKey);
        saved.folded = false; saved.autoFoldExpanded = true;
        writeMessageState(target.dataset.messageKey, saved);
        target.classList.remove('is-folded', 'is-auto-folded');
        searchFocusKey = target.dataset.messageKey;
        searchFocusUntil = Date.now() + 1800;
        var foldAction = target.querySelector('[data-action="fold"]');
        if (foldAction) foldAction.textContent = '折叠消息';
        if (window.codexConversationModules && window.codexConversationModules.markdown === 'ready') {
          messages.dispatchEvent(new CustomEvent('codex:open-message', {
            detail: {key: target.dataset.messageKey}
          }));
        } else {
          pendingSearchRevealKey = target.dataset.messageKey;
        }
        target.classList.add('native-turn-highlight');
        target.scrollIntoView({block: 'start', behavior: 'instant'});
        setTimeout(function () { target.classList.remove('native-turn-highlight'); }, 2200);
        requestedTurnId = requestedCommandId = requestedItemId = null;
      } else if (historyPaged && !historyFull && (!focusTurn(requestedTurnId) || exact)) {
        fullHistory('正在加载完整历史以定位这条消息…').catch(function (error) { showNote(error.message || '历史定位失败，请重试。', true, 'sync'); });
      } else if (focusTurn(requestedTurnId)) {
        if (exact) showToast('命中消息已更新，已定位到所在轮次');
        requestedTurnId = requestedCommandId = requestedItemId = null;
      } else if (exact && snapshot) {
        showToast('命中消息暂不可见，请同步后再试');
        requestedTurnId = requestedCommandId = requestedItemId = null;
      }
    });
  }

  function renderNotification(enabled) {
    notification.checked = Boolean(enabled);
    notificationButton.classList.toggle('is-on', notification.checked);
    notificationButton.setAttribute('aria-pressed', String(notification.checked));
    notificationButton.title = notification.checked ? '完成通知：已开启' : '完成通知：已关闭';
  }

  function renderFast() {
    var fast = Boolean(tier.value);
    tierButton.textContent = fast ? '⚡Fast' : '标准';
    tierButton.classList.toggle('is-fast', fast);
    tierButton.title = '当前速度：' + (fast ? 'Fast' : 'Standard') + '；点击切换';
  }

  function applyConversationSettings(conversation, force) {
    var values = conversation.settings || {};
    if (force || (!settingsDirty && !settingsSaving)) {
      if (values.model && Array.from(model.options).some(function (option) { return option.value === values.model; })) model.value = values.model;
      effort.dataset.current = values.effort || '';
      tier.dataset.current = values.service_tier || '';
      if (collaborationMode) collaborationMode.value = values.collaboration_mode || 'default';
      if (values.permission) permission.value = values.permission;
      updateModelOptions(true);
    }
    renderNotification(conversation.completion_notification);
    renderFast();
  }

  function compactControl(kind) {
    return composer.querySelector('[data-conversation-control="' + kind + '"]');
  }

  function goalValue(data) {
    return data && (data.goal || data.goal_draft) || null;
  }

  function goalStatusLabel(status, data) {
    if (status === 'active' && data && data.conversation && data.conversation.state === 'idle') {
      return data.conversation.settings && data.conversation.settings.collaboration_mode === 'plan' ?
        '待继续（Plan）' : '待继续';
    }
    return { active: '进行中', paused: '已暂停', blocked: '受阻', usageLimited: '用量受限',
      budgetLimited: '预算受限', complete: '已完成' }[status] || '目标待确认';
  }

  function renderCompactControls(data) {
    var permissionButton = compactControl('permission');
    var modeButton = compactControl('mode');
    var goalButton = compactControl('goal');
    if (permissionButton) permissionButton.textContent = {
      'read-only': '只读', 'workspace-write': '可写', 'danger-full-access': '完全访问'
    }[permission.value] || '权限';
    if (modeButton) modeButton.textContent = collaborationMode.value === 'plan' ? 'Plan' : '默认执行';
    if (!goalButton) return;
    var state = data && data.goal_sync_state;
    var goal = goalValue(data);
    if (state === 'unavailable') goalButton.textContent = '目标不可用';
    else if (state === 'unknown' || (!state && !Object.prototype.hasOwnProperty.call(data || {}, 'goal'))) goalButton.textContent = '目标待确认';
    else if (state === 'draft') goalButton.textContent = '目标待开始';
    else if (goal && goal.objective) goalButton.textContent = '目标' + goalStatusLabel(goal.status, data);
    else goalButton.textContent = '设置目标';
    goalButton.title = goal && goal.objective ? goal.objective : goalButton.textContent;
  }

  var optionSheet = document.getElementById('conversation-option-sheet');
  var optionTitle = document.getElementById('conversation-option-title');
  var optionContent = document.getElementById('conversation-option-content');
  var optionError = document.getElementById('conversation-option-error');
  var optionDone = document.getElementById('conversation-option-done');
  var stagedOption = null;
  var goalRecovery = null;

  function closeOptionSheet() {
    if (stagedOption && stagedOption.inflight) return;
    stagedOption = null; if (optionSheet.open) optionSheet.close();
  }
  function outsideDialog(dialog, event) {
    var rect = dialog.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom;
  }

  function appendChoice(box, select, item) {
    var row = node('label', 'compact-option-choice');
    var input = document.createElement('input');
    input.type = 'radio'; input.name = 'conversation-option-choice'; input.value = item.value;
    input.checked = item.value === select.value;
    row.append(input, node('span', '', item.textContent)); box.appendChild(row);
  }

  function appendGoalStatus(box, status, text) {
    var button = node('button', '', text); button.type = 'button'; button.dataset.goalStatus = status;
    button.setAttribute('aria-pressed', String(stagedOption.goal.status === status)); box.appendChild(button);
  }

  function openOptionSheet(kind) {
    if (!optionSheet || !optionContent || !optionDone) {
      showNote('这个页面暂不支持目标编辑面板；可使用 /goal 新目标，或刷新页面。', true, 'goal');
      return;
    }
    stagedOption = { kind: kind };
    optionDone.disabled = false;
    optionError.hidden = true; optionError.textContent = '';
    optionTitle.textContent = { permission: '执行权限', mode: '执行模式', goal: '目标' }[kind];
    optionContent.replaceChildren();
    if (kind === 'goal') {
      var goal = goalValue(snapshot);
      var draftGoal = Boolean(snapshot && !snapshot.goal && snapshot.goal_draft);
      var recovered = goalRecovery; goalRecovery = null;
      stagedOption.goal = { objective: recovered && recovered.objective || goal && goal.objective || '', originalObjective: goal && goal.objective || '',
        status: recovered && recovered.status || goal && goal.status || 'active', version: goal && (goal.version || goal.updatedAt || goal.revision) || '',
        exists: Boolean(goal), originalStatus: goal && goal.status, draft: draftGoal, clear: false };
      var input = document.createElement('textarea'); input.id = 'conversation-goal-draft'; input.className = 'compact-goal-input';
      input.maxLength = 4000; input.placeholder = '希望完成什么？'; input.value = stagedOption.goal.objective; optionContent.appendChild(input);
      var statuses = node('div', 'compact-status-options'); appendGoalStatus(statuses, 'active', '进行中'); appendGoalStatus(statuses, 'paused', '暂停后续');
      if (stagedOption.goal.exists && !draftGoal) appendGoalStatus(statuses, 'complete', '标记完成');
      optionContent.appendChild(statuses);
      if (stagedOption.goal.exists) { var clear = node('button', 'compact-goal-clear', '清除目标'); clear.type = 'button'; clear.dataset.clearGoal = 'true'; optionContent.appendChild(clear); }
      var meta = node('p', 'compact-goal-meta');
      if (snapshot.goal_sync_state === 'draft') meta.textContent = '将在开始这段新对话后生效。';
      else if (stagedOption.goal.status === 'paused') meta.textContent = '暂停只影响后续；当前轮继续，停止请用原停止按钮。';
      else if (snapshot.goal_sync_state === 'unknown') meta.textContent = '目标状态尚待确认；不会假定为无目标。';
      optionContent.appendChild(meta);
    } else {
      var select = { permission: permission, mode: collaborationMode }[kind]; var choices = node('div', 'compact-option-choices');
      Array.from(select.options).forEach(function (item) { appendChoice(choices, select, item); }); optionContent.appendChild(choices);
    }
    optionSheet.showModal(); requestAnimationFrame(function () { var focus = optionContent.querySelector('textarea, input:checked'); if (focus) focus.focus(); });
  }

  function goalSnapshot(data) { return data && data.snapshot ? data.snapshot : data; }
  function refreshGoal() {
    return jsonRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/goal')).then(function (data) {
      var next = goalSnapshot(data); if (next && next.conversation) render(next); return data;
    });
  }

  function currentGoalVersion(goal) {
    return String(goal && (goal.version || goal.revision || goal.updatedAt) || '');
  }

  function goalActionRecord(kind, objective, clearComposer) {
    return { id: commandId('g'), kind: kind, objective: objective || '',
      draft_revision: clearComposer ? storageGet(draftRevisionKey) : null,
      composer_value: clearComposer ? textarea.value : null,
      clear_composer: Boolean(clearComposer) };
  }

  function quickGoalAction(action, goal) {
    if (action === 'edit') return Promise.resolve();
    var startsDraft = action === 'resume' && snapshot && !snapshot.goal && snapshot.goal_draft;
    var record = goalActionRecord(startsDraft ? 'goal_start' : 'goal_' + action, startsDraft ? goal.objective : '');
    var path = '/conversation/' + encodeURIComponent(conversationId) + (startsDraft ? '/goal/start' : '/goal');
    var values = { command_id: record.id, goal_version: currentGoalVersion(goal) };
    if (startsDraft) { values.objective = goal.objective; values.skills = selectedSkills(); values.skills_explicit = '1'; }
    else values.action = action;
    return postAction(record, path, values).then(function (data) {
      var command = actionCommand(data);
      showToast(action === 'clear' ? '目标已清除' : action === 'pause' ? '已暂停后续目标' : '已继续目标');
      return command;
    }).catch(function (error) {
      showNote((error && error.message) || '目标操作结果待核对；不会自动重发。', true, 'goal');
      throw error;
    });
  }

  function saveGoal() {
    var draft = optionContent.querySelector('#conversation-goal-draft');
    var objective = draft && draft.value.trim(); var goal = stagedOption.goal;
    if (!goal.clear && !objective) { optionError.textContent = '请输入目标'; optionError.hidden = false; draft.focus(); return; }
    var action = goal.clear ? 'clear' : 'set';
    if (!goal.clear && goal.exists && !goal.draft && objective === goal.originalObjective && goal.status === 'complete') action = 'complete';
    else if (!goal.clear && goal.exists && !goal.draft && objective === goal.originalObjective && goal.status === 'paused' && goal.originalStatus !== 'paused') action = 'pause';
    else if (!goal.clear && goal.exists && !goal.draft && objective === goal.originalObjective && goal.status === 'active' && goal.originalStatus === 'paused') action = 'resume';
    if (stagedOption.inflight || stagedOption.retryRequired || actionFlight) return;
    var activeStage = stagedOption;
    activeStage.inflight = true; optionDone.disabled = true;
    // A new active objective starts via the dedicated endpoint. It is one native
    // Goal operation (and, for a new Thread, one first message), never a set+turn pair.
    var startsGoal = action === 'set' && (!goal.exists || goal.draft) && goal.status === 'active';
    var record = goalActionRecord(startsGoal ? 'goal_start' : 'goal_' + action, objective);
    var path = startsGoal ? '/conversation/' + encodeURIComponent(conversationId) + '/goal/start' :
      '/conversation/' + encodeURIComponent(conversationId) + '/goal';
    var values = startsGoal ? {
      command_id: record.id, objective: objective, goal_version: currentGoalVersion(goal),
      skills: selectedSkills(), skills_explicit: '1'
    } : { command_id: record.id, action: action, goal_version: currentGoalVersion(goal) };
    if (action === 'set' && !startsGoal) { values.objective = objective; values.status = goal.status; }
    postAction(record, path, values).then(function (data) {
      if (stagedOption === activeStage) { stagedOption = null; if (optionSheet.open) optionSheet.close(); }
      showToast(startsGoal ? '目标已开始' : action === 'complete' ? '目标已标记完成' : action === 'clear' ? '目标已清除' : '目标已更新');
    }).catch(function (error) {
      if (stagedOption !== activeStage) return;
      activeStage.inflight = false; activeStage.retryRequired = true;
      goalRecovery = { objective: objective || '', status: goal.status || 'active' };
      optionError.textContent = (error.message || '目标结果未确认。') + ' 请关闭后重新打开以核对。';
      optionError.hidden = false; showNote(optionError.textContent, true, 'goal');
    }).finally(function () {
      if (stagedOption === activeStage && !activeStage.retryRequired) { activeStage.inflight = false; optionDone.disabled = false; }
    });
  }

  if (optionSheet && optionContent && optionDone) {
  composer.querySelectorAll('[data-conversation-control]').forEach(function (button) {
    button.addEventListener('click', function () {
      if (stagedOption && stagedOption.inflight) return;
      var kind = button.dataset.conversationControl;
      if (kind !== 'goal') { openOptionSheet(kind); return; }
      button.disabled = true;
      refreshGoal().then(function () { openOptionSheet('goal'); }).catch(function (error) {
        showNote((error && error.message) || '目标状态读取失败，未打开编辑面板。', true, 'goal');
      }).finally(function () { button.disabled = false; });
    });
  });
  optionContent.addEventListener('click', function (event) {
    if (!stagedOption || stagedOption.kind !== 'goal') return;
    var status = event.target.closest('[data-goal-status]');
    if (status) { stagedOption.goal.status = status.dataset.goalStatus; stagedOption.goal.clear = false; var clearChoice = optionContent.querySelector('[data-clear-goal]'); if (clearChoice) clearChoice.textContent = '清除目标'; optionContent.querySelector('#conversation-goal-draft').disabled = false; optionContent.querySelectorAll('[data-goal-status]').forEach(function (button) { button.setAttribute('aria-pressed', String(button === status)); }); var meta = optionContent.querySelector('.compact-goal-meta'); if (meta) meta.textContent = status.dataset.goalStatus === 'paused' ? '暂停只影响后续；当前轮继续，停止请用原停止按钮。' : ''; return; }
    var clear = event.target.closest('[data-clear-goal]');
    if (clear) { stagedOption.goal.clear = !stagedOption.goal.clear; clear.textContent = stagedOption.goal.clear ? '已选择清除目标' : '清除目标'; var draft = optionContent.querySelector('#conversation-goal-draft'); if (draft) draft.disabled = stagedOption.goal.clear; }
  });
  optionDone.addEventListener('click', function () {
    if (!stagedOption) return;
    if (stagedOption.kind === 'goal') { saveGoal(); return; }
    var selected = optionContent.querySelector('input[name="conversation-option-choice"]:checked'); if (!selected) return;
    var select = { permission: permission, mode: collaborationMode }[stagedOption.kind]; select.value = selected.value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    var activeStage = stagedOption; activeStage.inflight = true; optionDone.disabled = true;
    saveSettings().then(function () { if (stagedOption === activeStage) { stagedOption = null; if (optionSheet.open) optionSheet.close(); } showToast('下一轮设置已保存'); }).catch(function (error) { if (stagedOption === activeStage) { optionError.textContent = error.message || '设置未保存。'; optionError.hidden = false; } }).finally(function () { if (stagedOption === activeStage) { activeStage.inflight = false; optionDone.disabled = false; } });
  });
  optionSheet.querySelector('[data-close-conversation-option]').addEventListener('click', closeOptionSheet);
  optionSheet.addEventListener('cancel', function (event) { if (stagedOption && stagedOption.inflight) event.preventDefault(); else stagedOption = null; });
  optionSheet.addEventListener('click', function (event) { if (event.target === optionSheet && outsideDialog(optionSheet, event)) closeOptionSheet(); });

  }

  function historyRange(token) {
    var parts = String(token || '').split(':');
    return { low: Number(parts[0]), high: Number(parts[1]), key: parts.slice(0, 2).join(':') };
  }

  function historyPage(data) {
    return { token: data.history.token, cursor: data.history.cursor, previousTurnId: data.history.previous_turn_id,
      turns: data.turns.filter(function (turn) { return data.history.page_turn_ids.includes(turn.id); }),
      questions: (data.questions || []).filter(function (question) { return data.history.page_turn_ids.includes(question.turn_id); }),
      messages: data.messages || [] };
  }

  function storedHistoryPages() {
    return Array.from(historyPages.values()).concat(historyTail ? [historyTail] : []);
  }

  function historyNextCursor() {
    var pages = storedHistoryPages().filter(function (page) { return page.token && page.turns.length; }).sort(function (left, right) {
      return historyRange(right.token).high - historyRange(left.token).high;
    });
    // Prefer any gap below the live window, then continue toward the beginning.
    // A long background interval can have more than 20 newly completed turns.
    for (var index = 0; index < pages.length; index += 1) {
      var page = pages[index];
      var low = historyRange(page.token).low;
      if (!page.cursor) return null;
      var covered = pages.some(function (other) {
        var range = historyRange(other.token);
        return page.previousTurnId ? other.turns.some(function (turn) { return turn.id === page.previousTurnId; }) :
          range.low < low && range.high >= low - 1;
      });
      if (!covered) return page.cursor;
    }
    return null;
  }

  function updateHistoryControls() {
    if (!historyBar) return;
    historyBar.hidden = !historyPaged && !historyFallback;
    historyOlder.hidden = historyFull || !historyNextCursor();
    historyOlder.style.display = historyOlder.hidden ? 'none' : '';
    historyOlder.disabled = historyLoading;
    historyOlder.textContent = historyLoading ? '正在加载…' : '加载更早历史';
    historyStatus.textContent = historyFallback || (historyFull ? '已加载完整历史' :
      historyNextCursor() ? '已加载 ' + (snapshot ? snapshot.turns.length : 0) + ' 轮；未加载历史可继续补齐' : '已加载全部历史');
  }

  function newerEntities(incoming, known) {
    var values = new Map((incoming || []).map(function (value) { return [value.id, value]; }));
    (known || []).forEach(function (value) { values.set(value.id, value); });
    return Array.from(values.values());
  }

  function mergeHistorySnapshot(data, older) {
    var stale = snapshot && Number(snapshot.conversation.revision) > Number(data.conversation.revision);
    if (historyFull && data.history && data.history.mode === 'paged' && snapshot) {
      if (stale) return snapshot;
      // A full read can win the race with an already-in-flight, newer view.
      // Retain the complete transcript while accepting its tail and controls.
      var complete = Object.assign({}, snapshot);
      (data.history.replacements || []).forEach(function (replacement) {
        ['turns', 'questions', 'messages'].forEach(function (key) {
          complete[key] = newerEntities(complete[key], replacement.snapshot[key]);
        });
      });
      return Object.assign({}, data, { history: undefined,
        turns: newerEntities(complete.turns, data.turns).sort(function (a, b) { return a.ordinal - b.ordinal; }),
        questions: newerEntities(complete.questions, data.questions),
        messages: newerEntities(complete.messages, data.messages) });
    }
    if (stale && data.history && !older) return snapshot;
    if (stale && !data.history) {
      // An explicit full read can finish after a newer tail/POST receipt.
      // Fill unseen history, never roll back the current controls or text.
      data = Object.assign({}, snapshot, { history: undefined,
        turns: newerEntities(data.turns, snapshot.turns).sort(function (a, b) { return a.ordinal - b.ordinal; }),
        questions: newerEntities(data.questions, snapshot.questions),
        messages: newerEntities(data.messages, snapshot.messages) });
    }
    if (stale && data.history) {
      var knownTurns = new Map(snapshot.turns.map(function (turn) { return [turn.id, turn]; }));
      var knownQuestions = new Map((snapshot.questions || []).map(function (question) { return [question.id, question]; }));
      var knownCommands = new Map((snapshot.messages || []).map(function (command) { return [command.id, command]; }));
      data = Object.assign({}, data, {
        turns: data.turns.map(function (turn) { return knownTurns.get(turn.id) || turn; }),
        questions: (data.questions || []).map(function (question) { return knownQuestions.get(question.id) || question; }),
        messages: (data.messages || []).map(function (command) { return knownCommands.get(command.id) || command; }),
        history: Object.assign({}, data.history, { replacements: [] })
      });
    }
    if (!data.history || data.history.mode !== 'paged') {
      if (!historyPaged || historyFull || !snapshot) return data;
      // Full POST receipts remain authoritative, but do not silently expand
      // the reader to every historical turn after an ordinary send/action.
      var byId = new Map(data.turns.map(function (turn) { return [turn.id, turn]; }));
      storedHistoryPages().forEach(function (page) {
        page.turns = page.turns.map(function (turn) { return byId.get(turn.id); }).filter(Boolean);
        page.questions = (data.questions || []).filter(function (question) {
          return page.turns.some(function (turn) { return turn.id === question.turn_id; });
        });
      });
      var visible = new Set(snapshot.turns.map(function (turn) { return turn.id; }));
      data.turns.slice(-20).forEach(function (turn) { visible.add(turn.id); });
      data = Object.assign({}, data, { turns: data.turns.filter(function (turn) {
        return visible.has(turn.id) || turn.id === data.conversation.active_turn_id;
      }) });
      return data;
    }
    historyPaged = true;
    var previousSnapshot = snapshot;
    (data.history.replacements || []).forEach(function (replacement) {
      var range = historyRange(replacement.previous);
      var existing = historyPages.get(range.key);
      if (!existing && historyTail && historyTail.token === replacement.previous) existing = historyTail;
      if (!existing) return;
      existing.token = replacement.token;
      existing.cursor = replacement.cursor;
      existing.previousTurnId = replacement.previous_turn_id;
      existing.turns = replacement.snapshot.turns;
      existing.questions = (replacement.snapshot.questions || []).filter(function (question) {
        return existing.turns.some(function (turn) { return turn.id === question.turn_id; });
      });
      existing.messages = replacement.snapshot.messages || [];
    });
    var current = historyPage(data);
    if (older) {
      if (current.token) historyPages.set(historyRange(current.token).key, current);
    } else {
      if (historyTail && historyTail.token && historyTail.turns.some(function (turn) {
        return !current.turns.some(function (value) { return value.id === turn.id; }) &&
          !Array.from(historyPages.values()).some(function (page) {
            return page.turns.some(function (value) { return value.id === turn.id; });
          });
      })) historyPages.set(historyRange(historyTail.token).key, historyTail);
      historyTail = current;
      // Exact/fully covered page ranges need only one validation token.
      if (current.token) historyPages.forEach(function (page, key) {
        var range = historyRange(page.token), tail = historyRange(current.token);
        if (range.low >= tail.low && range.high <= tail.high) historyPages.delete(key);
      });
    }
    var turns = new Map(), questions = new Map(), commands = new Map();
    storedHistoryPages().forEach(function (page) {
      page.turns.forEach(function (turn) { turns.set(turn.id, turn); });
      page.questions.forEach(function (question) { questions.set(question.id, question); });
      page.messages.forEach(function (command) { commands.set(command.id, command); });
    });
    // The most recent response wins overlapping history and live questions.
    data.turns.forEach(function (turn) { turns.set(turn.id, turn); });
    (data.questions || []).forEach(function (question) { questions.set(question.id, question); });
    (data.messages || []).forEach(function (command) { commands.set(command.id, command); });
    if (stale) {
      previousSnapshot.turns.forEach(function (turn) { turns.set(turn.id, turn); });
      (previousSnapshot.questions || []).forEach(function (question) { questions.set(question.id, question); });
      (previousSnapshot.messages || []).forEach(function (command) { commands.set(command.id, command); });
    }
    var authoritative = previousSnapshot && Number(previousSnapshot.conversation.revision) > Number(data.conversation.revision) ? previousSnapshot : data;
    return Object.assign({}, authoritative, {
      turns: Array.from(turns.values()).sort(function (left, right) { return left.ordinal - right.ordinal; }),
      questions: Array.from(questions.values()), messages: Array.from(commands.values())
    });
  }

  function fullHistory(reason) {
    if (fullHistoryFlight) return fullHistoryFlight;
    historyLoading = true;
    historyFallback = reason || '正在加载完整历史…';
    updateHistoryControls();
    fullHistoryFlight = timedHistoryRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/snapshot?text_previews=true')).then(function (data) {
      validateSnapshot(data);
      historyFull = true;
      historyPages.clear(); historyTail = null;
      historyFallback = reason ? '已切换到完整历史读取；不会重发消息或核对原生执行。' : '已加载完整历史';
      render(data, true);
      updateSearch(false);
      return data;
    }).catch(function (error) {
      historyFallback = (error.message || '完整历史未能加载') + ' 已加载记录仍保留，请重试。';
      if (searchDialog.open) searchStatus.textContent = '完整历史未加载；当前仅查已加载记录，关闭后重新打开查找可重试。';
      throw error;
    }).finally(function () { historyLoading = false; fullHistoryFlight = null; updateHistoryControls(); });
    return fullHistoryFlight;
  }

  function historyRequest(url, options) {
    options = Object.assign({ credentials: 'same-origin', redirect: 'follow',
      headers: { Accept: 'application/json' } }, options || {});
    return fetch(url, options).then(function (response) {
      return Promise.resolve().then(function () { return jsonResponse(response); }).catch(function (error) {
        // A missing/old paging endpoint is a compatibility failure, not a
        // missing conversation. Only actual auth failures stop all requests.
        if (!(options.signal && options.signal.aborted) && (response.redirected || [401, 403].includes(error.status))) {
          error.historyAuth = true;
          stopAutomaticRequests(error);
        }
        throw error;
      });
    });
  }

  function timedHistoryRequest(url) {
    var controller = new AbortController();
    var timedOut = false;
    var timer = setTimeout(function () { timedOut = true; controller.abort(); }, snapshotSyncTimeoutMs);
    return historyRequest(url, { cache: 'no-store', signal: controller.signal }).catch(function (error) {
      if (timedOut) throw new Error('读取历史等待超过15秒，已停止旧请求；可以重试。');
      throw transportError(error);
    }).finally(function () { clearTimeout(timer); });
  }

  function browserViewRequest(options) {
    if (historyFull || !historyPaged) return jsonRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/snapshot?text_previews=true'), options);
    var tokens = Array.from(new Set(storedHistoryPages().map(function (page) { return page.token; }).filter(Boolean)));
    var path = '/conversation/' + encodeURIComponent(conversationId) + '/view?text_previews=true';
    if (tokens.length) path += '&pages=' + encodeURIComponent(tokens.join(','));
    return historyRequest(route(path), options).then(function (data) {
      if (!data || !data.history || data.history.mode !== 'paged' || !Array.isArray(data.history.page_turn_ids) || !Array.isArray(data.turns) ||
          !Array.isArray(data.queue) || !Array.isArray(data.artifacts)) throw new Error('分段历史格式不完整');
      return data;
    }).catch(function (error) {
      // Authentication and explicit abort retain the existing request policy;
      // unsupported/invalid paging falls back to the read-only full endpoint.
      if (error.name === 'AbortError' || error.historyAuth || automaticRequestsStopped ||
          (options.signal && options.signal.aborted)) throw error;
      historyFallback = '分段读取不可用，正在切换完整历史…';
      updateHistoryControls();
      return jsonRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/snapshot?text_previews=true'), options)
        .then(function (data) {
          historyFull = true;
          historyPages.clear(); historyTail = null;
          historyFallback = '分段读取不可用，已切换完整历史；不会重发消息或核对原生执行。';
          return data;
        });
    });
  }

  if (historyOlder) historyOlder.addEventListener('click', function () {
    if (historyLoading || historyFull) return;
    var cursor = historyNextCursor();
    if (!cursor) return;
    historyLoading = true; initialBottomCancelled = true;
    updateHistoryControls();
    timedHistoryRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/view?text_previews=true&before=' + encodeURIComponent(cursor))).then(function (data) {
      if (!data || !data.history || data.history.mode !== 'paged' || !Array.isArray(data.history.page_turn_ids)) throw new Error('分段历史格式不完整');
      render(data, true);
    }).catch(function (error) {
      if (error.historyAuth || automaticRequestsStopped) throw error;
      return fullHistory('更早历史读取失败，正在切换完整历史…');
    }).catch(function (error) { showNote(error.message || '历史暂时无法读取，请重试。', true, 'sync'); })
      .finally(function () { historyLoading = false; updateHistoryControls(); });
  });

  function validateSnapshot(data) {
    if (!data || !data.conversation || !Array.isArray(data.turns) ||
        !Array.isArray(data.queue) || !Array.isArray(data.artifacts)) {
      var error = new Error('工作台返回的数据不完整；输入仍保留在下方。');
      error.permanent = true;
      stopAutomaticRequests(error);
      throw error;
    }
    return data;
  }

  function terminalGeneration(value) {
    var generation = Number(value);
    return Number.isFinite(generation) && generation > 0 ? generation : 0;
  }

  function dispatchTerminal(data) {
    data = data || {};
    var eventConversationId = String(data.conversation_id || '');
    if (!eventConversationId) return;
    var generation = terminalGeneration(data.generation);
    var previous = terminalEventGenerations[eventConversationId] || 0;
    if (generation && generation <= previous) return;
    if (generation) terminalEventGenerations[eventConversationId] = generation;
    window.dispatchEvent(new CustomEvent('codex:conversation-terminal', { detail: data }));
  }

  function sendReadReceipt(generation) {
    generation = terminalGeneration(generation);
    pendingReadGeneration = Math.max(pendingReadGeneration, generation);
    if (document.visibilityState !== 'visible' || readRequestRunning ||
        pendingReadGeneration <= markedReadGeneration) return;
    var target = pendingReadGeneration;
    var retry = true;
    readRequestRunning = true;
    fetch(route('/conversation/' + encodeURIComponent(conversationId) + '/read'), {
      method: 'POST', credentials: 'same-origin', redirect: 'follow',
      headers: { Accept: 'application/json' }
    }).then(function (response) {
      if (response.ok) {
        markedReadGeneration = Math.max(markedReadGeneration, target);
        retry = false;
      } else if (response.status < 500 && response.status !== 429) {
        // Do not hammer a permanently unauthenticated/unknown endpoint.  A
        // later authenticated page load writes the authoritative receipt.
        markedReadGeneration = Math.max(markedReadGeneration, target);
        retry = false;
      }
    }).catch(function () {}).finally(function () {
      readRequestRunning = false;
      if (retry && document.visibilityState === 'visible' &&
          pendingReadGeneration > markedReadGeneration) {
        setTimeout(function () { sendReadReceipt(pendingReadGeneration); }, 2500);
      }
    });
  }

  function render(data, older) {
    validateSnapshot(data);
    data = internPresentationTurns(mergeHistorySnapshot(data, older));
    var previousRevision = snapshot && Number(snapshot.conversation.revision) || 0;
    var initialRender = !snapshot;
    var wasNearBottom = !older && nearBottom();
    var oldScrollY = window.scrollY;
    var questionFocus = captureQuestionFocus();
    var anchor = viewportAnchor();
    snapshot = data;
    updateHistoryControls();
    updatePlanReview(data);
    if (attachmentDraft && typeof attachmentDraft.claim === 'function') attachmentDraft.claim(data);
    // Every validated snapshot is an authoritative receipt channel: inline
    // SSR, GET sync, websocket-triggered sync, reconcile, and POST all land
    // here. Never infer delivery from a completed conversation or an ID alone.
    settlePendingFromSnapshot(data);
    if (timestamp(data.server_now)) questionClockOffset = timestamp(data.server_now) - Date.now();
    var conversation = data.conversation;
    var generation = terminalGeneration(conversation.terminal_generation);
    if (initialRender) {
      terminalEventGenerations[conversationId] = generation;
      markedReadGeneration = generation;
      pendingReadGeneration = generation;
    } else if (generation > (terminalEventGenerations[conversationId] || 0)) {
      var terminalTurn = data.turns.find(function (turn) {
        return turn.id === conversation.last_terminal_turn_id;
      });
      dispatchTerminal({
        conversation_id: conversationId,
        title: conversation.title || '新对话',
        turn_id: conversation.last_terminal_turn_id,
        generation: generation,
        status: terminalTurn && terminalTurn.status,
        completed_at: conversation.last_terminal_at
      });
      sendReadReceipt(generation);
    }
    titleNode.textContent = conversation.title || 'Codex 对话';
    applyConversationSettings(conversation);
    renderCompactControls(data);
    renderContext(conversation);
    renderMessageLength();
    updateHeaderState();
    // Root idle does not prove that its agents or background terminals stopped.
    var treeStopAvailable = Boolean(data.capabilities && data.capabilities.conversation_tree_stop);
    stopButton.hidden = !conversation.thread_id || Boolean(conversation.native_delete_pending) ||
      (!treeStopAvailable && !conversation.active_turn_id);
    // Quiet styling reflects the root turn, not proof that every agent stopped.
    // Keep uncertain stop results prominent and preserve the idle tree action.
    stopButton.classList.toggle('is-idle', conversation.state === 'idle' &&
      !conversation.active_turn_id && !(data.stop && data.stop.status === 'unconfirmed'));
    stopButton.title = treeStopAvailable ? '停止本对话及子代理；不主动清理后台命令' : '仅停止当前轮；不含子代理及后台命令';
    stopButton.setAttribute('aria-label', treeStopAvailable ? '停止本对话及子代理' : '停止当前轮');
    if (data.stop && data.stop.status === 'unconfirmed') refreshButton.title = '只读核对停止结果；不会重发停止';
    sendButton.disabled = Boolean(sendFlight) || attachmentPreparation ||
      conversation.state === 'starting_thread' || conversation.state === 'starting_turn';
    refreshButton.classList.toggle('attention', conversation.state === 'needs_user');

    var signature = presentationSignature(data);
    var timelineChanged = signature !== lastPresentationSignature;
    if (timelineChanged) {
      // A sync can queue this frame while a navigation target is still
      // pending, then run it after another frame has consumed that target.
      // Its old near-bottom/anchor measurement must not undo the focus.
      var hadPendingNavigation = Boolean(requestedTurnId || requestedCommandId || requestedItemId);
      reconcilePresentation(data, conversation);
      restoreQuestionFocus(questionFocus);
      lastPresentationSignature = signature;
      updateSearch(false);
      requestAnimationFrame(function () {
        if (firstRender) {
          if (!requestedTurnId && !requestedCommandId && !requestedItemId && !initialBottomCancelled) {
            initialBottomUntil = Date.now() + 1800;
            scrollToConversationBottom();
          }
          firstRender = false;
        } else if (!hadPendingNavigation && !requestedTurnId && !requestedCommandId && !requestedItemId) {
          if (wasNearBottom) scrollToConversationBottom();
          else restoreAnchor(anchor, oldScrollY);
        }
        // Coalesce deep-link focus after all queued timeline/anchor frames.
        // An earlier focus must not be undone by a later initial-sync frame.
        scheduleRequestedTurnFocus();
      });
    }
    // Keep a notification target pending until an authoritative render has
    // actually inserted that turn. This also covers a damaged inline snapshot
    // that is recovered by the first asynchronous sync.
    if (!timelineChanged) scheduleRequestedTurnFocus();
    if (previousRevision && Number(conversation.revision) > previousRevision && !wasNearBottom) newContentButton.hidden = false;
    if (conversation.state === 'needs_user' || (data.stop && data.stop.status === 'unconfirmed')) {
      var recovery = conversation.recovery_state || '';
      var explanation = recovery === 'ambiguous_native_state' ?
        '原生历史中有多个未结束轮次，无法可靠定位当前轮；不是连接故障。可停止本对话及子代理，再接力任务。' :
        recovery === 'stale_native_turn_history' ?
        '原生主对话当前空闲，但历史轮次缺少结束记录；不会把历史当成正在执行。可停止本对话及子代理并核对子代理。' :
        recovery === 'active_native_turn_unidentified' ?
        '原生仍在执行，但当前轮次无法可靠定位；可停止本对话及子代理并核对结果。' :
        recovery.indexOf('tree_stop') === 0 || (data.stop && data.stop.status === 'unconfirmed') ?
        '代理停止尚未确认；可能仍有子代理或持续目标。请点刷新核对，必要时再次停止；不会自动重发。' :
        '连接或投递结果需要同步核对；不会自动重复发送。';
      showNote(explanation, true, 'state');
    } else if (data.stop && data.stop.status === 'verified' && (data.stop.terminals || data.stop.background_errors)) {
      showNote(data.stop.background_errors ?
        '本对话及子代理已停止；后台命令未能完整核对，未主动终止。交接时请记录仍在运行的命令。' :
        '本对话及子代理已停止；停止时仍有 ' + data.stop.terminals + ' 个原生后台命令，未主动终止。交接前请核对其副作用和结果路径。', false, 'state');
    } else if (terminalOutcome(conversation) === 'failed') {
      showNote('⚠ 本轮执行失败，未生成最终答复；可核对已完成操作后继续。', true, 'failure');
    } else if (note.dataset.kind === 'state') {
      showNote('');
    }
    // A timeline replacement removes the stable liveness row; restore it
    // without rebuilding or exposing any hidden App Server item.
    updateServerActivity();
    updateQuestionCountdowns();
    updateAsyncQuestionCountdowns();
    // A history-only read must not cancel a queued newer websocket sync.
    if (!older) schedulePoll((activeStates.has(conversation.state) || conversation.state === 'needs_user') && !socketConnected());
    updateSettingsSummary();
    window.dispatchEvent(new CustomEvent('codex:conversation-snapshot', {detail:data}));
  }

  window.addEventListener('codex:conversation-action-result', function (event) { render(event.detail); });

  function visibleDocument() {
    return document.visibilityState !== 'hidden';
  }

  function nextSyncNotBefore() {
    return Math.max(localRetryNotBefore, serverRetryNotBefore);
  }

  function retireSyncFlight() {
    if (!syncing) return;
    var previous = syncing;
    syncing = null;
    syncAgain = false;
    previous.intentionalAbort = true;
    clearTimeout(previous.timer);
    previous.controller.abort();
  }

  function sync(options) {
    var force = options === true || Boolean(options && options.force);
    var foreground = Boolean(options && options.foreground);
    if (force || foreground) {
      // An explicit refresh and foreground recovery are new observations.
      // They may immediately retry our own exponential delay, but never a
      // Retry-After deadline supplied by the server.
      pollBackoff = 4000;
      localRetryNotBefore = 0;
    }
    if (!visibleDocument() && !force) return Promise.resolve(snapshot);
    if (serverRetryNotBefore > Date.now()) {
      scheduleAutomaticSync();
      return Promise.resolve(snapshot);
    }
    if (force) {
      automaticRequestsStopped = false;
    }
    if (automaticRequestsStopped) return Promise.resolve(snapshot);
    if (syncing && !force && !foreground) {
      syncAgain = true;
      return syncing.promise;
    }
    if (syncing) {
      retireSyncFlight();
    }
    lastSyncStartedAt = Date.now();
    var flight = {
      controller: new AbortController(), intentionalAbort: false,
      timedOut: false, timer: null, promise: null
    };
    flight.timer = setTimeout(function () {
      if (syncing !== flight) return;
      flight.timedOut = true;
      flight.controller.abort();
    }, snapshotSyncTimeoutMs);
    flight.promise = browserViewRequest({ cache: 'no-store', signal: flight.controller.signal }).then(function (data) {
      if (syncing !== flight) return snapshot;
      pollBackoff = 4000;
      localRetryNotBefore = 0;
      serverRetryNotBefore = 0;
      render(data);
      // A successful explicit reconcile is evidence that this page is again
      // authenticated/authorized.  Recovery events alone never clear a halt.
      if (force && connectionRetry && connectionRetry.isHalted()) {
        connectionRetry.resume();
        connectSocket();
      }
      if (note.dataset.kind === 'sync' || note.dataset.kind === 'connection') showNote('');
      return data;
    }).catch(function (error) {
      if (flight.intentionalAbort || syncing !== flight) {
        var superseded = new Error('同步请求已由新的刷新替代。');
        superseded.superseded = true;
        throw superseded;
      }
      if (flight.timedOut) {
        error = new Error('同步等待超过15秒，已停止旧请求；请点 ↻ 立即重试。');
      }
      var retryAfterDelay = error.retryAfter ? error.retryAfter * 1000 : 0;
      var delay = retryAfterDelay || pollBackoff;
      if (retryAfterDelay) serverRetryNotBefore = Date.now() + retryAfterDelay;
      else {
        localRetryNotBefore = Date.now() + delay;
        pollBackoff = Math.min(Math.max(4000, delay * 2), 30000);
      }
      var historyHint = snapshot ? ' 已载入的历史记录会保留；点 ↻ 可重试。' : '';
      showNote((error.message || '暂时无法连接工作台。') + historyHint, true, 'sync');
      syncAgain = false;
      schedulePoll(!automaticRequestsStopped, delay);
      throw error;
    }).finally(function () {
      clearTimeout(flight.timer);
      if (syncing !== flight) return;
      syncing = null;
      if (syncAgain && !automaticRequestsStopped) {
        syncAgain = false;
        scheduleAutomaticSync();
      }
    });
    syncing = flight;
    return flight.promise;
  }

  function schedulePoll(enabled, delay) {
    clearTimeout(pollTimer);
    pollTimer = enabled && !automaticRequestsStopped && visibleDocument() ? setTimeout(function () {
      if (!automaticRequestsStopped && visibleDocument()) sync().catch(function () {});
    }, delay === undefined ? 4000 : delay) : null;
  }

  function scheduleAutomaticSync() {
    if (!visibleDocument()) return;
    if (syncing) { syncAgain = true; return; }
    var elapsed = Date.now() - lastSyncStartedAt;
    var retryWait = nextSyncNotBefore() - Date.now();
    // A known retry deadline is already the coalesced next read.  Do not add
    // a second automatic-sync interval after it (or let a one-millisecond
    // early timer turn into another full interval).
    schedulePoll(true, retryWait > 0 ? retryWait : Math.max(0, minimumAutomaticSyncInterval - elapsed));
  }

  function socketConnected() {
    return socket && socket.readyState === WebSocket.OPEN;
  }

  function diagnosticRows(options) {
    var fullConversationId = options && options.fullConversationId;
    var conversation = snapshot && snapshot.conversation || {};
    var connection = { connected: '正常', disconnected: '已断开', connecting: '正在连接' }[conversation.connection_state] || conversation.connection_state || '未知';
    return [
      ['所属项目', root.dataset.projectName || '临时对话'],
      ['页面状态', friendlyConversationState(conversation)],
      ['连接状态', connection],
      ['最近操作', lastOperation],
      ['对话编号', fullConversationId ? String(conversationId) : '…' + String(conversationId).slice(-6).toUpperCase()],
      ['需要核对', conversation.state === 'needs_user' || conversation.state === 'reconciling' || (snapshot && snapshot.stop && snapshot.stop.status === 'unconfirmed') ? '是' : '否'],
      ['停止时后台命令', snapshot && snapshot.stop && snapshot.stop.status === 'verified' ?
        (snapshot.stop.background_errors ? '未能完整核对；未主动终止' : String(snapshot.stop.terminals || 0) + ' 项；未主动终止') : '未核对'],
      ['待发送草稿', (textarea.value.trim() || pendingCommand) ? '有' : '无'],
      ['更新时间', new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })]
    ];
  }

  function renderDiagnostics() {
    var conversation = snapshot && snapshot.conversation || {};
    diagnosticsSummary.textContent = conversation.state === 'needs_user' ?
      '当前状态需要先同步核对，不会自动重复发送。' :
      terminalOutcome(conversation) === 'failed' ?
        '上一轮执行失败，未生成最终答复；可核对已完成操作后继续。' :
        '当前对话' + (conversation.state === 'idle' ? '已就绪，可以继续发送消息。' : '仍在处理中。');
    diagnosticsValues.replaceChildren();
    diagnosticRows().forEach(function (row) {
      diagnosticsValues.append(node('dt', '', row[0]), node('dd', '', row[1]));
    });
  }

  function diagnosticText() {
    return ['Codex 工作台排查信息'].concat(diagnosticRows({ fullConversationId: true }).map(function (row) {
      return row[0] + '：' + row[1];
    })).join('\n');
  }

  function exportFilename(response) {
    var disposition = response.headers.get('content-disposition') || '';
    var match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (!match) return 'conversation-export.md';
    try { return decodeURIComponent(match[1]); }
    catch (error) { return 'conversation-export.md'; }
  }

  function requestConversationExport() {
    return fetch(route('/conversation/' + encodeURIComponent(conversationId) + '/export'), {
      method: 'GET', credentials: 'same-origin', redirect: 'manual',
      headers: { Accept: 'text/markdown' }
    }).then(function (response) {
      var contentType = response.headers.get('content-type') || '';
      if (!response.ok || contentType.indexOf('text/markdown') !== 0) {
        throw new Error('导出失败，请确认登录状态后重试。');
      }
      return response.text().then(function (text) {
        return { text: text, filename: exportFilename(response) };
      });
    });
  }

  function setExportBusy(busy) {
    copyExportButton.disabled = busy;
    downloadExportButton.disabled = busy;
  }

  function hasExportContent() {
    return snapshot && ((snapshot.turns || []).length ||
      (snapshot.messages || []).length || (snapshot.queue || []).length);
  }

  function beginExport(action) {
    if (!hasExportContent()) {
      showNote('当前还没有可导出的提问和最终回答。', true, 'export');
      return;
    }
    setExportBusy(true);
    requestConversationExport().then(function (value) {
      if (action === 'copy') {
        return copyText(value.text).then(function () {
          showNote(''); showToast('完整对话已复制');
        });
      }
      var blob = new Blob([value.text], { type: 'text/markdown;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var link = document.createElement('a');
      link.href = url; link.download = value.filename;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      showNote(''); showToast('已交给浏览器下载；文件头包含所属项目');
    }).catch(function (error) {
      showNote((error && error.message) || '导出失败，请稍后重试。', true, 'export');
    }).finally(function () { setExportBusy(false); });
  }

  function selectedSkills() {
    return Array.from(composer.querySelectorAll('input[name="skills"]:checked')).map(function (item) { return item.value; });
  }

  function normalizedSettingValue(name, value) {
    if (name === 'service_tier') return value || null;
    if (name === 'collaboration_mode') return value || 'default';
    return value || '';
  }

  function changedSettings() {
    var values = snapshot && snapshot.conversation && snapshot.conversation.settings;
    if (!values) return {};
    var controls = { model: model, effort: effort, permission: permission, service_tier: tier, collaboration_mode: collaborationMode };
    var updates = {};
    Object.keys(controls).forEach(function (name) {
      var current = normalizedSettingValue(name, controls[name].value);
      var saved = normalizedSettingValue(name, values[name]);
      if (current !== saved) {
        // FormData omits null values. Keep the empty select value so the
        // server can distinguish switching back to Standard from no update.
        updates[name] = name === 'service_tier' ? controls[name].value : current;
      }
    });
    return updates;
  }

  function refreshSettingsDirty() {
    settingsDirty = Object.keys(changedSettings()).length > 0;
    return settingsDirty;
  }

  function restoreAuthoritativeSettings() {
    settingsDirty = false;
    if (snapshot && snapshot.conversation) {
      applyConversationSettings(snapshot.conversation, true);
      renderCompactControls(snapshot);
    }
  }

  function saveSettings() {
    // A second edit may arrive while an earlier request is in flight.  Wait
    // for its authoritative snapshot, then compare the live controls again.
    if (settingsSaving) return settingsSaving.then(function () { return saveSettings(); });
    refreshSettingsDirty();
    if (!settingsDirty) return Promise.resolve(snapshot);
    var updates = changedSettings();
    settingsDirty = false;
    settingsSaving = formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/settings'), {
      command_id: commandId('s'), ...updates
    }).then(function (data) {
      render(data); return data;
    }).catch(function (error) {
      // The request was not saved.  Do not leave controls showing a value that
      // the authoritative snapshot never accepted; a later edit can retry.
      restoreAuthoritativeSettings();
      error.operation = 'settings'; throw error;
    }).finally(function () { settingsSaving = null; });
    return settingsSaving;
  }

  function saveNotification(next) {
    notificationButton.disabled = true;
    return formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/settings'), {
      command_id: commandId('s'), completion_notification: next ? '1' : '0'
    }).then(function (data) {
      render(data); showToast(next ? '完成通知已开启' : '完成通知已关闭');
    }).catch(function (error) {
      renderNotification(!next); showNote(error.message, true, 'settings');
    }).finally(function () { notificationButton.disabled = false; });
  }

  function goalComposerCommand(text, attachmentCapture) {
    if (attachmentCapture && attachmentCapture.ids && attachmentCapture.ids.length) return false;
    var match = String(text || '').match(/^\/goal(?:\s+([\s\S]*?))?\s*$/i);
    if (!match) return false;
    if (!snapshot) {
      showNote('页面仍在载入，目标命令未发送，输入已保留。', true, 'goal');
      return true;
    }
    var argument = String(match[1] || '').trim();
    if (!argument) {
      refreshGoal().then(function () { openOptionSheet('goal'); }).catch(function (error) {
        showNote((error && error.message) || '目标状态读取失败，未打开编辑面板。', true, 'goal');
      });
      return true;
    }
    var knownAction = /^(pause|resume|clear)$/i.exec(argument);
    var goal = goalValue(snapshot);
    if (knownAction) {
      var actionName = knownAction[1].toLowerCase();
      if (!goal && actionName !== 'clear') {
        showNote('当前没有可管理的目标；输入已保留。', true, 'goal'); return true;
      }
      var startsDraft = actionName === 'resume' && !snapshot.goal && snapshot.goal_draft;
      var record = goalActionRecord(startsDraft ? 'goal_start' : 'goal_' + actionName, startsDraft ? goal.objective : '', true);
      var path = '/conversation/' + encodeURIComponent(conversationId) + (startsDraft ? '/goal/start' : '/goal');
      var values = { command_id: record.id, goal_version: currentGoalVersion(goal) };
      if (startsDraft) { values.objective = goal.objective; values.skills = selectedSkills(); values.skills_explicit = '1'; }
      else values.action = actionName;
      postAction(record, path, values).then(function () {
        showToast(actionName === 'pause' ? '已暂停后续目标' : actionName === 'resume' ? '已继续目标' : '目标已清除');
      }).catch(function (error) {
        showNote((error && error.message) || '目标操作结果待核对；不会自动重发。', true, 'goal');
      });
      return true;
    }
    var record = goalActionRecord('goal_start', argument, true);
    postAction(record, '/conversation/' + encodeURIComponent(conversationId) + '/goal/start', {
      command_id: record.id, objective: argument,
      goal_version: currentGoalVersion(goal), skills: selectedSkills(), skills_explicit: '1'
    }).then(function () { showToast('目标已开始'); }).catch(function (error) {
      showNote((error && error.message) || '目标开始结果待核对；不会自动重发。', true, 'goal');
    });
    return true;
  }

  // Receipt settlement, not the submit event, owns text/attachment cleanup.
  // Capture runs before the separately loaded draft.js bubble listener.
  composer.addEventListener('submit', function (event) {
    event.draftStorageAlreadyHandled = true;
  }, true);
  composer.addEventListener('submit', function (event) {
    event.preventDefault();
    if (sendFlight || (attachmentPreparation && !attachmentReadySubmission)) {
      event.draftStorageAlreadyHandled = true;
      return;
    }
    if (attachmentDraft && attachmentDraft.capture().ids.length && !attachmentReadySubmission) {
      if (attachmentPreparation) return;
      attachmentPreparation = true;
      sendButton.disabled = true;
      showNote('正在核验附件，尚未发送…', false, 'send');
      var submittedText = textarea.value;
      var submittedRevision = composerInputRevision;
      var submittedAttachments = attachmentDraft.capture();
      attachmentDraft.ready().then(function (captured) {
        if (textarea.value !== submittedText || composerInputRevision !== submittedRevision ||
            captured.revision !== submittedAttachments.revision ||
            captured.ids.join(',') !== submittedAttachments.ids.join(',')) {
          showNote('草稿或附件已变更，未发送；请再次点击发送。', true, 'send');
          return;
        }
        pendingAttachmentCapture = captured;
        attachmentReadySubmission = true;
        composer.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        attachmentReadySubmission = false;
      }).catch(function (error) {
        showNote((error && error.message) || '附件核验未完成，未发送。', true, 'send');
      }).finally(function () {
        attachmentPreparation = false;
        sendButton.disabled = Boolean(sendFlight) || Boolean(snapshot && conversationIsSending(snapshot.conversation));
      });
      return;
    }
    var text = textarea.value.trim();
    var attachmentCapture = pendingAttachmentCapture || (attachmentDraft ? attachmentDraft.capture() : { ids: [], records: [], revision: 0 });
    pendingAttachmentCapture = null;
    if (!text && !attachmentCapture.ids.length) return;
    if (text && goalComposerCommand(text, attachmentCapture)) return;
    if (!snapshot) {
      showNote('页面仍在载入，输入已保留；正在同步。', true, 'send');
      sync().catch(function () {}); return;
    }
    var existing = null;
    try { existing = JSON.parse(storageGet(pendingKey) || 'null'); } catch (error) {}
    if (!existing) existing = pendingCommand;
    var sameAttachments = function (candidate) {
      return candidate && (Array.isArray(candidate.attachment_ids) ? candidate.attachment_ids : []).join(',') === attachmentCapture.ids.join(',');
    };
    var pending = existing && existing.text === text && sameAttachments(existing) ? existing : {
      id: commandId('m'), text: text, action: snapshot.conversation.thread_id ? 'messages' : 'start'
    };
    // The durable revision distinguishes a fresh same-text draft after
    // navigation or an edit in another tab from the original submission.
    pending = Object.assign({}, pending, {
      composer_value: textarea.value, draft_revision: storageGet(draftRevisionKey),
      attachment_ids: attachmentCapture.ids.slice(), attachment_capture: attachmentCapture,
      attachment_revision: attachmentCapture.revision
    });
    if (!storageSet(pendingKey, JSON.stringify(pending)) && pending.attachment_ids.length) {
      // Without a durable command identity, a refresh after an unknown POST
      // could create a second native turn for the same files. Do not send it.
      showNote('浏览器无法保存附件投递状态，消息未发送；请释放存储空间或启用浏览器存储后再试。', true, 'send');
      return;
    }
    pendingCommand = pending;
    pendingMessageState = 'sending';
    pendingTextInComposerId = pending.id;
    pendingComposerRevision = composerInputRevision;
    pendingComposerValue = textarea.value;
    lastPresentationSignature = null;
    render(snapshot);
    if (receiptStates[pending.id]) return;
    var flight = { id: pending.id };
    sendFlight = flight;
    sendButton.disabled = true;
    lastOperation = '正在发送';
    showNote('正在发送…', false, 'send');
    saveSettings().then(function () {
      return formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/' + pending.action), {
        command_id: pending.id, text: pending.text, attachment_ids: pending.attachment_ids,
        skills: selectedSkills(), skills_explicit: '1'
      });
    }).then(function (data) {
      if (receiptStates[pending.id]) return;
      validateSnapshot(data);
      pendingMessageState = 'unconfirmed';
      render(data);
      if (receiptStates[pending.id]) return;
      lastOperation = '已提交，待核对';
      showNote('已提交，正在同步核对投递结果；不会自动重复发送。', true, 'send');
    }).catch(function (error) {
      if (receiptStates[pending.id]) return;
      if (error && error.operation === 'settings') {
        pendingMessageState = 'not_sent';
        if (snapshot) render(snapshot);
        lastOperation = '设置未保存，消息未发送';
        showNote(error.message || '设置未保存；消息没有发送，输入已保留。', true, 'send');
        return;
      }
      // Do not apply a stale HTTP error snapshot after another channel has
      // definitively acknowledged this command. Its render has side effects.
      if (receiptStates[pending.id]) return;
      var command = error && error.data && error.data.command;
      var commandState = command && command.state;
      pendingMessageState = 'unconfirmed';
      var snapshotError = null;
      if (error && error.data && error.data.snapshot) {
        try { render(error.data.snapshot); } catch (renderError) { snapshotError = renderError; }
      }
      if (receiptStates[pending.id]) {
        // A websocket or an error payload may have confirmed the command
        // before this transport failure reached the page. Do not resurrect a
        // warning or write its old pending state back over that receipt.
        return;
      }
      var rejected = commandState === 'rejected' && command && command.id === pending.id;
      if (rejected) {
        settlePendingReceipt(pending, 'rejected');
        if (snapshot) render(snapshot);
        if (snapshotError) showNote(snapshotError.message + '；这条消息未送达，输入已保留。', true, 'send');
        return;
      } else if (commandState === 'delivery_unknown') {
        lastOperation = '是否送达待核对';
      } else {
        lastOperation = '发送结果待核对';
      }
      lastPresentationSignature = null;
      if (snapshot) render(snapshot);
      var unavailableMessage = error && error.status >= 500 ?
        '工作台暂时不可用，可能正在重启。发送结果待核对，输入已保留；不会自动重发。' : null;
      showNote((snapshotError && snapshotError.message) || unavailableMessage ||
        (error && error.message) || '发送结果待核对；输入已保留，不会自动重发。', true, 'send');
    }).finally(function () {
      if (sendFlight === flight) sendFlight = null;
      if (snapshot) {
        sendButton.disabled = conversationIsSending(snapshot.conversation);
      }
      window.dispatchEvent(new CustomEvent('codex:conversation-send-settled', {
        detail: { command_id: flight.id }
      }));
    });
  });

  function conversationIsSending(conversation) {
    return Boolean(sendFlight) || conversation.state === 'starting_thread' ||
      conversation.state === 'starting_turn';
  }

  textarea.addEventListener('input', function (event) {
    // draft.js is deferred after this file and announces its boot-time value
    // with an input event. It is not a user edit and must not invalidate the
    // pending text we just restored for receipt reconciliation.
    var restored = event.detail && (event.detail.restoredDraft || event.detail.storageAlreadyHandled);
    if (textarea.value !== lastComposerValue || !restored) composerInputRevision += 1;
    lastComposerValue = textarea.value;
    if (pendingTextInComposerId && (!pendingCommand ||
        textarea.value !== pendingComposerValue || pendingCommand.id !== pendingTextInComposerId)) {
      pendingTextInComposerId = null;
    }
    if (attachmentPreparation && attachmentDraft) attachmentDraft.cancel('文字草稿已变更，未发送；请再次点击发送。');
    renderMessageLength();
  });

  stopButton.addEventListener('click', function () {
    if (!snapshot || !snapshot.conversation.thread_id) return;
    var tree = Boolean(snapshot.capabilities && snapshot.capabilities.conversation_tree_stop);
    if (!tree && !snapshot.conversation.active_turn_id) return;
    var question = tree ? '停止本对话及所有子代理，并暂停持续目标？未发送的排队消息会取消并保留文字。不主动终止后台命令（例如测试）；交接前请核对其副作用。已经发生的文件改动不会撤销。' :
      '仅停止当前轮？当前协议未核验整条停止能力，不会停止子代理或后台命令，已有文件改动不会撤销。';
    if (!confirm(question)) return;
    stopButton.disabled = true;
    var values = {command_id: commandId('i'), scope: tree ? 'conversation_tree' : 'root_turn'};
    if (!tree) values.expected_turn_id = snapshot.conversation.active_turn_id;
    formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/interrupt'), values)
      .then(function (data) {
        render(data);
        showToast(!tree ? '已请求停止当前轮；子代理及后台命令未核验' :
          data.stop && data.stop.status === 'verified' ? '已核对：本对话及子代理已停止' : '停止结果仍需核对');
      }).catch(function (error) {
        if (error.data && error.data.snapshot) render(error.data.snapshot);
        showNote('停止未确认完成；请同步核对后再决定是否重试。', true, 'state');
      }).finally(function () { stopButton.disabled = false; });
  });

  function reconcileNow() {
    refreshButton.disabled = true;
    refreshButton.classList.add('saving');
    showNote('正在同步状态和缺失内容…', false, 'reconcile');
    return formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/reconcile'), {})
      .then(function (data) {
        render(data);
        if (pendingCommand) {
          lastOperation = '投递结果待核对';
          showNote('同步未确认这条消息是否送达；输入已保留，不会自动重复发送。', true, 'send');
        } else if (data.conversation.connection_state !== 'connected' || data.conversation.state === 'reconciling') {
          lastOperation = '同步未完成';
          showNote('同步未完成：尚未取得原对话的可靠状态。草稿已保留，不会自动重复发送。', true, 'state');
        } else if (data.conversation.state === 'needs_user') {
          lastOperation = '仍需核对';
          // render() has the precise native-state/stop explanation.
          if (note.dataset.kind !== 'state') showNote('同步后仍需核对；不会自动重复发送。', true, 'state');
        } else { lastOperation = '已同步'; showNote(''); showToast('同步完成，草稿已保留'); }
        if (diagnostics.open) renderDiagnostics();
        return data;
      }).catch(function (error) {
        showNote(error.message, true, 'reconcile'); throw error;
      }).finally(function () {
        refreshButton.disabled = false; refreshButton.classList.remove('saving');
      });
  }

  function refreshPageContent() {
    var refreshToken = String(Date.now()) + Math.random();
    refreshButton.dataset.refreshToken = refreshToken;
    refreshButton.setAttribute('aria-busy', 'true');
    refreshButton.classList.add('saving');
    showNote('正在重新载入页面内容；现有历史和草稿会保留…', false, 'sync');
    return sync(true).then(function (data) {
      if (data && !socketConnected()) connectSocket();
      showToast('页面内容已更新，草稿已保留');
      return recoverPendingReceipt().then(function () { return data; });
    }).finally(function () {
      if (refreshButton.dataset.refreshToken === refreshToken) {
        refreshButton.removeAttribute('aria-busy');
        refreshButton.classList.remove('saving');
      }
    });
  }

  refreshButton.addEventListener('click', function () {
    var verifyStop = snapshot && snapshot.stop && snapshot.stop.status === 'unconfirmed';
    (verifyStop ? reconcileNow() : refreshPageContent()).catch(function () {});
  });
  document.getElementById('diagnostics-reconcile').addEventListener('click', function () { reconcileNow().catch(function () {}); });

  document.getElementById('diagnose-conversation').addEventListener('click', function () {
    renderDiagnostics(); diagnostics.showModal(); closeHeadMenu();
  });
  diagnostics.querySelector('[data-close-diagnostics]').addEventListener('click', function () { diagnostics.close(); });
  document.getElementById('copy-conversation-diagnostics').addEventListener('click', function () {
    copyText(diagnosticText()).then(function () { showToast('排查信息已复制，包含所属项目'); }, function () { showNote('复制失败，请长按排查信息后复制。', true, 'diagnostics'); });
  });
  document.getElementById('export-conversation').addEventListener('click', function () {
    closeHeadMenu(); exportDialog.showModal();
  });
  exportDialog.querySelector('[data-close-export]').addEventListener('click', function () { exportDialog.close(); });
  copyExportButton.addEventListener('click', function () { beginExport('copy'); });
  downloadExportButton.addEventListener('click', function () { beginExport('download'); });

  contextButton.addEventListener('click', function () { contextDialog.showModal(); });
  contextDialog.querySelector('[data-close-context]').addEventListener('click', function () { contextDialog.close(); });
  document.getElementById('compact-conversation').addEventListener('click', function () {
    if (!confirm('压缩较早上下文？聊天记录仍可查看，但模型后续不会逐字保留全部旧细节。')) return;
    formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/compact'), { command_id: commandId('c') })
      .then(function (data) { render(data); showToast('已请求压缩上下文'); })
      .catch(function (error) { showNote(error.message, true); });
  });

  function closeHeadMenu() {
    var menu = document.querySelector('.native-head-menu');
    if (menu) menu.removeAttribute('open');
  }

  document.getElementById('rename-conversation').addEventListener('click', function () {
    if (!snapshot) return;
    var value = prompt('新的对话名称', snapshot.conversation.title || '');
    if (value === null) return;
    value = value.trim();
    if (!value) { showToast('对话名称不能为空'); return; }
    formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/metadata'), { title: value })
      .then(function (data) { render(data); closeHeadMenu(); showToast('对话已重命名'); })
      .catch(function (error) { showNote(error.message, true); });
  });

  document.getElementById('delete-conversation').addEventListener('click', function () {
    if (!confirm('确定删除这段对话及其对话产物？')) return;
    formRequest(route('/conversation/' + encodeURIComponent(conversationId) + '/delete'), {})
      .then(function () { location.href = route(root.dataset.backUrl || '/home'); })
      .catch(function (error) { showNote(error.message, true); });
  });

  function parseData(select, name) {
    var option = select.selectedOptions[0];
    try { return JSON.parse(option.dataset[name] || '[]'); } catch (error) { return []; }
  }

  function updateModelOptions(first) {
    var currentEffort = first ? effort.dataset.current : effort.value;
    var currentTier = first ? tier.dataset.current : tier.value;
    var selected = model.selectedOptions[0];
    var efforts = parseData(model, 'efforts');
    var defaultEffort = selected ? selected.dataset.defaultEffort : '';
    var availableEfforts = efforts.length ? efforts : [defaultEffort || currentEffort || 'medium'];
    var selectedEffort = availableEfforts.includes(currentEffort) ? currentEffort :
      (availableEfforts.includes(defaultEffort) ? defaultEffort : availableEfforts[0]);
    effort.replaceChildren();
    availableEfforts.forEach(function (value) {
      var option = node('option', '', value); option.value = value; option.selected = value === selectedEffort; effort.appendChild(option);
    });
    var tiers = parseData(model, 'tiers');
    tier.replaceChildren();
    var standard = node('option', '', '标准'); standard.value = ''; tier.appendChild(standard);
    tiers.forEach(function (value) {
      var option = node('option', '', value.name || value.id); option.value = value.id; option.selected = value.id === currentTier; tier.appendChild(option);
    });
    if (!Array.from(tier.options).some(function (option) { return option.value === currentTier; })) tier.value = '';
    effort.dataset.current = effort.value; tier.dataset.current = tier.value;
    updateSettingsSummary(); renderFast();
  }

  function effortLabel(value) {
    return { low: 'Low', medium: 'Medium', high: 'High', xhigh: 'XHigh', max: 'Max', ultra: 'Ultra' }[String(value).toLowerCase()] || value;
  }

  function updateSettingsSummary() {
    if (!model || !model.selectedOptions.length) return;
    var labels = [model.selectedOptions[0].textContent.replace(/-/g, ' '), effortLabel(effort.value),
      { 'read-only': '只读', 'workspace-write': '可写', 'danger-full-access': '完全访问' }[permission.value]];
    settingsSummary.textContent = labels.filter(Boolean).join(' · ');
  }

  [model, effort, permission, collaborationMode].forEach(function (control) {
    control.addEventListener('change', function () {
      if (control === model) updateModelOptions(false);
      refreshSettingsDirty(); updateSettingsSummary(); renderCompactControls(snapshot);
    });
  });

  composer.querySelector('[data-close-settings]').addEventListener('click', function () {
    settings.removeAttribute('open');
    saveSettings().then(function () { showToast('下一轮设置已保存'); }).catch(function (error) { showNote(error.message, true, 'settings'); });
  });

  tierButton.addEventListener('click', function () {
    var fastOption = Array.from(tier.options).find(function (option) { return option.value; });
    if (!fastOption) { showToast('当前模型没有 Fast 档位'); return; }
    tier.value = tier.value ? '' : fastOption.value;
    tier.dataset.current = tier.value;
    var requestedFast = Boolean(tier.value);
    refreshSettingsDirty(); renderFast();
    saveSettings().then(function () {
      if (Boolean(tier.value) === requestedFast) {
        showToast('速度已切换为 ' + (requestedFast ? 'Fast' : 'Standard'));
      }
    })
      .catch(function (error) { showNote(error.message, true, 'settings'); });
  });

  notificationButton.addEventListener('click', function () {
    var next = !notification.checked;
    renderNotification(next); saveNotification(next);
  });

  messages.addEventListener('click', function (event) {
    var groupAction = event.target.closest('[data-commentary-group-action]');
    if (groupAction) {
      var group = groupAction.closest('.native-commentary-group');
      var turnId = group && group.dataset.commentaryGroupTurn;
      if (!turnId) return;
      var members = Array.from(messages.querySelectorAll('[data-commentary-group-member]')).filter(function (message) {
        return message.dataset.commentaryGroupMember === turnId;
      });
      if (!members.length) return;
      var folded = !group.classList.contains('is-group-folded');
      group.classList.toggle('is-group-folded', folded);
      groupAction.setAttribute('aria-expanded', String(!folded));
      groupAction.querySelector('strong').textContent = folded ?
        '本轮过程 · ' + members.length + ' 条 · ' +
          collapsedPreview(members[0].querySelector('.native-message-body').dataset.messageSource) :
        '收起本轮过程 · ' + members.length + ' 条';
      groupAction.querySelector('.native-commentary-group-toggle').textContent = folded ? '展开' : '收起';
      members.forEach(function (message) {
        message.classList.toggle('is-group-hidden', folded);
      });
      writeCommentaryGroupState(turnId, { expanded: !folded });
      return;
    }
    var more = event.target.closest('.native-message-more');
    if (more) {
      event.stopPropagation();
      var menu = more.closest('.native-bubble').querySelector('.native-message-menu');
      messages.querySelectorAll('.native-message-menu').forEach(function (value) { if (value !== menu) value.hidden = true; });
      menu.hidden = !menu.hidden; return;
    }
    var action = event.target.closest('[data-action]');
    if (action) {
      var message = action.closest('.native-message');
      var body = message.querySelector('.native-message-body');
      var fullText = body.dataset.messageSource || body.textContent;
      var key = message.dataset.messageKey;
      var saved = readMessageState(key);
      if (action.dataset.action === 'copy') {
        copyText(fullText).then(function () { showToast('已复制全文'); }, function () { showToast('请长按选择文字复制'); });
      } else if (action.dataset.action === 'quote') {
        var quoted = '> ' + fullText.replace(/\n/g, '\n> ') + '\n\n';
        textarea.value = quoted + textarea.value; textarea.dispatchEvent(new Event('input')); textarea.focus(); showToast('已引用到输入框');
      } else if (action.dataset.action === 'important') {
        saved.important = !saved.important; message.classList.toggle('is-important', saved.important); writeMessageState(key, saved); showToast(saved.important ? '已标记重要' : '已取消重要');
      } else if (action.dataset.action === 'fold') {
        saved.folded = !saved.folded; message.classList.toggle('is-folded', saved.folded); action.textContent = saved.folded ? '展开消息' : '折叠消息'; writeMessageState(key, saved);
      }
      action.closest('.native-message-menu').hidden = true; return;
    }
    var interruptedAction = event.target.closest('[data-interrupted-action]');
    if (interruptedAction) {
      var turn = snapshot && snapshot.turns.find(function (value) { return value.id === interruptedAction.dataset.turnId; });
      var first = turn && (turn.commands || []).find(function (command) { return command.kind === 'start' || command.kind === 'steer'; });
      textarea.value = interruptedAction.dataset.interruptedAction === 'retry' && first ? first.text : '请继续处理刚才的任务。';
      textarea.dispatchEvent(new Event('input')); textarea.focus(); showToast('已放入输入框，确认后发送'); return;
    }
    var preview = event.target.closest('[data-artifact-preview]');
    if (preview) {
      if (preview.dataset.artifactPreview === 'html') openHtmlArtifactViewer(preview);
      else openViewer(preview);
      return;
    }
  });

  document.addEventListener('click', function (event) {
    var download = event.target.closest('a[data-download]');
    if (download) showToast('已交给浏览器下载；完成提示由浏览器和系统决定');
  });

  function clearSearchHits() {
    messages.querySelectorAll('.is-search-hit').forEach(function (item) { item.classList.remove('is-search-hit'); });
  }

  function updateSearch(move) {
    clearSearchHits();
    if (historyPaged && !historyFull && fullHistoryFlight) {
      searchStatus.textContent = '正在加载完整历史后搜索…'; return;
    }
    var query = searchInput.value.trim().toLowerCase();
    if (!query) {
      searchHits = []; searchIndex = -1; searchStatus.textContent = '输入关键词开始查找'; return;
    }
    searchHits = Array.from(messages.querySelectorAll('.native-message')).filter(function (message) {
      var body = message.querySelector('.native-message-body');
      return body && body.textContent.toLowerCase().includes(query);
    });
    if (!searchHits.length) {
      searchIndex = -1; searchStatus.textContent = historyPaged && !historyFull ? '已加载记录中没有找到（完整历史尚未加载）' : '没有找到'; return;
    }
    if (move === 'next') searchIndex = (searchIndex + 1 + searchHits.length) % searchHits.length;
    else if (move === 'prev') searchIndex = (searchIndex - 1 + searchHits.length) % searchHits.length;
    else searchIndex = Math.min(Math.max(searchIndex, 0), searchHits.length - 1);
    searchHits[searchIndex].classList.add('is-search-hit');
    searchStatus.textContent = (searchIndex + 1) + ' / ' + searchHits.length + (historyPaged && !historyFull ? '（仅已加载记录）' : '');
    if (move) searchHits[searchIndex].scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  document.getElementById('search-conversation').addEventListener('click', function () {
    closeHeadMenu(); searchDialog.showModal(); searchInput.focus();
    if (historyPaged && !historyFull) fullHistory().catch(function () {});
    updateSearch(false);
  });
  searchInput.addEventListener('input', function () { searchIndex = -1; updateSearch(false); });
  document.getElementById('conversation-search-next').addEventListener('click', function () { updateSearch('next'); });
  document.getElementById('conversation-search-prev').addEventListener('click', function () { updateSearch('prev'); });
  searchDialog.querySelector('[data-close-search]').addEventListener('click', function () { searchDialog.close(); });
  searchDialog.addEventListener('close', clearSearchHits);

  function renderZoom() {
    viewerImage.style.setProperty('--artifact-zoom', zoom);
    document.getElementById('artifact-zoom-value').textContent = Math.round(zoom * 100) + '%';
  }

  function replaceFrameSource(frame, url) {
    frame.dataset.currentSource = url;
    try { frame.contentWindow.location.replace(url); }
    catch (error) { frame.src = url; }
  }

  function hideViewer() {
    viewer.hidden = true;
    document.body.style.overflow = '';
    viewerImage.src = '';
    replaceFrameSource(viewerFrame, 'about:blank');
    viewerHistoryToken = null;
    (viewerFocus || document.body).focus();
  }

  function openHtmlArtifactViewer(control) {
    // HTML artifacts get a separate document so browser zoom belongs to that
    // history entry rather than the conversation underneath it.
    var target = new URL(control.dataset.contentUrl, location.href);
    target.searchParams.set('viewer', '1');
    location.assign(target.href);
  }

  function openViewer(control) {
    viewerFocus = control;
    var kind = control.dataset.artifactPreview;
    viewerTitle.textContent = control.dataset.artifactName || '附件预览';
    viewerDownload.href = control.dataset.downloadUrl;
    viewerDownload.download = control.dataset.artifactName || '';
    viewerImage.hidden = kind !== 'image';
    viewerFrame.hidden = kind !== 'pdf';
    viewerControls.hidden = kind !== 'image';
    viewer.hidden = false;
    document.body.style.overflow = 'hidden';
    viewerHistoryToken = conversationId + ':' + Date.now();
    try {
      history.pushState(Object.assign({}, history.state || {}, { codexArtifactPreview: viewerHistoryToken }), document.title);
    } catch (error) { viewerHistoryToken = null; }
    if (kind === 'image') {
      viewerImage.src = control.dataset.contentUrl;
      viewerImage.alt = control.dataset.artifactName || '图片预览';
      zoom = 1; renderZoom();
    } else {
      replaceFrameSource(viewerFrame, control.dataset.contentUrl);
    }
  }

  function requestViewerClose() {
    if (viewerHistoryToken && history.state && history.state.codexArtifactPreview === viewerHistoryToken) {
      history.back();
      setTimeout(function () { if (!viewer.hidden) hideViewer(); }, 350);
    } else hideViewer();
  }

  document.getElementById('close-artifact-viewer').addEventListener('click', requestViewerClose);
  window.addEventListener('popstate', function () { if (!viewer.hidden) hideViewer(); });
  document.getElementById('artifact-zoom-in').addEventListener('click', function () { zoom = Math.min(3, zoom + .25); renderZoom(); });
  document.getElementById('artifact-zoom-out').addEventListener('click', function () { zoom = Math.max(.5, zoom - .25); renderZoom(); });
  document.getElementById('artifact-zoom-reset').addEventListener('click', function () { zoom = 1; renderZoom(); });
  document.getElementById('artifact-viewer-stage').addEventListener('dblclick', function () { if (!viewerImage.hidden) { zoom = zoom === 1 ? 2 : 1; renderZoom(); } });

  newContentButton.addEventListener('click', function () {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); newContentButton.hidden = true;
  });
  window.addEventListener('scroll', function () {
    if (nearBottom()) newContentButton.hidden = true;
  }, { passive: true });
  ['wheel', 'touchstart', 'pointerdown'].forEach(function (eventName) {
    window.addEventListener(eventName, function () {
      initialBottomCancelled = true; searchFocusCancelled = true;
    }, { passive: true });
  });
  window.addEventListener('keydown', function (event) {
    if (['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(event.key)) {
      initialBottomCancelled = true;
      searchFocusCancelled = true;
    }
  });

  function clearSocketHeartbeat() {
    clearInterval(socketHeartbeatTimer);
    socketHeartbeatTimer = null;
  }

  function coordinatorUnavailable() {
    showNote('实时连接组件未加载；页面会保留当前内容。请刷新页面后再试。', true, 'connection');
    window.dispatchEvent(new CustomEvent('codex:connection-unavailable'));
  }

  function socketConnecting() {
    return socket && socket.readyState === WebSocket.CONNECTING;
  }

  function socketNeedsRecovery() {
    return !socketConnected() || !socketLastConfirmedAt ||
      Date.now() - socketLastConfirmedAt > socketStaleAfter;
  }

  function restartSocket(message) {
    // A CONNECTING socket has no terminal outcome yet.  Replacing it here
    // causes visibility/online storms to make concurrent handshakes.
    if (socketConnecting()) return;
    clearSocketHeartbeat();
    var previous = socket;
    socket = null;
    socketLastConfirmedAt = 0;
    if (previous) {
      previous.onopen = null;
      previous.onmessage = null;
      previous.onclose = null;
      try { previous.close(); } catch (error) { if (window.codexDiagnostic) window.codexDiagnostic('ws', {phase:'close_failed', message:String(error && error.message || error)}); }
    }
    if (automaticRequestsStopped || !connectionRetry || connectionRetry.isHalted()) return;
    if (message && snapshot && activeStates.has(snapshot.conversation.state)) {
      showNote(message, true, 'connection');
    }
    if (snapshot && (activeStates.has(snapshot.conversation.state) ||
        snapshot.conversation.state === 'needs_user')) {
      schedulePoll(true, 0);
    }
    connectionRetry.failed();
    updateServerActivity();
  }

  function heartbeatSocket() {
    if (automaticRequestsStopped || document.visibilityState !== 'visible') return;
    if (socketConnecting()) return;
    if (!socketConnected()) {
      restartSocket('实时连接已断开，正在自动重连；状态会继续同步。');
      return;
    }
    if (socketLastConfirmedAt && Date.now() - socketLastConfirmedAt > socketStaleAfter) {
      restartSocket('实时连接长时间没有响应，正在重新建立连接。');
      return;
    }
    try {
      socket.send('ping');
    } catch (error) {
      if (window.codexDiagnostic) window.codexDiagnostic('ws', {phase:'send_failed', message:String(error && error.message || error)});
      restartSocket('实时连接发送失败，正在重新建立连接。');
    }
  }

  function startSocketHeartbeat() {
    clearSocketHeartbeat();
    socketLastConfirmedAt = Date.now();
    heartbeatSocket();
    socketHeartbeatTimer = setInterval(heartbeatSocket, socketHeartbeatInterval);
  }

  function connectSocket() {
    if (automaticRequestsStopped || !connectionRetry || connectionRetry.isHalted()) return;
    if (socket && (socket.readyState === WebSocket.CONNECTING ||
        socket.readyState === WebSocket.OPEN)) return;
    var scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var candidate;
    try {
      candidate = new WebSocket(scheme + '//' + location.host + route('/ws'));
    } catch (error) {
      if (window.codexDiagnostic) window.codexDiagnostic('ws', {phase:'construct_failed', message:String(error && error.message || error)});
      connectionRetry.failed();
      schedulePoll(Boolean(snapshot && activeStates.has(snapshot.conversation.state)));
      return;
    }
    socket = candidate;
    candidate.onopen = function () {
      if (socket !== candidate) return;
      connectionRetry.opened();
      startSocketHeartbeat();
      if (nextSyncNotBefore() > Date.now()) scheduleAutomaticSync(); else schedulePoll(false);
      if (note.dataset.kind === 'connection') showNote('');
      updateServerActivity();
    };
    candidate.onmessage = function (event) {
      if (socket !== candidate) return;
      socketLastConfirmedAt = Date.now();
      try {
        var message = JSON.parse(event.data);
        if (message.type === 'pong') {
          updateServerActivity();
        } else if (message.type === 'conversation.updated' && message.data) {
          window.dispatchEvent(new CustomEvent('codex:conversation-update', { detail: message.data }));
          if (message.data.conversation_id === conversationId) scheduleAutomaticSync();
        } else if (message.type === 'conversation.terminal' && message.data) {
          dispatchTerminal(message.data);
          if (message.data.conversation_id === conversationId) {
            sendReadReceipt(message.data.generation);
            scheduleAutomaticSync();
          }
        }
      } catch (error) { if (window.codexDiagnostic) window.codexDiagnostic('ws', {phase:'message_parse_or_handle_failed', message:String(error && error.message || error), stack:String(error && error.stack || '')}); }
    };
    candidate.onclose = function (event) {
      if (socket !== candidate) return;
      clearSocketHeartbeat();
      socket = null;
      socketLastConfirmedAt = 0;
      if (event.code === 4401 || event.code === 4403) {
        stopAutomaticRequests({ status: event.code === 4401 ? 401 : 403 });
        showNote(event.code === 4401 ? '登录已失效，请刷新页面重新登录；输入仍保留在下方。' : '当前账号无权访问这段对话；输入仍保留在下方。', true, 'connection');
        return;
      }
      if (window.codexCheckLogin) window.codexCheckLogin();
      if (!automaticRequestsStopped) connectionRetry.failed();
      if (snapshot && (activeStates.has(snapshot.conversation.state) || snapshot.conversation.state === 'needs_user')) schedulePoll(true);
      if (snapshot && activeStates.has(snapshot.conversation.state)) showNote('实时连接已断开，正在自动重连；状态会继续同步。', true, 'connection');
      updateServerActivity();
    };
  }

  function recoverConnection() {
    if (automaticRequestsStopped || !connectionRetry || connectionRetry.isHalted()) return;
    // This only reads the snapshot; it never replays a pending user command.
    // A server Retry-After remains authoritative across foreground recovery.
    if (serverRetryNotBefore > Date.now()) scheduleAutomaticSync();
    else sync({ foreground: true }).catch(function () {});
    if (socketNeedsRecovery()) {
      if (!socketConnecting() && socket) {
        // A stale OPEN socket cannot be reused: connectSocket correctly
        // protects OPEN sockets, so retire this one before the immediate
        // foreground recovery connect.  Do not convert it to a retry delay.
        clearSocketHeartbeat();
        var previous = socket;
        socket = null;
        socketLastConfirmedAt = 0;
        previous.onopen = previous.onmessage = previous.onclose = null;
        try { previous.close(); } catch (error) {}
      }
      connectSocket();
    } else {
      heartbeatSocket();
    }
    sendReadReceipt(Math.max(
      pendingReadGeneration,
      snapshot ? terminalGeneration(snapshot.conversation.terminal_generation) : 0
    ));
  }

  if (!window.CodexReconnectCoordinator ||
      typeof window.CodexReconnectCoordinator.create !== 'function') {
    coordinatorUnavailable();
  } else {
    connectionRetry = window.CodexReconnectCoordinator.create({
      connect: connectSocket,
      recover: recoverConnection
    });
  }

  window.addEventListener('codex:auth-required', function () {
    stopAutomaticRequests({ status: 401 });
  });

  var params = new URLSearchParams(location.search);
  var prefill = params.get('prefill');
  if (prefill) textarea.value = prefill;
  if (params.get('quoted')) {
    var quoted = storageGet('codex-new-quote');
    if (quoted) {
      textarea.value = '引用：\n' + quoted + '\n\n'; storageRemove('codex-new-quote');
    }
  }
  try { pendingCommand = JSON.parse(storageGet(pendingKey) || 'null'); } catch (error) {}
  try { pendingAction = JSON.parse(storageGet(actionPendingKey) || 'null'); } catch (error) {}
  if (pendingAction && (!pendingAction.id || !pendingAction.kind)) pendingAction = null;
  if (pendingCommand && (!pendingCommand.id || typeof pendingCommand.text !== 'string' ||
      (!pendingCommand.text && !(Array.isArray(pendingCommand.attachment_ids) && pendingCommand.attachment_ids.length)))) pendingCommand = null;
  if (pendingCommand) {
    var savedDraft = storageGet(draftKey);
    var recoveredText = typeof pendingCommand.composer_value === 'string' &&
      pendingCommand.composer_value.trim() === pendingCommand.text ?
      pendingCommand.composer_value : pendingCommand.text;
    var hasExplicitComposerText = Boolean(prefill || params.get('quoted'));
    // draft.js runs after this deferred script. Read its value now so an old
    // pending recovery cannot overwrite a newer ordinary draft before it has
    // a chance to restore itself.
    if (!hasExplicitComposerText && (!savedDraft || savedDraft === recoveredText)) {
      textarea.value = recoveredText;
      pendingTextInComposerId = ownsStoredDraft(pendingCommand) ? pendingCommand.id : null;
      pendingComposerRevision = composerInputRevision;
      pendingComposerValue = textarea.value;
      lastOperation = '上次发送待核对';
      showNote('已恢复上次输入。请先同步，再决定是否重新发送。', true, 'send');
    } else {
      if (!hasExplicitComposerText) textarea.value = savedDraft;
      lastOperation = '上次发送待核对';
      showNote('上次发送结果待核对；当前草稿已保留。', true, 'send');
    }
  }
  if (prefill || params.get('quoted')) {
    // This explicit draft is new even when its text matches the old pending
    // command. draft.js has not registered its input listener yet.
    storageSet(draftRevisionKey, commandId('d'));
    storageSet(draftKey, textarea.value);
    textarea.dispatchEvent(new Event('input'));
    history.replaceState(null, '', location.pathname);
  }
  // Remember setup-written content before draft.js emits its boot event.
  lastComposerValue = textarea.value;
  requestedTurnId = params.get('turn');
  if (!validTurnId(requestedTurnId)) requestedTurnId = null;
  requestedCommandId = params.get('command');
  if (!validTurnId(requestedCommandId)) requestedCommandId = null;
  requestedItemId = params.get('item');
  if (!validTurnId(requestedItemId) || !requestedTurnId || requestedCommandId) requestedItemId = null;
  if (pendingAction) {
    showNote('上次 Plan/Goal 操作结果待核对；不会自动重发。', true, 'goal');
    // This is read-only receipt recovery, deliberately not a replay.
    setTimeout(function () { actionReceipt(pendingAction); }, 0);
  }
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', function (event) {
      var data = event.data || {};
      if (data.type === 'web-push-open-turn' && data.conversation_id === conversationId) {
        requestedTurnId = String(data.turn_id || '');
        requestedCommandId = requestedItemId = null;
        if (!validTurnId(requestedTurnId)) requestedTurnId = null;
        scheduleRequestedTurnFocus();
      }
    });
  }
  var uploadForm = document.querySelector('#conversation-upload form');
  if (uploadForm) {
    uploadForm.action = route('/files/upload');
    var uploadReturn = uploadForm.querySelector('input[name="return_to"]');
    if (uploadReturn) uploadReturn.value = route('/conversation/' + encodeURIComponent(conversationId));
  }
  updateModelOptions(true);
  renderNotification(notification.checked);
  var initialSnapshotNode = document.getElementById('conversation-initial-snapshot');
  if (initialSnapshotNode) {
    try {
      render(JSON.parse(initialSnapshotNode.textContent));
    } catch (error) {
      if (window.codexDiagnostic) window.codexDiagnostic('js_error', {phase:'initial_snapshot_restore_failed', message:String(error && error.message || error), stack:String(error && error.stack || '')});
      automaticRequestsStopped = false;
      showNote('页面中的最近记录暂时无法读取，正在重新载入。', true, 'sync');
    }
  }
  // The bounded window intentionally omits old completed commands. Recover
  // only the stored pending ID once, not every receipt on every tail refresh.
  if (pendingCommand) setTimeout(function () { recoverPendingReceipt(); }, 0);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      // Do not leave a background GET or its retry timer alive to become the
      // foreground observation later.  POSTs have their own flights and are
      // deliberately not touched here.
      clearTimeout(pollTimer);
      pollTimer = null;
      retireSyncFlight();
    } else {
      inputMode(document.activeElement === textarea);
      scheduleMarkdownRecovery(250);
    }
  });
  window.addEventListener('pageshow', function (event) {
    inputMode(document.activeElement === textarea);
    scheduleMarkdownRecovery(250);
  });
  setInterval(updateHeaderState, 1000);
  sync().catch(function () {});
  if (connectionRetry) connectSocket();
})();
