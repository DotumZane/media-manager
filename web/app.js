const form = document.querySelector("#settings-form");
const saveButton = document.querySelector("#save-button");
const saveState = document.querySelector("#save-state");
const activityPageSize = 15;
let activePage = "folders";
let activityView = "history";
let vpnContainers = [];
let vpnConfigContainers = [];
let vpnConfigCheckContainer = "";
let vpnRefreshTimer = null;
let activityOffset = 0;
let activityTotal = 0;
let activityRequestId = 0;
let activitySearchTimer = null;
const settingNames = [
  "input_path", "quarantine_path", "clamav_host", "clamav_port",
  "clamav_scan_root", "radarr_url", "radarr_api_key", "radarr_import_root",
  "sonarr_url", "sonarr_api_key", "sonarr_import_root", "scan_interval",
  "settle_seconds", "import_retry_seconds", "vpn_check_container",
  "vpn_check_interval", "vpn_failure_threshold",
];

const pages = {
  folders: {
    kicker: "CONFIGURATION / FOLDERS",
    title: "Folders",
    description: "Set the locations MediaValidator watches and where it stages approved files.",
  },
  scanner: {
    kicker: "CONFIGURATION / SCANNER",
    title: "Scanner",
    description: "Configure ClamAV and the schedule for checking completed downloads.",
  },
  radarr: {
    kicker: "INTEGRATIONS / RADARR",
    title: "Radarr",
    description: "Connect movie imports and choose the folder Radarr uses for staged files.",
  },
  sonarr: {
    kicker: "INTEGRATIONS / SONARR",
    title: "Sonarr",
    description: "Connect TV episode imports and choose the folder Sonarr uses for staged files.",
  },
  activity: {
    kicker: "MONITORING / ACTIVITY",
    title: "Activity",
    description: "Review processed media, imports, and recent Media Manager events.",
  },
  "vpn-guard": {
    kicker: "MONITORING / VPN GUARD",
    title: "VPN Guard",
    description: "Check the VPN connection for a selected container and stop chosen apps if the check fails.",
  },
};

let savedSignature = null;

function formSettings() {
  const values = {};
  for (const name of settingNames) values[name] = form.elements[name].value.trim();
  values.vpn_enabled = form.elements.vpn_enabled.checked;
  values.vpn_shutdown_containers = Array.from(
    document.querySelectorAll("[data-vpn-shutdown-container]:checked"),
    (input) => input.value,
  );
  values.clear_radarr_api_key = form.elements.clear_radarr_api_key.checked;
  values.clear_sonarr_api_key = form.elements.clear_sonarr_api_key.checked;
  return values;
}

function signature() {
  return JSON.stringify(formSettings());
}

function setResult(element, message, state = "") {
  element.textContent = message;
  element.classList.remove("success", "error");
  if (state) element.classList.add(state);
}

function setSaveState(message, state = "") {
  saveState.textContent = message;
  saveState.classList.remove("pending", "error");
  if (state) saveState.classList.add(state);
}

function updateDirtyState() {
  if (savedSignature === null) return;
  const dirty = signature() !== savedSignature;
  saveButton.disabled = !dirty;
  setSaveState(dirty ? "Unsaved changes" : "All changes saved", dirty ? "pending" : "");
}

function showPage(name) {
  const page = pages[name];
  if (!page) return;
  activePage = name;
  for (const panel of document.querySelectorAll("[data-settings-page]")) {
    panel.hidden = panel.dataset.settingsPage !== name;
  }
  for (const item of document.querySelectorAll("[data-page-target]")) {
    const active = item.dataset.pageTarget === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.setAttribute("aria-current", "false");
  }
  document.querySelector("#page-kicker").textContent = page.kicker;
  document.querySelector("#page-title").textContent = page.title;
  document.querySelector("#page-description").textContent = page.description;
  if (name === "activity") loadActivity();
  if (name === "vpn-guard") {
    loadVpnData();
    window.clearInterval(vpnRefreshTimer);
    vpnRefreshTimer = window.setInterval(loadVpnStatus, 5000);
  } else {
    window.clearInterval(vpnRefreshTimer);
  }
}

function formattedDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "Unknown time";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function createTextElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function historyStatus(item) {
  if (item.result === "REJECTED") return { label: "Rejected", className: "rejected" };
  if (item.import_state === "IMPORTED" || item.result === "IMPORTED") return { label: "Imported", className: "complete" };
  if (item.import_state === "RETRY") return { label: "Import retry", className: "attention" };
  if (item.import_state === "QUEUED") return { label: "Import queued", className: "working" };
  if (item.import_state === "PENDING") return { label: "Waiting to import", className: "waiting" };
  return { label: "Approved", className: "complete" };
}

function renderHistory(items) {
  const list = document.querySelector("#activity-list");
  list.replaceChildren();
  for (const item of items) {
    const row = document.createElement("article");
    row.className = "activity-row history-row";

    const media = document.createElement("div");
    media.className = "activity-media";
    const parts = (item.job || "Unknown item").split("/");
    const category = parts.length > 1 ? parts.shift() : "Media";
    media.append(
      createTextElement("span", "activity-category", category),
      createTextElement("strong", "activity-name", parts.join("/") || item.job || "Unknown item"),
    );

    const statusColumn = document.createElement("div");
    statusColumn.className = "activity-status-column";
    const status = historyStatus(item);
    statusColumn.append(createTextElement("span", "status-pill " + status.className, status.label));
    if (item.attempts > 0) {
      statusColumn.append(createTextElement("small", "activity-detail", item.attempts + " import attempt" + (item.attempts === 1 ? "" : "s")));
    }

    const time = document.createElement("time");
    time.className = "activity-time";
    time.dateTime = item.timestamp || "";
    time.textContent = formattedDate(item.timestamp);
    row.append(media, statusColumn, time);
    list.append(row);
  }
}

function renderEvents(items) {
  const list = document.querySelector("#activity-list");
  list.replaceChildren();
  for (const item of items) {
    const row = document.createElement("article");
    row.className = "activity-row event-row";
    const marker = document.createElement("span");
    marker.className = "event-marker " + (item.level || "info");
    marker.setAttribute("aria-hidden", "true");
    const content = document.createElement("div");
    content.className = "event-content";
    const message = createTextElement("span", "event-message", item.message || "");
    const time = document.createElement("time");
    time.className = "activity-time";
    time.dateTime = item.timestamp || "";
    time.textContent = formattedDate(item.timestamp);
    content.append(message);
    row.append(marker, content, time);
    list.append(row);
  }
}

async function loadActivity(silent = false) {
  if (activePage !== "activity") return;
  const requestId = ++activityRequestId;
  const list = document.querySelector("#activity-list");
  const empty = document.querySelector("#activity-empty");
  const refresh = document.querySelector("#activity-refresh");
  const search = document.querySelector("#activity-search").value.trim();
  const params = new URLSearchParams({
    limit: String(activityPageSize),
    offset: String(activityOffset),
    q: search,
  });
  const endpoint = activityView === "history" ? "/api/history" : "/api/activity";
  refresh.disabled = true;
  if (!silent) {
    empty.hidden = true;
    list.replaceChildren(createTextElement("div", "activity-loading", "Loading activity…"));
  }
  try {
    const response = await requestJSON(endpoint + "?" + params.toString());
    if (requestId !== activityRequestId) return;
    activityTotal = response.total;
    if (activityView === "history") {
      const stats = response.stats || {};
      document.querySelector("#stat-processed").textContent = stats.processed || 0;
      document.querySelector("#stat-rejected").textContent = stats.rejected || 0;
      document.querySelector("#stat-imported").textContent = stats.imported || 0;
      document.querySelector("#stat-waiting").textContent = stats.waiting || 0;
      renderHistory(response.items || []);
    } else {
      renderEvents(response.items || []);
    }

    const hasItems = (response.items || []).length > 0;
    empty.hidden = hasItems;
    if (!hasItems) {
      document.querySelector("#activity-empty-title").textContent = search ? "No matching activity" : "No activity yet";
      document.querySelector("#activity-empty-copy").textContent = search
        ? "Try a different search."
        : (activityView === "history" ? "Processed media will appear here once a file is checked." : "Runtime events will appear here as Media Manager works.");
    }

    const start = activityTotal ? activityOffset + 1 : 0;
    const end = Math.min(activityOffset + activityPageSize, activityTotal);
    document.querySelector("#activity-page-label").textContent =
      "Showing " + start + "–" + end + " of " + activityTotal;
    document.querySelector("#activity-newer").disabled = activityOffset === 0;
    document.querySelector("#activity-older").disabled = activityOffset + activityPageSize >= activityTotal;
  } catch (error) {
    if (requestId !== activityRequestId) return;
    list.replaceChildren();
    empty.hidden = false;
    document.querySelector("#activity-empty-title").textContent = "Could not load activity";
    document.querySelector("#activity-empty-copy").textContent = error.message;
    document.querySelector("#activity-page-label").textContent = "Activity unavailable";
  } finally {
    if (requestId === activityRequestId) refresh.disabled = false;
  }
}

