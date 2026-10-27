(function () {
  'use strict';
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function refresh(root) {
    const selected = [...root.querySelectorAll('[data-skill-input]:checked')].map(input => input.value);
    const summary = root.querySelector('[data-skill-summary]');
    summary.hidden = !selected.length;
    summary.textContent = selected.length ? '已指定 ' + selected.length + ' 项' : '';
    const unavailable = root.querySelectorAll('[data-unavailable]:checked').length;
    const warning = root.querySelector('[data-skill-warning]');
    warning.hidden = !unavailable;
    warning.textContent = '有 ' + unavailable + ' 项已指定技能无法从当前目录确认，请取消指定或稍后重试。';
    root.querySelector('[data-skill-clear]').hidden = !selected.length;
    const query = root.querySelector('[data-skill-search]').value.trim().toLowerCase();
    let visible = 0;
    root.querySelectorAll('[data-skill-choice]').forEach(row => {
      row.hidden = !(row.dataset.search || row.textContent).toLowerCase().includes(query);
      if (!row.hidden) visible++;
    });
    root.querySelectorAll('[data-skill-group]').forEach(group => {
      group.hidden = ![...group.querySelectorAll('[data-skill-choice]')].some(row => !row.hidden);
    });
    const empty = root.querySelector('[data-skill-empty]');
    empty.hidden = !!visible;
    empty.textContent = query ? '未找到匹配的技能。清空搜索可查看全部。' : '当前目录没有可手动指定的技能。';
    return selected;
  }
  function changed(root) {
    root.querySelectorAll('[data-unavailable]').forEach(input => { input.disabled = !input.checked; });
    root.dispatchEvent(new CustomEvent('skill-picker-change', {bubbles:true, detail:{selected:refresh(root)}}));
  }
  function init(root) {
    if (root.dataset.ready) return;
    root.dataset.ready = 'true';
    root.querySelector('[data-skill-search]').addEventListener('input', () => refresh(root));
    root.addEventListener('change', event => { if (event.target.matches('[data-skill-input]')) changed(root); });
    root.querySelector('[data-skill-clear]').addEventListener('click', () => {
      root.querySelectorAll('[data-skill-input]').forEach(input => { input.checked = false; });
      root.querySelector('[data-skill-search]').value = '';
      changed(root);
      root.querySelector('.skill-advanced').open = false;
      root.querySelector('.skill-advanced summary').focus();
    });
    refresh(root);
  }
  function render(root, rows, selected) {
    init(root);
    const list = root.querySelector('[data-skill-list]');
    list.replaceChildren();
    const groups = new Map();
    rows.forEach(row => {
      const groupName = row.group_label || '其他技能';
      if (!groups.has(groupName)) {
        const section = make('section', undefined, 'skill-group');section.dataset.skillGroup = '';
        section.append(make('h3', groupName));groups.set(groupName, section);list.append(section);
      }
      const label = make('label', undefined, 'skill-choice');label.dataset.skillChoice = '';
      label.dataset.search = [row.name, row.title, row.usage, row.description].join(' ');
      const input = make('input');input.type = 'checkbox';input.value = row.name;input.dataset.skillInput = '';
      input.checked = selected.includes(row.name);input.disabled = !row.enabled && !input.checked;
      if (!row.enabled) input.dataset.unavailable = 'true';
      input.setAttribute('aria-label', '默认选择 ' + row.name);
      const text = make('span');text.append(make('strong', row.title || row.name),
        make('small', row.usage || '不确定时保持自动选择。'), make('small', row.name, 'skill-name'));
      if ((!row.title || row.title === row.name) && row.description) text.append(make('small', row.description));
      if (!row.enabled) text.append(make('small', row.unavailable_reason || '已不可用，请取消此默认选择后保存。', 'skill-warning'));
      label.append(input, text);groups.get(groupName).append(label);
    });
    refresh(root);
  }
  window.codexSkillPicker = {init, render, refresh};
  document.querySelectorAll('[data-skill-picker]').forEach(init);
})();
