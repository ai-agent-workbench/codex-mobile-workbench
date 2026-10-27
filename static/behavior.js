(function () {
  'use strict';

  function toast(text) {
    var el = document.getElementById('action-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'action-toast';
      el.style.cssText = 'position:fixed;left:50%;bottom:calc(var(--nav-height,56px) + 64px);'
        + 'transform:translateX(-50%);background:rgba(20,24,28,.92);color:#e2e8f0;'
        + 'padding:8px 16px;border-radius:9px;font-size:.85rem;z-index:70;'
        + 'pointer-events:none;max-width:80vw;text-align:center';
      document.body.appendChild(el);
    }
    el.textContent = text;
    window.setTimeout(function () { el.remove(); }, 1800);
  }

  document.addEventListener('submit', function (event) {
    // A composer may cancel the native submit for asynchronous attachment
    // validation. Only the later, uncancelled submit is actually sending.
    if (event.defaultPrevented) return;
    var form = event.target;
    var action = form && form.getAttribute('action');
    if (form && form.method.toLowerCase() === 'post'
        && action && new URL(action, window.location.href).pathname === '/logout') {
      event.preventDefault();
      var logoutButton = form.querySelector('button[type="submit"],button:not([type])');
      if (logoutButton) logoutButton.disabled = true;
      fetch('/logout', {
        method: 'POST', credentials: 'same-origin', redirect: 'follow'
      }).then(function (response) {
        if (!response.ok) throw new Error('logout rejected');
        window.location.assign('/login');
      }).catch(function () {
        if (logoutButton) logoutButton.disabled = false;
        toast('退出失败，请重试');
      });
      return;
    }
    if (!form || !/(^|-)send$/.test(form.id || '')) return;
    var button = document.querySelector('.send-button[form="' + form.id + '"]')
      || form.querySelector('.send-button');
    if (button) {
      button.disabled = true;
      button.textContent = '发送中…';
    }
  });

  function activeHref(path) {
    path = path.replace(/^\/[0-9a-f]{40,}(?=\/)/, '');
    if (path.length > 1) path = path.replace(/\/+$/, '');
    if (path === '/' || path === '/home' || path === '/quick') return '/home';
    if (path === '/projects/history' || path === '/history'
        || path.indexOf('/conversation/') === 0
        || path === '/settings/dot' || path.indexOf('/settings/dot/') === 0) return '/projects/history';
    if (path === '/projects' || path.indexOf('/project/') === 0) return '/projects';
    if (path.indexOf('/files') === 0 || path === '/inbox' || path.indexOf('/edit') === 0
        || path.indexOf('/move') === 0 || path.indexOf('/delete') === 0) return '/files';
    if (path === '/settings' || path.startsWith('/settings/') || path === '/notifications') return '/settings';
    return '';
  }

  var current = activeHref(window.location.pathname);
  document.querySelectorAll('.nav a').forEach(function (link) {
    var href = link.getAttribute('href') || '';
    var matches = current === '/files' ? href.indexOf('/files') === 0 : href === current;
    if (matches) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
})();
