import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for extension config tests")
def test_extension_hydrates_backend_config_as_source_of_truth():
    script = r"""
const fs = require("fs");
const vm = require("vm");

const backgroundPath = process.argv[1];
const source = fs.readFileSync(backgroundPath, "utf8");
const syncStore = {
  nyxifyConfig: {
    localApiUrl: "http://127.0.0.1:8866",
    localToken: "tok",
    tagOne: "Snapchat",
    adspowerTagsEnabled: true,
    fullAutoModeEnabled: false,
    continuousModeEnabled: false,
    keepProfileOpenAfterSignup: false
  }
};
const localStore = {};
const storageSets = [];
const fetchCalls = [];

function pick(store, key) {
  if (Array.isArray(key)) {
    return Object.fromEntries(key.map((item) => [item, store[item]]));
  }
  if (typeof key === "string") {
    return { [key]: store[key] };
  }
  return { ...store };
}

const chromeStub = {
  storage: {
    sync: {
      get: async (key) => pick(syncStore, key),
      set: async (value) => {
        storageSets.push(value);
        Object.assign(syncStore, value);
      }
    },
    local: {
      get: async (key) => pick(localStore, key),
      set: async (value) => Object.assign(localStore, value)
    },
    onChanged: { addListener: () => {} }
  },
  runtime: {
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} },
    onConnect: { addListener: () => {} },
    onMessage: { addListener: () => {} },
    getURL: (path) => path,
    lastError: null
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: () => {} }
  },
  tabs: {
    onRemoved: { addListener: () => {} },
    create: async () => ({ id: 1 }),
    remove: async () => {},
    get: async () => ({}),
    sendMessage: (_tabId, _message, callback) => callback({ ok: true })
  },
  action: {
    setBadgeBackgroundColor: async () => {},
    setBadgeText: async () => {}
  }
};

const backendConfig = {
  max_parallel_profiles: 3,
  temporary_profile_name: "Snapchat: xoxoxo",
  adspower_group: "Snapchat20",
  extension_category: "Snap",
  tag_one: "",
  tag_two: "",
  adspower_tags_enabled: false,
  blocked_proxies: ["178.", "23.", "109."],
  proxy_blocker_enabled: true,
  proxy_checker_enabled: true,
  push_adspower_id_enabled: true,
  full_auto_mode_enabled: true,
  continuous_mode_enabled: true,
  keep_profile_open_after_signup: true,
  verification_priority: "phone"
};

const context = {
  console,
  chrome: chromeStub,
  setTimeout,
  clearTimeout,
  Date,
  fetch: async (url, options = {}) => {
    fetchCalls.push({ url, method: options.method || "GET" });
    if (String(url).endsWith("/config") && (options.method || "GET") === "GET") {
      return { ok: true, status: 200, json: async () => ({ ok: true, config: backendConfig }) };
    }
    if (String(url).endsWith("/status")) {
      return { ok: true, status: 200, json: async () => ({ ok: true, status: { config: backendConfig } }) };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }
};
vm.createContext(context);
vm.runInContext(
  source + "\n" + `
    globalThis.__test = {
      normalizeConfig,
      extensionConfigFromRunnerConfig,
      runnerConfigPayloadFromExtensionConfig,
      getStatusSnapshot
    };
  `,
  context,
  { filename: backgroundPath }
);

(async () => {
  const defaults = context.__test.normalizeConfig({});
  const mapped = context.__test.extensionConfigFromRunnerConfig(backendConfig, defaults);
  const payload = context.__test.runnerConfigPayloadFromExtensionConfig(mapped);
  const status = await context.__test.getStatusSnapshot(true);
  const savedConfig = syncStore.nyxifyConfig;

  process.stdout.write(JSON.stringify({
    defaults,
    mapped,
    payload,
    statusConfig: status.config,
    savedConfig,
    storageSetCount: storageSets.length,
    fetchedConfig: fetchCalls.some((call) => call.url.endsWith("/config") && call.method === "GET")
  }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "nyxify_extension" / "background.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)

    assert data["defaults"]["tagOne"] == ""
    assert data["defaults"]["adspowerTagsEnabled"] is False
    assert data["mapped"]["tagOne"] == ""
    assert data["mapped"]["adspowerTagsEnabled"] is False
    assert data["mapped"]["fullAutoModeEnabled"] is True
    assert data["mapped"]["continuousModeEnabled"] is True
    assert data["mapped"]["keepProfileOpenAfterSignup"] is True
    assert data["mapped"]["verificationPriority"] == "phone"
    assert data["payload"]["tag_one"] == ""
    assert data["payload"]["adspower_tags_enabled"] is False
    assert data["payload"]["continuous_mode_enabled"] is True
    assert data["payload"]["keep_profile_open_after_signup"] is True
    assert data["payload"]["verification_priority"] == "phone"
    assert data["statusConfig"]["tagOne"] == ""
    assert data["statusConfig"]["adspowerTagsEnabled"] is False
    assert data["statusConfig"]["continuousModeEnabled"] is True
    assert data["statusConfig"]["keepProfileOpenAfterSignup"] is True
    assert data["statusConfig"]["verificationPriority"] == "phone"
    assert data["savedConfig"]["tagOne"] == ""
    assert data["savedConfig"]["keepProfileOpenAfterSignup"] is True
    assert data["savedConfig"]["verificationPriority"] == "phone"
    assert data["fetchedConfig"] is True
    assert data["storageSetCount"] >= 1


def test_nyxify_popup_and_options_expose_synced_runner_controls():
    popup_html = (ROOT / "nyxify_extension" / "popup.html").read_text()
    options_html = (ROOT / "nyxify_extension" / "options.html").read_text()
    options_js = (ROOT / "nyxify_extension" / "options.js").read_text()
    dashboard_js = (ROOT / "webui" / "dashboard.js").read_text()

    assert 'id="popupProxyBlockerToggle"' in popup_html
    assert 'id="popupProxyCheckerToggle"' in popup_html
    assert 'id="popupContinuousModeToggle"' in popup_html
    assert 'id="popupKeepProfileOpenToggle"' in popup_html
    assert "Keep Profile Open" in popup_html
    assert 'id="popupPushAdspowerIdToggle"' not in popup_html
    assert 'id="popupAdspowerTagsToggle"' not in popup_html
    assert 'id="popupTagOne" class="input" type="text" placeholder="Optional tag"' in popup_html

    assert 'id="continuousModeToggle"' in options_html
    assert 'id="keepProfileOpenToggle"' in options_html
    assert 'id="adspowerTagsToggle" type="checkbox" checked' not in options_html
    assert 'id="tagOne" class="input" type="text" placeholder="Optional tag"' in options_html

    assert 'const DEFAULT_TAG_ONE = "";' in options_js
    assert '["continuousModeToggle", "continuousModeEnabled"' in options_js
    assert '["keepProfileOpenToggle", "keepProfileOpenAfterSignup"' in options_js
    assert "adspowerTagsEnabled: safeConfig.adspowerTagsEnabled === true" in options_js
    assert 'document.getElementById("continuousModeToggle").checked = config.continuousModeEnabled === true;' in options_js
    assert 'document.getElementById("keepProfileOpenToggle").checked = config.keepProfileOpenAfterSignup === true;' in options_js
    assert 'continuousModeEnabled: document.getElementById("continuousModeToggle").checked' in options_js
    assert 'keepProfileOpenAfterSignup: document.getElementById("keepProfileOpenToggle").checked' in options_js

    assert 'id="ncfg-proxy_blocker_enabled"' in dashboard_js
    assert 'id="ncfg-proxy_checker_enabled"' in dashboard_js
    assert 'id="ncfg-proxy_priority_enabled"' in dashboard_js
    assert 'id="ncfg-proxy_priority_patterns"' in dashboard_js
    assert "v.adspower_tags_enabled === true" in dashboard_js
    assert 'proxy_blocker_enabled: el("ncfg-proxy_blocker_enabled").checked' in dashboard_js
    assert 'proxy_checker_enabled: el("ncfg-proxy_checker_enabled").checked' in dashboard_js
    assert 'proxy_priority_enabled: el("ncfg-proxy_priority_enabled").checked' in dashboard_js
    assert 'proxy_priority_patterns: el("ncfg-proxy_priority_patterns").value.split' in dashboard_js


def test_extension_keeps_valid_priority_proxy_when_snapboard_sends_stale_row():
    background_js = (ROOT / "nyxify_extension" / "background.js").read_text()

    assert "const proxyPrepInFlight = new Set();" in background_js
    assert "proxyMatchesPriority(existingProxy" in background_js
    assert "normalizedDetectedRows" in background_js


def test_snapboard_proxy_button_click_does_not_start_a_detection_scan():
    content_js = (ROOT / "nyxify_extension" / "content.js").read_text()

    assert "function queueScanFromInteraction" in content_js
    assert "buttonMatchesRotateIntent(button)" in content_js
    assert 'document.addEventListener("click", queueScanFromInteraction, true)' in content_js


def test_proxy_rotation_returns_without_click_when_current_proxy_is_already_valid():
    content_js = (ROOT / "nyxify_extension" / "content.js").read_text()

    assert "var initialPriorityOk = !patterns.length || proxyMatchesPriority(oldProxy, patterns);" in content_js
    assert "if (oldProxy && initialPriorityOk && initialBlockerOk && proxyMatchesType(rowId, desiredType) && !force)" in content_js


def test_extension_refreshes_runner_config_while_snapboard_bridge_is_connected():
    background_js = (ROOT / "nyxify_extension" / "background.js").read_text()

    assert "REMOTE_CONFIG_SYNC_INTERVAL_MS" in background_js
    assert "syncExtensionConfigFromRunner" in background_js


def test_extension_retries_pending_proxy_preparation_without_page_events():
    background_js = (ROOT / "nyxify_extension" / "background.js").read_text()

    assert "PROXY_PREP_RETRY_INTERVAL_MS" in background_js
    assert "prepareStoredProxyRows" in background_js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for extension config tests")
def test_extension_prepares_priority_proxy_rows_when_nyxify_is_off():
    script = r"""
