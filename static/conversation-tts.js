(function () {
  'use strict';

  var api = window.codexTts;
  var root = document.getElementById('native-conversation');
  var messages = document.getElementById('conversation-messages');
  var audioBar = document.getElementById('conversation-audio-bar');
  var audioLabel = document.getElementById('conversation-audio-label');
  var audioPrevious = document.getElementById('conversation-audio-previous');
  var audioNext = document.getElementById('conversation-audio-next');
  var audioPause = document.getElementById('conversation-audio-pause');
  var audioStop = document.getElementById('conversation-audio-stop');
  var audioDialog = document.getElementById('conversation-audio-settings-dialog');
  var voiceSelect = document.getElementById('conversation-audio-voice');
  var rateRange = document.getElementById('conversation-audio-rate');
  var pitchRange = document.getElementById('conversation-audio-pitch');
  var rateValue = document.getElementById('conversation-audio-rate-value');
  var pitchValue = document.getElementById('conversation-audio-pitch-value');
  var testButton = document.getElementById('conversation-audio-test');
  var settingsButton = document.getElementById('conversation-audio-settings');
  var settingsLink = document.getElementById('conversation-audio-more-settings');
  if (!api || !root || !messages || !audioBar || !audioLabel || !audioPrevious ||
      !audioNext || !audioPause || !audioStop || !audioDialog || !voiceSelect ||
      !rateRange || !pitchRange || !rateValue || !pitchValue || !testButton ||
      !settingsButton || !settingsLink) return;

  var appPrefixMatch = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
  var appPrefix = appPrefixMatch ? '/' + appPrefixMatch[1] : '';
  settingsLink.href = appPrefix + '/settings/tts';

  var audioPrefs = api.preferences();
  var speech = { chunks: [], index: 0, paused: false, token: 0, button: null };

  function element(tag, className, text) {
    var value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }

  function ensureSpeakButtons() {
    messages.querySelectorAll('.native-message.agent .native-message-body').forEach(function (body) {
      var footer = body.parentElement && body.parentElement.querySelector('.native-message-footer');
      if (!footer || footer.querySelector('.native-message-speak')) return;
      var button = element('button', 'native-message-speak', '▶');
      button.type = 'button';
      button.title = '朗读此回复';
      button.setAttribute('aria-label', '朗读此回复');
      footer.insertBefore(button, footer.querySelector('.native-message-more'));
    });
  }

  function loadVoices() {
    var available = api.voices();
    if (audioPrefs.voice && available.length && !api.voiceFor(audioPrefs.voice)) {
      audioPrefs.voice = '';
      api.save(audioPrefs);
    }
    voiceSelect.replaceChildren();
    var automatic = element('option', '', '跟随 Android 系统默认（推荐）');
    automatic.value = '';
    voiceSelect.appendChild(automatic);
    available.forEach(function (voice) {
      var option = element('option', '', api.voiceLabel(voice));
      option.value = voice.voiceURI;
      option.selected = voice.voiceURI === audioPrefs.voice;
      voiceSelect.appendChild(option);
    });
    voiceSelect.value = audioPrefs.voice;
  }

  function reloadPreferences() {
    audioPrefs = api.preferences();
    rateRange.value = String(audioPrefs.rate);
    pitchRange.value = String(audioPrefs.pitch);
    rateValue.textContent = audioPrefs.rate.toFixed(2) + '×';
    pitchValue.textContent = audioPrefs.pitch.toFixed(2);
    loadVoices();
  }

  function readableText(body) {
    var copy = body.cloneNode(true);
    copy.querySelectorAll('.native-table-wrap').forEach(function (value) {
      value.replaceWith(document.createTextNode('表格内容请在页面查看。'));
    });
    copy.querySelectorAll('.native-code-card').forEach(function (value) {
      value.replaceWith(document.createTextNode('代码示例已省略。'));
    });
    copy.querySelectorAll('button').forEach(function (value) { value.remove(); });
    return copy.innerText.replace(/\s+/g, ' ').trim();
  }

  function chunks(text) {
    var sentences = text.match(/[^。！？!?；;]+[。！？!?；;]?/g) || [text];
    var result = [];
    sentences.forEach(function (sentence) {
      if (sentence.length <= 120) result.push(sentence.trim());
      else {
        for (var index = 0; index < sentence.length; index += 100) {
          result.push(sentence.slice(index, index + 100).trim());
        }
      }
    });
    return result.filter(Boolean);
  }

  function updateSentenceButtons() {
    audioPrevious.disabled = !speech.chunks.length || speech.index <= 0;
    audioNext.disabled = !speech.chunks.length || speech.index >= speech.chunks.length - 1;
    audioPause.disabled = !speech.chunks.length;
  }

  function clearActiveButton() {
    messages.querySelectorAll('.native-message-speak').forEach(function (button) {
      button.classList.remove('active');
    });
  }

  function resetSpeech(hide) {
    if (hide !== false) audioBar.hidden = true;
    clearActiveButton();
    audioPause.textContent = 'Ⅱ';
    updateSentenceButtons();
  }

  function stopSpeech() {
    var token = speech.token + 1;
    api.cancel();
    speech = { chunks: [], index: 0, paused: false, token: token, button: null };
    resetSpeech(true);
  }

  function failSpeech(error) {
    var token = speech.token + 1;
    api.cancel();
    speech = { chunks: [], index: 0, paused: false, token: token, button: null };
    clearActiveButton();
    audioBar.hidden = false;
    audioLabel.textContent = api.errorLabel(error) + '；点 ⚙ 查看诊断';
    audioPause.textContent = '▶';
    updateSentenceButtons();
  }

  function speakCurrent() {
    if (speech.paused || speech.index >= speech.chunks.length) {
      if (speech.index >= speech.chunks.length) stopSpeech();
      return;
    }
    if (!api.supported()) {
      failSpeech('not-supported');
      return;
    }
    updateSentenceButtons();
    var token = ++speech.token;
    var utterance;
    try {
      utterance = api.createUtterance(speech.chunks[speech.index], audioPrefs);
      utterance.onstart = function () {
        if (token === speech.token) {
          audioLabel.textContent = '正在朗读 · ' + (speech.index + 1) + ' / ' + speech.chunks.length;
        }
      };
      utterance.onend = function () {
        if (token !== speech.token || speech.paused) return;
        speech.index += 1;
        speakCurrent();
      };
      utterance.onerror = function (event) {
        if (token !== speech.token || ['canceled', 'interrupted'].includes(event.error)) return;
        failSpeech(event.error);
      };
      audioLabel.textContent = '已交给系统 TTS · ' + (speech.index + 1) + ' / ' + speech.chunks.length;
      api.speak(utterance);
    } catch (error) {
      failSpeech(error && error.message || 'synthesis-failed');
    }
  }

  function startSpeech(values, button) {
    stopSpeech();
    if (!values.length) {
      failSpeech('没有可朗读的文字');
      return;
    }
    speech = {
      chunks: values,
      index: 0,
      paused: false,
      token: speech.token,
      button: button || null,
    };
    if (button) button.classList.add('active');
    audioBar.hidden = false;
    requestAnimationFrame(restoreAudioBar);
    speakCurrent();
  }

  function skipSentence(delta) {
    if (!speech.chunks.length) return;
    var target = Math.max(0, Math.min(speech.chunks.length - 1, speech.index + delta));
    if (target === speech.index) return;
    speech.token += 1;
    api.cancel();
    speech.index = target;
    speech.paused = false;
    audioPause.textContent = 'Ⅱ';
    updateSentenceButtons();
    setTimeout(function () {
      if (!speech.paused && speech.index === target) speakCurrent();
    }, 140);
  }

  function makeDraggable(value, key) {
    var handle = value.querySelector('.native-drag-handle');
    if (!handle) return function () {};
    var dragging = false;
    var pointerId = null;
    var dx = 0;
    var dy = 0;
    function stored(keyName) {
      try { return localStorage.getItem(keyName); } catch (error) { return null; }
    }
    function savePosition(position) {
      try { localStorage.setItem(key, JSON.stringify(position)); } catch (error) {}
    }
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
      value.style.left = Math.round(x) + 'px'; value.style.top = Math.round(y) + 'px';
      value.style.right = 'auto'; value.style.bottom = 'auto'; value.style.transform = 'none';
      if (save !== false) savePosition({ x: x, y: y });
    }
    function restore() {
      if (value.hidden) return;
      try {
        var saved = JSON.parse(stored(key) || 'null');
        if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) place(saved.x, saved.y, false);
      } catch (error) {}
    }
    handle.addEventListener('pointerdown', function (event) {
      var rect = value.getBoundingClientRect();
      dragging = true; pointerId = event.pointerId;
      dx = event.clientX - rect.left; dy = event.clientY - rect.top;
      try { handle.setPointerCapture(pointerId); } catch (error) {}
      event.preventDefault();
    });
    window.addEventListener('pointermove', function (event) {
      if (!dragging || event.pointerId !== pointerId) return;
      place(event.clientX - dx, event.clientY - dy); event.preventDefault();
    }, { passive: false });
    function end(event) {
      if (!dragging || event.pointerId !== pointerId) return;
      dragging = false;
      try { if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId); } catch (error) {}
      pointerId = null;
    }
    window.addEventListener('pointerup', end); window.addEventListener('pointercancel', end);
    window.addEventListener('resize', restore);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', restore);
    requestAnimationFrame(restore);
    return restore;
  }

  var restoreAudioBar = makeDraggable(audioBar, 'codex-audio-bar-position');
  reloadPreferences();
  api.onVoicesChanged(loadVoices);
  window.addEventListener('pageshow', reloadPreferences);
  window.addEventListener('storage', function (event) {
    if (Object.values(api.keys).includes(event.key)) reloadPreferences();
  });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') reloadPreferences();
  });
  ensureSpeakButtons();
  new MutationObserver(ensureSpeakButtons).observe(messages, { childList: true, subtree: true });
  window.addEventListener('codex:conversation-messages-enhanced', ensureSpeakButtons);

  messages.addEventListener('click', function (event) {
    var button = event.target.closest('.native-message-speak');
    if (!button) return;
    var bubble = button.closest('.native-bubble');
    var body = bubble && bubble.querySelector('.native-message-body');
    if (!body) return;
    startSpeech(chunks(readableText(body)), button);
  });
  audioPause.addEventListener('click', function () {
    if (!speech.chunks.length) return;
    if (speech.paused) {
      speech.paused = false;
      audioPause.textContent = 'Ⅱ';
      audioLabel.textContent = '正在恢复朗读…';
      setTimeout(function () { if (!speech.paused && speech.chunks.length) speakCurrent(); }, 140);
    } else {
      speech.paused = true;
      speech.token += 1;
      api.cancel();
      audioPause.textContent = '▶';
      audioLabel.textContent = '已暂停 · 继续时从当前句重读';
    }
  });
  audioPrevious.addEventListener('click', function () { skipSentence(-1); });
  audioNext.addEventListener('click', function () { skipSentence(1); });
  audioStop.addEventListener('click', stopSpeech);
  settingsButton.addEventListener('click', function () { audioDialog.showModal(); });
  audioDialog.querySelector('[data-close-audio-settings]').addEventListener('click', function () { audioDialog.close(); });
  voiceSelect.addEventListener('change', function () {
    audioPrefs.voice = voiceSelect.value;
    api.save(audioPrefs);
  });
  rateRange.addEventListener('input', function () {
    audioPrefs.rate = Number(rateRange.value);
    rateValue.textContent = audioPrefs.rate.toFixed(2) + '×';
    api.save(audioPrefs);
  });
  pitchRange.addEventListener('input', function () {
    audioPrefs.pitch = Number(pitchRange.value);
    pitchValue.textContent = audioPrefs.pitch.toFixed(2);
    api.save(audioPrefs);
  });
  testButton.addEventListener('click', function () {
    audioDialog.close();
    startSpeech(['这是当前音色和播放速度的试听效果。'], null);
  });
  updateSentenceButtons();
})();
