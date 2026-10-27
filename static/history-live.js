(function () {
  'use strict';

  var list = document.querySelector('.conversation-history[data-status-endpoint]');
  if (!list) return;

  var POLL_MS = 25000;
  var REQUEST_TIMEOUT_MS = 10000;
  var MIN_REQUEST_INTERVAL_MS = 1000;
  var MAX_IDS = 100;
  var STATE_CLASSES = ['running', 'needs', 'complete', 'unread', 'failed', 'stopped'];
  var timer = null;
  var scheduledAt = 0;
  var requestAbort = null;
  var fetching = false;
  var queued = false;
  var stopped = false;
  var pageHidden = false;
  var nextAllowedAt = 0;
  var failures = 0;
  var observer = null;
  // Paging/reordering replaces DOM nodes. Keep title versions outside those
  // nodes so an older response cannot undo a newer websocket title.
  var titleVersions = Object.create(null);
  var pendingOrder = false;
  var terminalUpdate = false;
  var reordering = false;
  var reorderAbort = null;
  var orderRetryAt = 0;
  var interactionEpoch = 0;
  var lastInteraction = 0;
  var pointerHeld = false;
  var touching = false;
  var guardTimer = null;
  var orderError = false;
  var browse = list.closest('[data-history-browse]');
  var search = document.querySelector('[data-history-search]');
  var notice = document.createElement('div');
  notice.className = 'history-update-notice';
  notice.hidden = true;
  notice.setAttribute('role', 'status');
  notice.setAttribute('aria-live', 'polite');
  var updateButton = document.createElement('button');
  updateButton.type = 'button';
  updateButton.setAttribute('data-history-update', '');
  notice.appendChild(updateButton);
  list.before(notice);

  function prefix() {
    var match = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/i);
    return match ? '/' + match[1] : '';
  }

  function route(path) {
    var base = prefix();
    return base && path.indexOf(base + '/') !== 0 ? base + path : path;
  }

  function isVisible() {
    return document.visibilityState === 'visible';
  }

  function suspended() {
    return pageHidden || !isVisible();
  }

  function protectedMode() {
    var input = search && search.querySelector('[data-history-search-input]');
    var disclosure = search && search.querySelector('[data-search-disclosure]');
    return (browse && browse.hidden) || (input && input.value.trim()) ||
      (disclosure && disclosure.open) ||
      (search && search.dataset.navigationRestoring === 'true') ||
      document.body.classList.contains('managing') || !!list.querySelector('.manage-check:checked') ||
      !!document.querySelector('dialog[open]');
  }

  function blocked() {
    var focused = document.activeElement;
    var selection = window.getSelection();
    return protectedMode() || list.dataset.paginationBusy === 'true' ||
      (selection && !selection.isCollapsed && list.contains(selection.anchorNode)) ||
      (focused && (list.contains(focused) || focused.matches('input,textarea,select,[contenteditable="true"]')));
  }

  function atTop() {
    // Project pages contain a composer above the list. Use the list's own top,
    // not scrollY === 0; offscreen lists may update without moving the composer.
    return list.getBoundingClientRect().top >= -8;
  }

  function renderNotice() {
    notice.hidden = !pendingOrder || stopped || suspended() || protectedMode();
    updateButton.disabled = reordering || fetching;
    var label = reordering ? '正在更新…' : orderError ? '更新失败，点此重试' :
      terminalUpdate ? '有新完成的对话，点此更新' : '对话列表有更新，点此更新';
    if (updateButton.textContent !== label) updateButton.textContent = label;
  }

  function considerOrder() {
    clearTimeout(guardTimer);
    renderNotice();
    if (!pendingOrder || reordering || fetching || stopped || suspended() || blocked() || !atTop()) return;
    var wait = Math.max(orderRetryAt - Date.now(), 900 - (Date.now() - lastInteraction));
    if (pointerHeld || touching) return;
    if (wait > 0) { guardTimer = setTimeout(considerOrder, wait); return; }
    refreshOrder(false);
  }

  function checkOrder(version) {
    if (typeof version !== 'string' || !version) return;
    if (!list.dataset.orderVersion) list.dataset.orderVersion = version;
    pendingOrder = version !== list.dataset.orderVersion;
    if (!pendingOrder) { terminalUpdate = false; orderError = false; }
    renderNotice();
  }

  function refreshOrder(manual) {
    function unsafe() {
      return manual ? protectedMode() || list.dataset.paginationBusy === 'true' : blocked();
    }
    if (fetching || reordering || stopped || suspended() || pointerHeld || touching || unsafe() || !list.codexRefreshList) return;
    var epoch = interactionEpoch;
    reordering = true;
    orderError = false;
    reorderAbort = new AbortController();
    renderNotice();
    list.codexRefreshList({
      manual: manual, signal: reorderAbort.signal,
      canCommit: function () {
        return !stopped && !suspended() && !unsafe() && !pointerHeld && !touching &&
          epoch === interactionEpoch && (manual || atTop());
      }
    }).then(function (payload) {
      if (payload) { pendingOrder = false; terminalUpdate = false; orderRetryAt = 0; }
    }).catch(function (error) {
      if (suspended() || (error && error.name === 'AbortError' && reorderAbort.signal.aborted)) return;
      if (error && (error.status === 401 || error.status === 403)) {
        stopped = true;
        if (error.status === 401) window.dispatchEvent(new CustomEvent('codex:auth-required'));
      } else {
        orderError = true;
        orderRetryAt = Date.now() + 5000;
      }
    }).finally(function () {
      reordering = false;
      reorderAbort = null;
      renderNotice();
      if (!stopped && !suspended()) {
        schedule(queued ? 0 : POLL_MS);
        queued = false;
        considerOrder();
      }
    });
  }

  updateButton.addEventListener('click', function () { refreshOrder(true); });

  function interaction() {
    interactionEpoch += 1;
    lastInteraction = Date.now();
    considerOrder();
  }
  ['scroll', 'wheel', 'input', 'change', 'keydown'].forEach(function (name) {
    window.addEventListener(name, interaction, {passive: true});
  });
  window.addEventListener('pointerdown', function () { pointerHeld = true; interaction(); }, {passive: true});
  ['pointerup', 'pointercancel'].forEach(function (name) {
    window.addEventListener(name, function () { pointerHeld = false; interaction(); }, {passive: true});
  });
  // Native touch scrolling cancels Pointer Events before the finger lifts.
  // Track the actual touch stream independently, including multiple fingers.
  ['touchstart', 'touchmove', 'touchend', 'touchcancel'].forEach(function (name) {
    window.addEventListener(name, function (event) {
      touching = event.touches.length > 0;
      interaction();
    }, {passive: true});
  });
  document.addEventListener('focusout', function () { setTimeout(considerOrder, 0); });
  document.addEventListener('close', considerOrder, true);
  document.addEventListener('selectionchange', considerOrder);
  var guardObserver = new MutationObserver(considerOrder);
  guardObserver.observe(document.body, {attributes: true, attributeFilter: ['class']});
  if (browse) guardObserver.observe(browse, {attributes: true, attributeFilter: ['hidden']});
  if (search) {
    guardObserver.observe(search, {attributes: true, attributeFilter: ['data-navigation-restoring']});
    var disclosure = search.querySelector('[data-search-disclosure]');
    if (disclosure) guardObserver.observe(disclosure, {attributes: true, attributeFilter: ['open']});
  }

  function retryDelay() {
    return Math.min(60000, 2000 * Math.pow(2, failures - 1));
  }

  function cards() {
    return Array.prototype.slice.call(
      list.querySelectorAll('.manage-card[data-id]')
    );
  }

  function cardFor(id) {
    var all = cards();
    for (var index = 0; index < all.length; index += 1) {
      if (all[index].getAttribute('data-id') === id) return all[index];
    }
    return null;
  }

  function cardIds() {
    var seen = Object.create(null);
    return cards().map(function (card) {
      return card.getAttribute('data-id') || '';
    }).filter(function (id) {
      if (!id || seen[id]) return false;
      seen[id] = true;
      return true;
    });
  }

  function updateTitle(target, title, revision) {
    var link = target.querySelector('a[data-conversation-state]');
    var label = target.querySelector('.conversation-card-copy b');
    if (!link || !label || typeof title !== 'string') return;
    var incoming = Number(revision);
    var known = Number(link.dataset.titleRevision || link.dataset.conversationRevision || 0);
    if (!Number.isSafeInteger(incoming) || incoming < 1) return;
    var id = target.getAttribute('data-id');
    var cached = titleVersions[id];
    if ((!cached || incoming >= cached.revision) && incoming >= known) {
      cached = titleVersions[id] = {title: title, revision: incoming};
    }
    if (!cached || cached.revision < known) return;
    if (label.textContent !== cached.title) label.textContent = cached.title;
    link.dataset.titleRevision = String(cached.revision);
  }

  function updateCard(target, source) {
    var sourceLink = source.querySelector('a[data-conversation-state]');
    var targetLink = target.querySelector('a[data-conversation-state]');
    var sourceMeta = source.querySelector('.conversation-card-copy small');
    var targetMeta = target.querySelector('.conversation-card-copy small');
    var sourceStatus = source.querySelector('.conversation-card-status');
    var targetStatus = target.querySelector('.conversation-card-status');
    if (!sourceLink || !targetLink || !sourceMeta || !targetMeta ||
        !sourceStatus || !targetStatus) return;
    var sourceTitle = source.querySelector('.conversation-card-copy b');
    if (sourceTitle) updateTitle(target, sourceTitle.textContent,
      sourceLink.getAttribute('data-conversation-revision'));

    STATE_CLASSES.forEach(function (name) { target.classList.remove(name); });
    STATE_CLASSES.forEach(function (name) {
      if (source.classList.contains(name)) target.classList.add(name);
    });
    targetLink.setAttribute(
      'data-conversation-state', sourceLink.getAttribute('data-conversation-state') || ''
    );
    targetLink.setAttribute(
      'data-conversation-revision', sourceLink.getAttribute('data-conversation-revision') || '0'
    );
    targetMeta.textContent = sourceMeta.textContent;
    // Keep the label, checkbox and link nodes in place so management state,
    // long-press handling and keyboard focus are never reset by a live update.
    targetStatus.innerHTML = sourceStatus.innerHTML;
  }

  function apply(markup) {
    if (typeof markup !== 'string') return;
    // Changing a chip can alter a card's height. Hold the first visible card
    // at the same viewport position rather than making a reading user jump.
    var anchor = cards().find(function (card) {
      return card.getBoundingClientRect().bottom > 0;
    });
    var anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
    var template = document.createElement('template');
    template.innerHTML = markup;
    Array.prototype.forEach.call(
      template.content.querySelectorAll('.manage-card[data-id]'),
      function (source) {
        var target = cardFor(source.getAttribute('data-id') || '');
        if (target) updateCard(target, source);
      }
    );
    if (anchor && anchor.isConnected) {
      var delta = anchor.getBoundingClientRect().top - anchorTop;
      if (delta) window.scrollBy(0, delta);
    }
  }

  function statusUrl(ids, check) {
    var query = ids.map(function (id) {
      return 'ids=' + encodeURIComponent(id);
    });
    if (check) query.push('check_order=true');
    return route(list.getAttribute('data-status-endpoint') || '') + '?' + query.join('&');
  }

  function schedule(delay) {
    if (stopped || suspended()) return;
    if (fetching || reordering) {
      queued = true;
      return;
    }
    var now = Date.now();
    var runAt = Math.max(now + Math.max(0, delay), nextAllowedAt);
    // Keep the earliest pending refresh. A token/event burst therefore cannot
    // postpone it indefinitely, nor bypass a failed-request backoff.
    if (timer && scheduledAt <= runAt) return;
    clearTimeout(timer);
    scheduledAt = runAt;
    timer = setTimeout(refresh, Math.max(0, runAt - now));
  }

  function finish() {
    fetching = false;
    requestAbort = null;
    if (stopped || suspended()) return;
    considerOrder();
    if (reordering) return;
    if (queued) {
      queued = false;
      schedule(0);
      return;
    }
    schedule(failures ? retryDelay() : POLL_MS);
  }

  function refresh() {
    timer = null;
    scheduledAt = 0;
    if (stopped || suspended() || fetching || reordering) return;
    var ids = cardIds();
    if (!ids.length && !list.dataset.pageEndpoint) {
      schedule(POLL_MS);
      return;
    }
    fetching = true;
    nextAllowedAt = Date.now() + MIN_REQUEST_INTERVAL_MS;
    var batches = [];
    for (var offset = 0; offset < ids.length; offset += MAX_IDS) {
      batches.push(ids.slice(offset, offset + MAX_IDS));
    }
    if (!batches.length) batches.push([]);
    var index = 0;

    function next() {
      if (stopped || suspended() || index >= batches.length) {
        if (!stopped && !suspended() && index >= batches.length) failures = 0;
        finish();
        return;
      }
      var controller = new AbortController();
      var activeRequest = {controller: controller, cancelled: false};
      var timeout = setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
      requestAbort = activeRequest;
      fetch(statusUrl(batches[index], index === 0 && !!list.dataset.pageEndpoint), {
        credentials: 'same-origin', cache: 'no-store',
        headers: {'Accept': 'application/json'}, signal: controller.signal
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          if (!response.ok) {
            var error = new Error(payload.error || '状态更新失败。');
            error.status = payload.reason_code === 'authentication_required' ? 401 : response.status;
            throw error;
          }
          if (!payload || typeof payload.html !== 'string') {
            throw new Error('状态更新响应无效。');
          }
          return payload;
        });
      }).then(function (payload) {
        clearTimeout(timeout);
        if (!stopped && !suspended()) {
          apply(payload.html);
          checkOrder(payload.order_version);
        }
        index += 1;
        next();
      }).catch(function (error) {
        clearTimeout(timeout);
        if (activeRequest.cancelled || stopped || suspended()) {
          finish();
          return;
        }
        if (error && (error.status === 401 || error.status === 403)) {
          stopped = true;
          if (error.status === 401) window.dispatchEvent(new CustomEvent('codex:auth-required'));
        } else {
          // A timeout is an aborted fetch too, but it is a retryable failure;
          // only page hiding cancels quietly.
          failures += 1;
          nextAllowedAt = Date.now() + retryDelay();
        }
        finish();
      });
    }
    next();
  }

  function refreshForEvent(delay) {
    schedule(typeof delay === 'number' ? delay : 80);
  }

  function recoverNetwork() {
    if (stopped) return;
    failures = 0;
    nextAllowedAt = 0;
    refreshForEvent(0);
  }

  window.addEventListener('codex:conversation-terminal', function (event) {
    var id = String((event.detail || {}).conversation_id || '');
    if (id) { terminalUpdate = true; refreshForEvent(0); }
  });
  window.addEventListener('codex:conversation-updated', function (event) {
    var data = event.detail || {};
    var id = String(data.conversation_id || '');
    var state = typeof data.state === 'string' ? data.state : '';
    var card = id && cardFor(id);
    var link = card && card.querySelector('a[data-conversation-state]');
    if (card) updateTitle(card, data.title, data.revision);
    if (link && state && link.getAttribute('data-conversation-state') !== state) {
      refreshForEvent();
    }
  });
  window.addEventListener('codex:connection-restored', recoverNetwork);
  window.addEventListener('online', recoverNetwork);
  window.addEventListener('pageshow', function () {
    pageHidden = false;
    recoverNetwork();
  });
  window.addEventListener('pagehide', function () {
    pageHidden = true;
    pointerHeld = false;
    touching = false;
    interactionEpoch += 1;
    clearTimeout(timer);
    timer = null;
    scheduledAt = 0;
    queued = false;
    clearTimeout(guardTimer);
    if (reorderAbort) reorderAbort.abort();
    renderNotice();
    if (requestAbort) {
      requestAbort.cancelled = true;
      requestAbort.controller.abort();
    }
  });
  document.addEventListener('visibilitychange', function () {
    if (isVisible()) recoverNetwork();
    else {
      pointerHeld = false;
      touching = false;
      interactionEpoch += 1;
      clearTimeout(timer);
      timer = null;
      scheduledAt = 0;
      queued = false;
      clearTimeout(guardTimer);
      if (reorderAbort) reorderAbort.abort();
      renderNotice();
      if (requestAbort) {
        requestAbort.cancelled = true;
        requestAbort.controller.abort();
      }
    }
  });

  observer = new MutationObserver(function (records) {
    var addedCards = false;
    for (var index = 0; index < records.length; index += 1) {
      var nodes = records[index].addedNodes;
      for (var nodeIndex = 0; nodeIndex < nodes.length; nodeIndex += 1) {
        var node = nodes[nodeIndex];
        if (node.nodeType === 1 && (node.matches('.manage-card[data-id]') ||
            node.querySelector('.manage-card[data-id]'))) {
          var incomingCards = node.matches('.manage-card[data-id]') ? [node] :
            Array.prototype.slice.call(node.querySelectorAll('.manage-card[data-id]'));
          incomingCards.forEach(function (card) {
            var link = card.querySelector('a[data-conversation-state]');
            var title = card.querySelector('.conversation-card-copy b');
            if (link && title) updateTitle(card, title.textContent, link.dataset.conversationRevision);
          });
          addedCards = true;
        }
      }
    }
    if (addedCards) refreshForEvent(80);
  });
  observer.observe(list, {childList: true, subtree: true});
  refreshForEvent(0);
})();
