(function () {
  'use strict';
  var button = document.getElementById('web-push-toggle');
  var status = document.getElementById('web-push-status');
  if (!button || !status) return;
  var prefixMatch = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
  var appPrefix = prefixMatch ? '/' + prefixMatch[1] : '';

  function route(path) { return appPrefix + path; }

  function show(message) { status.textContent = message; }
  function b64urlToBytes(value) {
    var padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
    var raw = atob(padded); var result = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i += 1) result[i] = raw.charCodeAt(i);
    return result;
  }
  function sameBytes(left, right) {
    if (!left || !right) return false;
    var a = new Uint8Array(left); var b = new Uint8Array(right);
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
    return true;
  }
  function jsonFetch(path, options) {
    return fetch(path, Object.assign({credentials: 'same-origin', headers: {'Accept': 'application/json'}}, options || {}))
      .then(function (response) { return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error(body.error || 'request_failed'); return body;
      }); });
  }
  function subscriptionBody(subscription) {
    var json = subscription.toJSON ? subscription.toJSON() : {};
    return {endpoint: subscription.endpoint, keys: json.keys || {p256dh: subscription.getKey('p256dh'), auth: subscription.getKey('auth')}};
  }
  async function enable() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
      show('当前浏览器不支持系统通知。'); return;
    }
    var permission = await Notification.requestPermission();
    if (permission !== 'granted') { show('系统通知未获授权；可在 Chrome 网站设置中开启。'); return; }
    var registration = await navigator.serviceWorker.register(route('/sw.js'));
    var key = await jsonFetch(route('/settings/web-push/public-key'));
    var keyBytes = b64urlToBytes(key.public_key);
    var existing = await registration.pushManager.getSubscription();
    if (existing && existing.options && existing.options.applicationServerKey &&
        !sameBytes(existing.options.applicationServerKey, keyBytes)) {
      await existing.unsubscribe(); existing = null;
    }
    var subscription = existing || await registration.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: keyBytes
    });
    try {
      await jsonFetch(route('/settings/web-push/subscribe'), {
        method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify(subscriptionBody(subscription))
      });
    } catch (error) {
      // Do not leave a browser-only subscription that the page would later
      // mistake for a working server registration.
      if (!existing) await subscription.unsubscribe().catch(function () {});
      throw error;
    }
    button.dataset.enabled = 'true'; button.textContent = '关闭系统通知'; show('已保存此设备订阅；浏览器权限、网络和系统限制仍可能影响送达。');
  }
  async function disable() {
    var registration = await navigator.serviceWorker.getRegistration(route('/notifications'));
    if (!registration) {
      button.dataset.enabled = 'false'; button.textContent = '开启系统通知';
      show('此设备的系统通知已关闭。'); return;
    }
    var subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      await jsonFetch(route('/settings/web-push/unsubscribe'), {
        method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify(subscriptionBody(subscription))
      });
      await subscription.unsubscribe();
    }
    button.dataset.enabled = 'false'; button.textContent = '开启系统通知'; show('此设备的系统通知已关闭。');
  }
  button.addEventListener('click', function () {
    if (button.disabled) return;
    button.disabled = true;
    (button.dataset.enabled === 'true' ? disable() : enable()).catch(function (error) {
      show(error && error.message === 'web push signing key is not configured' ? '系统通知尚未完成服务器配置。' : '通知设置失败，请稍后重试。');
    }).finally(function () { button.disabled = false; });
  });
  if (!('Notification' in window)) show('当前浏览器不支持系统通知。');
  else if (Notification.permission === 'denied') show('系统通知已被浏览器阻止；请在 Chrome 网站设置中允许通知。');
  else if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistration(route('/notifications')).then(function (registration) {
      return registration && registration.pushManager.getSubscription();
    }).then(function (subscription) {
      if (!subscription) return;
      button.dataset.enabled = 'true'; button.textContent = '关闭系统通知'; show('检测到此浏览器订阅；无法在此页确认送达。');
    }).catch(function () {});
  }
}());
