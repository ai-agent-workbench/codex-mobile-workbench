(function (window) {
  'use strict';

  // Keep socket retry policy small and shared.  It deliberately does not own a
  // WebSocket: pages retain their existing heartbeat, event and receipt rules.
  if (window.CodexReconnectCoordinator) return;

  function create(options) {
    options = options || {};
    var clock = options.clock || window;
    var random = options.random || Math.random;
    var retryTimer = null;
    var stableTimer = null;
    var recoveryTimer = null;
    var attempts = 0;
    var halted = false;
    var baseDelay = Number(options.baseDelay) || 2500;
    var maximumDelay = Number(options.maximumDelay) || 30000;
    var stableFor = Number(options.stableFor) || 10000;

    function visible() {
      return !window.document || window.document.visibilityState !== 'hidden';
    }

    function online() {
      return !window.navigator || window.navigator.onLine !== false;
    }

    function allowed() {
      return !halted && visible() && online();
    }

    function clearRetry() {
      if (retryTimer !== null) clock.clearTimeout(retryTimer);
      retryTimer = null;
    }

    function clearStable() {
      if (stableTimer !== null) clock.clearTimeout(stableTimer);
      stableTimer = null;
    }

    function delayForAttempt() {
      var raw = Math.min(maximumDelay, baseDelay * Math.pow(2, Math.max(0, attempts - 1)));
      // Cap again after jitter: the visible maximum is always 30 seconds.
      return Math.min(maximumDelay, Math.round(raw * (.8 + random() * .4)));
    }

    function connectNow(kind) {
      if (!allowed()) return false;
      if (typeof options.connect === 'function') options.connect({kind: kind || 'retry', attempts: attempts});
      return true;
    }

    function failed() {
      clearStable();
      clearRetry();
      if (!allowed()) return false;
      attempts += 1;
      var delay = delayForAttempt();
      retryTimer = clock.setTimeout(function () {
        retryTimer = null;
        connectNow('retry');
      }, delay);
      return delay;
    }

    function opened() {
      clearRetry();
      clearStable();
      if (halted) return;
      stableTimer = clock.setTimeout(function () {
        stableTimer = null;
        attempts = 0;
      }, stableFor);
    }

    function recover() {
      clearRetry();
      if (recoveryTimer !== null || !allowed()) return false;
      // visibilitychange, online and pageshow often arrive in one task.  A
      // zero-delay merge preserves one connect and one read-only sync.
      recoveryTimer = clock.setTimeout(function () {
        recoveryTimer = null;
        if (!allowed()) return;
        if (typeof options.recover === 'function') options.recover({attempts: attempts});
        else connectNow('recovery');
      }, 0);
      return true;
    }

    function suspend() {
      clearRetry();
    }

    function resume() {
      // Only a page's successful explicit read is allowed to call this;
      // visibility/online/pageshow always go through recover() and remain halted.
      halted = false;
      attempts = 0;
    }

    function halt() {
      halted = true;
      clearRetry();
      clearStable();
      if (recoveryTimer !== null) clock.clearTimeout(recoveryTimer);
      recoveryTimer = null;
    }

    function state() {
      return {attempts: attempts, halted: halted, retryPending: retryTimer !== null,
        recoveryPending: recoveryTimer !== null};
    }

    document.addEventListener('visibilitychange', function () {
      if (visible()) recover(); else suspend();
    });
    window.addEventListener('online', recover);
    window.addEventListener('pageshow', function (event) {
      // Initial navigation already starts its normal connection path; pageshow
      // recovery is for a restored (BFCache) document.
      if (event && event.persisted) recover();
    });

    return {
      failed: failed,
      opened: opened,
      recover: recover,
      suspend: suspend,
      halt: halt,
      resume: resume,
      isHalted: function () { return halted; },
      state: state,
      delayForAttempt: delayForAttempt
    };
  }

  window.CodexReconnectCoordinator = {create: create};
})(window);
