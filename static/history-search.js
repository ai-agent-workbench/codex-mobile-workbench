(function () {
  'use strict';

  var root = document.querySelector('[data-history-search]');
  if (!root) return;
  var form = root.querySelector('[data-search-form]');
  var input = root.querySelector('[data-history-search-input]');
  var panel = root.querySelector('[data-search-panel]');
  var results = root.querySelector('[data-search-results]');
  var status = root.querySelector('[data-search-status]');
  var more = root.querySelector('[data-search-more]');
  var browse = document.querySelector('[data-history-browse]');
  if (!form || !input || !panel || !results || !status || !more || !browse) return;
  var prefixMatch = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/i);
  var prefix = prefixMatch ? '/' + prefixMatch[1] : '';
  var generation = 0;
  var controller = null;
  var currentQuery = root.dataset.searchQuery || '';
  var disclosure = root.querySelector('[data-search-disclosure]');
  var toggle = root.querySelector('[data-search-toggle]');
  var browsePages = 1;
  var searchPages = 1;
  var browseScroll = 0;
  var restoring = false;
  var restoreGeneration = 0;
  var userMoved = false;
  var browseTargetPages = 1;
  var browseRecovery = null;
  var pendingBrowseScroll = null;
  var recoveryView = null;

  function normalizeQuery(query) {
    // Match Python str.strip(), including literal U+FEFF and Python-only spaces.
    return query.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g, '');
  }

  function savedView(state) {
    var view = state && state.codexHistoryView;
    return view && view.path === location.pathname && view.version === 1 ? view : null;
  }

  function saveView() {
    if (!disclosure || restoring) return;
    var target = recoveryView && recoveryView.query === currentQuery ? recoveryView : null;
    if (!currentQuery && !disclosure.open && !pendingBrowseScroll && !target) browseScroll = window.scrollY;
    // Only navigation metadata belongs in this entry; never store card HTML or
    // conversation text in browser storage. Fresh API cursors rebuild cold returns.
    var view = {version: 1, path: location.pathname, query: currentQuery,
      open: disclosure.open, browsePages: Math.max(browsePages, browseTargetPages),
      searchPages: Math.max(searchPages, target ? pageCount(target.searchPages) : 1),
      browseScroll: browseScroll, scroll: pendingBrowseScroll ? pendingBrowseScroll.y
        : target && Number.isFinite(target.scroll) ? target.scroll : window.scrollY};
    try {
      history.replaceState(Object.assign({}, history.state, {codexHistoryView: view}), '', location.href);
    } catch (_) { /* The page still works when history state is unavailable. */ }
  }

  function stopRestoring() {
    restoreGeneration += 1;
    restoring = false;
    pendingBrowseScroll = null;
    root.dataset.navigationRestoring = 'false';
  }

  function pageCount(value) {
    return Number.isInteger(value) && value > 0 && value <= 1000 ? value : 1;
  }

  function recoverBrowsePages(target) {
    browseTargetPages = Math.max(browseTargetPages, pageCount(target));
    if (!browseRecovery) {
      // This job owns ordinary pagination, independently of search-mode changes.
      // Cancelling search must not strand the still-hidden ordinary list.
      browseRecovery = (async function () {
        while (browsePages < browseTargetPages) {
          var control = browse.querySelector('[data-conversation-pagination]');
          if (!control || !control.codexLoadMore || !(await control.codexLoadMore())) break;
        }
      })().finally(function () {
        browseRecovery = null;
        if (!restoring && recoveryView && !recoveryView.query && browsePages >= browseTargetPages) {
          recoveryView = null;
          saveView();
        }
      });
    }
    return browseRecovery;
  }

  function returnToBrowse() {
    var target = {y: browseScroll};
    pendingBrowseScroll = target;
    window.scrollTo(0, target.y);
    recoverBrowsePages(browseTargetPages).then(function () {
      if (pendingBrowseScroll === target && !currentQuery && !disclosure.open) {
        window.scrollTo(0, target.y);
        pendingBrowseScroll = null;
        saveView();
      }
    });
  }

  function route(path) {
    return prefix && path.indexOf(prefix + '/') !== 0 ? prefix + path : path;
  }

  // A cold return starts with server-rendered results rather than fetched HTML.
  results.querySelectorAll('a[href^="/"]').forEach(function (link) {
    link.setAttribute('href', route(link.getAttribute('href')));
  });

  function updateAddress(query) {
    var url = new URL(location.href);
    if (query) url.searchParams.set('q', query);
    else url.searchParams.delete('q');
    url.searchParams.delete('search_cursor');
    if (url.href !== location.href) {
      saveView();
      history.pushState(null, '', url.href);
    }
  }

  function summary() {
    var count = results.querySelectorAll('.history-search-card').length;
    return count ? '已显示 ' + count + ' 条匹配对话' : '没有找到匹配的对话';
  }

  function cancelManagement() {
    if (!document.body.classList.contains('managing')) return;
    var cancel = browse.querySelector('.manage-bar [data-cancel]');
    if (cancel) cancel.click();
    else document.body.classList.remove('managing');
  }

  function run(query, append, changeAddress) {
    query = normalizeQuery(query);
    if (!append) input.value = query;
    if (controller) controller.abort();
    generation += 1;
    var requestGeneration = generation;
    if (changeAddress) updateAddress(query);
    // Save the departing entry with its full recovery target before starting a
    // deliberate new query (including Cancel). Partial DOM is not that target.
    if (!append && changeAddress) recoveryView = null;
    currentQuery = query;
    if (!append) searchPages = 1;
    if (disclosure && query) disclosure.open = true;
    if (!query) {
      panel.hidden = true; browse.hidden = false;
      results.replaceChildren(); more.hidden = true; more.dataset.cursor = '';
      status.textContent = ''; root.dataset.searchBusy = 'false'; more.disabled = false;
      controller = null;
      return Promise.resolve(true);
    }
    cancelManagement();
    panel.hidden = false; browse.hidden = true;
    if (!append) {
      results.replaceChildren(); more.hidden = true; more.dataset.cursor = '';
    }
    status.textContent = append ? '正在加载…' : '正在搜索…';
    root.dataset.searchBusy = 'true'; more.disabled = true;
    var url = new URL(route(root.dataset.searchEndpoint), location.origin);
    url.searchParams.set('q', query);
    if (append && more.dataset.cursor) url.searchParams.set('cursor', more.dataset.cursor);
    controller = new AbortController();
    var requestController = controller;
    var timeout = setTimeout(function () { requestController.abort(); }, 15000);
    return fetch(url.href, {
      credentials: 'same-origin', cache: 'no-store',
      headers: {'Accept': 'application/json'}, signal: requestController.signal
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          var error = new Error(payload.error || '搜索失败，请重试。');
          error.code = payload.code || payload.reason_code;
          throw error;
        }
        return payload;
      });
    }).then(function (payload) {
      if (requestGeneration !== generation) return;
      if (typeof payload.html !== 'string') throw new Error('搜索失败，请重试。');
      var template = document.createElement('template');
      template.innerHTML = payload.html;
      template.content.querySelectorAll('a[href^="/"]').forEach(function (link) {
        link.setAttribute('href', route(link.getAttribute('href')));
      });
      if (!append) results.replaceChildren();
      results.appendChild(template.content);
      more.dataset.cursor = typeof payload.next_cursor === 'string' ? payload.next_cursor : '';
      more.hidden = !more.dataset.cursor;
      var nextPage = new URL(route(form.getAttribute('action')), location.origin);
      nextPage.searchParams.set('q', query);
      nextPage.searchParams.set('search_cursor', more.dataset.cursor);
      more.href = nextPage.href;
      status.textContent = summary();
      if (append) searchPages += 1;
      saveView();
      return true;
    }).catch(function (error) {
      if (requestGeneration !== generation) return;
      if (error.code === 'auth_required' || error.code === 'authentication_required') {
        status.textContent = '登录已失效，请重新登录后再搜索。';
        more.hidden = true;
      } else if (error.code && error.code.indexOf('cursor_') === 0) {
        status.textContent = '搜索结果已过期，请重新搜索。';
        more.hidden = true;
      } else {
        status.textContent = error.code === 'invalid_query' ? error.message :
          append ? '加载失败，请重试。' : '搜索失败，请重试。';
      }
      return false;
    }).finally(function () {
      clearTimeout(timeout);
      if (requestGeneration !== generation) return;
      controller = null; more.disabled = false; root.dataset.searchBusy = 'false';
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault(); stopRestoring(); input.blur(); run(input.value, false, true);
  });
  root.querySelector('[data-search-clear]').addEventListener('click', function (event) {
    event.preventDefault(); stopRestoring(); input.blur(); input.value = '';
    run('', false, true);
    if (disclosure) {
      disclosure.open = false;
      toggle.focus({preventScroll: true});
      returnToBrowse();
      saveView();
    }
  });
  input.addEventListener('search', function () {
    if (!input.value) { stopRestoring(); run('', false, true); saveView(); }
  });
  more.addEventListener('click', function (event) {
    event.preventDefault();
    if (!more.disabled && more.dataset.cursor) {
      stopRestoring(); run(currentQuery, true, false);
    }
  });
  async function restoreView(view) {
    var query = normalizeQuery(new URL(location.href).searchParams.get('q') || '');
    if (restoring && currentQuery === query) return;
    var ticket = ++restoreGeneration;
    restoring = true; userMoved = false;
    root.dataset.navigationRestoring = 'true';
    if (!view || normalizeQuery(view.query || '') !== query) view = {};
    recoveryView = Object.assign({}, view, {query: query});
    browseScroll = Number.isFinite(view.browseScroll) ? view.browseScroll : 0;
    disclosure.open = !!query || !!view.open;
    try {
      var ordinaryRecovery = recoverBrowsePages(view.browsePages);
      // Same-document back/forward can revisit a different query. BFCache does
      // not pass through this path: its live DOM and pagination remain intact.
      if (query !== currentQuery) {
        if (!(await run(query, false, false))) return;
      }
      await ordinaryRecovery;
      while (query && searchPages < pageCount(view.searchPages) && more.dataset.cursor
          && ticket === restoreGeneration) {
        if (!(await run(query, true, false))) break;
      }
    } finally {
      if (ticket === restoreGeneration) {
        if (!userMoved && Number.isFinite(view.scroll)) window.scrollTo(0, view.scroll);
        if (browsePages >= pageCount(view.browsePages) && searchPages >= pageCount(view.searchPages)) recoveryView = null;
        restoring = false;
        root.dataset.navigationRestoring = 'false';
        saveView();
      }
    }
  }

  window.addEventListener('popstate', function (event) {
    if (disclosure) { restoreView(savedView(event.state)); return; }
    var query = new URL(location.href).searchParams.get('q') || '';
    input.value = query; run(query, false, false);
  });
  window.addEventListener('pageshow', function (event) {
    if (!disclosure) {
      if (event.persisted && currentQuery) run(currentQuery, false, false);
      return;
    }
    var navigation = performance.getEntriesByType('navigation')[0];
    if (!event.persisted && navigation && navigation.type === 'back_forward') {
      restoreView(savedView(history.state));
    } else saveView();
  });

  if (disclosure) {
    toggle.addEventListener('click', function (event) {
      event.preventDefault(); stopRestoring();
      if (!currentQuery && !recoveryView) browseScroll = window.scrollY;
      cancelManagement();
      disclosure.open = true;
      window.scrollTo(0, 0);
      input.focus({preventScroll: true});
      saveView();
    });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault(); root.querySelector('[data-search-clear]').click();
      }
    });
    browse.addEventListener('codex:conversation-page-loaded', function () {
      browsePages += 1; saveView();
    });
    browse.addEventListener('codex:conversation-list-refreshed', function (event) {
      browsePages = pageCount(event.detail.pages);
      browseTargetPages = browsePages;
      recoveryView = null;
      browseScroll = window.scrollY;
      saveView();
    });
    window.addEventListener('pagehide', saveView);
    var scrollTimer;
    window.addEventListener('scroll', function () {
      clearTimeout(scrollTimer); scrollTimer = setTimeout(saveView, 120);
    }, {passive: true});
    function markUserMoved() {
      if (restoring) userMoved = true;
      pendingBrowseScroll = null;
    }
    window.addEventListener('wheel', markUserMoved, {passive: true});
    window.addEventListener('touchstart', markUserMoved, {passive: true});
    window.addEventListener('pointerdown', markUserMoved, {passive: true});
    window.addEventListener('keydown', function (event) {
      if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].indexOf(event.key) !== -1) markUserMoved();
    });
  }
})();