function vpnLabel(state) {
  return ({
    disabled: "Off",
    checking: "Checking",
    protected: "Protected",
    warning: "Check failed",
    waiting: "Waiting",
    unavailable: "Unavailable",
    tripped: "Stopped apps",
  })[state] || "Unknown";
}

function renderVpnStatus(status = {}) {
  const state = status.tripped ? "tripped" : (status.state || "disabled");
  const pill = document.querySelector("#vpn-state-pill");
  pill.textContent = vpnLabel(state);
  pill.className = "vpn-state-pill " + state;
  document.querySelector("#vpn-state-title").textContent = vpnLabel(state);
  document.querySelector("#vpn-state-message").textContent = status.last_message || "VPN Guard is turned off.";
  document.querySelector("#vpn-last-check").textContent = status.last_check ? formattedDate(status.last_check) : "Not checked";
  document.querySelector("#vpn-manager-ip").textContent = status.check_ip || "—";
  document.querySelector("#vpn-container-ip").textContent = status.container_ip || "—";
  document.querySelector("#vpn-failure-count").textContent = String(status.consecutive_failures || 0);
  const indicator = document.querySelector("#vpn-nav-status");
  indicator.classList.remove("ready", "vpn-protected", "vpn-warning", "vpn-tripped", "vpn-waiting");
  if (state === "protected") indicator.classList.add("vpn-protected");
  else if (state === "warning" || state === "unavailable") indicator.classList.add("vpn-warning");
  else if (state === "tripped") indicator.classList.add("vpn-tripped");
  else if (state === "waiting" || state === "checking") indicator.classList.add("vpn-waiting");
  indicator.setAttribute("aria-label", "VPN Guard " + vpnLabel(state).toLowerCase());
}

function renderVpnContainers(containers) {
  const monitorSelect = document.querySelector("#vpn-check-container");
  const currentMonitor = monitorSelect.value || vpnConfigCheckContainer;
  const selected = new Set(
    document.querySelectorAll("[data-vpn-shutdown-container]:checked").length
      ? Array.from(document.querySelectorAll("[data-vpn-shutdown-container]:checked"), (input) => input.value)
      : vpnConfigContainers,
  );
  vpnContainers = Array.isArray(containers) ? containers : [];

  monitorSelect.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = vpnContainers.length ? "Choose a container" : "No containers available";
  monitorSelect.append(placeholder);
  if (currentMonitor && !vpnContainers.some((item) => item.name === currentMonitor)) {
    const missing = document.createElement("option");
    missing.value = currentMonitor;
    missing.textContent = currentMonitor + " (not found)";
    monitorSelect.append(missing);
  }
  for (const item of vpnContainers) {
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = item.name + (item.running ? " · running" : " · stopped");
    monitorSelect.append(option);
  }
  monitorSelect.value = currentMonitor;

  const list = document.querySelector("#vpn-shutdown-list");
  list.replaceChildren();
  const eligible = vpnContainers;
  if (!eligible.length && !selected.size) {
    list.append(createTextElement("span", "vpn-list-empty", "No other containers are available to select."));
    return;
  }
  const items = [...eligible];
  for (const name of selected) {
    if (!items.some((item) => item.name === name)) {
      items.push({ name, running: false, missing: true });
    }
  }
  for (const item of items) {
    const label = document.createElement("label");
    label.className = "vpn-container-choice";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = item.name;
    input.dataset.vpnShutdownContainer = "true";
    input.checked = selected.has(item.name);
    input.disabled = Boolean(item.missing);
    const name = createTextElement("span", "", item.name);
    const state = createTextElement("small", "", item.missing ? "not found" : (item.running ? "running" : "stopped"));
    label.append(input, name, state);
    list.append(label);
  }
}

