(function () {
  'use strict';

  var api = window.codexCompletionSound;
  var panel = document.getElementById('completion-sound-panel');
  var enabled = document.getElementById('completion-sound-enabled');
  var volume = document.getElementById('completion-sound-volume');
  var volumeValue = document.getElementById('completion-sound-volume-value');
  var toneName = document.getElementById('completion-sound-tone-name');
  var saved = document.getElementById('completion-sound-saved');
  var preview = document.getElementById('completion-sound-preview');
  if (!api || !panel || !enabled || !volume || !volumeValue || !toneName || !saved || !preview) return;

  var value = api.preferences();
  var previewTimer = null;

  function toneButton() {
    return document.querySelector('[data-completion-tone="' + value.tone + '"]');
  }

  function render() {
    enabled.checked = value.enabled;
    panel.classList.toggle('disabled', !value.enabled);
    volume.value = String(Math.round(value.volume * 100));
    volumeValue.textContent = volume.value + '%';
    document.querySelectorAll('[data-completion-tone]').forEach(function (button) {
      var selected = button.dataset.completionTone === value.tone;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });
    var selected = toneButton();
    toneName.textContent = selected ? selected.dataset.name : '清脆';
  }

  function persist(message) {
    saved.textContent = api.save(value) ? '✓ ' + message : '当前浏览器无法保存设置。';
  }

  function test() {
    saved.textContent = '正在试听…';
    api.play({ force: true, preferences: value }).then(function (played) {
      saved.textContent = played ? '✓ 已播放试听音' : '当前浏览器暂时无法播放提示音。';
    });
  }

  enabled.addEventListener('change', function () {
    value.enabled = enabled.checked;
    render();
    persist(value.enabled ? '已开启前台提示音' : '已关闭前台提示音');
  });
  volume.addEventListener('input', function () {
    value.volume = Number(volume.value) / 100;
    volumeValue.textContent = volume.value + '%';
    saved.textContent = '';
  });
  document.querySelectorAll('[data-completion-tone]').forEach(function (button) {
    button.addEventListener('click', function () {
      value.tone = button.dataset.completionTone;
      render();
      saved.textContent = '';
    });
  });
  document.getElementById('completion-sound-test').addEventListener('click', test);
  document.getElementById('completion-sound-save').addEventListener('click', function () {
    persist('已保存到这台设备');
  });
  document.getElementById('completion-sound-simulate').addEventListener('click', function () {
    clearTimeout(previewTimer);
    preview.hidden = false;
    previewTimer = setTimeout(function () { preview.hidden = true; }, 3500);
    test();
  });
  render();
})();
