const CONFIG_KEY = "nyxifyConfig";
const SNAPBOARD_LOGIN_KEY = "nyxifySnapboardLogin";
const DEFAULT_TEMPORARY_PROFILE_NAME = "Snapchat:";
const DEFAULT_ADSPOWER_GROUP = "Snapchat";
const DEFAULT_EXTENSION_CATEGORY = "Snap";
const DEFAULT_TAG_ONE = "";
const DEFAULT_VERIFICATION_PRIORITY = "auto";
const TOGGLE_OPTIONS = [
  ["proxyBlockerToggle", "proxyBlockerEnabled", "Proxy Blocker enabled.", "Proxy Blocker disabled."],
  ["proxyCheckerToggle", "proxyCheckerEnabled", "Proxy Checker enabled.", "Proxy Checker disabled."],
  ["pushAdspowerIdToggle", "pushAdspowerIdEnabled", "Push AdsPower ID enabled.", "Push AdsPower ID disabled."],
  ["adspowerTagsToggle", "adspowerTagsEnabled", "AdsPower tags enabled.", "AdsPower tags disabled."],
  ["fullAutoModeToggle", "fullAutoModeEnabled", "Full Auto Mode enabled.", "Full Auto Mode disabled."],
  ["continuousModeToggle", "continuousModeEnabled", "Continuous Mode enabled.", "Continuous Mode disabled."],
  ["keepProfileOpenToggle", "keepProfileOpenAfterSignup", "Keep Profile Open enabled.", "Keep Profile Open disabled."],
  ["autoFillRowToggle", "autoFillRow", "Auto-Fill Row enabled.", "Auto-Fill Row disabled."],
  ["lockTVToggle", "lockTV", "Lock in TV enabled.", "Lock in TV disabled."],
  ["enabledToggle", "enabled", "Nyxify enabled.", "Nyxify disabled."],
];

function normalizeStringConfig(config, key, defaultValue, allowBlank = true) {
  const hasStoredValue = Object.prototype.hasOwnProperty.call(config || {}, key);
  const value = hasStoredValue ? config[key] : defaultValue;
  const normalized = String(value == null ? "" : value).trim();
  if (normalized) {
    return normalized;
  }
  if (allowBlank && hasStoredValue) {
    return "";
  }
  return defaultValue;
}

