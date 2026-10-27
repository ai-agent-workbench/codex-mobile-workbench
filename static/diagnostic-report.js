(() => {
  "use strict";
  const form = document.getElementById("fault-report");
  if (!form) return;
  const status = document.getElementById("submit-status");
  const button = document.getElementById("submit-button");
  const faultTime = document.getElementById("fault-time");
  let inFlight = false;
  const isoLocal = date => { const z = n => String(n).padStart(2, "0"); return `${date.getFullYear()}-${z(date.getMonth()+1)}-${z(date.getDate())}T${z(date.getHours())}:${z(date.getMinutes())}`; };
  // Server markup is UTC for the no-script form; interactive users see their
  // local wall clock and submission converts it back to an explicit UTC instant.
  const serverInstant = new Date(faultTime.dataset.serverUtc);
  if (!Number.isNaN(serverInstant.getTime())) faultTime.value = isoLocal(serverInstant);
  const reportId = document.getElementById("report-id");
  const hexId = () => {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
    if (window.crypto && crypto.getRandomValues) { const b = new Uint8Array(16); crypto.getRandomValues(b); return [...b].map(x => x.toString(16).padStart(2,"0")).join(""); }
    return "";
  };
  // Do not overwrite a server-issued id: it makes no-JS errors and uncertain
  // deliveries manually retryable with the same idempotency key.
  if (!reportId.value) reportId.value = hexId();
  try {
    const raw = sessionStorage.getItem("codex-startup-v1"), value = raw && JSON.parse(raw);
    if (value && typeof value === "object") {
      const put = (id, key) => { if (typeof value[key] === "string") document.getElementById(id).value = value[key]; };
      put("client-page-id", "page_id"); put("source-path", "path"); put("last-phase", "phase"); put("client-time", "ts"); put("client-version", "version");
    }
  } catch (_) { /* Optional startup projection is absent or malformed. */ }
  const errorText = code => ({
    save_unconfirmed: "未确认保存。请勿自动重发；如需重试请手动提交。", authentication_required: "登录已失效，请重新登录后再试。", origin_required: "此请求必须从当前工作台页面提交。",
    body_too_large: "提交内容超过允许大小。", image_too_large: "截图超过 3 MiB，未保存。",
    invalid_image: "截图必须是手动选择的 PNG 或 JPEG 文件。", report_capacity_exhausted: "本机报告存储已满，未保存；不会自动删除已有报告。",
    report_id_conflict: "此报告编号已用于不同内容。请刷新页面建立新的报告，避免覆盖原报告。",
    report_storage_unavailable: "本机报告存储暂不可用，未确认保存。"
  })[code] || "提交内容无效，未保存；请检查后手动再试。";
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (inFlight) return;
    const local = new Date(faultTime.value);
    if (!faultTime.value || Number.isNaN(local.getTime())) { status.textContent = "请填写有效的发生时间。"; return; }
    inFlight = true; button.disabled = true;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    status.textContent = "正在保存…";
    try {
      const data = new FormData(form);
      data.set("fault_time", local.toISOString());
      const response = await fetch(form.action, {method:"POST", body:data, credentials:"same-origin", headers:{Accept:"application/json"}, signal:controller.signal});
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.saved) throw new Error(body.error || "save_unconfirmed");
      status.textContent = `已保存，报告编号：${body.report_id}${body.diagnostics_logged ? "" : "；诊断日志关联未确认。"}`;
    } catch (error) {
      status.textContent = error.name === "AbortError" ? "15 秒内未确认保存。请勿自动重发；如需重试请手动提交。" : errorText(error.message);
      // The user may manually retry the same server/client id; never do it here.
      button.disabled = false;
    } finally { clearTimeout(timer); inFlight = false; }
  });
})();
