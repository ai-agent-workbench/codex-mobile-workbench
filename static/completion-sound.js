(function () {
  'use strict';

  var KEYS = {
    enabled: 'codex-completion-sound-enabled',
    tone: 'codex-completion-sound-tone',
    volume: 'codex-completion-sound-volume'
  };
  var TONES = new Set(['soft', 'clear', 'strong']);
  var context = null;
  var socket = null;
  var connectionRetry = null;

  function get(key) {
    try { return localStorage.getItem(key); } catch (error) { return null; }
  }

  function set(key, value) {
    try { localStorage.setItem(key, value); return true; } catch (error) { return false; }
  }

  function sessionGet(key) {
    try { return sessionStorage.getItem(key); } catch (error) { return null; }
  }

  function sessionSet(key, value) {
    try { sessionStorage.setItem(key, value); } catch (error) {}
  }

  function preferences() {
    var enabled = get(KEYS.enabled);
    var tone = get(KEYS.tone) || 'clear';
    var volume = Number(get(KEYS.volume) || .65);
    return {
      enabled: enabled === null ? true : enabled === '1',
      tone: TONES.has(tone) ? tone : 'clear',
      volume: Number.isFinite(volume) ? Math.max(.1, Math.min(1, volume)) : .65
    };
  }

  function normalize(value) {
    value = value || {};
    var tone = TONES.has(value.tone) ? value.tone : 'clear';
    var volume = Number(value.volume);
    return {
      enabled: Boolean(value.enabled),
      tone: tone,
      volume: Number.isFinite(volume) ? Math.max(.1, Math.min(1, volume)) : .65
    };
  }

  function save(value) {
    var next = normalize(value);
    return set(KEYS.enabled, next.enabled ? '1' : '0') &&
      set(KEYS.tone, next.tone) && set(KEYS.volume, String(next.volume));
  }

  function audioContext() {
    var Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) return null;
    try {
      if (!context) context = new Audio();
    } catch (error) {
      return null;
    }
    return context;
  }

  function unlock() {
    var audio = audioContext();
    if (!audio) return Promise.resolve(false);
    return audio.resume().then(function () { return true; }).catch(function () { return false; });
  }

  function synthesize(value) {
    var audio = audioContext();
    if (!audio) return false;
    var now = audio.currentTime + .02;
    var gain = audio.createGain();
    var level = Math.max(.001, value.volume * .17);
    var duration = value.tone === 'strong' ? .65 : .48;
    var notes = value.tone === 'soft' ? [
      [620, 0, .34, 'sine']
    ] : value.tone === 'strong' ? [
      [659, 0, .22, 'triangle'], [831, .15, .25, 'triangle'],
      [1047, .31, .32, 'sine']
    ] : [
      [740, 0, .22, 'sine'], [988, .17, .31, 'sine']
    ];
    gain.connect(audio.destination);
    gain.gain.setValueAtTime(.0001, now);
    gain.gain.exponentialRampToValueAtTime(level, now + .018);
    gain.gain.exponentialRampToValueAtTime(.0001, now + duration);
    notes.forEach(function (note) {
      var oscillator = audio.createOscillator();
      oscillator.type = note[3];
      oscillator.frequency.setValueAtTime(note[0], now + note[1]);
      oscillator.connect(gain);
      oscillator.start(now + note[1]);
      oscillator.stop(now + note[1] + note[2]);
    });
    return true;
  }

  function play(options) {
    options = options || {};
    var value = normalize(options.preferences || preferences());
    if (!options.force && (!value.enabled || document.visibilityState !== 'visible')) {
      return Promise.resolve(false);
    }
    return unlock().then(function (ready) {
      return ready ? synthesize(value) : false;
    });
  }

  function playedKey(conversationId) {
    return 'codex-completion-sound-played:' + conversationId;
  }

  function handleTerminal(event) {
    var data = event.detail || {};
    var conversationId = String(data.conversation_id || '');
    var turnId = String(data.turn_id || '');
    if (!conversationId || !turnId) return;
    var key = playedKey(conversationId);
    if (sessionGet(key) === turnId) return;
    sessionSet(key, turnId);
    play();
  }

  function route(path) {
    var match = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
    return (match ? '/' + match[1] : '') + path;
  }

  function coordinatorUnavailable() {
    var nav = document.querySelector('.nav');
    if (nav && !document.getElementById('native-conversation') &&
        !document.getElementById('codex-connection-unavailable')) {
      var notice = document.createElement('p');
      notice.id = 'codex-connection-unavailable';
      notice.className = 'native-connection-note warning';
      notice.setAttribute('role', 'status');
      notice.textContent = '实时完成提醒组件未加载；请刷新页面后再试。页面仍可手动刷新。';
      nav.insertAdjacentElement('afterend', notice);
    }
    window.dispatchEvent(new CustomEvent('codex:connection-unavailable'));
  }

  function connectGlobalSocket() {
    if (!document.querySelector('.nav') || document.getElementById('native-conversation') ||
        !connectionRetry || connectionRetry.isHalted() || window.codexAuthenticationRequired) return;
    // Visibility/online/pageshow recovery is merged by the coordinator.  Do
    // not replace a healthy or still-handshaking listener socket.
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) return;
    var scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var candidate;
    try {
      candidate = new WebSocket(scheme + '//' + location.host + route('/ws'));
    } catch (error) {
      connectionRetry.failed();
      return;
    }
    socket = candidate;
    candidate.onopen = function () {
      if (socket !== candidate) return;
      connectionRetry.opened();
      // Non-conversation pages reuse this single authenticated socket for
      // lightweight listeners such as the history index.
      window.dispatchEvent(new CustomEvent('codex:connection-restored'));
    };
    candidate.onmessage = function (event) {
      if (socket !== candidate) return;
      try {
        var message = JSON.parse(event.data);
        if (message.type === 'conversation.terminal' && message.data) {
          window.dispatchEvent(new CustomEvent('codex:conversation-terminal', {
            detail: message.data
          }));
        } else if (message.type === 'conversation.updated' && message.data) {
          window.dispatchEvent(new CustomEvent('codex:conversation-updated', {
            detail: message.data
          }));
        }
      } catch (error) {}
    };
    candidate.onclose = function (event) {
      // A superseded socket can close late; it must not schedule a new
      // connection over the current one.
      if (socket !== candidate) return;
      socket = null;
      if (event.code === 4401 || event.code === 4403) {
        connectionRetry.halt();
        window.dispatchEvent(new CustomEvent('codex:auth-required'));
        return;
      }
      connectionRetry.failed();
    };
  }

  function recoverGlobalSocket() {
    connectGlobalSocket();
  }

  if (!window.CodexReconnectCoordinator ||
      typeof window.CodexReconnectCoordinator.create !== 'function') {
    coordinatorUnavailable();
  } else if (document.querySelector('.nav') && !document.getElementById('native-conversation')) {
    connectionRetry = window.CodexReconnectCoordinator.create({
      connect: connectGlobalSocket,
      recover: recoverGlobalSocket
    });
  }

  window.addEventListener('codex:auth-required', function () {
    if (connectionRetry) connectionRetry.halt();
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
  });

  window.codexCompletionSound = {
    preferences: preferences,
    save: save,
    unlock: unlock,
    play: play
  };
  window.addEventListener('codex:conversation-terminal', handleTerminal);
  document.addEventListener('pointerdown', unlock, { once: true, capture: true });
  document.addEventListener('keydown', unlock, { once: true, capture: true });
  if (connectionRetry) connectGlobalSocket();
})();
