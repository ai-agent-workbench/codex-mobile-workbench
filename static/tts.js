(function () {
  'use strict';

  var KEYS = {
    voice: 'codex-tts-voice',
    rate: 'codex-tts-rate',
    pitch: 'codex-tts-pitch',
  };

  function stored(key) {
    try { return localStorage.getItem(key); } catch (error) { return null; }
  }

  function bounded(value, fallback, minimum, maximum) {
    var number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.min(maximum, Math.max(minimum, number));
  }

  function preferences() {
    return {
      voice: stored(KEYS.voice) || '',
      rate: bounded(stored(KEYS.rate), 0.95, 0.70, 1.50),
      pitch: bounded(stored(KEYS.pitch), 1, 0.70, 1.30),
    };
  }

  function save(value) {
    var normalized = {
      voice: String(value && value.voice || ''),
      rate: bounded(value && value.rate, 0.95, 0.70, 1.50),
      pitch: bounded(value && value.pitch, 1, 0.70, 1.30),
    };
    try {
      localStorage.setItem(KEYS.voice, normalized.voice);
      localStorage.setItem(KEYS.rate, String(normalized.rate));
      localStorage.setItem(KEYS.pitch, String(normalized.pitch));
      return true;
    } catch (error) {
      return false;
    }
  }

  function supported() {
    return Boolean(window.speechSynthesis && window.SpeechSynthesisUtterance);
  }

  function voices() {
    if (!supported() || typeof window.speechSynthesis.getVoices !== 'function') return [];
    try {
      return window.speechSynthesis.getVoices().slice().sort(function (left, right) {
        var leftChinese = /^zh(?:-|$)/i.test(left.lang || '') ? 0 : 1;
        var rightChinese = /^zh(?:-|$)/i.test(right.lang || '') ? 0 : 1;
        return leftChinese - rightChinese || String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN');
      });
    } catch (error) {
      return [];
    }
  }

  function voiceFor(uri) {
    if (!uri) return null;
    return voices().find(function (voice) { return voice.voiceURI === uri; }) || null;
  }

  function createUtterance(text, value) {
    if (!supported()) throw new Error('当前浏览器不支持 Web Speech 朗读。');
    var prefs = value || preferences();
    var utterance = new window.SpeechSynthesisUtterance(String(text || ''));
    utterance.lang = 'zh-CN';
    utterance.rate = bounded(prefs.rate, 0.95, 0.70, 1.50);
    utterance.pitch = bounded(prefs.pitch, 1, 0.70, 1.30);
    var voice = voiceFor(prefs.voice);
    // An empty preference deliberately leaves utterance.voice untouched.
    // Chromium can then delegate selection to Android's current default TTS.
    if (voice) utterance.voice = voice;
    return utterance;
  }

  function cancel() {
    if (!supported()) return;
    try { window.speechSynthesis.cancel(); } catch (error) {}
  }

  function speak(utterance) {
    if (!supported()) throw new Error('当前浏览器不支持 Web Speech 朗读。');
    window.speechSynthesis.speak(utterance);
  }

  function onVoicesChanged(callback) {
    if (!supported() || typeof callback !== 'function') return;
    if (typeof window.speechSynthesis.addEventListener === 'function') {
      window.speechSynthesis.addEventListener('voiceschanged', callback);
    }
  }

  function voiceLabel(voice) {
    var service = voice.localService === true ? '本机音色' :
      voice.localService === false ? '可能联网' : '来源未知';
    return [voice.name || '未命名音色', voice.lang || '语言未知', service].join(' · ');
  }

  function modeLabel(value) {
    var prefs = value || preferences();
    if (!prefs.voice) return '跟随 Android 系统默认';
    var voice = voiceFor(prefs.voice);
    return voice ? '指定音色：' + (voice.name || voice.voiceURI) : '原指定音色不可用，实际跟随系统默认';
  }

  function errorLabel(value) {
    var code = String(value || 'unknown');
    return {
      'not-supported': '当前浏览器不支持系统朗读',
      'not-allowed': '浏览器未允许播放',
      network: '语音引擎报告网络错误',
      'synthesis-failed': '语音合成失败',
      'language-unavailable': '中文语言不可用',
      'voice-unavailable': '所选音色不可用',
      'audio-busy': '音频设备正忙',
      'audio-hardware': '音频设备不可用',
      'text-too-long': '朗读文本过长',
      canceled: '朗读已取消',
      interrupted: '朗读被新的播放打断',
    }[code] || '朗读失败（' + code + '）';
  }

  window.codexTts = {
    keys: KEYS,
    supported: supported,
    preferences: preferences,
    save: save,
    voices: voices,
    voiceFor: voiceFor,
    voiceLabel: voiceLabel,
    modeLabel: modeLabel,
    createUtterance: createUtterance,
    speak: speak,
    cancel: cancel,
    onVoicesChanged: onVoicesChanged,
    errorLabel: errorLabel,
  };
})();
