(function () {
  'use strict';

  var api = window.codexTts;
  var voiceSelect = document.getElementById('tts-voice');
  var rate = document.getElementById('tts-rate');
  var pitch = document.getElementById('tts-pitch');
  var rateValue = document.getElementById('tts-rate-value');
  var pitchValue = document.getElementById('tts-pitch-value');
  var status = document.getElementById('tts-status');
  var summaryTitle = document.getElementById('tts-summary-title');
  var summaryDetail = document.getElementById('tts-summary-detail');
  var badge = document.getElementById('tts-support-badge');
  var diagnosticSupport = document.getElementById('tts-diagnostic-support');
  var diagnosticMode = document.getElementById('tts-diagnostic-mode');
  var diagnosticVoices = document.getElementById('tts-diagnostic-voices');
  var diagnosticResult = document.getElementById('tts-diagnostic-result');
  if (!api || !voiceSelect || !rate || !pitch || !rateValue || !pitchValue ||
      !status || !summaryTitle || !summaryDetail || !badge || !diagnosticSupport ||
      !diagnosticMode || !diagnosticVoices || !diagnosticResult) return;

  var prefs = api.preferences();
  var lastResult = '尚未试听';

  function option(text, value) {
    var item = document.createElement('option');
    item.textContent = text;
    item.value = value;
    return item;
  }

  function setStatus(text, warning) {
    status.textContent = text;
    status.classList.toggle('warning', Boolean(warning));
  }

  function renderDiagnostics() {
    var available = api.voices();
    var localCount = available.filter(function (voice) { return voice.localService === true; }).length;
    var serviceCount = available.filter(function (voice) { return voice.localService === false; }).length;
    var unknownCount = available.length - localCount - serviceCount;
    var counts = available.length + ' 个';
    if (available.length) {
      counts += '（本机 ' + localCount + '，可能联网 ' + serviceCount;
      if (unknownCount) counts += '，来源未知 ' + unknownCount;
      counts += '）';
    }
    diagnosticSupport.textContent = api.supported() ? '可用' : '不可用';
    diagnosticMode.textContent = api.modeLabel(prefs);
    diagnosticVoices.textContent = counts;
    diagnosticResult.textContent = lastResult;
    if (api.supported()) {
      summaryTitle.textContent = '浏览器朗读能力可用';
      summaryDetail.textContent = prefs.voice ?
        '当前指定了浏览器音色；改选“系统默认”可让 Android 默认 TTS 接管。' :
        '当前跟随 Android 系统默认 TTS；网页不会覆盖具体音色。';
      badge.textContent = '可用';
      badge.classList.remove('warning');
    } else {
      summaryTitle.textContent = '当前浏览器没有提供朗读能力';
      summaryDetail.textContent = '请改用支持 Web Speech 的 Chrome/PWA，或检查浏览器权限和系统 TTS。';
      badge.textContent = '不可用';
      badge.classList.add('warning');
    }
  }

  function loadVoices(fromUser) {
    var available = api.voices();
    var missing = prefs.voice && available.length && !api.voiceFor(prefs.voice);
    if (missing) {
      prefs.voice = '';
      api.save(prefs);
      setStatus('原来指定的音色已不可用，已改为跟随 Android 系统默认。', true);
    } else if (fromUser) {
      setStatus(available.length ? '已刷新浏览器音色列表。' : '暂未读取到具体音色；仍可试听系统默认。', !available.length);
    }
    voiceSelect.replaceChildren(option('跟随 Android 系统默认（推荐）', ''));
    available.forEach(function (voice) {
      voiceSelect.appendChild(option(api.voiceLabel(voice), voice.voiceURI));
    });
    voiceSelect.value = prefs.voice;
    renderDiagnostics();
  }

  function renderControls() {
    rate.value = String(prefs.rate);
    pitch.value = String(prefs.pitch);
    rateValue.textContent = prefs.rate.toFixed(2) + '×';
    pitchValue.textContent = prefs.pitch.toFixed(2);
    renderDiagnostics();
  }

  function testSpeech() {
    if (!api.supported()) {
      lastResult = '浏览器不支持 Web Speech';
      setStatus('当前浏览器无法调用系统 TTS。', true);
      renderDiagnostics();
      return;
    }
    api.cancel();
    var utterance;
    try {
      utterance = api.createUtterance('这是 Android 系统文字转语音的试听。', prefs);
      utterance.onstart = function () {
        lastResult = '系统已开始发声';
        setStatus('正在试听；如果无声，请检查媒体音量并查看下方排查步骤。');
        renderDiagnostics();
      };
      utterance.onend = function () {
        lastResult = '试听完成';
        setStatus('✓ 试听完成。听到的就是当前实际调用结果。');
        renderDiagnostics();
      };
      utterance.onerror = function (event) {
        if (['canceled', 'interrupted'].includes(event.error)) return;
        lastResult = api.errorLabel(event.error);
        setStatus(lastResult + '。请检查媒体音量、系统 TTS 试听，并彻底重启 Chrome/PWA。', true);
        renderDiagnostics();
      };
      lastResult = prefs.voice ? '已提交给指定音色' : '已提交给 Android 系统默认';
      setStatus('已把试听交给系统 TTS；请听是否为预期的 Baker 音色。');
      renderDiagnostics();
      api.speak(utterance);
    } catch (error) {
      lastResult = error && error.message || '语音合成失败';
      setStatus(lastResult, true);
      renderDiagnostics();
    }
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var area = document.createElement('textarea');
      area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.appendChild(area); area.select();
      try { document.execCommand('copy') ? resolve() : reject(new Error('copy failed')); }
      catch (error) { reject(error); }
      area.remove();
    });
  }

  voiceSelect.addEventListener('change', function () {
    prefs.voice = voiceSelect.value;
    setStatus(prefs.voice ? '已选择具体音色；点“保存到这台设备”生效。' : '已选择 Android 系统默认；点“保存到这台设备”生效。');
    renderDiagnostics();
  });
  rate.addEventListener('input', function () {
    prefs.rate = Number(rate.value);
    rateValue.textContent = prefs.rate.toFixed(2) + '×';
  });
  pitch.addEventListener('input', function () {
    prefs.pitch = Number(pitch.value);
    pitchValue.textContent = prefs.pitch.toFixed(2);
  });
  document.getElementById('tts-refresh-voices').addEventListener('click', function () { loadVoices(true); });
  document.getElementById('tts-test').addEventListener('click', testSpeech);
  document.getElementById('tts-save').addEventListener('click', function () {
    var saved = api.save(prefs);
    setStatus(saved ? '✓ 已保存到这台设备。' : '当前浏览器无法保存设置。', !saved);
    renderDiagnostics();
  });
  document.getElementById('tts-copy-diagnostics').addEventListener('click', function () {
    var text = [
      'Web Speech：' + diagnosticSupport.textContent,
      '当前模式：' + diagnosticMode.textContent,
      '浏览器音色：' + diagnosticVoices.textContent,
      '最近结果：' + diagnosticResult.textContent,
      '建议：系统默认试听正常后，彻底关闭并重开 Chrome/PWA。',
    ].join('\n');
    copyText(text).then(function () { setStatus('✓ 排查信息已复制。'); }, function () { setStatus('复制失败，请长按诊断内容复制。', true); });
  });

  api.onVoicesChanged(function () { loadVoices(false); });
  loadVoices(false);
  renderControls();
})();
