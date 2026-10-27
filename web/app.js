const form = document.querySelector("#settings-form");
const saveResult = document.querySelector("#save-result");
const stepOrder = ["folders", "scanner", "integrations", "review"];

const settingNames = [
  "input_path", "quarantine_path", "clamav_host", "clamav_port",
  "clamav_scan_root", "radarr_url", "radarr_api_key", "radarr_import_root",
  "sonarr_url", "sonarr_api_key", "sonarr_import_root", "scan_interval",
  "settle_seconds", "import_retry_seconds",
];

function formSettings() {
  const values = {};
  for (const name of settingNames) values[name] = form.elements[name].value.trim();
  values.clear_radarr_api_key = form.elements.clear_radarr_api_key.checked;
  values.clear_sonarr_api_key = form.elements.clear_sonarr_api_key.checked;
  return values;
}

function setResult(element, message, state = "") {
  element.textContent = message;
  element.classList.remove("success", "error");
  if (state) element.classList.add(state);
}

function setKeyState(service, saved) {
  const key = service.toLowerCase();
  const pill = document.querySelector(`#${key}-key-state`);
  const clear = document.querySelector(`#${key}-clear-wrap`);
  pill.textContent = saved ? "Key saved" : "Key not set";
  pill.classList.toggle("saved", saved);
  clear.hidden = !saved;
}

function showStep(name) {
  const index = stepOrder.indexOf(name);
  if (index < 0) return;
  for (const panel of document.querySelectorAll("[data-step-panel]")) {
    panel.hidden = panel.dataset.stepPanel !== name;
  }
  for (const tab of document.querySelectorAll("[data-step-target]")) {
    const tabIndex = stepOrder.indexOf(tab.dataset.stepTarget);
    const active = tab.dataset.stepTarget === name;
    tab.classList.toggle("active", active);
    tab.classList.toggle("complete", tabIndex < index);
    if (active) tab.setAttribute("aria-current", "step");
    else tab.removeAttribute("aria-current");
  }
  document.querySelector("#stepper-progress").style.width =
    ((index / (stepOrder.length - 1)) * 100) + "%";
  updateReview();
}

function validatePanel(name) {
  const panel = document.querySelector('[data-step-panel="' + name + '"]');
  for (const input of panel.querySelectorAll("input")) {
    if (!input.checkValidity()) {
      input.reportValidity();
      return false;
    }
  }
  return true;
}

function updateReview() {
  if (!form || !form.elements.input_path) return;
  const rows = [
    ["Completed downloads", form.elements.input_path.value || "Not set"],
    ["Quarantine", form.elements.quarantine_path.value || "Not set"],
    ["ClamAV", (form.elements.clamav_host.value || "Not set") + ":" + (form.elements.clamav_port.value || "—")],
    ["Scan timing", (form.elements.scan_interval.value || "—") + " second interval"],
    ["Radarr", form.elements.radarr_url.value || "Not configured"],
    ["Sonarr", form.elements.sonarr_url.value || "Not configured"],
  ];
  const summary = document.querySelector("#review-summary");
  summary.replaceChildren(...rows.map(([label, value]) => {
    const item = document.createElement("div");
    const title = document.createElement("span");
    const content = document.createElement("span");
    item.className = "review-item";
    title.className = "review-label";
    content.className = "review-value";
    title.textContent = label;
    content.textContent = value;
    item.append(title, content);
    return item;
  }));
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

for (const tab of document.querySelectorAll("[data-step-target]")) {
  tab.addEventListener("click", () => showStep(tab.dataset.stepTarget));
}

for (const button of document.querySelectorAll("[data-next]")) {
  button.addEventListener("click", () => {
    const currentPanel = button.closest("[data-step-panel]");
    if (validatePanel(currentPanel.dataset.stepPanel)) showStep(button.dataset.next);
  });
}

for (const button of document.querySelectorAll("[data-previous]")) {
  button.addEventListener("click", () => showStep(button.dataset.previous));
}

form.addEventListener("input", updateReview);
form.addEventListener("change", updateReview);

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
    document.querySelector("#service-state").textContent = "MediaValidator online";
    document.querySelector("#service-dot").classList.add("online");
    updateReview();
  } catch (error) {
    document.querySelector("#service-state").textContent = "Could not load settings";
    setResult(saveResult, error.message, "error");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  for (const input of form.querySelectorAll("input")) {
    if (!input.checkValidity()) {
      showStep(input.closest("[data-step-panel]").dataset.stepPanel);
      input.reportValidity();
      return;
    }
  }
  setResult(saveResult, "Saving…");
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const response = await requestJSON("/api/config", formSettings());
    fillSettings(response.settings);
    form.elements.radarr_api_key.value = "";
    form.elements.sonarr_api_key.value = "";
    form.elements.clear_radarr_api_key.checked = false;
    form.elements.clear_sonarr_api_key.checked = false;
    setResult(saveResult, "Saved. New settings are active.", "success");
  } catch (error) {
    setResult(saveResult, error.message, "error");
  } finally {
    button.disabled = false;
  }
});

for (const button of document.querySelectorAll("[data-test]")) {
  button.addEventListener("click", async () => {
    const name = button.dataset.test;
    const resultNode = document.querySelector(`#result-${name}`);
    setResult(resultNode, "Checking…");
    button.disabled = true;
    try {
      const response = await requestJSON(`/api/test/${name}`, formSettings());
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

showStep("folders");
loadSettings();
