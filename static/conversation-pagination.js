(function () {
  'use strict';

  function prefix() {
    var match = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/i);
    return match ? '/' + match[1] : '';
  }

  function route(path) {
    var base = prefix();
    return base && path.indexOf(base + '/') !== 0 ? base + path : path;
  }

  function mergeHistoryGroups(list, fragment) {
    Array.from(fragment.children).forEach(function (group) {
      if (!group.classList || !group.classList.contains('conversation-date-group')) {
        list.appendChild(group);
        return;
      }
      var heading = group.querySelector(':scope > h2');
      var cards = group.querySelector(':scope > .conversation-date-cards');
      var previous = list.lastElementChild;
      var previousHeading = previous && previous.querySelector(':scope > h2');
      var previousCards = previous && previous.querySelector(':scope > .conversation-date-cards');
      if (heading && cards && previous && previous.classList.contains('conversation-date-group')
          && previousHeading && previousCards && previousHeading.textContent === heading.textContent) {
        while (cards.firstChild) previousCards.appendChild(cards.firstChild);
      } else {
        list.appendChild(group);
      }
    });
  }

  function appendMarkup(list, markup, groupDates) {
    var template = document.createElement('template');
    template.innerHTML = markup;
    Array.prototype.forEach.call(template.content.querySelectorAll('a[href^="/"]'), function (link) {
      link.setAttribute('href', route(link.getAttribute('href')));
    });
    if (groupDates) {
      mergeHistoryGroups(list, template.content);
    } else {
      list.appendChild(template.content);
    }
  }

  function initialize(control) {
    if (control.codexLoadMore) return;
    var list = control.previousElementSibling;
    if (!list || !list.classList.contains('history-list')) return;
    var button = control.querySelector('[data-load-more]');
    var status = control.querySelector('[data-pagination-status]');
    if (!button || !status) return;
    var loading = false;
    var pending;
    function load() {
      if (loading) return pending;
      if (list.dataset.refreshing === 'true') return Promise.resolve(false);
      var cursor = control.dataset.cursor;
      if (!cursor) return Promise.resolve(false);
      loading = true;
      list.dataset.paginationBusy = 'true';
      button.disabled = true;
      status.textContent = '正在加载…';
      pending = fetch(route(control.dataset.endpoint) + '?cursor=' + encodeURIComponent(cursor), {
        credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          if (!response.ok) {
            var error = new Error(payload.error || '加载失败，请重试。');
            error.code = payload.code;
            throw error;
          }
          return payload;
        });
      }).then(function (payload) {
        if (typeof payload.html !== 'string') throw new Error('加载失败，请重试。');
        var scrollTop = window.scrollY;
        appendMarkup(list, payload.html, control.dataset.groupDates === 'true');
        list.dataset.loadedPages = String(Number(list.dataset.loadedPages || 1) + 1);
        control.dispatchEvent(new CustomEvent('codex:conversation-page-loaded', {bubbles: true}));
        if (typeof payload.next_cursor === 'string' && payload.next_cursor) {
          control.dataset.cursor = payload.next_cursor;
          status.textContent = '';
          button.disabled = false;
        } else {
          control.remove();
        }
        window.scrollTo(0, scrollTop);
        return true;
      }).catch(function (error) {
        status.textContent = error && error.code === 'auth_required'
          ? '登录已失效，请重新登录后刷新页面。'
          : error && error.code && error.code.indexOf('cursor_') === 0
            ? '分页已失效，请刷新页面后重试。'
            : '加载失败，请重试。';
        button.disabled = false;
        return false;
      }).finally(function () { loading = false; list.dataset.paginationBusy = 'false'; });
      return pending;
    }
    // History can rebuild loaded pages on a cold browser return using the same
    // authenticated loader. No rendered conversation content is persisted.
    control.codexLoadMore = load;
    button.addEventListener('click', load);
  }

  async function fetchPage(endpoint, cursor, signal) {
    var controller = new AbortController();
    var cancel = function () { controller.abort(); };
    if (signal) {
      if (signal.aborted) cancel();
      signal.addEventListener('abort', cancel, {once: true});
    }
    var timeout = setTimeout(cancel, 10000);
    try {
      var response = await fetch(route(endpoint) + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''), {
        credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'},
        signal: controller.signal
      });
      var payload = await response.json().catch(function () { return {}; });
      if (!response.ok || typeof payload.html !== 'string') {
        var error = new Error(payload.error || '列表更新失败，请重试。');
        error.status = payload.reason_code === 'authentication_required' ? 401 : response.status;
        throw error;
      }
      return payload;
    } finally {
      clearTimeout(timeout);
      if (signal) signal.removeEventListener('abort', cancel);
    }
  }

  Array.prototype.forEach.call(document.querySelectorAll('.conversation-history[data-page-endpoint]'), function (list) {
    list.dataset.loadedPages = '1';
    list.codexRefreshList = async function (options) {
      if (list.dataset.refreshing === 'true' || list.dataset.paginationBusy === 'true') return null;
      list.dataset.refreshing = 'true';
      try {
        var pages = Math.max(1, Number(list.dataset.loadedPages || 1));
        var staged = document.createElement('div');
        var cursor = null, payload, loaded = 0;
        do {
          payload = await fetchPage(list.dataset.pageEndpoint, cursor, options.signal);
          if (!options.canCommit()) return null;
          appendMarkup(staged, payload.html, true);
          loaded += 1;
          cursor = payload.next_cursor;
        } while (cursor && loaded < pages);
        var template = document.createElement('template');
        template.innerHTML = payload.pagination_html || '';
        var control = template.content.querySelector('[data-conversation-pagination]');
        if (cursor && (!control || control.dataset.cursor !== cursor)) {
          throw new Error('列表分页响应无效，请重试。');
        }
        // Commit cards, group headings and cursor together. Never append an old
        // snapshot's continuation to a fresh ordering, even after a slow fetch.
        if (!options.canCommit()) return null;
        var oldControl = list.nextElementSibling;
        if (oldControl && oldControl.matches('[data-conversation-pagination]')) oldControl.remove();
        list.replaceChildren.apply(list, Array.from(staged.childNodes));
        if (!list.childNodes.length) {
          var empty = document.createElement('p'); empty.textContent = '还没有对话。';
          list.appendChild(empty);
        }
        if (control) { list.after(control); initialize(control); }
        list.dataset.loadedPages = String(loaded);
        list.dataset.orderVersion = payload.order_version || '';
        if (options.manual) {
          window.scrollTo(0, Math.max(0, window.scrollY + list.getBoundingClientRect().top - 12));
          var firstLink = list.querySelector('a');
          if (firstLink) firstLink.focus({preventScroll: true});
        }
        list.dispatchEvent(new CustomEvent('codex:conversation-list-refreshed', {
          bubbles: true, detail: {pages: loaded, manual: options.manual}
        }));
        return payload;
      } finally {
        list.dataset.refreshing = 'false';
      }
    };
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-conversation-pagination]'), initialize);
})();
