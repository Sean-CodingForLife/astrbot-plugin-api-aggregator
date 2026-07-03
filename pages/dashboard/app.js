const state = {
  groups: [],
  apis: [],
  test_logs: [],
  settings: { strategy: "first-ok", cursors: {} },
  selectedGroupId: "all",
  search: "",
};
const busyState = new Map();

const $ = (id) => document.getElementById(id);

function log(message, data) {
  const lines = [`[${new Date().toLocaleTimeString()}] ${message}`];
  if (data !== undefined) lines.push(JSON.stringify(data, null, 2));
  $("logOutput").textContent = lines.join("\n");
}

function setBusy(key, active) {
  if (active) busyState.set(key, true);
  else busyState.delete(key);
  document.body.classList.toggle("is-busy", busyState.size > 0);
}

async function withBusy(key, options, task) {
  const target = typeof options?.target === "string" ? $(options.target) : options?.target;
  const startText = options?.startText;
  const busyText = options?.busyText;
  const restoreText = target && "textContent" in target ? target.textContent : null;

  if (startText) {
    log(startText);
  }
  if (target && "disabled" in target) {
    target.disabled = true;
  }
  if (target && busyText && "textContent" in target) {
    target.textContent = busyText;
  }
  setBusy(key, true);
  try {
    return await task();
  } finally {
    setBusy(key, false);
    if (target && "disabled" in target) {
      target.disabled = false;
    }
    if (target && restoreText !== null && "textContent" in target) {
      target.textContent = restoreText;
    }
  }
}

function parseNumberInput(id, fallback, minimum, maximum) {
  const raw = $(id).value;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(minimum, Math.min(maximum, parsed));
}

function bridge() {
  const api = window.AstrBotPluginPage;
  if (!api || typeof api.ready !== "function" || typeof api.apiGet !== "function" || typeof api.apiPost !== "function") {
    throw new Error("AstrBotPluginPage bridge unavailable");
  }
  return api;
}

async function apiGet(endpoint, params = {}) {
  const api = bridge();
  await api.ready();
  return await api.apiGet(endpoint, params);
}

async function apiPost(endpoint, body = {}) {
  const api = bridge();
  await api.ready();
  return await api.apiPost(endpoint, body);
}

function applyState(data) {
  state.groups = Array.isArray(data.groups) ? data.groups : [];
  state.apis = Array.isArray(data.apis) ? data.apis : [];
  state.test_logs = Array.isArray(data.test_logs) ? data.test_logs : [];
  state.settings = data.settings && typeof data.settings === "object" ? data.settings : { strategy: "first-ok", cursors: {} };
  if (state.selectedGroupId !== "all" && !state.groups.some((group) => group.id === state.selectedGroupId)) {
    state.selectedGroupId = "all";
  }
  render();
}

async function refresh() {
  return withBusy("refresh", { target: "refreshBtn", busyText: "Refreshing...", startText: "Refreshing state..." }, async () => {
    const data = await apiGet("state");
    applyState(data);
    log("State refreshed", { apis: state.apis.length, groups: state.groups.length });
  });
}

function groupName(id) {
  return state.groups.find((group) => group.id === id)?.name || "Unknown";
}

function cooldownLabel(api) {
  const remainingMs = Math.max(0, Number(api?.cooldown_until || 0) - Date.now());
  if (!remainingMs) return "";
  return `Cooldown ${Math.ceil(remainingMs / 1000)}s`;
}

function summarizeAggregateResult(result) {
  if (!result || typeof result !== "object") return "No aggregate result.";
  if (result.ok && result.selected) {
    return [
      `Selected API: ${result.selected.api_name}`,
      `Status: ${result.selected.status ?? "-"}`,
      `Elapsed: ${result.selected.elapsed_ms ?? 0} ms`,
      `Attempts: ${Array.isArray(result.attempts) ? result.attempts.length : 0}`,
    ].join("\n");
  }
  return [
    "Aggregate call failed.",
    `Reason: ${result.failure_reason || "No candidate succeeded."}`,
    `Attempts: ${Array.isArray(result.attempts) ? result.attempts.length : 0}`,
  ].join("\n");
}

