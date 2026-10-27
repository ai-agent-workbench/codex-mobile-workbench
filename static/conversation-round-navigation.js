(function () {
  'use strict';

  var root = document.getElementById('native-conversation');
  var messages = document.getElementById('conversation-messages');
  var roundNav = document.getElementById('conversation-round-nav');
  var roundUp = document.getElementById('conversation-round-up');
  var roundDown = document.getElementById('conversation-round-down');
  var newContent = document.getElementById('conversation-new-content');
  if (!root || !messages || !roundNav || !roundUp || !roundDown || !newContent) return;

  function storeGet(key) {
    try { return localStorage.getItem(key); } catch (error) { return null; }
  }
  function storeSet(key, value) {
    try { localStorage.setItem(key, value); } catch (error) {}
  }

  function roundTargets() {
    var users = Array.from(messages.querySelectorAll('.native-message.user'));
    var primary = users.filter(function (message) {
      var meta = message.querySelector('.native-message-meta');
      return meta && /第\s*\d+\s*轮/.test(meta.textContent);
    });
    return primary.length ? primary : users;
  }

  function currentRoundIndex(targets) {
    var line = (document.querySelector('.native-conversation-head') || root)
      .getBoundingClientRect().bottom + 8;
    var found = 0;
    targets.forEach(function (target, index) {
      if (target.getBoundingClientRect().top <= line) found = index;
    });
    return found;
  }

  var restorePosition = function () {};

  function updateRoundButtons() {
    var targets = roundTargets();
    var current = currentRoundIndex(targets);
    var wasHidden = roundNav.hidden;
    roundNav.hidden = targets.length < 2;
    roundUp.disabled = !targets.length || current <= 0;
    roundDown.disabled = !targets.length || (
      current >= targets.length - 1 &&
      window.innerHeight + window.scrollY >= document.body.scrollHeight - 150
    );
    if (wasHidden && !roundNav.hidden) requestAnimationFrame(restorePosition);
  }

  function jumpRound(direction) {
    var targets = roundTargets();
    if (!targets.length) return;
    var current = currentRoundIndex(targets);
    var target = direction < 0 ? Math.max(0, current - 1) :
      Math.min(targets.length - 1, current + 1);
    if (direction > 0 && current === targets.length - 1) {
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    } else {
      targets[target].scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    roundDown.classList.remove('new');
    newContent.hidden = true;
    requestAnimationFrame(updateRoundButtons);
  }

  function makeDraggable(value, key) {
    var handle = value.querySelector('.native-drag-handle');
    if (!handle) return function () {};
    var dragging = false;
    var pointerId = null;
    var dx = 0;
    var dy = 0;

    function bounds() {
      var viewport = window.visualViewport;
      var pageWidth = document.documentElement.clientWidth;
      var left = (viewport ? viewport.offsetLeft : 0) + 6;
      var visibleRight = viewport ? viewport.offsetLeft + viewport.width : innerWidth;
      var rightEdge = Math.min(pageWidth, visibleRight);
      return {
        left: left,
        top: (viewport ? viewport.offsetTop : 0) + 50,
        right: rightEdge - value.offsetWidth - 6,
        bottom: (viewport ? viewport.offsetTop + viewport.height : innerHeight) - value.offsetHeight - 6,
      };
    }

    function place(x, y, save) {
      var limit = bounds();
      x = Math.min(Math.max(x, limit.left), Math.max(limit.left, limit.right));
      y = Math.min(Math.max(y, limit.top), Math.max(limit.top, limit.bottom));
      value.style.left = Math.round(x) + 'px';
      value.style.top = Math.round(y) + 'px';
      value.style.right = 'auto';
      value.style.bottom = 'auto';
      value.style.transform = 'none';
      if (save !== false) storeSet(key, JSON.stringify({ x: x, y: y }));
    }

    function restore() {
      if (value.hidden) return;
      try {
        var saved = JSON.parse(storeGet(key) || 'null');
        if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
          place(saved.x, saved.y, false);
        }
      } catch (error) {}
    }

    handle.addEventListener('pointerdown', function (event) {
      var rect = value.getBoundingClientRect();
      dragging = true;
      pointerId = event.pointerId;
      dx = event.clientX - rect.left;
      dy = event.clientY - rect.top;
      try { handle.setPointerCapture(pointerId); } catch (error) {}
      event.preventDefault();
    });
    window.addEventListener('pointermove', function (event) {
      if (!dragging || event.pointerId !== pointerId) return;
      place(event.clientX - dx, event.clientY - dy);
      event.preventDefault();
    }, { passive: false });
    function end(event) {
      if (!dragging || event.pointerId !== pointerId) return;
      dragging = false;
      try {
        if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      } catch (error) {}
      pointerId = null;
    }
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    window.addEventListener('resize', restore);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', restore);
    requestAnimationFrame(restore);
    return restore;
  }

  roundUp.addEventListener('click', function () { jumpRound(-1); });
  roundDown.addEventListener('click', function () { jumpRound(1); });
  window.addEventListener('scroll', updateRoundButtons, { passive: true });
  window.addEventListener('codex:conversation-messages-enhanced', updateRoundButtons);
  new MutationObserver(updateRoundButtons).observe(messages, { childList: true, subtree: true });
  new MutationObserver(function () {
    roundDown.classList.toggle('new', !newContent.hidden);
  }).observe(newContent, { attributes: true, attributeFilter: ['hidden'] });
  restorePosition = makeDraggable(roundNav, 'codex-round-nav-position');
  updateRoundButtons();
})();
