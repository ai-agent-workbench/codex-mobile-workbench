(function () {
  "use strict";
  var card = document.getElementById("codex-quota-card");
  if (!card) return;
  var endpoint = card.dataset.endpoint;
  var refresh = document.getElementById("codex-quota-refresh");
  var plan = document.getElementById("codex-quota-plan");
  var bar = document.getElementById("codex-weekly-bar");
  var fill = document.getElementById("codex-weekly-fill");
  var value = document.getElementById("codex-weekly-value");
  var status = document.getElementById("codex-quota-status");
  var resetTimestamp = null;
  var statusHasError = false;
  var autoReloadedReset = null;
  var planLabels = {
    plus: "ChatGPT Plus",
    pro: "ChatGPT Pro",
    team: "ChatGPT Business",
    business: "ChatGPT Business",
    enterprise: "ChatGPT Enterprise",
    edu: "ChatGPT Edu"
  };

  function percentage(number) {
    return Number.isInteger(number) ? String(number) : number.toFixed(1);
  }

  function resetLabel(timestamp) {
    var date = new Date(timestamp * 1000);
    if (Number.isNaN(date.getTime())) throw new Error("invalid reset time");
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
      hour12: false
    }).format(date) + " 重置";
  }

  function remainingLabel(timestamp, now) {
    var minutes = Math.ceil((timestamp * 1000 - now) / 60000);
    if (minutes <= 0) return "即将重置";
    var days = Math.floor(minutes / 1440);
    var hours = Math.floor((minutes % 1440) / 60);
    if (days > 0) return "还剩 " + days + "d" + (hours > 0 ? " " + hours + "h" : "");
    if (hours > 0) return "还剩 " + hours + "h";
    return "还剩 " + minutes + "分钟";
  }

  function updateCountdown() {
    if (resetTimestamp === null || statusHasError) return;
    var label = remainingLabel(resetTimestamp, Date.now()) + " · " + resetLabel(resetTimestamp);
    if (status.textContent !== label) status.textContent = label;
    if (resetTimestamp * 1000 <= Date.now() && autoReloadedReset !== resetTimestamp) {
      autoReloadedReset = resetTimestamp;
      loadQuota();
    }
  }

  function showQuota(payload) {
    var remaining = payload && payload.remaining_percent;
    if (typeof remaining !== "number" || !Number.isFinite(remaining) || remaining < 0 || remaining > 100) {
      throw new Error("invalid percentage");
    }
    fill.style.width = remaining + "%";
    value.textContent = "剩余 " + percentage(remaining) + "%";
    bar.setAttribute("aria-valuenow", String(remaining));
    bar.setAttribute("aria-valuetext", value.textContent);
    var rawPlan = typeof payload.plan_type === "string" ? payload.plan_type.trim() : "";
    plan.textContent = planLabels[rawPlan.toLowerCase()] || rawPlan || "订阅额度";
    resetLabel(payload.resets_at);
    resetTimestamp = payload.resets_at;
    statusHasError = false;
    status.classList.remove("error");
    updateCountdown();
  }

  async function loadQuota() {
    if (refresh.disabled) return;
    refresh.disabled = true;
    refresh.classList.add("loading");
    status.classList.remove("error");
    status.textContent = "正在读取最新额度…";
    try {
      var response = await fetch(endpoint, {
        method: "GET", credentials: "same-origin", cache: "no-store",
        headers: {Accept: "application/json"}
      });
      var payload = await response.json();
      if (!response.ok) throw new Error("quota unavailable");
      showQuota(payload);
    } catch (_error) {
      statusHasError = true;
      status.classList.add("error");
      status.textContent = value.textContent === "—"
        ? "暂时无法读取，请点刷新重试"
        : "刷新失败，当前保留上次数据";
    } finally {
      refresh.disabled = false;
      refresh.classList.remove("loading");
    }
  }

  refresh.addEventListener("click", loadQuota);
  window.setInterval(updateCountdown, 60000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) updateCountdown();
  });
  loadQuota();
})();