function summarizePreview(preview, result) {
  const lines = [
    preview?.message || "Preview finished.",
    `Matched: ${preview?.matched ? "yes" : "no"}`,
    `Type: ${preview?.value_type || "unknown"}`,
  ];
  if (result?.attempt_count) {
    lines.push(`Request attempts: ${result.attempt_count}`);
  }
  if (result?.error) {
    lines.push(`Request error: ${result.error}`);
  }
  lines.push("");
  lines.push(JSON.stringify(preview, null, 2));
  return lines.join("\n");
}

function filteredApis() {
  const query = state.search.trim().toLowerCase();
  return state.apis.filter((api) => {
    const groupOk = state.selectedGroupId === "all" || api.group_id === state.selectedGroupId;
    const searchOk = !query || `${api.name} ${api.url} ${api.description}`.toLowerCase().includes(query);
    return groupOk && searchOk;
  });
}

function renderSummary() {
  $("apiCount").textContent = String(state.apis.length);
  $("enabledCount").textContent = String(state.apis.filter((api) => api.enabled).length);
  $("okCount").textContent = String(state.apis.filter((api) => api.last_ok).length);
}

function renderGroups() {
  const groupList = $("groupList");
  const allCount = state.apis.length;
  const items = [
    `<div class="group-row"><button class="group-item ${state.selectedGroupId === "all" ? "is-active" : ""}" data-action="select-group" data-id="all"><span>All</span><span>${allCount}</span></button></div>`,
    ...state.groups.map((group) => {
      const count = state.apis.filter((api) => api.group_id === group.id).length;
      const deleteButton = group.id === "default"
        ? ""
        : `<button class="danger" data-action="delete-group" data-id="${escapeHtml(group.id)}">Delete</button>`;
      return `
        <div class="group-row">
          <button class="group-item ${state.selectedGroupId === group.id ? "is-active" : ""}" data-action="select-group" data-id="${escapeHtml(group.id)}"><span>${escapeHtml(group.name)}</span><span>${count}</span></button>
          <button data-action="edit-group" data-id="${escapeHtml(group.id)}">Edit</button>
          ${deleteButton}
        </div>
      `;
    }),
  ];
  groupList.innerHTML = items.join("");
}

function renderAggregateControls() {
  const options = [
    `<option value="all">All enabled APIs</option>`,
    ...state.groups.map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`),
  ];
  const aggregateGroup = $("aggregateGroup");
  const currentGroup = aggregateGroup.value || "all";
  aggregateGroup.innerHTML = options.join("");
  aggregateGroup.value = currentGroup === "all" || state.groups.some((group) => group.id === currentGroup) ? currentGroup : "all";
  $("strategySelect").value = state.settings.strategy || "first-ok";
}

function groupOptions(selectedId = "all") {
  const options = [
    `<option value="all">All enabled APIs</option>`,
    ...state.groups.map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`),
  ];
  const valid = selectedId === "all" || state.groups.some((group) => group.id === selectedId);
  return { html: options.join(""), value: valid ? selectedId : "all" };
}

function apiOptions(selectedId = "") {
  const options = state.apis.map((api) => `<option value="${escapeHtml(api.id)}">${escapeHtml(api.name)}</option>`);
  const valid = selectedId && state.apis.some((api) => api.id === selectedId);
  return { html: options.join(""), value: valid ? selectedId : state.apis[0]?.id || "" };
}

function renderTriggers() {
  const list = $("triggerList");
  const triggers = Array.isArray(state.settings.triggers) ? state.settings.triggers : [];
  if (!triggers.length) {
    list.innerHTML = `<div class="api-card">No message triggers yet.</div>`;
    return;
  }
  list.innerHTML = triggers.map((rule) => `
    <article class="trigger-card">
      <div>
        <div class="api-title">
          <strong>${escapeHtml(rule.trigger)}</strong>
          <span class="badge">${escapeHtml(rule.match_mode)}</span>
          <span class="badge">${escapeHtml(rule.strategy)}</span>
          <span class="badge">${escapeHtml(rule.response_type || "summary")}</span>
          <span class="badge">${rule.enabled ? "Enabled" : "Disabled"}</span>
          <span class="badge">${rule.stop_event ? "Stops event" : "Pass-through"}</span>
        </div>
        <div class="api-meta">
          <span class="badge">${escapeHtml(groupName(rule.group_id))}</span>
          ${rule.response_path ? `<span class="badge">Path: ${escapeHtml(rule.response_path)}</span>` : ""}
        </div>
      </div>
      <div class="api-actions">
        <button data-action="toggle-trigger" data-id="${escapeHtml(rule.id)}">${rule.enabled ? "Turn off" : "Turn on"}</button>
        <button data-action="edit-trigger" data-id="${escapeHtml(rule.id)}">Edit</button>
        <button class="danger" data-action="delete-trigger" data-id="${escapeHtml(rule.id)}">Delete</button>
      </div>
    </article>
  `).join("");
}