function normalizeConfig(config) {
  const safeConfig = config || {};
  const verificationPriority = String(safeConfig.verificationPriority || DEFAULT_VERIFICATION_PRIORITY).trim().toLowerCase();
  const parsedAutoFillTarget = Number.parseInt(safeConfig.autoFillAccountTarget, 10);
  return {
    localApiUrl: String(safeConfig.localApiUrl || "http://127.0.0.1:8866").trim(),
    enabled: safeConfig.enabled !== false,
    rowLimit: Number.parseInt(safeConfig.rowLimit, 10) || 20,
    temporaryProfileName: normalizeStringConfig(safeConfig, "temporaryProfileName", DEFAULT_TEMPORARY_PROFILE_NAME, false),
    adspowerGroup: normalizeStringConfig(safeConfig, "adspowerGroup", DEFAULT_ADSPOWER_GROUP),
    extensionCategory: normalizeStringConfig(safeConfig, "extensionCategory", DEFAULT_EXTENSION_CATEGORY, false),
    tagOne: normalizeStringConfig(safeConfig, "tagOne", DEFAULT_TAG_ONE),
    tagTwo: String(safeConfig.tagTwo || "").trim(),
    maxParallelProfiles: Number.parseInt(safeConfig.maxParallelProfiles, 10) || 1,
    blockedProxies: Array.isArray(safeConfig.blockedProxies)
      ? safeConfig.blockedProxies
      : String(safeConfig.blockedProxies || safeConfig.bannedProxies || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
    proxyBlockerEnabled: safeConfig.proxyBlockerEnabled !== false,
    proxyCheckerEnabled: safeConfig.proxyCheckerEnabled !== false,
    pushAdspowerIdEnabled: safeConfig.pushAdspowerIdEnabled !== false,
    adspowerTagsEnabled: safeConfig.adspowerTagsEnabled === true,
    fullAutoModeEnabled: safeConfig.fullAutoModeEnabled === true,
    continuousModeEnabled: safeConfig.continuousModeEnabled === true,
    keepProfileOpenAfterSignup: safeConfig.keepProfileOpenAfterSignup === true,
    verificationPriority: ["email", "phone", "auto"].includes(verificationPriority) ? verificationPriority : DEFAULT_VERIFICATION_PRIORITY,
    autoFillRow: safeConfig.autoFillRow === true,
    autoFillAccountTarget: Number.isFinite(parsedAutoFillTarget) && parsedAutoFillTarget > 0 ? parsedAutoFillTarget : 0,
    lockG5: safeConfig.lockG5 === true,
    emailProviderLock: ["am", "g5", "5m"].includes(String(safeConfig.emailProviderLock || (safeConfig.lockG5 ? "g5" : "am")).toLowerCase())
      ? String(safeConfig.emailProviderLock || (safeConfig.lockG5 ? "g5" : "am")).toLowerCase()
      : "am",
    lockTV: safeConfig.lockTV === true,
  };
}

function loadOptions() {
  chrome.storage.sync.get(CONFIG_KEY, (result) => {
    const config = normalizeConfig(result[CONFIG_KEY] || {});
    document.getElementById("localApiUrl").value = config.localApiUrl;
    document.getElementById("temporaryProfileName").value = config.temporaryProfileName;
    document.getElementById("adspowerGroup").value = config.adspowerGroup;
    document.getElementById("extensionCategory").value = config.extensionCategory;
    document.getElementById("tagOne").value = config.tagOne;
    document.getElementById("tagTwo").value = config.tagTwo;
    document.getElementById("rowLimit").value = config.rowLimit;
    document.getElementById("maxParallelProfiles").value = config.maxParallelProfiles;
    document.getElementById("proxyBlockerToggle").checked = config.proxyBlockerEnabled;
    document.getElementById("blockedProxies").value = config.blockedProxies.join("\n");
    document.getElementById("proxyCheckerToggle").checked = config.proxyCheckerEnabled;
    document.getElementById("pushAdspowerIdToggle").checked = config.pushAdspowerIdEnabled;
    document.getElementById("adspowerTagsToggle").checked = config.adspowerTagsEnabled;
    document.getElementById("fullAutoModeToggle").checked = config.fullAutoModeEnabled === true;
    document.getElementById("continuousModeToggle").checked = config.continuousModeEnabled === true;
    document.getElementById("keepProfileOpenToggle").checked = config.keepProfileOpenAfterSignup === true;
    document.getElementById("autoFillRowToggle").checked = config.autoFillRow;
    document.getElementById("autoFillAccountTarget").value = config.autoFillAccountTarget > 0 ? config.autoFillAccountTarget : "";
    document.getElementById("verificationPriority").value = config.verificationPriority;
    document.getElementById("emailProviderLock").value = config.emailProviderLock;
    document.getElementById("lockTVToggle").checked = config.lockTV;
    document.getElementById("enabledToggle").checked = config.enabled;
    chrome.storage.local.get(SNAPBOARD_LOGIN_KEY, (localResult) => {
      const creds = (localResult && localResult[SNAPBOARD_LOGIN_KEY]) || {};
      document.getElementById("snapboardLoginName").value = creds.name || "";
      document.getElementById("snapboardLoginPassword").value = creds.password || "";
    });
  });
}

function saveOptions() {
  const config = normalizeConfig({
    localApiUrl: document.getElementById("localApiUrl").value,
    temporaryProfileName: document.getElementById("temporaryProfileName").value,
    adspowerGroup: document.getElementById("adspowerGroup").value,
    extensionCategory: document.getElementById("extensionCategory").value,
    tagOne: document.getElementById("tagOne").value,
    tagTwo: document.getElementById("tagTwo").value,
    rowLimit: document.getElementById("rowLimit").value,
    maxParallelProfiles: document.getElementById("maxParallelProfiles").value,
    proxyBlockerEnabled: document.getElementById("proxyBlockerToggle").checked,
    blockedProxies: document.getElementById("blockedProxies").value.split(/\r?\n/),
    proxyCheckerEnabled: document.getElementById("proxyCheckerToggle").checked,
    pushAdspowerIdEnabled: document.getElementById("pushAdspowerIdToggle").checked,
    adspowerTagsEnabled: document.getElementById("adspowerTagsToggle").checked,
    fullAutoModeEnabled: document.getElementById("fullAutoModeToggle").checked,
    continuousModeEnabled: document.getElementById("continuousModeToggle").checked,
    keepProfileOpenAfterSignup: document.getElementById("keepProfileOpenToggle").checked,
    autoFillRow: document.getElementById("autoFillRowToggle").checked,
    autoFillAccountTarget: document.getElementById("autoFillAccountTarget").value,
    verificationPriority: document.getElementById("verificationPriority").value,
    emailProviderLock: document.getElementById("emailProviderLock").value,
    lockG5: document.getElementById("emailProviderLock").value === "g5",
    lockTV: document.getElementById("lockTVToggle").checked,
    enabled: document.getElementById("enabledToggle").checked,
  });

  chrome.storage.local.set({
    [SNAPBOARD_LOGIN_KEY]: {
      name: document.getElementById("snapboardLoginName").value.trim(),
      password: document.getElementById("snapboardLoginPassword").value,
    },
  });

  chrome.runtime.sendMessage({
    type: "NYXIFY_SAVE_CONFIG",
    ...config,
    blockedProxiesReplace: true,
  }, (response) => {
    const feedback = document.getElementById("feedback");
    if (!response || !response.ok) {
      feedback.textContent = (response && response.error) || "Could not save Nyxify settings.";
      return;
    }

    chrome.storage.sync.set({ [CONFIG_KEY]: config }, () => {
      feedback.textContent = "Nyxify settings saved.";
    });
  });
}

function saveToggleOption(toggleId, configKey, enabledMessage, disabledMessage) {
  const toggle = document.getElementById(toggleId);
  const feedback = document.getElementById("feedback");
  if (!toggle || !feedback) {
    return;
  }

  const checked = toggle.checked;
  const payload = { type: "NYXIFY_SAVE_CONFIG" };
  payload[configKey] = checked;
  feedback.textContent = checked ? enabledMessage : disabledMessage;

  chrome.runtime.sendMessage(payload, (response) => {
    if (!response || !response.ok) {
      toggle.checked = !checked;
      feedback.textContent = (response && response.error) || "Could not save Nyxify toggle.";
      return;
    }
    feedback.textContent = checked ? enabledMessage : disabledMessage;
  });
}

document.getElementById("saveOptionsButton").addEventListener("click", saveOptions);
TOGGLE_OPTIONS.forEach(([toggleId, configKey, enabledMessage, disabledMessage]) => {
  const toggle = document.getElementById(toggleId);
  if (!toggle) {
    return;
  }
  toggle.addEventListener("change", () => {
    saveToggleOption(toggleId, configKey, enabledMessage, disabledMessage);
  });
});
loadOptions();
