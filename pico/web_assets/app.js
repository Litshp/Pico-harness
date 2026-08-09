(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const shell = $("#app-shell");
  const stream = $("#message-stream");
  const input = $("#message-input");
  const sendButton = $("#send-button");
  const errorBox = $("#composer-error");
  const approvalDialog = $("#approval-dialog");
  const clearDialog = $("#clear-dialog");
  const runtimeDialog = $("#runtime-dialog");
  const toast = $("#toast");
  let state = null;
  let historySignature = "";
  let sessionSignature = "";
  let pendingApprovalId = "";
  let toastTimer = null;

  function refreshIcons() {
    if (window.lucide) {
      window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
    }
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `Request failed (${response.status})`);
    }
    return payload;
  }

  function showToast(message, isError = false) {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.hidden = false;
    toastTimer = window.setTimeout(() => {
      toast.hidden = true;
    }, 4200);
  }

  function basename(path) {
    const parts = String(path || "").split("/").filter(Boolean);
    return parts.at(-1) || path || "workspace";
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function formatSessionTime(seconds) {
    const date = new Date(Number(seconds) * 1000);
    if (Number.isNaN(date.getTime())) return "";
    const today = new Date();
    if (date.toDateString() === today.toDateString()) return formatTime(date);
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
  }

  function setText(selector, value) {
    const element = $(selector);
    if (element) element.textContent = value ?? "-";
  }

  function messageSignature(history, active) {
    return JSON.stringify([
      active,
      ...history.map((item) => [item.role, item.name, item.created_at, item.content, item.args]),
    ]);
  }

  function renderHistory(history, active) {
    const signature = messageSignature(history, active);
    if (signature === historySignature) return;
    const wasNearBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 120;
    historySignature = signature;
    stream.replaceChildren();

    if (!history.length && !active) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const mark = document.createElement("div");
      mark.className = "empty-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = "p";
      const heading = document.createElement("h1");
      heading.textContent = "准备就绪";
      const workspace = document.createElement("p");
      workspace.textContent = state?.agent?.workspace || "当前工作区";
      empty.append(mark, heading, workspace);
      stream.append(empty);
      return;
    }

    const list = document.createElement("div");
    list.className = "message-list";
    for (const item of history) {
      list.append(renderMessage(item));
    }
    if (active) {
      const working = document.createElement("div");
      working.className = "typing-row";
      const dots = document.createElement("span");
      dots.className = "typing-dots";
      dots.setAttribute("aria-hidden", "true");
      dots.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
      const label = document.createElement("span");
      label.textContent = "Pico 正在工作";
      working.append(dots, label);
      list.append(working);
    }
    stream.append(list);
    if (wasNearBottom || active) {
      requestAnimationFrame(() => stream.scrollTo({ top: stream.scrollHeight, behavior: "smooth" }));
    }
  }

  function renderMessage(item) {
    const role = item.role === "tool" ? "tool" : item.role === "assistant" ? "assistant" : "user";
    const article = document.createElement("article");
    article.className = `message ${role}`;
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = role === "user" ? "YOU" : role === "tool" ? "T" : "P";

    const body = document.createElement("div");
    body.className = "message-body";
    const header = document.createElement("div");
    header.className = "message-header";
    const author = document.createElement("span");
    author.className = "message-author";
    author.textContent = role === "user" ? "你" : role === "tool" ? "工具" : "Pico";
    const time = document.createElement("time");
    time.className = "message-time";
    time.textContent = formatTime(item.created_at);
    header.append(author, time);
    body.append(header);

    if (role === "tool") {
      const details = document.createElement("details");
      details.className = "tool-details";
      const summary = document.createElement("summary");
      summary.textContent = item.name || "tool";
      const output = document.createElement("pre");
      output.className = "tool-output";
      const args = item.args && Object.keys(item.args).length ? `${JSON.stringify(item.args, null, 2)}\n\n` : "";
      output.textContent = `${args}${item.content || ""}`;
      details.append(summary, output);
      body.append(details);
    } else {
      const content = document.createElement("div");
      content.className = "message-content";
      content.textContent = item.content || "";
      body.append(content);
    }

    article.append(avatar, body);
    return article;
  }

  function renderSessions(sessions) {
    const signature = JSON.stringify(sessions.map((item) => [item.id, item.title, item.active, item.updated_at]));
    if (signature === sessionSignature) return;
    sessionSignature = signature;
    const list = $("#session-list");
    list.replaceChildren();
    setText("#session-count", sessions.length);
    for (const session of sessions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `session-item${session.active ? " active" : ""}`;
      button.dataset.sessionId = session.id;
      button.setAttribute("aria-current", session.active ? "page" : "false");
      const icon = document.createElement("i");
      icon.setAttribute("data-lucide", session.active ? "message-square-dot" : "message-square");
      const copy = document.createElement("span");
      copy.className = "session-copy";
      const title = document.createElement("span");
      title.className = "session-title";
      title.textContent = session.title;
      const updated = document.createElement("span");
      updated.className = "session-time";
      updated.textContent = formatSessionTime(session.updated_at);
      copy.append(title, updated);
      button.append(icon, copy);
      button.addEventListener("click", () => switchSession(session.id));
      list.append(button);
    }
    refreshIcons();
  }

  function renderActivity(history, task) {
    const list = $("#activity-list");
    list.replaceChildren();
    const events = [];
    for (const item of history.filter((entry) => entry.role === "tool").slice(-5).reverse()) {
      events.push({ title: item.name || "tool", detail: formatTime(item.created_at) || "tool result" });
    }
    if (task?.stop_reason) events.unshift({ title: task.status || "finished", detail: task.stop_reason });
    if (!events.length) events.push({ title: "Runtime ready", detail: "等待任务" });
    for (const event of events) {
      const item = document.createElement("li");
      const node = document.createElement("span");
      node.className = "activity-node";
      const copy = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = event.title;
      const detail = document.createElement("span");
      detail.textContent = event.detail;
      copy.append(title, detail);
      item.append(node, copy);
      list.append(item);
    }
  }

  function renderApproval(pending) {
    if (!pending) {
      pendingApprovalId = "";
      if (approvalDialog.open) approvalDialog.close();
      return;
    }
    if (pending.id === pendingApprovalId && approvalDialog.open) return;
    pendingApprovalId = pending.id;
    setText("#approval-tool", pending.tool);
    setText("#approval-args", JSON.stringify(pending.args || {}, null, 2));
    if (!approvalDialog.open) approvalDialog.showModal();
  }

  function render(nextState) {
    state = nextState;
    const agent = state.agent || {};
    const runtime = state.runtime || {};
    const task = state.task_state || {};
    const active = Boolean(runtime.active);

    setText("#branch-name", agent.branch);
    setText("#model-name", agent.model);
    setText("#workspace-path", agent.workspace);
    setText("#sidebar-workspace", basename(agent.repo_root));
    setText("#approval-mode", agent.approval);
    setText("#step-budget", `${agent.max_steps || 0} 步`);
    setText("#max-steps", agent.max_steps || 0);
    setText("#provider-name", agent.provider);
    setText("#detail-approval", agent.approval);
    setText("#session-id", agent.session_id);
    setText("#attempt-count", task.attempts || 0);
    setText("#tool-count", task.tool_steps || 0);
    setText("#last-tool", task.last_tool || "-");
    setText("#stop-reason", task.stop_reason || "-");
    setText("#run-state", active ? "working" : task.status || "idle");

    renderConfiguration(agent.configuration || {});

    const status = $("#runtime-status");
    status.classList.toggle("working", active);
    status.classList.toggle("error", Boolean(runtime.error));
    status.querySelector("span:last-child").textContent = runtime.error ? "错误" : active ? "运行中" : "就绪";
    stream.setAttribute("aria-busy", String(active));
    sendButton.disabled = active;
    input.disabled = active;
    sendButton.querySelector("span").textContent = active ? "运行中" : "发送";

    if (runtime.error) {
      errorBox.textContent = runtime.error;
      errorBox.hidden = false;
    } else {
      errorBox.hidden = true;
    }

    renderHistory(state.history || [], active);
    renderSessions(state.sessions || []);
    renderActivity(state.history || [], state.task_state);
    renderApproval(runtime.pending_approval);
  }

  function renderConfiguration(configuration) {
    if (runtimeDialog.open) return;
    const config = {
      workspace: configuration.workspace || state?.agent?.workspace || "",
      provider: configuration.provider || "deepseek",
      model: configuration.model || state?.agent?.model || "",
      endpoint: configuration.endpoint || "",
    };
    $("#config-workspace").value = config.workspace;
    $("#config-provider").value = config.provider;
    $("#config-model").value = config.model;
    $("#config-endpoint").value = config.endpoint;
    updateEndpointCopy();
  }

  function updateEndpointCopy() {
    const isOllama = $("#config-provider").value === "ollama";
    setText("#config-endpoint-label", isOllama ? "Ollama 地址" : "自定义 API 地址");
    setText(
      "#config-endpoint-help",
      isOllama ? "留空时使用 http://127.0.0.1:11434。" : "仅在需要代理或自定义兼容端点时填写。",
    );
    $("#config-endpoint").placeholder = isOllama
      ? "http://127.0.0.1:11434"
      : "留空时使用该后端的默认地址";
  }

  async function poll() {
    try {
      render(await api("/api/state"));
    } catch (error) {
      const status = $("#runtime-status");
      status.classList.add("error");
      status.querySelector("span:last-child").textContent = "离线";
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } finally {
      window.setTimeout(poll, state?.runtime?.active ? 550 : 1100);
    }
  }

  async function submitMessage(event) {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || sendButton.disabled) return;
    sendButton.disabled = true;
    errorBox.hidden = true;
    try {
      await api("/api/messages", { method: "POST", body: JSON.stringify({ message }) });
      input.value = "";
      resizeInput();
      render(await api("/api/state"));
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      sendButton.disabled = false;
    }
  }

  async function resolveApproval(approved) {
    if (!pendingApprovalId) return;
    const id = pendingApprovalId;
    $("#allow-approval").disabled = true;
    $("#deny-approval").disabled = true;
    try {
      await api("/api/approval", { method: "POST", body: JSON.stringify({ id, approved }) });
      approvalDialog.close();
      pendingApprovalId = "";
    } catch (error) {
      showToast(error.message, true);
    } finally {
      $("#allow-approval").disabled = false;
      $("#deny-approval").disabled = false;
    }
  }

  async function switchSession(sessionId) {
    if (state?.runtime?.active || sessionId === state?.agent?.session_id) return;
    try {
      await api("/api/session/switch", { method: "POST", body: JSON.stringify({ session_id: sessionId }) });
      historySignature = "";
      sessionSignature = "";
      closeSidebar();
      render(await api("/api/state"));
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function newSession() {
    if (state?.runtime?.active) return;
    try {
      await api("/api/session/new", { method: "POST", body: "{}" });
      historySignature = "";
      sessionSignature = "";
      closeSidebar();
      render(await api("/api/state"));
      input.focus();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function clearSession() {
    try {
      await api("/api/session/reset", { method: "POST", body: "{}" });
      clearDialog.close();
      historySignature = "";
      render(await api("/api/state"));
      showToast("当前会话已清空");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function configureRuntime(event) {
    event.preventDefault();
    if (state?.runtime?.active) return;
    const button = $("#apply-runtime-settings");
    const error = $("#config-error");
    const settings = {
      workspace: $("#config-workspace").value.trim(),
      provider: $("#config-provider").value,
      model: $("#config-model").value.trim(),
      endpoint: $("#config-endpoint").value.trim(),
    };
    button.disabled = true;
    error.hidden = true;
    try {
      await api("/api/configuration", { method: "POST", body: JSON.stringify(settings) });
      runtimeDialog.close();
      historySignature = "";
      sessionSignature = "";
      render(await api("/api/state"));
      showToast("运行时已切换");
      input.focus();
    } catch (exception) {
      error.textContent = exception.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  }

  function resizeInput() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  }

  function closeSidebar() {
    shell.classList.remove("sidebar-open");
    $("#mobile-scrim").hidden = true;
  }

  function closeInspector() {
    shell.classList.remove("inspector-open");
    $("#toggle-inspector").setAttribute("aria-expanded", "false");
  }

  $("#composer").addEventListener("submit", submitMessage);
  input.addEventListener("input", resizeInput);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#new-session").addEventListener("click", newSession);
  $("#clear-session").addEventListener("click", () => {
    if (!state?.runtime?.active && !clearDialog.open) clearDialog.showModal();
  });
  $("#open-runtime-settings").addEventListener("click", () => {
    if (!state?.runtime?.active && !runtimeDialog.open) runtimeDialog.showModal();
  });
  $("#runtime-form").addEventListener("submit", configureRuntime);
  $("#config-provider").addEventListener("change", updateEndpointCopy);
  $("#close-runtime-settings").addEventListener("click", () => runtimeDialog.close());
  $("#cancel-runtime-settings").addEventListener("click", () => runtimeDialog.close());
  $("#confirm-clear").addEventListener("click", clearSession);
  $("#allow-approval").addEventListener("click", () => resolveApproval(true));
  $("#deny-approval").addEventListener("click", () => resolveApproval(false));
  approvalDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    resolveApproval(false);
  });
  $("#open-sidebar").addEventListener("click", () => {
    shell.classList.add("sidebar-open");
    $("#mobile-scrim").hidden = false;
  });
  $("#close-sidebar").addEventListener("click", closeSidebar);
  $("#mobile-scrim").addEventListener("click", closeSidebar);
  $("#toggle-inspector").addEventListener("click", () => {
    const open = shell.classList.toggle("inspector-open");
    $("#toggle-inspector").setAttribute("aria-expanded", String(open));
  });
  $("#close-inspector").addEventListener("click", closeInspector);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSidebar();
      closeInspector();
    }
  });

  refreshIcons();
  resizeInput();
  poll();
})();