const fs = require("fs");
const vm = require("vm");

const backgroundPath = process.argv[1];
const source = fs.readFileSync(backgroundPath, "utf8");
const syncStore = {
  nyxifyConfig: {
    enabled: false,
    localApiUrl: "http://127.0.0.1:8866",
    proxyPriorityEnabled: true,
    proxyPriorityPatterns: ["23.54"]
  }
};
const localStore = {};
const tabMessages = [];
const fetchCalls = [];

function pick(store, key) {
  if (Array.isArray(key)) return Object.fromEntries(key.map((item) => [item, store[item]]));
  if (typeof key === "string") return { [key]: store[key] };
  return { ...store };
}

const chromeStub = {
  storage: {
    sync: {
      get: async (key) => pick(syncStore, key),
      set: async (value) => Object.assign(syncStore, value)
    },
    local: {
      get: async (key) => pick(localStore, key),
      set: async (value) => Object.assign(localStore, value)
    },
    onChanged: { addListener: () => {} }
  },
  runtime: {
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} },
    onConnect: { addListener: () => {} },
    onMessage: { addListener: () => {} },
    getURL: (path) => path,
    lastError: null
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: () => {} }
  },
  tabs: {
    onRemoved: { addListener: () => {} },
    create: async () => ({ id: 1 }),
    remove: async () => {},
    get: async () => ({}),
    sendMessage: (_tabId, message, callback) => {
      tabMessages.push(message);
      callback({ ok: true, proxy: "23.54.1.2:9000:u:p" });
    }
  },
  action: {
    setBadgeBackgroundColor: async () => {},
    setBadgeText: async () => {}
  }
};

