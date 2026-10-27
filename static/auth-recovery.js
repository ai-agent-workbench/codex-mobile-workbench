(function () {
  'use strict';

  if (!document.querySelector('.nav') && !document.getElementById('native-conversation')) return;

  var banner;
  var running = false;
  var lastCheck = 0;
  var hiddenForTab = false;

  function loginUrl() {
    var target = location.pathname + location.search;
    // Match the server's strict conversation/turn redirect allowlist.
    if (!/^\/conversation\/[A-Za-z0-9][A-Za-z0-9._:-]{0,159}(?:\?turn=[A-Za-z0-9][A-Za-z0-9._:-]{0,159})?$/.test(target)) return '/login';
    return '/login?return_to=' + encodeURIComponent(target);
  }

  function show(mode) {
    if (!banner) {
      banner = document.createElement('aside');
      banner.id = 'workbench-auth-recovery';
      banner.setAttribute('role', 'status');
      banner.style.cssText = 'position:fixed;left:12px;right:12px;top:calc(12px + env(safe-area-inset-top));z-index:20000;max-width:560px;margin:auto;padding:14px 16px;border:1px solid #59ddc1;border-radius:12px;background:#171d29;color:#f2f4f7;box-shadow:0 8px 32px #0008;font:15px/1.5 system-ui;';
      var message = document.createElement('p');
      message.style.margin = '0 0 10px';
      banner.appendChild(message);
      var link = document.createElement('a');
      link.style.cssText = 'display:inline-block;padding:9px 13px;border-radius:8px;background:#59ddc1;color:#08282a;font-weight:700;text-decoration:none';
      link.addEventListener('click', function (event) {
        var composer = document.querySelector('[data-draft-key]');
        if (!composer || !composer.value) return;
        try {
          localStorage.setItem(composer.dataset.draftKey, composer.value);
          if (localStorage.getItem(composer.dataset.draftKey) !== composer.value) throw new Error('draft_not_saved');
        } catch (error) {
          if (!window.confirm('浏览器无法保存当前草稿。请先取消并复制输入内容；已备份后再确认离开登录。')) event.preventDefault();
        }
      });
      banner.appendChild(link);
      var dismiss = document.createElement('button');
      dismiss.type = 'button';
      dismiss.textContent = '稍后';
      dismiss.style.cssText = 'padding:9px 13px;margin-left:12px;border:1px solid #59616a;border-radius:8px;background:transparent;color:#f2f4f7;font:inherit';
      dismiss.addEventListener('click', function () { hiddenForTab = true; banner.hidden = true; });
      banner.appendChild(dismiss);
      document.body.appendChild(banner);
    }
    var required = mode === 'none';
    if (required) hiddenForTab = false;
    banner.querySelector('p').textContent = required ?
      '登录已失效。输入内容仍保留，请用指纹重新登录后继续。' :
      '当前使用旧应急登录。切换指纹登录后，日常打开会沿用长期会话。';
    var link = banner.querySelector('a');
    link.href = loginUrl();
    link.textContent = required ? '指纹重新登录' : '切换指纹登录';
    banner.hidden = hiddenForTab;
  }

  window.addEventListener('codex:auth-required', function () {
    window.codexAuthenticationRequired = true;
    show('none');
  });

  window.codexCheckLogin = function () {
    if (document.visibilityState === 'hidden' || running || Date.now() - lastCheck < 15000) return;
    lastCheck = Date.now();
    running = true;
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 8000);
    return fetch('/login/status', {
      credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}, signal: controller.signal
    }).then(function (response) {
      if (!response.ok || response.redirected || !(response.headers.get('Content-Type') || '').includes('application/json')) return null;
      return response.json();
    }).then(function (status) {
      if (!status || !['passkey', 'legacy', 'none'].includes(status.method)) return;
      if (!status.authenticated) {
        window.dispatchEvent(new CustomEvent('codex:auth-required'));
      } else if (status.method === 'legacy') {
        window.codexAuthenticationRequired = false;
        if (status.passkey_enabled) show('legacy');
      } else {
        window.codexAuthenticationRequired = false;
        if (banner) banner.hidden = true;
      }
    }).catch(function () {
      // A timeout/network failure is not evidence that login expired.
    }).finally(function () { clearTimeout(timeout); running = false; });
  };
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') window.codexCheckLogin();
  });
  window.addEventListener('pageshow', function () { window.codexCheckLogin(); });
  window.codexCheckLogin();
})();
