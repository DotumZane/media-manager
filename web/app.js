const form = document.querySelector("#settings-form");
const saveButton = document.querySelector("#save-button");
const saveState = document.querySelector("#save-state");
const settingNames = [
  "input_path", "quarantine_path", "clamav_host", "clamav_port",
  "clamav_scan_root", "radarr_url", "radarr_api_key", "radarr_import_root",
  "sonarr_url", "sonarr_api_key", "sonarr_import_root", "scan_interval",
  "settle_seconds", "import_retry_seconds",
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
};

let savedSignature = null;

function formSettings() {
  const values = {};
  for (const name of settingNames) values[name] = form.elements[name].value.trim();
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
}

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

form.addEventListener("input", () => {
  updateDirtyState();
  updateNavStatus("radarr");
  updateNavStatus("sonarr");
});
form.addEventListener("change", () => {
  updateDirtyState();
  updateNavStatus("radarr");
  updateNavStatus("sonarr");
});

function fillSettings(settings) {
  for (const name of settingNames) {
    if (name.endsWith("_api_key")) continue;
    if (settings[name] !== undefined) form.elements[name].value = settings[name];
  }
  setKeyState("Radarr", settings.radarr_api_key_set);
  setKeyState("Sonarr", settings.sonarr_api_key_set);
}

async function loadSettings() {
  try {
    const response = await requestJSON("/api/config");
    fillSettings(response.settings);
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