const context = {
  console,
  chrome: chromeStub,
  setTimeout,
  clearTimeout,
  Date,
  fetch: async (url, options = {}) => {
    fetchCalls.push({ url, method: options.method || "GET" });
    return { ok: true, status: 200, json: async () => ({ ok: true, rows: [] }) };
  }
};

vm.createContext(context);
vm.runInContext(
  source + "\n" + `
    globalThis.__test = { handleDetectedRows };
  `,
  context,
  { filename: backgroundPath }
);

(async () => {
  const result = await context.__test.handleDetectedRows({
    rows: [{
      row_key: "snapboard:1",
      model: "Clea",
      ip_address: "45.10.1.1",
      proxy_address: "45.10.1.1:9000:u:p",
      username: "cleauser",
      password: "Password1!"
    }]
  }, { tab: { id: 77, url: "https://snapboard.test/" } });

  process.stdout.write(JSON.stringify({
    result,
    pending: localStore.nyxifyPendingEntries,
    tabMessages,
    queueUpserts: fetchCalls.filter((call) => String(call.url).endsWith("/queue/upsert")).length
  }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "nyxify_extension" / "background.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)

    assert data["result"]["skippedQueue"] is True
    assert data["result"]["priorityPrepared"] == 1
    assert data["queueUpserts"] == 0
    assert data["tabMessages"][0]["action"] == "proxy_rotate"
    assert data["tabMessages"][0]["priority_patterns"] == ["23.54"]
    assert data["pending"][0]["proxy_address"] == "23.54.1.2:9000:u:p"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for extension config tests")
def test_extension_prepares_blocked_proxy_rows_when_nyxify_is_off_and_priority_is_off():
    script = r"""
const fs = require("fs");
const vm = require("vm");

const backgroundPath = process.argv[1];
const source = fs.readFileSync(backgroundPath, "utf8");
const syncStore = {
  nyxifyConfig: {
    enabled: false,
    localApiUrl: "http://127.0.0.1:8866",
    proxyBlockerEnabled: true,
    blockedProxies: ["45.10"],
    bannedProxies: ["45.10"],
    proxyPriorityEnabled: false,
    proxyPriorityPatterns: []
  }
};
const localStore = {};
const tabMessages = [];
const fetchCalls = [];

function pick(store, key) {
  if (Array.isArray(key)) return Object.fromEntries(key.map((item) => [item, store[item]]));
  if (typeof key === "string") return { [key]: store[key] };
  return { ...store };
}

const chromeStub = {
  storage: {
    sync: {
      get: async (key) => pick(syncStore, key),
      set: async (value) => Object.assign(syncStore, value)
    },
    local: {
      get: async (key) => pick(localStore, key),
      set: async (value) => Object.assign(localStore, value)
    },
    onChanged: { addListener: () => {} }
  },
  runtime: {
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} },
    onConnect: { addListener: () => {} },
    onMessage: { addListener: () => {} },
    getURL: (path) => path,
    lastError: null
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: () => {} }
  },
  tabs: {
    onRemoved: { addListener: () => {} },
    create: async () => ({ id: 1 }),
    remove: async () => {},
    get: async () => ({}),
    sendMessage: (_tabId, message, callback) => {
      tabMessages.push(message);
      callback({ ok: true, proxy: "130.24.1.2:9000:u:p" });
    }
  },
  action: {
    setBadgeBackgroundColor: async () => {},
    setBadgeText: async () => {}
  }
};

const context = {
  console,
  chrome: chromeStub,
  setTimeout,
  clearTimeout,
  Date,
  fetch: async (url, options = {}) => {
    fetchCalls.push({ url, method: options.method || "GET" });
    return { ok: true, status: 200, json: async () => ({ ok: true, rows: [] }) };
  }
};

vm.createContext(context);
vm.runInContext(
  source + "\n" + `
    globalThis.__test = { handleDetectedRows };
  `,
  context,
  { filename: backgroundPath }
);

(async () => {
  const result = await context.__test.handleDetectedRows({
    rows: [{
      row_key: "snapboard:1",
      model: "Clea",
      ip_address: "45.10.1.1",
      proxy_address: "45.10.1.1:9000:u:p",
      username: "cleauser",
      password: "Password1!"
    }]
  }, { tab: { id: 77, url: "https://snapboard.test/" } });

  process.stdout.write(JSON.stringify({
    result,
    pending: localStore.nyxifyPendingEntries,
    tabMessages,
    queueUpserts: fetchCalls.filter((call) => String(call.url).endsWith("/queue/upsert")).length
  }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "nyxify_extension" / "background.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)

    assert data["result"]["skippedQueue"] is True
    assert data["result"]["blockedPrepared"] == 1
    assert data["queueUpserts"] == 0
    assert data["tabMessages"][0]["action"] == "proxy_rotate"
    assert data["tabMessages"][0]["blocked_patterns"] == ["45.10"]
    assert data["tabMessages"][0]["priority_patterns"] == []
    assert data["pending"][0]["proxy_address"] == "130.24.1.2:9000:u:p"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for extension config tests")
def test_extension_retries_stored_pending_priority_proxy_rows_when_nyxify_is_off():
    script = r"""
const fs = require("fs");
const vm = require("vm");

const backgroundPath = process.argv[1];
const source = fs.readFileSync(backgroundPath, "utf8");
const syncStore = {
  nyxifyConfig: {
    enabled: false,
    localApiUrl: "http://127.0.0.1:8866",
    proxyPriorityEnabled: true,
    proxyPriorityPatterns: ["23", "9", "65", "204"]
  }
};
const localStore = {
  nyxifyPendingEntries: [{
    row_key: "snapboard:1",
    model: "Clea",
    ip_address: "79.176.64.105",
    proxy_address: "79.176.64.105:47457:qNFHslAp:zYJPXLU5",
    username: "cleauser",
    password: "Password1!"
  }]
};
const tabMessages = [];

function pick(store, key) {
  if (Array.isArray(key)) return Object.fromEntries(key.map((item) => [item, store[item]]));
  if (typeof key === "string") return { [key]: store[key] };
  return { ...store };
}

const chromeStub = {
  storage: {
    sync: {
      get: async (key) => pick(syncStore, key),
      set: async (value) => Object.assign(syncStore, value)
    },
    local: {
      get: async (key) => pick(localStore, key),
      set: async (value) => Object.assign(localStore, value)
    },
    onChanged: { addListener: () => {} }
  },
  runtime: {
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} },
    onConnect: { addListener: () => {} },
    onMessage: { addListener: () => {} },
    getURL: (path) => path,
    lastError: null
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: () => {} }
  },
  tabs: {
    onRemoved: { addListener: () => {} },
    query: async () => [{ id: 77 }],
    create: async () => ({ id: 1 }),
    remove: async () => {},
    get: async () => ({}),
    sendMessage: (_tabId, message, callback) => {
      tabMessages.push(message);
      callback({ ok: false, error: "Proxy did not match priority after rotation." });
    }
  },
  action: {
    setBadgeBackgroundColor: async () => {},
    setBadgeText: async () => {}
  }
};

const context = {
  console,
  chrome: chromeStub,
  setTimeout,
  clearTimeout,
  Date,
  fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, rows: [] }) })
};

