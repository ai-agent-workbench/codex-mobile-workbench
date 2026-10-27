(function () {
  'use strict';

  var effortLabels = {
    none: '无', minimal: '极低', low: '低', medium: '中', high: '高',
    xhigh: 'XH', max: 'MAX', ultra: 'U'
  };

  function updateEdges(group) {
    group.classList.toggle('has-more-left', group.scrollLeft > 1);
    group.classList.toggle('has-more-right',
      group.scrollLeft + group.clientWidth < group.scrollWidth - 1);
  }

  function revealSelected(group) {
    if (!group.classList.contains('setting-segments-model') || !group.clientWidth) return;
    var selected = group.querySelector('.selected');
    if (!selected) return;
    var viewport = group.getBoundingClientRect();
    var button = selected.getBoundingClientRect();
    // Move only this strip, never the page or its containing settings panel.
    if (button.left < viewport.left + 14) {
      group.scrollLeft += button.left - viewport.left - 14;
    } else if (button.right > viewport.right - 14) {
      group.scrollLeft += button.right - viewport.right + 14;
    }
    updateEdges(group);
  }

  function render(select, group) {
    var hadFocus = group.contains(document.activeElement);
    group.replaceChildren();
    Array.from(select.options).forEach(function (option) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'setting-segment';
      button.dataset.value = option.value;
      button.textContent = select.dataset.segmentSource === 'effort' ?
        (effortLabels[option.value.toLowerCase()] || option.textContent) :
        option.textContent.replace(/^GPT[- ]?/i, '').replace(/-/g, ' ');
      button.classList.toggle('selected', option.value === select.value);
      button.setAttribute('aria-pressed', String(option.value === select.value));
      button.addEventListener('click', function () {
        if (select.value === option.value) return;
        select.value = option.value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
      });
      group.appendChild(button);
    });
    if (hadFocus && group.querySelector('.selected')) {
      group.querySelector('.selected').focus({ preventScroll: true });
    }
    requestAnimationFrame(function () { revealSelected(group); });
  }

  document.querySelectorAll('select[data-segment-source]').forEach(function (select) {
    var group = document.createElement('div');
    group.className = 'setting-segments setting-segments-' + select.dataset.segmentSource;
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', select.dataset.segmentSource === 'effort' ? '思考等级' : '模型');
    select.classList.add('setting-segment-source');
    select.insertAdjacentElement('afterend', group);
    render(select, group);
    if (select.dataset.segmentSource === 'model') {
      group.addEventListener('scroll', function () { updateEdges(group); }, { passive: true });
      var details = select.closest('details');
      if (details) details.addEventListener('toggle', function () {
        if (details.open) requestAnimationFrame(function () { revealSelected(group); });
      });
      new ResizeObserver(function () { revealSelected(group); }).observe(group);
    }
    select.addEventListener('change', function () { render(select, group); });
    new MutationObserver(function () { render(select, group); }).observe(select, { childList: true });
  });
})();
