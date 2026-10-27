(function () {
  'use strict';

  var form = document.getElementById('project-send');
  var model = document.getElementById('project-conversation-model');
  var effort = document.getElementById('project-conversation-effort');
  var tier = document.getElementById('project-conversation-tier');
  var prompt = form && form.querySelector('textarea[name="prompt"]');
  var messageLength = document.getElementById('project-message-length');
  if (!form || !model || !effort || !tier || !prompt || !messageLength) return;
  var attachmentDraft = window.CodexMessageAttachments && window.CodexMessageAttachments.forForm(form);
  var clientCommandId = form.querySelector('input[name="client_command_id"]');

  function characterCount(value) {
    return Array.from(String(value || '').trim()).length;
  }

  function renderMessageLength() {
    var count = characterCount(prompt.value);
    messageLength.hidden = !count;
    messageLength.textContent = '已输入 ' + count.toLocaleString('zh-CN') + ' 字符';
    messageLength.classList.remove('warning', 'failure');
  }

  var prefixMatch = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
  var appPrefix = prefixMatch ? '/' + prefixMatch[1] : '';
  form.action = appPrefix + '/conversation/new';
  var upload = document.querySelector('#project-upload form');
  if (upload) {
    upload.action = appPrefix + '/files/upload';
    var returnTo = upload.querySelector('input[name="return_to"]');
    if (returnTo) returnTo.value = appPrefix + location.pathname.slice(appPrefix.length);
  }

  function values(name) {
    var selected = model.selectedOptions[0];
    try { return JSON.parse(selected.dataset[name] || '[]'); } catch (error) { return []; }
  }

  function option(value, label, selected) {
    var item = document.createElement('option');
    item.value = value;
    item.textContent = label;
    item.selected = selected;
    return item;
  }

  function update(first) {
    var selected = model.selectedOptions[0];
    var efforts = values('efforts');
    var savedEffort = first ? effort.dataset.current : effort.value;
    var defaultEffort = selected.dataset.defaultEffort || 'medium';
    var nextEffort = (first && savedEffort) || (efforts.includes(savedEffort) ? savedEffort : defaultEffort);
    effort.replaceChildren();
    if (first && efforts.length && !efforts.includes(nextEffort)) effort.appendChild(option(nextEffort, nextEffort + '（不可用）', true));
    (efforts.length ? efforts : [nextEffort]).forEach(function (value) {
      effort.appendChild(option(value, value, value === nextEffort));
    });

    var tiers = values('tiers');
    var savedTier = first ? tier.dataset.current : tier.value;
    tier.replaceChildren(option('', '标准', !savedTier));
    if (first && savedTier && !tiers.some(function (value) { return value.id === savedTier; })) tier.appendChild(option(savedTier, savedTier + '（不可用）', true));
    tiers.forEach(function (value) {
      tier.appendChild(option(value.id, value.name || value.id, value.id === savedTier));
    });
  }

  // A file upload navigates away from this form. Keep this tab's draft settings
  // separate from persistent defaults and from other projects/entry prefixes.
  var collaborationMode = document.getElementById('project-conversation-collaboration-mode');
  var permission = document.getElementById('project-conversation-permission');
  var settingsKey = 'codex-project-settings:' + appPrefix + ':' + prompt.dataset.draftKey;

  function saveSettings() {
    try {
      sessionStorage.setItem(settingsKey, JSON.stringify({
        model: model.value, effort: effort.value, tier: tier.value,
        collaboration_mode: collaborationMode ? collaborationMode.value : null,
        permission: permission ? permission.value : null,
        goal_objective: goalObjective ? goalObjective.value : '', goal_status: goalStatus ? goalStatus.value : 'active'
      }));
    } catch (_) { /* Storage restrictions must not block upload or submission. */ }
  }

  function hasOption(select, value) {
    return typeof value === 'string' && Array.from(select.options).some(function (item) {
      return item.value === value && !item.disabled;
    });
  }

  function restoreSettings() {
    try {
      var saved = JSON.parse(sessionStorage.getItem(settingsKey));
      if (!saved || !hasOption(model, saved.model)) return;
      model.value = saved.model;
      update(false);
      var efforts = values('efforts');
      effort.value = efforts.includes(saved.effort) ? saved.effort :
        model.selectedOptions[0].dataset.defaultEffort || 'medium';
      tier.value = values('tiers').some(function (item) { return item.id === saved.tier; }) ? saved.tier : '';
      if (collaborationMode && hasOption(collaborationMode, saved.collaboration_mode)) collaborationMode.value = saved.collaboration_mode;
      if (permission && hasOption(permission, saved.permission)) permission.value = saved.permission;
      // Also refresh segmented controls if they were initialized before us.
      if (goalObjective && typeof saved.goal_objective === 'string' && saved.goal_objective.length <= 4000) goalObjective.value = saved.goal_objective;
      if (goalStatus && ['active','paused'].includes(saved.goal_status)) goalStatus.value = saved.goal_status;
      model.dispatchEvent(new Event('change', { bubbles: true }));
      effort.dispatchEvent(new Event('change', { bubbles: true }));
    } catch (_) { /* Corrupt or unavailable storage leaves server defaults intact. */ }
  }

  model.addEventListener('change', function () { update(false); saveSettings(); updateCompactControls(); });
  effort.addEventListener('change', saveSettings);
  tier.addEventListener('change', saveSettings);
  if (collaborationMode) collaborationMode.addEventListener('change', saveSettings);
  if (permission) permission.addEventListener('change', saveSettings);
  if (upload) upload.addEventListener('submit', saveSettings);
  prompt.addEventListener('input', renderMessageLength);
  var startSubmission = null;
  window.addEventListener('pagehide', function () {
    if (startSubmission && !startSubmission.defaultPrevented &&
        !(attachmentDraft && attachmentDraft.capture().ids.length)) {
      try { sessionStorage.removeItem(settingsKey); } catch (_) {}
    }
  });
  var attachmentSubmissionReady = false;
  var attachmentSubmissionFlight = false;
  prompt.addEventListener('input', function () {
    if (attachmentSubmissionFlight && attachmentDraft) attachmentDraft.cancel('文字草稿已变更，未发送；请再次点击开始。');
  });
  // draft.js clears on a normal submit.  A first-turn attachment must survive
  // redirects/unknown delivery until its durable command receipt claims it.
  form.addEventListener('submit', function (event) {
    if (attachmentDraft && attachmentDraft.capture().ids.length) event.draftStorageAlreadyHandled = true;
  }, true);
  form.addEventListener('submit', function (event) {
    // A first conversation can redirect through an unknown/rejected delivery
    // state.  Keep its attachment source draft until the destination snapshot
    // has authoritatively claimed the same attachment revision.
    if (attachmentSubmissionReady) { startSubmission = event; return; }
    if (!attachmentDraft) { startSubmission = event; return; }
    if (attachmentSubmissionFlight) { event.preventDefault(); return; }
    var immediateCapture = attachmentDraft.capture();
    // No attachment requires no network validation. Keep the native submit
    // timing (and its existing validation listeners) unchanged.
    if (!immediateCapture.ids.length) {
      var immediateData = new FormData(form); immediateData.delete('client_command_id');
      var immediateTransfer = attachmentDraft.prepareTransfer(immediateCapture, immediateData, prompt.value);
      if (clientCommandId) clientCommandId.value = immediateTransfer.command_id;
      startSubmission = event; return;
    }
    event.preventDefault();
    if (attachmentSubmissionFlight) return;
    attachmentSubmissionFlight = true;
    var submitter = event.submitter;
    var submittedText = prompt.value;
    var capturedBefore = immediateCapture;
    attachmentDraft.ready().then(function (captured) {
      if (prompt.value !== submittedText || captured.revision !== capturedBefore.revision ||
          captured.ids.join(',') !== capturedBefore.ids.join(',')) {
        prompt.setCustomValidity('草稿或附件已变更，未开始新对话；请再次点击开始。');
        prompt.reportValidity(); prompt.setCustomValidity(''); return;
      }
      if (!prompt.value.trim() && !captured.ids.length) { prompt.required = true; form.reportValidity(); return; }
      saveSettings();
      var data = new FormData(form); data.delete('client_command_id');
      var transfer = attachmentDraft.prepareTransfer(captured, data, prompt.value);
      if (clientCommandId) clientCommandId.value = transfer.command_id;
      attachmentSubmissionReady = true;
      try { if (submitter && submitter.name) form.requestSubmit(submitter); else form.requestSubmit(); }
      finally { attachmentSubmissionReady = false; }
    }).catch(function (error) {
      prompt.setCustomValidity((error && error.message) || '附件核验未完成，未开始新对话。');
      prompt.reportValidity(); prompt.setCustomValidity('');
    }).finally(function () { attachmentSubmissionFlight = false; });
  });
  var optionSheet = document.getElementById('project-option-sheet');
  var optionTitle = document.getElementById('project-option-title');
  var optionContent = document.getElementById('project-option-content');
  var optionDone = document.getElementById('project-option-done');
  var optionError = document.getElementById('project-option-error');
  var goalObjective = document.getElementById('project-goal-objective');
  var goalStatus = document.getElementById('project-goal-status');
  var pendingOption = null;
  var permissionNames = { 'read-only': '只读', 'workspace-write': '可写', 'danger-full-access': '完全访问' };
  function control(kind) { return form.querySelector('[data-project-control="' + kind + '"]'); }
  function updateCompactControls() {
    if (!optionSheet) return;
    control('permission').textContent = permissionNames[permission.value] || permission.value;
    control('speed').textContent = tier.value ? (tier.selectedOptions[0].textContent || 'Fast') : '标准速度';
    control('mode').textContent = collaborationMode.value === 'plan' ? 'Plan' : '默认执行';
    control('goal').textContent = goalObjective.value.trim() ? '目标已设置' : '设置目标';
  }
  function closeOptionSheet() { pendingOption = null; if (optionSheet.open) optionSheet.close(); }
  function openOptionSheet(kind) {
    pendingOption = { kind: kind, value: kind === 'goal' ? { objective: goalObjective.value, status: goalStatus.value || 'active' } : ({ permission: permission, speed: tier, mode: collaborationMode }[kind]).value };
    optionError.hidden = true; optionError.textContent = '';
    optionTitle.textContent = { permission: '执行权限', speed: '服务速度', mode: '执行模式', goal: '目标' }[kind];
    if (kind === 'goal') {
      optionContent.innerHTML = '<textarea id="project-goal-draft" class="compact-goal-input" maxlength="4000" placeholder="希望完成什么？"></textarea><div class="compact-status-options"><button type="button" data-project-goal-status="active">进行中</button><button type="button" data-project-goal-status="paused">暂停</button></div>';
      optionContent.querySelector('#project-goal-draft').value = pendingOption.value.objective;
      if (goalObjective.value) { var clear = document.createElement('button'); clear.type = 'button'; clear.className = 'compact-goal-clear'; clear.dataset.projectGoalClear = ''; clear.textContent = '清除目标'; optionContent.appendChild(clear); }
      optionContent.querySelectorAll('[data-project-goal-status]').forEach(function (button) { button.setAttribute('aria-pressed', String(button.dataset.projectGoalStatus === pendingOption.value.status)); });
    } else {
      var select = { permission: permission, speed: tier, mode: collaborationMode }[kind];
      var choices = document.createElement('div'); choices.className = 'compact-option-choices';
      Array.from(select.options).forEach(function (item) { var row = document.createElement('label'); row.className = 'compact-option-choice'; var input = document.createElement('input'); input.type = 'radio'; input.name = 'project-option-choice'; input.value = item.value; input.checked = item.value === select.value; row.append(input, document.createTextNode(item.textContent)); choices.appendChild(row); });
      optionContent.replaceChildren(choices);
    }
    optionSheet.showModal(); requestAnimationFrame(function () { var focus = optionContent.querySelector('textarea, input:checked'); if (focus) focus.focus(); });
  }
  form.querySelectorAll('[data-project-control]').forEach(function (button) { button.addEventListener('click', function () { openOptionSheet(button.dataset.projectControl); }); });
  if (optionSheet && optionContent && optionDone) {
  optionContent.addEventListener('click', function (event) { var clear = event.target.closest('[data-project-goal-clear]'); if (clear && pendingOption && pendingOption.kind === 'goal') { pendingOption.value.clear = !pendingOption.value.clear; clear.textContent = pendingOption.value.clear ? '已选择清除目标' : '清除目标'; optionContent.querySelector('#project-goal-draft').disabled = pendingOption.value.clear; return; } var status = event.target.closest('[data-project-goal-status]'); if (!status || !pendingOption || pendingOption.kind !== 'goal') return; pendingOption.value.status = status.dataset.projectGoalStatus; pendingOption.value.clear = false; var clearChoice = optionContent.querySelector('[data-project-goal-clear]'); if (clearChoice) clearChoice.textContent = '清除目标'; optionContent.querySelector('#project-goal-draft').disabled = false; optionContent.querySelectorAll('[data-project-goal-status]').forEach(function (button) { button.setAttribute('aria-pressed', String(button === status)); }); });
  optionDone.addEventListener('click', function () {
    if (!pendingOption) return;
    if (pendingOption.kind === 'goal') { var draft = optionContent.querySelector('#project-goal-draft').value.trim(); if (!draft && !pendingOption.value.clear) { optionError.textContent = '请输入目标'; optionError.hidden = false; return; } goalObjective.value = pendingOption.value.clear ? '' : draft; goalStatus.value = pendingOption.value.clear ? 'active' : pendingOption.value.status; saveSettings(); }
    else { var choice = optionContent.querySelector('input[name="project-option-choice"]:checked'); if (!choice) return; var select = { permission: permission, speed: tier, mode: collaborationMode }[pendingOption.kind]; select.value = choice.value; select.dispatchEvent(new Event('change', { bubbles: true })); }
    updateCompactControls(); closeOptionSheet();
  });
  optionSheet.querySelector('[data-project-option-close]').addEventListener('click', closeOptionSheet);
  optionSheet.addEventListener('cancel', function () { pendingOption = null; });
  optionSheet.addEventListener('click', function (event) { if (event.target !== optionSheet) return; var rect = optionSheet.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeOptionSheet(); });
  [tier, collaborationMode, permission].forEach(function (select) { select.addEventListener('change', updateCompactControls); });
  }

  update(true);
  restoreSettings();
  renderMessageLength();
  updateCompactControls();
})();