async function loadVpnData() {
  const warning = document.querySelector("#vpn-docker-warning");
  try {
    const response = await requestJSON("/api/vpn");
    renderVpnContainers(response.containers || []);
    renderVpnStatus(response.status || {});
    warning.hidden = response.docker_available;
    if (!response.docker_available) {
      document.querySelector("#vpn-docker-warning-copy").textContent = response.message || "Set the Docker socket host path to /var/run/docker.sock in Unraid to load containers.";
    }
  } catch (error) {
    warning.hidden = false;
    document.querySelector("#vpn-docker-warning-copy").textContent = error.message;
  }
}

async function loadVpnStatus() {
  if (activePage !== "vpn-guard") return;
  try {
    const response = await requestJSON("/api/vpn/status");
    renderVpnStatus(response.status || {});
  } catch (error) {
    renderVpnStatus({ state: "unavailable", last_message: error.message });
  }
}

document.querySelector("#vpn-refresh-containers").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.querySelector("#vpn-action-result");
  button.disabled = true;
  setResult(result, "Loading containers…");
  try {
    await loadVpnData();
    setResult(result, vpnContainers.length ? "Container list updated." : "No containers found.", vpnContainers.length ? "success" : "error");
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#vpn-check-now").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.querySelector("#vpn-action-result");
  button.disabled = true;
  setResult(result, "Checking connection…");
  try {
    const response = await requestJSON("/api/vpn/check", {});
    renderVpnStatus(response.status || {});
    const safe = response.status && response.status.check_ok === true;
    setResult(result, response.status?.last_message || "Check complete.", safe ? "success" : "error");
  } catch (error) {
    setResult(result, error.message, "error");
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#vpn-rearm").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.querySelector("#vpn-action-result");
  button.disabled = true;
  try {
    const response = await requestJSON("/api/vpn/rearm", {});
    renderVpnStatus(response.status || {});
    setResult(result, "VPN Guard re-armed. Start stopped apps manually when it’s safe.", "success");
  } catch (error) {
    setResult(result, error.message, "error");
  } finally {
    button.disabled = false;
  }
});

function setKeyState(service, saved) {
  const key = service.toLowerCase();
  const pill = document.querySelector("#" + key + "-key-state");
  const clear = document.querySelector("#" + key + "-clear-wrap");
  pill.textContent = saved ? "Key saved" : "Key not set";
  pill.classList.toggle("saved", saved);
  clear.hidden = !saved;
  updateNavStatus(key);
}

function updateNavStatus(key) {
  const url = form.elements[key + "_url"].value.trim();
  const enteredKey = form.elements[key + "_api_key"].value.trim();
  const savedKey = document.querySelector("#" + key + "-key-state").classList.contains("saved");
  const removingKey = form.elements["clear_" + key + "_api_key"].checked;
  const ready = Boolean(url && (enteredKey || (savedKey && !removingKey)));
  const indicator = document.querySelector("#" + key + "-nav-status");
  indicator.classList.toggle("ready", ready);
  indicator.setAttribute("aria-label", ready ? key + " is configured" : key + " is not configured");
}

async function requestJSON(url, body) {
  const response = await fetch(url, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (response.status === 401) {
    window.location.replace("/");
    throw new Error("Your session expired. Please sign in again.");
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || "The request could not be completed.");
  return result;
}

document.querySelector("#logout-button").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    await fetch("/api/logout", { method: "POST", cache: "no-store" });
  } finally {
    window.location.replace("/");
  }
});

for (const item of document.querySelectorAll("[data-page-target]")) {
  item.addEventListener("click", () => showPage(item.dataset.pageTarget));
}

