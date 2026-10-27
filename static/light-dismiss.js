(function () {
  'use strict';

  var pointer = null;
  var tapLimit = 12;
  var tapWindow = 1200;

  function pointOutside(element, event) {
    if (element.tagName === 'DIALOG') {
      if (event.target !== element) return false;
      var rect = element.getBoundingClientRect();
      return event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom;
    }
    if (element.dataset.lightDismiss === 'menu') {
      var selector = element.dataset.lightDismissTrigger;
      var trigger = selector && element.parentElement && element.parentElement.querySelector(selector);
      return !element.contains(event.target) && !(trigger && trigger.contains(event.target));
    }
    return !element.contains(event.target);
  }

  function isOpen(element) {
    if (element.dataset.lightDismiss === 'menu') return !element.hidden;
    return Boolean(element.open);
  }

  function uploadHasFiles(dialog) {
    return Array.prototype.some.call(dialog.querySelectorAll('input[type="file"]'), function (input) {
      return input.files && input.files.length;
    });
  }

  // Upload dialogs keep selected files until their normal form flow finishes.
  function canDismiss(element) {
    if (!isOpen(element)) return false;
    if (element.dataset.lightDismiss !== 'empty-upload') return true;
    return !element.dataset.lightDismissUploading && !uploadHasFiles(element);
  }

  function dismiss(element, restoreFocus) {
    if (element.dataset.lightDismiss === 'menu') element.hidden = true;
    else if (element.tagName === 'DETAILS') element.removeAttribute('open');
    else element.close();
    if (!restoreFocus) return;
    var target = element.tagName === 'DETAILS' ? element.querySelector('summary') : null;
    if (!target && element.dataset.lightDismiss === 'menu') {
      var selector = element.dataset.lightDismissTrigger;
      target = selector && element.parentElement && element.parentElement.querySelector(selector);
    }
    if (target) target.focus({ preventScroll: true });
  }

  function dismissOutside(event, down) {
    document.querySelectorAll('[data-light-dismiss]').forEach(function (element) {
      if (!canDismiss(element) || !pointOutside(element, event)) return;
      if (down && !pointOutside(element, down)) return;
      dismiss(element, false);
    });
  }

  document.addEventListener('pointerdown', function (event) {
    if (pointer && pointer.id !== event.pointerId) pointer.cancelled = true;
    if (!event.isPrimary || (event.pointerType === 'mouse' && event.button !== 0)) return;
    pointer = {
      id: event.pointerId,
      target: event.target,
      clientX: event.clientX,
      clientY: event.clientY,
      moved: false,
      cancelled: false,
      time: Date.now()
    };
  }, true);

  document.addEventListener('pointermove', function (event) {
    if (!pointer || pointer.id !== event.pointerId) return;
    if (Math.abs(event.clientX - pointer.clientX) > tapLimit ||
        Math.abs(event.clientY - pointer.clientY) > tapLimit) pointer.moved = true;
  }, true);

  document.addEventListener('pointercancel', function (event) {
    if (pointer && pointer.id === event.pointerId) pointer.cancelled = true;
  }, true);

  // A close must be a stationary tap that started outside as well.
  document.addEventListener('click', function (event) {
    var down = pointer;
    pointer = null;
    if (down && (down.cancelled || down.moved || Date.now() - down.time > tapWindow)) return;
    dismissOutside(event, down);
  }, true);

  document.addEventListener('submit', function (event) {
    var dialog = event.target.closest('dialog[data-light-dismiss="empty-upload"]');
    if (!dialog) return;
    dialog.dataset.lightDismissUploading = 'true';
    // Other form handlers may cancel submission after this capture listener.
    Promise.resolve().then(function () {
      if (event.defaultPrevented || !dialog.open) delete dialog.dataset.lightDismissUploading;
    });
  }, true);

  document.addEventListener('close', function (event) {
    if (event.target.tagName === 'DIALOG' && event.target.dataset.lightDismiss === 'empty-upload') {
      delete event.target.dataset.lightDismissUploading;
    }
  }, true);

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || document.querySelector('dialog[open]')) return;
    document.querySelectorAll('[data-light-dismiss="details"], [data-light-dismiss="menu"]').forEach(function (element) {
      if (canDismiss(element)) dismiss(element, true);
    });
  }, true);
}());