vm.createContext(context);
vm.runInContext(
  source + "\n" + `
    globalThis.__test = { prepareStoredProxyRows };
  `,
  context,
  { filename: backgroundPath }
);

(async () => {
  const result = await context.__test.prepareStoredProxyRows();
  process.stdout.write(JSON.stringify({ result, pending: localStore.nyxifyPendingEntries, tabMessages }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "nyxify_extension" / "background.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)

    assert data["result"]["priorityPrepared"] == 0
    assert data["result"]["priorityAttempted"] == 1
    assert data["tabMessages"][0]["action"] == "proxy_rotate"
    assert data["tabMessages"][0]["max_clicks"] == 10
    assert data["tabMessages"][0]["priority_patterns"] == ["23", "9", "65", "204"]
    assert data["pending"][0]["proxy_address"] == "79.176.64.105:47457:qNFHslAp:zYJPXLU5"


def test_nyxify_extension_exposes_lock_tv_provider_lock():
    ext = ROOT / "nyxify_extension"
    popup_html = (ext / "popup.html").read_text()
    options_html = (ext / "options.html").read_text()
    popup_js = (ext / "popup.js").read_text()
    options_js = (ext / "options.js").read_text()
    background_js = (ext / "background.js").read_text()

    # Popup provider locks use the compact AM/G5/5M and SP/TV segmented controls.
    assert 'data-provider-lock="email"' in popup_html
    assert 'data-provider-lock="tv"' in popup_html
    assert 'data-config-key="emailProviderLock"' in popup_html
    assert 'data-config-key="lockTV"' in popup_html
    assert 'data-value="am" aria-pressed="true">AM</button>' in popup_html
    assert 'data-value="g5" aria-pressed="false">G5</button>' in popup_html
    assert 'data-value="5m" aria-pressed="false">5M</button>' in popup_html
    assert 'data-value="false" aria-pressed="true">SP</button>' in popup_html
    assert 'data-value="true" aria-pressed="false">TV</button>' in popup_html
    assert "popupLockG5Toggle" not in popup_html
    assert "popupLockTVToggle" not in popup_html
    assert "popupLockG5Toggle" not in popup_js
    assert "popupLockTVToggle" not in popup_js

    # The full options page still exposes the same stored config keys.
    assert 'id="lockTVToggle"' in options_html
    assert '["lockTVToggle", "lockTV"' in options_js

    # lockTV survives every config normalizer so the runner sync never drops it.
    assert "lockTV: safeConfig.lockTV === true" in background_js
    assert "lockTV: safeConfig.lockTV === true" in options_js
    assert "lockTV: safeConfig.lockTV === true" in popup_js