for (const tab of document.querySelectorAll("[data-activity-view]")) {
  tab.addEventListener("click", () => {
    activityView = tab.dataset.activityView;
    activityOffset = 0;
    for (const activityTab of document.querySelectorAll("[data-activity-view]")) {
      const active = activityTab === tab;
      activityTab.classList.toggle("active", active);
      activityTab.setAttribute("aria-selected", String(active));
    }
    document.querySelector("#activity-stats").hidden = activityView !== "history";
    document.querySelector("#activity-search").placeholder =
      activityView === "history" ? "Search media history" : "Search event log";
    loadActivity();
  });
}

document.querySelector("#activity-search").addEventListener("input", () => {
  window.clearTimeout(activitySearchTimer);
  activitySearchTimer = window.setTimeout(() => {
    activityOffset = 0;
    loadActivity();
  }, 250);
});

document.querySelector("#activity-refresh").addEventListener("click", () => loadActivity());
document.querySelector("#activity-newer").addEventListener("click", () => {
  activityOffset = Math.max(0, activityOffset - activityPageSize);
  loadActivity();
});
document.querySelector("#activity-older").addEventListener("click", () => {
  activityOffset += activityPageSize;
  loadActivity();
});
window.setInterval(() => {
  if (activePage === "activity") loadActivity(true);
}, 15000);

form.addEventListener("input", () => {
  updateDirtyState();
  updateNavStatus("radarr");
  updateNavStatus("sonarr");
});
form.addEventListener("change", (event) => {
  updateDirtyState();
  updateNavStatus("radarr");
  updateNavStatus("sonarr");
  if (event.target === form.elements.vpn_check_container) {
    renderVpnContainers(vpnContainers);
    updateDirtyState();
  }
});

function fillSettings(settings) {
  for (const name of settingNames) {
    if (name.endsWith("_api_key")) continue;
    if (settings[name] !== undefined) form.elements[name].value = settings[name];
  }
  form.elements.vpn_enabled.checked = Boolean(settings.vpn_enabled);
  vpnConfigCheckContainer = settings.vpn_check_container || "";
  vpnConfigContainers = Array.isArray(settings.vpn_shutdown_containers) ? [...settings.vpn_shutdown_containers] : [];
  setKeyState("Radarr", settings.radarr_api_key_set);
  setKeyState("Sonarr", settings.sonarr_api_key_set);
}

async function loadSettings() {
  try {
    const response = await requestJSON("/api/config");
    fillSettings(response.settings);
    await loadVpnData();
    savedSignature = signature();
    updateDirtyState();
    document.querySelector("#service-state").textContent = "MediaValidator online";
    document.querySelector("#service-dot").classList.add("online");
  } catch (error) {
    document.querySelector("#service-state").textContent = "Could not load settings";
    document.querySelector("#service-dot").classList.add("error");
    setSaveState(error.message, "error");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  for (const input of form.querySelectorAll("input")) {
    if (!input.checkValidity()) {
      showPage(input.closest("[data-settings-page]").dataset.settingsPage);
      input.reportValidity();
      return;
    }
  }

  saveButton.disabled = true;
  setSaveState("Saving changes…");
  try {
    const response = await requestJSON("/api/config", formSettings());
    fillSettings(response.settings);
    await loadVpnData();
    form.elements.radarr_api_key.value = "";
    form.elements.sonarr_api_key.value = "";
    form.elements.clear_radarr_api_key.checked = false;
    form.elements.clear_sonarr_api_key.checked = false;
    savedSignature = signature();
    updateDirtyState();
    setSaveState("Changes saved", "");
  } catch (error) {
    saveButton.disabled = false;
    setSaveState(error.message, "error");
  }
});

for (const button of document.querySelectorAll("[data-test]")) {
  button.addEventListener("click", async () => {
    const name = button.dataset.test;
    const resultNode = document.querySelector("#result-" + name);
    setResult(resultNode, "Checking…");
    button.disabled = true;
    try {
      const response = await requestJSON("/api/test/" + name, formSettings());
      if (response.details) {
        const message = response.details.map((item) => item.message).join(" ");
        setResult(resultNode, message, response.ok ? "success" : "error");
      } else {
        setResult(resultNode, response.message, response.ok ? "success" : "error");
      }
    } catch (error) {
      setResult(resultNode, error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
}

showPage("folders");
loadSettings();
