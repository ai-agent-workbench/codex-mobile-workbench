/* Dot status stays outside native conversation paging and management. */
(function () {
  'use strict';
  var card = document.querySelector('[data-dot-history-card]');
  if (!card) return;
  var prefix = (location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/) || [])[1];
  var url = (prefix ? '/' + prefix : '') + '/settings/dot/status';
  var running = false;
  async function refresh() {
    if (running || document.hidden) return;
    running = true;
    try {
      var response = await fetch(url, {credentials:'same-origin',headers:{Accept:'application/json'}});
      if (!response.ok) return;
      var value = await response.json();
      var unread = value.unread === true;
      var waiting = value.waiting === true;
      card.classList.toggle('unread', unread);
      card.classList.toggle('complete', !waiting);
      card.classList.toggle('running', waiting && !unread);
      card.querySelector('.conversation-card-status').textContent = unread ? '已完成未读' : waiting ? '等待 dot' : value.reply_generation > 0 ? '已完成' : '打开';
    } catch (_) {} finally { running = false; }
  }
  refresh();
  setInterval(refresh, 5000);
  window.addEventListener('pageshow', refresh);
  document.addEventListener('visibilitychange', refresh);
})();
