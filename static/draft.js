(function () {
  const composer = document.querySelector('[data-draft-key]');
  if (!composer) return;
  const key = composer.dataset.draftKey;
  const revisionKey = key + ':revision';
  let saved = null;
  try { saved = localStorage.getItem(key); } catch (_) {}
  if (saved && !composer.value) composer.value = saved;
  composer.addEventListener('input', function (event) {
    if (event.detail && event.detail.storageAlreadyHandled) return;
    try {
      if (!(event.detail && event.detail.restoredDraft)) {
        const revision = window.crypto && crypto.randomUUID ? crypto.randomUUID() :
          Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
        localStorage.setItem(revisionKey, revision);
      }
      if (composer.value) localStorage.setItem(key, composer.value);
      else localStorage.removeItem(key);
    } catch (_) {}
  });
  if (composer.value) composer.dispatchEvent(new CustomEvent('input', {
    detail: { restoredDraft: true }
  }));
  const form = composer.form;
  if (form) {
    form.addEventListener('submit', function (event) {
      if (event.draftStorageAlreadyHandled) return;
      try {
        if (localStorage.getItem(key) === composer.value) localStorage.removeItem(key);
      } catch (_) {}
    });
  }
})();