function renderApis() {
  const list = $("apiList");
  const apis = filteredApis();
  $("apiPanelTitle").textContent = state.selectedGroupId === "all" ? "APIs" : `APIs - ${groupName(state.selectedGroupId)}`;
  if (!apis.length) {
    list.innerHTML = `<div class="api-card">No APIs yet. Click Add API to start.</div>`;
    return;
  }
  list.innerHTML = apis.map((api) => {
    const statusClass = api.last_tested_at ? (api.last_ok ? "ok" : "fail") : "";
    const statusText = api.last_tested_at ? (api.last_ok ? "OK" : "FAIL") : "Not tested";
    return `
      <article class="api-card">
        <div class="api-main">
          <div>
            <div class="api-title">
              <strong>${escapeHtml(api.name)}</strong>
              <span class="badge">${escapeHtml(api.method)}</span>
              <span class="badge ${statusClass}">${statusText}</span>
              <span class="badge">${api.enabled ? "Enabled" : "Disabled"}</span>
            </div>
            <div class="api-url">${escapeHtml(api.url)}</div>
            <div class="api-meta">
              <span class="badge">${escapeHtml(groupName(api.group_id))}</span>
              <span class="badge">Status: ${api.last_status ?? "-"}</span>
              <span class="badge">Timeout: ${escapeHtml(api.timeout_seconds ?? 12)}s</span>
              <span class="badge">Retry: ${escapeHtml(api.retry_count ?? 0)}</span>
              <span class="badge">Cooldown: ${escapeHtml(api.cooldown_seconds ?? 0)}s</span>
              ${cooldownLabel(api) ? `<span class="badge fail">${escapeHtml(cooldownLabel(api))}</span>` : ""}
            </div>
            ${api.last_error ? `<div class="api-url">Last error: ${escapeHtml(api.last_error)}</div>` : ""}
          </div>
          <div class="api-actions">
            <button data-action="test-api" data-id="${escapeHtml(api.id)}">Test</button>
            <button data-action="toggle-api" data-id="${escapeHtml(api.id)}">${api.enabled ? "Disable" : "Enable"}</button>
            <button data-action="edit-api" data-id="${escapeHtml(api.id)}">Edit</button>
            <button class="danger" data-action="delete-api" data-id="${escapeHtml(api.id)}">Delete</button>
          </div>
        </div>
      </article>
    `;
  }).join("");
}

function renderLogs() {
  if (!state.test_logs.length) return;
  const latest = state.test_logs.slice(-5).reverse();
  $("logOutput").textContent = latest.map((item) => {
    const status = item.ok ? "OK" : "FAIL";
    return `[${new Date(item.tested_at).toLocaleString()}] ${status} ${item.api_name} ${item.status ?? ""} ${item.error ?? ""}`;
  }).join("\n");
}

function render() {
  renderSummary();
  renderGroups();
  renderAggregateControls();
  renderTriggers();
  renderApis();
  renderLogs();
}

function parseJsonInput(id) {
  const raw = $(id).value.trim();
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("must be object");
    }
    return parsed;
  } catch {
    throw new Error(`${id} must be a JSON object`);
  }
}

