(function () {
  'use strict';

  var modules = window.codexConversationModules = window.codexConversationModules || {};
  if (modules.markdown === 'ready') return;

  var root = document.getElementById('native-conversation');
  var messages = document.getElementById('conversation-messages');
  if (!root || !messages) return;

  var composer = document.getElementById('conversation-composer');
  var textarea = composer && composer.querySelector('textarea[name="text"]');
  var toast = document.getElementById('conversation-toast');
  var toastTimer = null;
  var activeTable = null;
  var conversationId = root.dataset.conversationId;
  var crossToast = document.getElementById('conversation-cross-toast');
  var crossTitle = document.getElementById('conversation-cross-title');
  var crossOpen = document.getElementById('conversation-cross-open');
  var appPrefixMatch = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
  var appPrefix = appPrefixMatch ? '/' + appPrefixMatch[1] : '';
  var messageStatePrefix = 'codex-conversation-message:' + conversationId + ':';
  var autoFoldSession = Object.create(null);
  var foldChoices = Object.create(null);
  var queuedMessages = new Set();
  var queuedOrder = [];
  var deferredFolds = new Set();
  var foldOrder = [];
  var visibleMessages = new Set();
  var observedMessages = new WeakSet();
  var foldVersions = new WeakMap();
  var enhancementFrame = null;
  var visibilityObserver = typeof IntersectionObserver === 'function' ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.target.isConnected) { discardEnhancementTarget(entry.target); return; }
      if (entry.isIntersecting) { visibleMessages.add(entry.target); queueEnhancement(entry.target); }
      else visibleMessages.delete(entry.target);
    });
  }, {rootMargin: '100px 0px'}) : null;

  function route(path) { return appPrefix + path; }
  function element(tag, className, text) {
    var value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }

  function actionButton(className, text) {
    var button = element('button', className, text);
    button.type = 'button';
    return button;
  }

  function actionHead(label, extraClass) {
    var head = element('div', 'native-local-head' + (extraClass ? ' ' + extraClass : ''));
    head.append(
      element('b', '', label),
      actionButton('native-local-quote', '引用'),
      actionButton('native-local-copy', '复制')
    );
    return head;
  }

  function readMessageState(key) {
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(messageStatePrefix + key) || '{}') || {}; } catch (error) {}
    return Object.assign({}, saved, foldChoices[key] || {});
  }

  function writeMessageState(key, value) {
    foldChoices[key] = { autoFoldExpanded: value.autoFoldExpanded, autoCodeExpanded: value.autoCodeExpanded };
    try { localStorage.setItem(messageStatePrefix + key, JSON.stringify(value)); } catch (error) {}
  }

  function autoFoldControls(message) {
    var bubble = message.querySelector('.native-bubble');
    var body = message.querySelector('.native-message-body');
    var controls = Array.from(bubble.querySelectorAll('.native-message-auto-fold-toggle'));
    if (controls.length) return controls;
    var top = actionButton('native-message-auto-fold-toggle native-message-auto-fold-top', '展开全文');
    var bottom = actionButton('native-message-auto-fold-toggle native-message-auto-fold-bottom', '展开全文');
    top.dataset.autoFoldAction = 'toggle'; bottom.dataset.autoFoldAction = 'toggle';
    bubble.insertBefore(top, body);
    // Keep the lower control outside the clipped body, so it remains usable
    // for a one-screen preview.
    bubble.insertBefore(bottom, body.nextSibling);
    return [top, bottom];
  }

  function updateAutoFoldControls(message, controls) {
    var folded = message.classList.contains('is-auto-folded');
    controls.forEach(function (control) {
      var label = folded ? '展开全文' : '收起全文';
      if (control.textContent !== label) control.textContent = label;
      var expanded = String(!folded);
      if (control.getAttribute('aria-expanded') !== expanded) control.setAttribute('aria-expanded', expanded);
    });
  }

  function applyAutomaticFolds(candidates) {
    var minMessageHeight = Math.round(window.innerHeight * 1.5);
    var minCodeHeight = Math.max(360, Math.round(window.innerHeight * 0.55));
    (candidates || messages.querySelectorAll('.native-message[data-auto-fold-candidate]')).forEach(function (message) {
      if (!message.matches('[data-auto-fold-candidate]')) return;
      // In-progress cards must remain live and readable.
      if (message.dataset.autoFoldLive === '1') {
        autoFoldSession[message.dataset.messageKey] = true;
        return;
      }
      var body = message.querySelector('.native-message-body');
      if (!body) return;
      var key = message.dataset.messageKey;
      var saved = readMessageState(key);
      body.querySelectorAll('.native-code-card').forEach(function (card, index) {
        var pre = card.querySelector('pre');
        var codeIsLong = card.dataset.autoCodeLong === '1' || (pre && pre.scrollHeight > minCodeHeight);
        if (!codeIsLong) return;
        if (card.dataset.autoCodeLong !== '1') card.dataset.autoCodeLong = '1';
        if (card.dataset.autoCodeIndex !== String(index)) card.dataset.autoCodeIndex = String(index);
        var expandedCodes = saved.autoCodeExpanded || {};
        card.classList.toggle('auto-folded', expandedCodes[String(index)] !== true);
        var fold = card.querySelector('.native-code-fold');
        if (fold) {
          var codeLabel = card.classList.contains('auto-folded') ? '展开代码' : '折叠代码';
          if (fold.textContent !== codeLabel) fold.textContent = codeLabel;
        }
      });
      // Measure before the outer class changes.  A long fenced log can fold
      // independently without forcing otherwise short surrounding prose to
      // become a collapsed message.
      var longMessage = body.scrollHeight > minMessageHeight;
      if (!longMessage) return;
      var controls = autoFoldControls(message);
      var expanded = saved.autoFoldExpanded;
      if (expanded === undefined && message.dataset.autoFoldLatest === '1') {
        // Keep a reply open for this page session if it becomes historical in
        // a later poll, but let a fresh history visit use the normal fold.
        autoFoldSession[key] = true;
      }
      if (expanded === undefined) expanded = autoFoldSession[key] === true;
      message.classList.toggle('is-auto-folded', !expanded);
      updateAutoFoldControls(message, controls);
    });
  }

  function safeLink(value) {
    try {
      var url = new URL(value, location.href);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
    } catch (error) { return null; }
  }

  function appendInline(target, text) {
    var pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\))/g;
    var cursor = 0;
    var match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > cursor) target.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      if (match[2] !== undefined) target.appendChild(element('strong', '', match[2]));
      else if (match[3] !== undefined) target.appendChild(element('code', '', match[3]));
      else {
        var href = safeLink(match[5]);
        if (href) {
          var link = element('a', '', match[4]);
          link.href = href; link.rel = 'noopener noreferrer'; target.appendChild(link);
        } else target.appendChild(document.createTextNode(match[4]));
      }
      cursor = pattern.lastIndex;
    }
    if (cursor < text.length) target.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function tableCells(line) {
    var value = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return value.split('|').map(function (cell) { return cell.trim(); });
  }

  function renderMarkdown(target, source) {
    target.replaceChildren();
    var lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
    var index = 0;
    function special(line, next) {
      return !line.trim() || /^```/.test(line) || /^#{1,3}\s+/.test(line) ||
        /^\s*>/.test(line) || /^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line) ||
        (line.includes('|') && next && /^\s*\|?\s*:?-{3,}/.test(next));
    }
    while (index < lines.length) {
      var line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      var fence = line.match(/^```\s*([^`]*)$/);
      if (fence) {
        var codeLines = []; index += 1;
        while (index < lines.length && !/^```/.test(lines[index])) codeLines.push(lines[index++]);
        if (index < lines.length) index += 1;
        var language = (fence[1] || '').trim();
        var card = element('section', 'native-code-card'); card.dataset.localKind = 'code'; card.dataset.language = language;
        var head = actionHead(language || '代码', 'native-code-head');
        head.appendChild(actionButton('native-code-fold', '折叠'));
        var pre = element('pre'); pre.appendChild(element('code', '', codeLines.join('\n')));
        card.append(head, pre); target.appendChild(card); continue;
      }
      if (line.includes('|') && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
        var headers = tableCells(line); index += 2;
        var rows = [];
        while (index < lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(tableCells(lines[index++]));
        var wrap = element('section', 'native-table-wrap'); wrap.dataset.localKind = 'table';
        var tableScroll = element('div', 'native-table-scroll'); var table = element('table');
        var thead = element('thead'); var headerRow = element('tr');
        headers.forEach(function (cell) { var th = element('th'); appendInline(th, cell); headerRow.appendChild(th); });
        thead.appendChild(headerRow); table.appendChild(thead);
        var tbody = element('tbody'); rows.forEach(function (row) { var tr = element('tr'); row.forEach(function (cell) { var td = element('td'); appendInline(td, cell); tr.appendChild(td); }); tbody.appendChild(tr); });
        table.appendChild(tbody); tableScroll.appendChild(table);
        wrap.append(actionHead('表格'), tableScroll); target.appendChild(wrap); continue;
      }
      var heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) { var h = element('h' + Math.min(3, heading[1].length + 1)); appendInline(h, heading[2]); target.appendChild(h); index += 1; continue; }
      if (/^\s*>/.test(line)) {
        var quotes = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) quotes.push(lines[index++].replace(/^\s*>\s?/, ''));
        var quoteCard = element('section', 'native-quote-card'); quoteCard.dataset.localKind = 'quote';
        var quote = element('blockquote'); appendInline(quote, quotes.join(' '));
        quoteCard.append(actionHead('引用内容'), quote); target.appendChild(quoteCard); continue;
      }
      if (/^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
        var orderedMarker = line.match(/^\s*(\d+)\.\s+/);
        var ordered = Boolean(orderedMarker); var list = element(ordered ? 'ol' : 'ul');
        if (ordered) list.setAttribute('start', orderedMarker[1]);
        var itemPattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/;
        while (index < lines.length && itemPattern.test(lines[index])) { var li = element('li'); appendInline(li, lines[index++].replace(itemPattern, '')); list.appendChild(li); }
        target.appendChild(list); continue;
      }
      var paragraphLines = [line.trim()]; index += 1;
      while (index < lines.length && !special(lines[index], lines[index + 1])) paragraphLines.push(lines[index++].trim());
      var paragraph = element('p'); appendInline(paragraph, paragraphLines.join(' ')); target.appendChild(paragraph);
    }
  }

  function discardEnhancementTarget(message) {
    queuedMessages.delete(message); deferredFolds.delete(message); visibleMessages.delete(message);
    if (visibilityObserver) visibilityObserver.unobserve(message);
    observedMessages.delete(message);
    // With no live jobs, stale ordering entries must not hold removed history
    // indefinitely (including a removal before the first animation frame).
    if (!queuedMessages.size) queuedOrder.length = 0;
    if (!deferredFolds.size) foldOrder.length = 0;
  }

  function hasSelection(message) {
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
    try { return selection.getRangeAt(0).intersectsNode(message); } catch (_) { return false; }
  }

  function enhanceOne(message, forceFold) {
    if (!message.isConnected || hasSelection(message)) return false;
    var body = message.querySelector('.native-message-body');
    if (!body) return false;
    var needsMarkdown = message.classList.contains('agent') && body.dataset.nativeEnhanced !== '1';
    var version = String(message.dataset.autoFoldLatest || '') + ':' + String(message.dataset.autoFoldLive || '');
    var previous = foldVersions.get(message);
    var measureFold = forceFold || !visibilityObserver || visibleMessages.has(message);
    if (!needsMarkdown && previous && previous.body === body && previous.version === version &&
        (previous.folded || !measureFold)) return false;
    if (needsMarkdown) {
      var raw = body.textContent;
      var rendered = document.createDocumentFragment();
      if (!body.parentElement.classList.contains('native-final') &&
          !body.parentElement.classList.contains('native-plan')) body.parentElement.classList.add('native-progress');
      renderMarkdown(rendered, raw);
      body.replaceChildren(rendered);
      body.dataset.speechSource = raw;
      body.dataset.nativeEnhanced = '1';
    }
    // Parse text before spending layout work on offscreen folds. Visible and
    // explicitly opened messages get their folds now; other folds are budgeted
    // after Markdown jobs, retaining the same eventual collapsed presentation.
    if (measureFold) { applyAutomaticFolds([message]); deferredFolds.delete(message); }
    else if (!deferredFolds.has(message)) { deferredFolds.add(message); foldOrder.push(message); }
    foldVersions.set(message, {body: body, version: version, folded: measureFold});
    return true;
  }

  function scheduleEnhancements() {
    if (enhancementFrame !== null || (!queuedMessages.size && !deferredFolds.size)) return;
    enhancementFrame = requestAnimationFrame(runEnhancements);
  }

  function enhancementViewportAnchor(line) {
    // Hit testing avoids scanning every historical child's geometry on a
    // slow phone. Keep a direct timeline child as the stable reflow anchor.
    var hit = document.elementFromPoint(Math.max(0, Math.min(innerWidth - 1, innerWidth / 2)),
      Math.max(0, Math.min(innerHeight - 1, line + 2)));
    var anchor = hit;
    while (anchor && anchor.parentElement !== messages) anchor = anchor.parentElement;
    if (anchor && anchor.parentElement === messages) return anchor;
    // In gaps, observer delivery/order is not authoritative for what the user
    // is reading. Preserve the first timeline child, exactly as before hit
    // testing was added, rather than a later observer-inserted message.
    return Array.from(messages.children).find(function (item) { return item.getBoundingClientRect().bottom >= line; });
  }

  function runEnhancements() {
    enhancementFrame = null;
    // Raw messages remain readable immediately. Markdown/fold measurement is
    // interruptible between messages, with visible/newest content first.
    var work = false, attempted = 0, inspected = 0;
    var selection = window.getSelection();
    var selecting = selection && !selection.isCollapsed;
    var atBottom = innerHeight + scrollY >= document.body.scrollHeight - 150;
    var head = document.querySelector('.native-conversation-head');
    var line = head ? head.getBoundingClientRect().bottom : 0;
    var anchor = !selecting && !atBottom && enhancementViewportAnchor(line);
    var offset = anchor && anchor.getBoundingClientRect().top;
    var visible = Array.from(visibleMessages).filter(function (message) {
      // Blocked selections stay queued for selectionchange, but must not
      // consume the same priority/inspection allowance on every frame.
      return queuedMessages.has(message) && (!selecting || !hasSelection(message));
    });
    // Geometry can itself exceed the budget. Start the work budget after it,
    // and always allow progress instead of starving every frame indefinitely.
    var start = performance.now();
    while ((visible.length || queuedOrder.length || foldOrder.length) && attempted < 12 && inspected++ < 24 &&
        (attempted === 0 || performance.now() - start < 6)) {
      var foldOnly = !visible.length && !queuedOrder.length;
      var message = visible.length ? visible.shift() : queuedOrder.length ? queuedOrder.pop() : foldOrder.pop();
      if (!(foldOnly ? deferredFolds : queuedMessages).has(message)) continue;
      if (!message.isConnected) {
        discardEnhancementTarget(message);
        continue;
      }
      // Replacing selected raw text would discard the native selection. Its
      // job stays queued until selectionchange, rather than spinning frames.
      if (hasSelection(message)) continue;
      queuedMessages.delete(message);
      if (enhanceOne(message, foldOnly)) { work = true; attempted++; }
    }
    if (work) {
      if (!selecting) {
        if (atBottom) window.scrollTo(0, document.body.scrollHeight);
        else if (anchor && anchor.isConnected) window.scrollBy(0, anchor.getBoundingClientRect().top - offset);
      }
      window.dispatchEvent(new CustomEvent('codex:conversation-messages-enhanced'));
    }
    if (queuedOrder.length || foldOrder.length) scheduleEnhancements();
  }

  function enhanceMessages(records) {
    var changed = new Set();
    if (records) records.forEach(function (record) {
      record.removedNodes.forEach(function (item) {
        if (item.nodeType !== 1 || item.isConnected) return;
        var removed = item.matches('.native-message') ? [item] : [];
        item.querySelectorAll('.native-message').forEach(function (message) { removed.push(message); });
        removed.forEach(function (message) {
          discardEnhancementTarget(message);
        });
      });
      if (record.type === 'attributes') changed.add(record.target);
      var owner = record.target.nodeType === 1 && record.target.closest('.native-message');
      if (owner) changed.add(owner);
      record.addedNodes.forEach(function (item) {
        if (item.nodeType !== 1) return;
        if (item.matches('.native-message')) changed.add(item);
        item.querySelectorAll('.native-message').forEach(function (message) { changed.add(message); });
      });
    });
    else messages.querySelectorAll('.native-message').forEach(function (message) { changed.add(message); });
    changed.forEach(queueEnhancement);
  }

  function queueEnhancement(message) {
    if (!message.isConnected) return;
    if (visibilityObserver && !observedMessages.has(message)) {
      observedMessages.add(message); visibilityObserver.observe(message);
    }
    if (!queuedMessages.has(message)) { queuedMessages.add(message); queuedOrder.push(message); }
    scheduleEnhancements();
  }

  document.addEventListener('selectionchange', function () {
    queuedMessages.forEach(function (message) { if (!hasSelection(message)) queuedOrder.push(message); });
    deferredFolds.forEach(function (message) { if (!hasSelection(message)) foldOrder.push(message); });
    scheduleEnhancements();
  });

  function showToast(text) {
    if (!toast) return;
    toast.textContent = text; toast.hidden = false; clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 1800);
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return legacyCopy(text); });
    }
    return legacyCopy(text);
  }

  function legacyCopy(text) {
    return new Promise(function (resolve, reject) {
      var area = element('textarea'); area.value = text; area.readOnly = true;
      area.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0'; document.body.appendChild(area);
      area.focus(); area.select(); area.setSelectionRange(0, area.value.length);
      var copied = false; try { copied = document.execCommand('copy'); } catch (error) {}
      area.remove(); copied ? resolve() : reject(new Error('copy failed'));
    });
  }

  function localText(container) {
    if (container.dataset.localKind === 'code') return container.querySelector('code').textContent;
    if (container.dataset.localKind === 'quote') return container.querySelector('blockquote').textContent.trim();
    return tableRows(container).map(function (row) { return row.join('\t'); }).join('\n');
  }

  function tableRows(container) {
    return Array.from(container.querySelectorAll('tr')).map(function (row) {
      return Array.from(row.querySelectorAll('th,td')).map(function (cell) { return cell.textContent.trim(); });
    });
  }

  function markdownTable(container) {
    var rows = tableRows(container); if (!rows.length) return '';
    function escaped(row) { return row.map(function (cell) { return cell.replace(/\|/g, '\\|').replace(/\s*\n\s*/g, ' '); }); }
    var header = escaped(rows[0]);
    return '| ' + header.join(' | ') + ' |\n| ' + header.map(function () { return '---'; }).join(' | ') + ' |' +
      rows.slice(1).map(function (row) { return '\n| ' + escaped(row).join(' | ') + ' |'; }).join('');
  }

  function plainTable(container) {
    var rows = tableRows(container); if (rows.length < 2) return rows.flat().join('\n');
    var headers = rows[0];
    return rows.slice(1).map(function (row) {
      return headers.map(function (header, index) {
        return (header || ('第' + (index + 1) + '列')) + '：' + (row[index] || '');
      }).join('\n');
    }).join('\n\n');
  }

  function quoteValue(container) {
    var text = localText(container);
    if (container.dataset.localKind === 'table') return markdownTable(container);
    if (container.dataset.localKind === 'quote') {
      return text.split('\n').map(function (line) { return '> ' + line; }).join('\n');
    }
    var fence = text.includes('```') ? '````' : '```';
    return fence + (container.dataset.language || '') + '\n' + text + '\n' + fence;
  }

  function putInComposer(text) {
    if (!textarea) return;
    textarea.value = text + '\n\n' + textarea.value;
    textarea.dispatchEvent(new Event('input')); textarea.focus(); showToast('已引用到输入框');
  }

  function ensureCopySheet() {
    var sheet = document.getElementById('native-table-copy-sheet'); if (sheet) return sheet;
    sheet = element('div', 'native-table-copy-sheet'); sheet.id = 'native-table-copy-sheet'; sheet.hidden = true;
    var panel = element('div', 'native-table-copy-panel'); panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true');
    panel.append(element('b', '', '复制格式'));
    var options = element('div', 'native-table-copy-options');
    [['table', '表格'], ['markdown', 'Markdown'], ['plain', '纯文字']].forEach(function (entry) {
      var button = actionButton('', entry[1]); button.dataset.tableCopyFormat = entry[0]; options.appendChild(button);
    });
    panel.append(options, actionButton('native-table-copy-cancel', '取消')); sheet.appendChild(panel); document.body.appendChild(sheet);
    sheet.querySelector('.native-table-copy-cancel').addEventListener('click', function () { sheet.hidden = true; activeTable = null; });
    sheet.addEventListener('click', function (event) {
      if (event.target === sheet) { sheet.hidden = true; activeTable = null; return; }
      var choice = event.target.closest('[data-table-copy-format]'); if (!choice || !activeTable) return;
      var format = choice.dataset.tableCopyFormat;
      var text = format === 'markdown' ? markdownTable(activeTable) : format === 'plain' ? plainTable(activeTable) : localText(activeTable);
      sheet.hidden = true; activeTable = null;
      copyText(text).then(function () { showToast('已复制'); }, function () { showToast('复制失败，请长按选择文字'); });
    });
    return sheet;
  }

  messages.addEventListener('codex:open-message', function (event) {
    var key = event.detail && event.detail.key;
    var message = Array.from(messages.querySelectorAll('[data-message-key]')).find(function (value) {
      return value.dataset.messageKey === key;
    });
    if (!message) return;
    // A deep link/search hit may target a still-raw historical message.
    enhanceOne(message, true);
    var saved = readMessageState(key);
    saved.autoFoldExpanded = true;
    var codes = saved.autoCodeExpanded || {};
    message.querySelectorAll('.native-code-card').forEach(function (card, index) {
      codes[String(index)] = true;
      card.classList.remove('auto-folded', 'folded');
      var fold = card.querySelector('.native-code-fold');
      if (fold && fold.textContent !== '折叠代码') fold.textContent = '折叠代码';
    });
    saved.autoCodeExpanded = codes;
    autoFoldSession[key] = true;
    writeMessageState(key, saved);
    message.classList.remove('is-auto-folded');
    updateAutoFoldControls(message, Array.from(message.querySelectorAll('.native-message-auto-fold-toggle')));
  });

  messages.addEventListener('click', function (event) {
    var messageFold = event.target.closest('[data-auto-fold-action]');
    if (messageFold) {
      var message = messageFold.closest('.native-message');
      var key = message && message.dataset.messageKey;
      if (!message || !key) return;
      var top = messageFold.getBoundingClientRect().top;
      var saved = readMessageState(key);
      var folded = !message.classList.contains('is-auto-folded');
      message.classList.toggle('is-auto-folded', folded);
      saved.autoFoldExpanded = !folded;
      autoFoldSession[key] = !folded;
      writeMessageState(key, saved);
      updateAutoFoldControls(message, Array.from(message.querySelectorAll('.native-message-auto-fold-toggle')));
      // Correct the clicked control's position before the next paint.
      window.scrollBy(0, messageFold.getBoundingClientRect().top - top);
      return;
    }
    if (root.dataset.conversationKind === 'dot') {
      var more = event.target.closest('.native-message-more');
      if (more) {
        var menu = more.closest('.native-bubble').querySelector('.native-message-menu');
        messages.querySelectorAll('.native-message-menu').forEach(function (value) { if (value !== menu) value.hidden = true; });
        menu.hidden = !menu.hidden;
        return;
      }
      var action = event.target.closest('[data-dot-action]');
      if (action) {
        var source = action.closest('.native-message').querySelector('.native-message-body').dataset.messageSource || '';
        if (action.dataset.dotAction === 'copy') {
          copyText(source).then(function () { showToast('已复制全文'); }, function () { showToast('复制失败，请长按选择文字'); });
        } else if (action.dataset.dotAction === 'quote') {
          putInComposer('> ' + source.replace(/\n/g, '\n> '));
        }
        action.closest('.native-message-menu').hidden = true;
        return;
      }
    }
    var quote = event.target.closest('.native-local-quote');
    if (quote) { putInComposer(quoteValue(quote.closest('[data-local-kind]'))); return; }
    var copy = event.target.closest('.native-local-copy');
    if (copy) {
      var container = copy.closest('[data-local-kind]');
      if (container.dataset.localKind === 'table') { activeTable = container; ensureCopySheet().hidden = false; return; }
      copyText(localText(container)).then(function () { showToast('已复制'); }, function () { showToast('复制失败，请长按选择文字'); });
      return;
    }
    var fold = event.target.closest('.native-code-fold');
    if (fold) {
      var card = fold.closest('.native-code-card');
      if (card.classList.contains('auto-folded')) {
        var parentMessage = card.closest('.native-message');
        var state = readMessageState(parentMessage.dataset.messageKey);
        var expandedCodes = state.autoCodeExpanded || {};
        expandedCodes[card.dataset.autoCodeIndex] = true;
        state.autoCodeExpanded = expandedCodes;
        writeMessageState(parentMessage.dataset.messageKey, state);
        card.classList.remove('auto-folded'); fold.textContent = '折叠代码';
      } else if (card.dataset.autoCodeIndex !== undefined) {
        var owner = card.closest('.native-message');
        var ownerState = readMessageState(owner.dataset.messageKey);
        var ownerCodes = ownerState.autoCodeExpanded || {};
        ownerCodes[card.dataset.autoCodeIndex] = false;
        ownerState.autoCodeExpanded = ownerCodes;
        writeMessageState(owner.dataset.messageKey, ownerState);
        card.classList.add('auto-folded'); fold.textContent = '展开代码';
      } else {
        card.classList.toggle('folded');
        fold.textContent = card.classList.contains('folded') ? '展开' : '折叠';
      }
    }
  });

  window.addEventListener('codex:conversation-terminal', function (event) {
    if (!crossToast || !crossTitle || !crossOpen) return;
    var data = event.detail || {}; var id = String(data.conversation_id || ''); if (!id) return;
    if (id === conversationId) return;
    var ending = data.status === 'failed' ? '执行失败' :
      data.status === 'interrupted' ? '已经停止' : '已经完成';
    crossTitle.textContent = '“' + (data.title || '其他对话') + '”' + ending;
    crossOpen.href = route('/conversation/' + encodeURIComponent(id)); crossToast.hidden = false;
    clearTimeout(crossToast._timer); crossToast._timer = setTimeout(function () { crossToast.hidden = true; }, 7000);
  });

  modules.markdown = 'ready';
  // The first enhancement emits a synchronous readiness event. Register the
  // exact-hit opener above before it can consume the core's pending reveal.
  new MutationObserver(enhanceMessages).observe(messages, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-auto-fold-latest'] });
  enhanceMessages();
})();