function openApiDialog(api = null) {
  $("apiDialogTitle").textContent = api ? "Edit API" : "Add API";
  $("apiId").value = api?.id || "";
  $("apiName").value = api?.name || "";
  $("apiGroup").innerHTML = state.groups.map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`).join("");
  $("apiGroup").value = api?.group_id || state.groups[0]?.id || "default";
  $("apiUrl").value = api?.url || "";
  $("apiMethod").value = api?.method || "GET";
  $("apiQuery").value = JSON.stringify(api?.query || {}, null, 2);
  $("apiHeaders").value = JSON.stringify(api?.headers || {}, null, 2);
  $("apiBody").value = api?.body || "";
  $("apiTimeoutSeconds").value = String(api?.timeout_seconds ?? 12);
  $("apiRetryCount").value = String(api?.retry_count ?? 0);
  $("apiCooldownSeconds").value = String(api?.cooldown_seconds ?? 0);
  $("apiDescription").value = api?.description || "";
  $("apiEnabled").checked = api?.enabled !== false;
  $("apiDialog").showModal();
}

function openGroupDialog(group = null) {
  $("groupDialogTitle").textContent = group ? "Edit Group" : "Add Group";
  $("groupId").value = group?.id || "";
  $("groupName").value = group?.name || "";
  $("groupDescription").value = group?.description || "";
  $("groupDialog").showModal();
}

function openTriggerDialog(rule = null) {
  $("triggerDialogTitle").textContent = rule ? "Edit Trigger" : "Add Trigger";
  $("triggerId").value = rule?.id || "";
  $("triggerPhrase").value = rule?.trigger || "";
  $("triggerMode").value = rule?.match_mode || "contains";
  const group = groupOptions(rule?.group_id || "all");
  $("triggerGroup").innerHTML = group.html;
  $("triggerGroup").value = group.value;
  $("triggerStrategy").value = rule?.strategy || state.settings.strategy || "first-ok";
  const apiSelect = apiOptions(rule?.preview_api_id || "");
  $("triggerPreviewApi").innerHTML = apiSelect.html;
  $("triggerPreviewApi").value = apiSelect.value;
  $("triggerResponseType").value = rule?.response_type || "summary";
  $("triggerResponsePath").value = rule?.response_path || "";
  $("triggerResponsePreview").textContent = "No preview yet.";
  $("triggerEnabled").checked = rule?.enabled !== false;
  $("triggerStopEvent").checked = rule?.stop_event !== false;
  $("triggerDialog").showModal();
}

async function saveApi(event) {
  event.preventDefault();
  const id = $("apiId").value;
  const payload = {
    id,
    name: $("apiName").value,
    group_id: $("apiGroup").value,
    url: $("apiUrl").value,
    method: $("apiMethod").value,
    query: parseJsonInput("apiQuery"),
    headers: parseJsonInput("apiHeaders"),
    body: $("apiBody").value,
    timeout_seconds: parseNumberInput("apiTimeoutSeconds", 12, 1, 120),
    retry_count: parseNumberInput("apiRetryCount", 0, 0, 5),
    cooldown_seconds: parseNumberInput("apiCooldownSeconds", 0, 0, 3600),
    description: $("apiDescription").value,
    enabled: $("apiEnabled").checked,
  };
  const endpoint = id ? "apis/update" : "apis/create";
  await withBusy("save-api", { target: event.submitter, busyText: "Saving...", startText: id ? "Saving API..." : "Creating API..." }, async () => {
    const result = await apiPost(endpoint, payload);
    applyState(result.state);
    $("apiDialog").close();
    log(id ? "API updated" : "API created", result.api);
  });
}

async function saveGroup(event) {
  event.preventDefault();
  const id = $("groupId").value;
  const payload = {
    id,
    name: $("groupName").value,
    description: $("groupDescription").value,
  };
  const endpoint = id ? "groups/update" : "groups/create";
  await withBusy("save-group", { target: event.submitter, busyText: "Saving...", startText: id ? "Saving group..." : "Creating group..." }, async () => {
    const result = await apiPost(endpoint, payload);
    applyState(result.state);
    $("groupDialog").close();
    log(id ? "Group updated" : "Group created", result.group);
  });
}

async function saveTrigger(event) {
  event.preventDefault();
  const id = $("triggerId").value;
  const payload = {
    id,
    trigger: $("triggerPhrase").value,
    match_mode: $("triggerMode").value,
    group_id: $("triggerGroup").value,
    strategy: $("triggerStrategy").value,
    preview_api_id: $("triggerPreviewApi").value,
    response_type: $("triggerResponseType").value,
    response_path: $("triggerResponsePath").value,
    enabled: $("triggerEnabled").checked,
    stop_event: $("triggerStopEvent").checked,
  };
  const endpoint = id ? "triggers/update" : "triggers/create";
  await withBusy("save-trigger", { target: event.submitter, busyText: "Saving...", startText: id ? "Saving trigger..." : "Creating trigger..." }, async () => {
    const result = await apiPost(endpoint, payload);
    applyState(result.state);
    $("triggerDialog").close();
    log(id ? "Trigger updated" : "Trigger created", result.trigger);
  });
}

async function previewTriggerResponsePath() {
  const apiId = $("triggerPreviewApi").value;
  if (!apiId) {
    throw new Error("Choose an API to preview");
  }
  $("triggerResponsePreview").textContent = "Preview running...";
  await withBusy("preview-response-path", { target: "previewResponsePathBtn", busyText: "Previewing...", startText: "Running response path preview..." }, async () => {
    const result = await apiPost("triggers/preview-response-path", {
      api_id: apiId,
      response_path: $("triggerResponsePath").value.trim(),
    });
    applyState(result.state);
    $("triggerResponsePreview").textContent = summarizePreview(result.preview, result.result);
    log("Response path preview completed", result.preview);
  });
}

async function handleAction(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  event.preventDefault();
  const action = target.dataset.action;
  const id = target.dataset.id;
  try {
    if (action === "select-group") {
      state.selectedGroupId = id || "all";
      render();
    } else if (action === "edit-group") {
      openGroupDialog(state.groups.find((group) => group.id === id));
    } else if (action === "delete-group") {
      if (!confirm("Delete this group? Its APIs will move to the default group.")) return;
      await withBusy(`delete-group-${id}`, { target, busyText: "Deleting...", startText: "Deleting group..." }, async () => {
        const result = await apiPost("groups/delete", { id });
        applyState(result.state);
        log("Group deleted", result.result);
      });
    } else if (action === "edit-api") {
      openApiDialog(state.apis.find((api) => api.id === id));
    } else if (action === "delete-api") {
      if (!confirm("Delete this API?")) return;
      await withBusy(`delete-api-${id}`, { target, busyText: "Deleting...", startText: "Deleting API..." }, async () => {
        const result = await apiPost("apis/delete", { id });
        applyState(result.state);
        log("API deleted", result.result);
      });
    } else if (action === "toggle-api") {
      const api = state.apis.find((item) => item.id === id);
      await withBusy(`toggle-api-${id}`, { target, busyText: "Updating...", startText: "Updating API state..." }, async () => {
        const result = await apiPost("apis/toggle", { id, enabled: !api?.enabled });
        applyState(result.state);
        log("API state updated", result.api);
      });
    } else if (action === "test-api") {
      await withBusy(`test-api-${id}`, { target, busyText: "Testing...", startText: "Testing API..." }, async () => {
        const result = await apiPost("apis/test", { id });
        applyState(result.state);
        log("API test completed", result.result);
      });
    } else if (action === "edit-trigger") {
      const triggers = Array.isArray(state.settings.triggers) ? state.settings.triggers : [];
      openTriggerDialog(triggers.find((rule) => rule.id === id));
    } else if (action === "delete-trigger") {
      if (!confirm("Delete this trigger?")) return;
      await withBusy(`delete-trigger-${id}`, { target, busyText: "Deleting...", startText: "Deleting trigger..." }, async () => {
        const result = await apiPost("triggers/delete", { id });
        applyState(result.state);
        log("Trigger deleted", result.result);
      });
    } else if (action === "toggle-trigger") {
      const triggers = Array.isArray(state.settings.triggers) ? state.settings.triggers : [];
      const rule = triggers.find((item) => item.id === id);
      await withBusy(`toggle-trigger-${id}`, { target, busyText: "Updating...", startText: rule?.enabled ? "Turning trigger off..." : "Turning trigger on..." }, async () => {
        const result = await apiPost("triggers/toggle", { id, enabled: !rule?.enabled });
        applyState(result.state);
        log(result.trigger?.enabled ? "Trigger turned on" : "Trigger turned off", {
          trigger: result.trigger?.trigger,
          match_mode: result.trigger?.match_mode,
        });
      });
    }
  } catch (error) {
    log("Action failed", { message: error?.message || String(error) });
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

$("refreshBtn").addEventListener("click", () => refresh().catch((error) => log("Refresh failed", { message: error.message })));
$("addApiBtn").addEventListener("click", () => openApiDialog());
$("addGroupBtn").addEventListener("click", () => openGroupDialog());
$("addTriggerBtn").addEventListener("click", () => openTriggerDialog());
$("cancelApiBtn").addEventListener("click", () => $("apiDialog").close());
$("cancelGroupBtn").addEventListener("click", () => $("groupDialog").close());
$("cancelTriggerBtn").addEventListener("click", () => $("triggerDialog").close());
$("apiForm").addEventListener("submit", (event) => saveApi(event).catch((error) => log("Save API failed", { message: error.message })));
$("groupForm").addEventListener("submit", (event) => saveGroup(event).catch((error) => log("Save group failed", { message: error.message })));
$("triggerForm").addEventListener("submit", (event) => saveTrigger(event).catch((error) => log("Save trigger failed", { message: error.message })));
$("previewResponsePathBtn").addEventListener("click", () => previewTriggerResponsePath().catch((error) => {
  $("triggerResponsePreview").textContent = JSON.stringify({ message: error.message }, null, 2);
  log("Response path preview failed", { message: error.message });
}));
$("apiList").addEventListener("click", (event) => handleAction(event));
$("groupList").addEventListener("click", (event) => handleAction(event));
$("triggerList").addEventListener("click", (event) => handleAction(event));
$("searchInput").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderApis();
});
$("testAllBtn").addEventListener("click", async () => {
  try {
    await withBusy("test-all", { target: "testAllBtn", busyText: "Testing...", startText: "Testing enabled APIs..." }, async () => {
      const result = await apiPost("apis/test-all", {});
      applyState(result.state);
      log("Batch test completed", result.results);
    });
  } catch (error) {
    log("Batch test failed", { message: error.message });
  }
});
$("saveSettingsBtn").addEventListener("click", async () => {
  try {
    await withBusy("save-settings", { target: "saveSettingsBtn", busyText: "Saving...", startText: "Saving strategy..." }, async () => {
      const result = await apiPost("settings/save", { strategy: $("strategySelect").value });
      applyState(result.state);
      log("Settings saved", result.settings);
    });
  } catch (error) {
    log("Save settings failed", { message: error.message });
  }
});
$("callAggregateBtn").addEventListener("click", async () => {
  try {
    $("aggregateOutput").textContent = "Aggregate call running...";
    await withBusy("aggregate-call", { target: "callAggregateBtn", busyText: "Calling...", startText: "Calling aggregate group..." }, async () => {
      const result = await apiPost("aggregate/call", {
        group_id: $("aggregateGroup").value,
        strategy: $("strategySelect").value,
      });
      applyState(result.state);
      $("aggregateOutput").textContent = `${summarizeAggregateResult(result.result)}\n\n${JSON.stringify(result.result, null, 2)}`;
      log("Aggregate call completed", result.result);
    });
  } catch (error) {
    $("aggregateOutput").textContent = JSON.stringify({ message: error.message }, null, 2);
    log("Aggregate call failed", { message: error.message });
  }
});
$("clearLogViewBtn").addEventListener("click", () => log("View cleared"));
$("exportBtn").addEventListener("click", async () => {
  try {
    await withBusy("export", { target: "exportBtn", busyText: "Exporting...", startText: "Exporting data..." }, async () => {
      const data = await apiGet("export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `api-aggregator-${Date.now()}.json`;
      link.click();
      URL.revokeObjectURL(url);
      log("Export completed");
    });
  } catch (error) {
    log("Export failed", { message: error.message });
  }
});
$("importInput").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    await withBusy("import", { target: "importStrategy", startText: "Importing data..." }, async () => {
      const data = JSON.parse(await file.text());
      const strategy = $("importStrategy").value || "replace";
      const result = await apiPost("import", { data, strategy });
      if (result.state) {
        applyState(result.state);
      }
      if (strategy === "validate") {
        log("Import validation completed", result.summary);
      } else {
        log(`Import completed (${strategy})`, {
          groups: result.state?.groups?.length ?? 0,
          apis: result.state?.apis?.length ?? 0,
          triggers: result.state?.settings?.triggers?.length ?? 0,
        });
      }
    });
  } catch (error) {
    log("Import failed", { message: error.message });
  } finally {
    event.target.value = "";
  }
});

log("Page handlers registered. Loading state...");
refresh().catch((error) => log("Initialization failed", { message: error.message }));
