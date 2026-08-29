const FLUSH_ALARM = "nyxify-flush-queue";
const STORAGE_KEYS = {
  config: "nyxifyConfig",
  pending: "nyxifyPendingEntries",
  lastSeen: "nyxifyLastSeenEntries",
  lastSync: "nyxifyLastSync",
  eventLog: "nyxifyEventLog",
  autoFillProgress: "nyxifyAutoFillAccountProgress",
};
const SCRAPE_STORAGE_KEYS = {
  config: "nyxifyUsernameScrapeConfig",
  inputText: "nyxifyUsernameScrapeInputText",
  scrapeResults: "nyxifyUsernameScrapeResults",
  eventLog: "nyxifyUsernameScrapeEventLog",
  runnerState: "nyxifyUsernameScrapeRunnerState",
};
const SCRAPE_RUNNER_IDLE = "idle";
const SCRAPE_RUNNER_RUNNING = "running";
const SCRAPE_RUNNER_PAUSED = "paused";
const SCRAPE_RUNNER_STOPPED = "stopped";
const SCRAPE_MAX_PARALLEL_TABS = 50;
// A worker is force-settled once it has been open for this long with no result,
// covering tabs whose content script never reported and whose in-memory
// setTimeout was lost to MV3 service-worker hibernation.
const SCRAPE_WORKER_HARD_DEADLINE_MS = 90000;
const POPUP_PORT_NAME = "nyxify-popup-live";
const POPUP_LIVE_POLL_MS = 1500;
const STATUS_CACHE_TTL_MS = 1000;
const RUNNER_STATUS_CACHE_TTL_MS = 2500;
// Cap a local-API fetch so a slow/hung bridge never keeps the popup waiting.
const LOCAL_API_TIMEOUT_MS = 4000;
// Short-lived config cache so per-tick calls don't hit chrome.storage.sync each
// request.
const LOCAL_CONFIG_CACHE_TTL_MS = 3500;
let localConfigCache = null;
let localConfigCacheAt = 0;

function fetchWithTimeout(url, options, timeoutMs) {
  // AbortController may not be available in every sandbox (e.g. tests); degrade
  // to a plain fetch rather than throwing on a slow call.
  if (typeof AbortController === "undefined") {
    return fetch(url, options || {});
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || LOCAL_API_TIMEOUT_MS);
  return fetch(url, Object.assign({}, options || {}, { signal: controller.signal }))
    .finally(() => clearTimeout(timer));
}

async function getLocalConfig() {
  const now = Date.now();
  if (localConfigCache && (now - localConfigCacheAt) < LOCAL_CONFIG_CACHE_TTL_MS) {
    return localConfigCache;
  }
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  localConfigCache = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  localConfigCacheAt = now;
  return localConfigCache;
}
const MAX_EMAIL_BRIDGE_BATCH = 50;
const DEFAULT_TEMPORARY_PROFILE_NAME = "Snapchat:";
const DEFAULT_ADSPOWER_GROUP = "Snapchat";
const DEFAULT_EXTENSION_CATEGORY = "Snap";
const DEFAULT_TAG_ONE = "";

let flushInFlight = null;
let activeScrapeRun = null;
let scrapeHydrationInFlight = null;
const snapboardPorts = new Map();
let bridgeLoopPromise = null;
const popupPorts = new Set();
let popupStatusTimer = null;
let popupStatusSignature = "";
let popupStatusRequest = null;
let autoFillReserveInFlight = Promise.resolve();
const fullAutoRowsInFlight = new Set();
let runnerStatusRequest = null;
let runnerStatusCache = {
  at: 0,
  status: null,
};
let popupStatusCache = {
  at: 0,
  status: null,
};

function normalizePositiveInteger(value, fallback = 0) {
  const parsed = parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function normalizeConfig(config) {
  const safeConfig = config || {};
  const parsedRowLimit = normalizePositiveInteger(safeConfig.rowLimit, 20);
  const parsedMaxParallel = normalizePositiveInteger(safeConfig.maxParallelProfiles, 1);
  const hasBlockedProxies = Object.prototype.hasOwnProperty.call(safeConfig, "blockedProxies");
  const hasBannedProxies = Object.prototype.hasOwnProperty.call(safeConfig, "bannedProxies");
  const rawBlocked = hasBannedProxies
    ? safeConfig.bannedProxies
    : (hasBlockedProxies ? safeConfig.blockedProxies : "");
  const bannedProxies = Array.isArray(rawBlocked)
    ? rawBlocked
    : String(rawBlocked).split(/\r?\n/);

  return {
    localApiUrl: String(safeConfig.localApiUrl || "http://127.0.0.1:8866").trim(),
    localToken: String(safeConfig.localToken || "").trim(),
    enabled: safeConfig.enabled !== false,
    rowLimit: parsedRowLimit,
    temporaryProfileName: normalizeStringConfig(safeConfig, "temporaryProfileName", DEFAULT_TEMPORARY_PROFILE_NAME, false),
    adspowerGroup: normalizeStringConfig(safeConfig, "adspowerGroup", DEFAULT_ADSPOWER_GROUP),
    extensionCategory: normalizeStringConfig(safeConfig, "extensionCategory", DEFAULT_EXTENSION_CATEGORY, false),
    tagOne: normalizeStringConfig(safeConfig, "tagOne", DEFAULT_TAG_ONE),
    tagTwo: String(safeConfig.tagTwo || "").trim(),
    adspowerTagsEnabled: safeConfig.adspowerTagsEnabled === true,
    maxParallelProfiles: parsedMaxParallel,
    bannedProxies: bannedProxies.map((item) => String(item || "").trim()).filter(Boolean),
    blockedProxies: bannedProxies.map((item) => String(item || "").trim()).filter(Boolean),
    proxyBlockerEnabled: safeConfig.proxyBlockerEnabled !== false,
    proxyCheckerEnabled: safeConfig.proxyCheckerEnabled !== false,
    pushAdspowerIdEnabled: safeConfig.pushAdspowerIdEnabled !== false,
    fullAutoModeEnabled: safeConfig.fullAutoModeEnabled === true,
    continuousModeEnabled: safeConfig.continuousModeEnabled === true,
    keepProfileOpenAfterSignup: safeConfig.keepProfileOpenAfterSignup === true,
    autoFillRow: safeConfig.autoFillRow === true,
    autoFillAccountTarget: normalizePositiveInteger(safeConfig.autoFillAccountTarget, 0),
    lockG5: safeConfig.lockG5 === true,
    lockTV: safeConfig.lockTV === true,
  };
}

function extensionConfigFromRunnerConfig(runnerConfig, baseConfig = {}) {
  const runner = runnerConfig || {};
  const base = normalizeConfig(baseConfig || {});
  const blocked = Array.isArray(runner.blocked_proxies)
    ? runner.blocked_proxies
    : (Array.isArray(runner.banned_proxies) ? runner.banned_proxies : base.blockedProxies);
  return normalizeConfig({
    ...base,
    maxParallelProfiles: runner.max_parallel_profiles,
    temporaryProfileName: runner.temporary_profile_name,
    adspowerGroup: runner.adspower_group,
    extensionCategory: runner.extension_category,
    tagOne: Object.prototype.hasOwnProperty.call(runner, "tag_one") ? runner.tag_one : base.tagOne,
    tagTwo: Object.prototype.hasOwnProperty.call(runner, "tag_two") ? runner.tag_two : base.tagTwo,
    adspowerTagsEnabled: runner.adspower_tags_enabled === true,
    blockedProxies: blocked,
    bannedProxies: blocked,
    proxyBlockerEnabled: runner.proxy_blocker_enabled !== false,
    proxyCheckerEnabled: runner.proxy_checker_enabled !== false,
    pushAdspowerIdEnabled: runner.push_adspower_id_enabled !== false,
    fullAutoModeEnabled: runner.full_auto_mode_enabled === true,
    continuousModeEnabled: runner.continuous_mode_enabled === true,
    keepProfileOpenAfterSignup: runner.keep_profile_open_after_signup === true,
  });
}

function runnerConfigPayloadFromExtensionConfig(config, replaceBlocked = false) {
  const safe = normalizeConfig(config || {});
  const payload = {
    max_parallel_profiles: safe.maxParallelProfiles,
    temporary_profile_name: safe.temporaryProfileName,
    adspower_group: safe.adspowerGroup,
    extension_category: safe.extensionCategory,
    tag_one: safe.tagOne,
    tag_two: safe.tagTwo,
    adspower_tags_enabled: safe.adspowerTagsEnabled,
    blocked_proxies: safe.blockedProxies || safe.bannedProxies,
    proxy_blocker_enabled: safe.proxyBlockerEnabled,
    proxy_checker_enabled: safe.proxyCheckerEnabled,
    push_adspower_id_enabled: safe.pushAdspowerIdEnabled,
    full_auto_mode_enabled: safe.fullAutoModeEnabled,
    continuous_mode_enabled: safe.continuousModeEnabled,
    keep_profile_open_after_signup: safe.keepProfileOpenAfterSignup,
  };
  // The runner ignores blocked_proxies unless this flag says the caller is a
  // deliberate banned-list editor — so an incidental save can't wipe bans added
  // from the Proxy Ranking "Ban" button. Only set it when the user actually
  // edited the banned-proxies field.
  if (replaceBlocked) {
    payload.blocked_proxies_replace = true;
  }
  return payload;
}

function normalizeAutoFillProgress(progress, target) {
  const normalizedTarget = normalizePositiveInteger(target, 0);
  const safeProgress = progress || {};
  const progressTarget = normalizePositiveInteger(safeProgress.target, 0);
  const rawCount = parseInt(safeProgress.count, 10);
  const count = Number.isFinite(rawCount) && rawCount > 0 ? rawCount : 0;

  if (!normalizedTarget || progressTarget !== normalizedTarget) {
    return { target: normalizedTarget, count: 0 };
  }

  return {
    target: normalizedTarget,
    count: Math.min(count, normalizedTarget),
  };
}

async function resetAutoFillProgress(target) {
  const progress = { target: normalizePositiveInteger(target, 0), count: 0 };
  await chrome.storage.local.set({
    [STORAGE_KEYS.autoFillProgress]: progress,
  });
  return progress;
}

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

function normalizeUsername(value) {
  return String(value || "").trim();
}

function isTempUsername(value) {
  return /^temp(?:[_-].+|\d.*)?$/i.test(normalizeUsername(value));
}

function normalizeScrapeText(value) {
  return String(value || "").trim();
}

function normalizeScrapeUsernameKey(value) {
  return normalizeScrapeText(value).toLowerCase();
}

function normalizeScrapeConfig(config) {
  const safeConfig = config || {};
  const parsedParallelTabs = parseInt(safeConfig.maxParallelTabs, 10);
  const parsedTimeoutMs = parseInt(safeConfig.profileTimeoutMs, 10);

  return {
    enabled: safeConfig.enabled !== false,
    maxParallelTabs: Math.max(1, Math.min(SCRAPE_MAX_PARALLEL_TABS, Number.isFinite(parsedParallelTabs) ? parsedParallelTabs : 4)),
    profileTimeoutMs: Math.max(4000, Math.min(60000, Number.isFinite(parsedTimeoutMs) ? parsedTimeoutMs : 12000)),
  };
}

// Serialize every scrape storage read-modify-write. With many parallel tabs
// finishing at once, concurrent upserts/counter writes used to clobber each
// other (lost results + undercounted totals) — the "inaccurate numbers / stuck
// at finish" bug. Routing all mutations through one promise chain makes them
// atomic with respect to each other.
let scrapeStorageMutex = Promise.resolve();
function withScrapeStorageLock(fn) {
  const run = scrapeStorageMutex.then(() => fn());
  scrapeStorageMutex = run.then(() => undefined, () => undefined);
  return run;
}

async function getScrapeConfig() {
  const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.config);
  return normalizeScrapeConfig(data[SCRAPE_STORAGE_KEYS.config] || {});
}

async function saveScrapeConfig(patch) {
  const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.config);
  const nextConfig = normalizeScrapeConfig({
    ...(data[SCRAPE_STORAGE_KEYS.config] || {}),
    ...(patch || {}),
  });
  await chrome.storage.local.set({
    [SCRAPE_STORAGE_KEYS.config]: nextConfig,
  });
  return nextConfig;
}

function buildScrapeProfileUrl(username) {
  return `https://www.snapchat.com/@${encodeURIComponent(normalizeScrapeText(username))}`;
}

function getDefaultScrapeRunnerState() {
  return {
    status: SCRAPE_RUNNER_IDLE,
    run_id: "",
    total: 0,
    completed: 0,
    has_bitmoji: 0,
    no_bitmoji: 0,
    not_found: 0,
    unknown: 0,
    current_username: "",
    queue: [],
    queue_entries: [],
    active_usernames: [],
    active_workers: [],
    active_count: 0,
    run_started_at: "",
    updated_at: "",
  };
}

async function getScrapeRunnerState() {
  const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.runnerState);
  return data[SCRAPE_STORAGE_KEYS.runnerState] || getDefaultScrapeRunnerState();
}

async function setScrapeRunnerState(patch) {
  return withScrapeStorageLock(async () => {
    const current = await getScrapeRunnerState();
    const next = {
      ...current,
      ...(patch || {}),
      updated_at: new Date().toISOString(),
    };
    await chrome.storage.local.set({
      [SCRAPE_STORAGE_KEYS.runnerState]: next,
    });
    return next;
  });
}

async function appendScrapeEvent(message) {
  return withScrapeStorageLock(async () => {
    const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.eventLog);
    const current = Array.isArray(data[SCRAPE_STORAGE_KEYS.eventLog]) ? data[SCRAPE_STORAGE_KEYS.eventLog] : [];
    const next = [{
      at: new Date().toISOString(),
      message: normalizeScrapeText(message),
    }].concat(current).slice(0, 120);

    await chrome.storage.local.set({
      [SCRAPE_STORAGE_KEYS.eventLog]: next,
    });
  });
}

function extractScrapeUsernameToken(rawToken) {
  let token = normalizeScrapeText(rawToken);
  if (!token) {
    return "";
  }

  const urlMatch = token.match(/(?:https?:\/\/)?(?:www\.)?snapchat\.com\/@([^/?#]+)/i);
  if (urlMatch && urlMatch[1]) {
    token = urlMatch[1];
  }

  token = token
    .replace(/^@+/, "")
    .split(/[/?#]/)[0]
    .replace(/^[("'`\[]+/, "")
    .replace(/[)"'`,.;:\]]+$/, "")
    .replace(/[^A-Za-z0-9._-]/g, "");

  return token.length >= 2 ? token : "";
}

function parseScrapeInputUsernames(rawText) {
  const tokens = String(rawText || "").split(/[\s,;]+/);
  const unique = new Map();
  let inputIndex = 0;

  tokens.forEach((token) => {
    const username = extractScrapeUsernameToken(token);
    const key = normalizeScrapeUsernameKey(username);
    if (!key || unique.has(key)) {
      return;
    }
    unique.set(key, {
      username,
      key,
      input_index: inputIndex,
    });
    inputIndex += 1;
  });

  return Array.from(unique.values());
}

async function getScrapeStatus() {
  const localData = await chrome.storage.local.get([
    SCRAPE_STORAGE_KEYS.inputText,
    SCRAPE_STORAGE_KEYS.scrapeResults,
    SCRAPE_STORAGE_KEYS.eventLog,
    SCRAPE_STORAGE_KEYS.runnerState,
  ]);
  const config = await getScrapeConfig();
  const inputText = String(localData[SCRAPE_STORAGE_KEYS.inputText] || "");

  return {
    config,
    inputText,
    inputCount: parseScrapeInputUsernames(inputText).length,
    scrapeResults: Array.isArray(localData[SCRAPE_STORAGE_KEYS.scrapeResults]) ? localData[SCRAPE_STORAGE_KEYS.scrapeResults] : [],
    eventLog: Array.isArray(localData[SCRAPE_STORAGE_KEYS.eventLog]) ? localData[SCRAPE_STORAGE_KEYS.eventLog] : [],
    runnerState: localData[SCRAPE_STORAGE_KEYS.runnerState] || await getScrapeRunnerState(),
  };
}

function sortScrapeResults(entries) {
  return entries.slice().sort((left, right) => {
    const leftIndex = Number.isFinite(Number(left && left.input_index)) ? Number(left.input_index) : Number.MAX_SAFE_INTEGER;
    const rightIndex = Number.isFinite(Number(right && right.input_index)) ? Number(right.input_index) : Number.MAX_SAFE_INTEGER;
    if (leftIndex !== rightIndex) {
      return leftIndex - rightIndex;
    }
    return String(left && left.username || "").localeCompare(String(right && right.username || ""));
  });
}

async function upsertScrapeResult(payload) {
  const rawUsername = normalizeScrapeText(payload && payload.username);
  const usernameKey = normalizeScrapeUsernameKey(rawUsername);
  if (!usernameKey) {
    return null;
  }

  return withScrapeStorageLock(async () => {
    const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.scrapeResults);
    const current = Array.isArray(data[SCRAPE_STORAGE_KEYS.scrapeResults]) ? data[SCRAPE_STORAGE_KEYS.scrapeResults] : [];
    const map = new Map(current.map((entry) => [normalizeScrapeUsernameKey(entry && entry.username), entry]));
    const existing = map.get(usernameKey);
    const username = rawUsername || normalizeScrapeText(existing && existing.username);
    const parsedInputIndex = Number(payload && payload.input_index);

    const result = {
      username,
      input_index: Number.isFinite(parsedInputIndex)
        ? parsedInputIndex
        : (Number.isFinite(Number(existing && existing.input_index)) ? Number(existing.input_index) : Number.MAX_SAFE_INTEGER),
      checked_at: new Date().toISOString(),
      profile_url: normalizeScrapeText(payload && payload.profile_url) || buildScrapeProfileUrl(username),
      has_bitmoji: payload && payload.has_bitmoji === true,
      status: normalizeScrapeText(payload && payload.status) || "unknown",
      evidence: normalizeScrapeText(payload && payload.evidence),
    };

    map.set(usernameKey, result);
    await chrome.storage.local.set({
      [SCRAPE_STORAGE_KEYS.scrapeResults]: sortScrapeResults(Array.from(map.values())),
    });
    return result;
  });
}

function getActiveScrapeUsernames() {
  if (!activeScrapeRun) {
    return [];
  }
  return Array.from(activeScrapeRun.workers.values()).map((worker) => worker.username);
}

function serializeScrapeQueueEntries(entries) {
  return (entries || []).map((entry) => ({
    username: normalizeScrapeText(entry && entry.username),
    key: normalizeScrapeUsernameKey(entry && entry.username),
    input_index: Number.isFinite(Number(entry && entry.input_index)) ? Number(entry.input_index) : 0,
  })).filter((entry) => entry.username && entry.key);
}

function serializeActiveScrapeWorkers() {
  if (!activeScrapeRun) {
    return [];
  }
  return Array.from(activeScrapeRun.workers.values()).map((worker) => ({
    username: normalizeScrapeText(worker && worker.username),
    key: normalizeScrapeUsernameKey(worker && worker.username),
    input_index: Number.isFinite(Number(worker && worker.inputIndex)) ? Number(worker.inputIndex) : 0,
    tabId: worker && worker.tabId ? Number(worker.tabId) : 0,
    profile_url: normalizeScrapeText(worker && worker.profileUrl),
    started_at: Number.isFinite(Number(worker && worker.startedAt)) ? Number(worker.startedAt) : Date.now(),
  })).filter((worker) => worker.username && worker.key);
}

async function syncScrapeRunnerLiveState(extraPatch) {
  await setScrapeRunnerState({
    queue: activeScrapeRun ? activeScrapeRun.queue.map((entry) => entry.username) : [],
    queue_entries: activeScrapeRun ? serializeScrapeQueueEntries(activeScrapeRun.queue) : [],
    active_usernames: getActiveScrapeUsernames(),
    active_workers: serializeActiveScrapeWorkers(),
    active_count: activeScrapeRun ? activeScrapeRun.workers.size : 0,
    current_username: getActiveScrapeUsernames().join(", "),
    ...(extraPatch || {}),
  });
}

async function finishScrapeRun(status) {
  activeScrapeRun = null;
  await setScrapeRunnerState({
    status: status || SCRAPE_RUNNER_IDLE,
    current_username: "",
    queue: [],
    queue_entries: [],
    active_usernames: [],
    active_workers: [],
    active_count: 0,
  });
}

async function closeScrapeWorkerTab(worker) {
  if (!worker || !worker.tabId) {
    return;
  }

  try {
    await chrome.tabs.remove(worker.tabId);
  } catch (_error) {
  }
}

// Derive the completed + per-status counters from the saved results array
// rather than incrementing. Incrementing raced under parallel settles and
// drifted below reality; deriving from the authoritative results is always
// exact, so the final numbers (and the "Checked X of Y" line) are correct.
async function recomputeScrapeCounters() {
  const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.scrapeResults);
  const results = Array.isArray(data[SCRAPE_STORAGE_KEYS.scrapeResults]) ? data[SCRAPE_STORAGE_KEYS.scrapeResults] : [];
  const counters = { has_bitmoji: 0, no_bitmoji: 0, not_found: 0, unknown: 0 };
  for (const entry of results) {
    const statusKey = normalizeScrapeText(entry && entry.status) || "unknown";
    if (Object.prototype.hasOwnProperty.call(counters, statusKey)) {
      counters[statusKey] += 1;
    } else {
      counters.unknown += 1;
    }
  }
  return setScrapeRunnerState({ completed: results.length, ...counters });
}

function createScrapeWorkerTimeout(worker) {
  const timeoutMs = Math.max(
    (activeScrapeRun && activeScrapeRun.config && activeScrapeRun.config.profileTimeoutMs) || 12000,
    12000
  );
  return setTimeout(async () => {
    if (!activeScrapeRun || !worker || !activeScrapeRun.workers.has(worker.usernameKey)) {
      return;
    }
    await appendScrapeEvent(`Timed out while checking ${worker.username}.`);
    await settleScrapeWorker(worker.usernameKey, {
      username: worker.username,
      input_index: worker.inputIndex,
      profile_url: worker.profileUrl,
      has_bitmoji: false,
      status: "unknown",
      evidence: "timeout",
    });
  }, timeoutMs);
}

async function hydrateScrapeRunFromStorage() {
  if (activeScrapeRun) {
    return activeScrapeRun;
  }
  if (scrapeHydrationInFlight) {
    return scrapeHydrationInFlight;
  }

  scrapeHydrationInFlight = (async () => {
    return _hydrateScrapeRunFromStorageImpl();
  })().finally(() => {
    scrapeHydrationInFlight = null;
  });

  return scrapeHydrationInFlight;
}

async function _reconcileScrapeCountersFromResults(existingResults, runnerState) {
  const expectedCompleted = Array.isArray(existingResults) ? existingResults.length : 0;
  const counters = { has_bitmoji: 0, no_bitmoji: 0, not_found: 0, unknown: 0 };
  for (const entry of existingResults || []) {
    const statusKey = normalizeScrapeText(entry && entry.status) || "unknown";
    if (Object.prototype.hasOwnProperty.call(counters, statusKey)) {
      counters[statusKey] += 1;
    } else {
      counters.unknown += 1;
    }
  }

  const patch = {};
  if (Number(runnerState && runnerState.completed) !== expectedCompleted) {
    patch.completed = expectedCompleted;
  }
  for (const key of Object.keys(counters)) {
    if (Number(runnerState && runnerState[key]) !== counters[key]) {
      patch[key] = counters[key];
    }
  }
  if (Object.keys(patch).length) {
    await setScrapeRunnerState(patch);
  }
}

async function _hydrateScrapeRunFromStorageImpl() {
  if (activeScrapeRun) {
    return activeScrapeRun;
  }

  const status = await getScrapeStatus();
  const runnerState = status.runnerState || {};
  const currentStatus = String(runnerState.status || "");
  const total = Number(runnerState.total || 0);
  const existingResults = Array.isArray(status.scrapeResults) ? status.scrapeResults : [];

  // Reconcile counters against the actual saved results. If the service
  // worker hibernated mid-flight, some results may have been upserted
  // without the matching settle/recordScrapeRunnerResult call, leaving
  // completed and per-status counters stuck below reality.
  await _reconcileScrapeCountersFromResults(existingResults, runnerState);

  const reconciledRunnerState = await getScrapeRunnerState();
  const completed = Number(reconciledRunnerState.completed || 0);
  if (!total || currentStatus === SCRAPE_RUNNER_STOPPED) {
    return null;
  }
  if (completed >= total && currentStatus === SCRAPE_RUNNER_IDLE) {
    return null;
  }

  const entries = parseScrapeInputUsernames(status.inputText || "");
  if (!entries.length) {
    return null;
  }

  const checkedKeys = new Set(
    existingResults
      .map((entry) => normalizeScrapeUsernameKey(entry && entry.username))
      .filter(Boolean)
  );

  const storedWorkers = Array.isArray(runnerState.active_workers) ? runnerState.active_workers : [];
  const workers = new Map();
  const activeKeys = new Set();
  for (const storedWorker of storedWorkers) {
    const username = normalizeScrapeText(storedWorker && storedWorker.username);
    const key = normalizeScrapeUsernameKey(username);
    const tabId = Number(storedWorker && storedWorker.tabId);
    if (!username || !key || checkedKeys.has(key) || !Number.isFinite(tabId) || tabId <= 0) {
      continue;
    }
    try {
      await chrome.tabs.get(tabId);
    } catch (_error) {
      continue;
    }
    const worker = {
      username,
      usernameKey: key,
      inputIndex: Number.isFinite(Number(storedWorker && storedWorker.input_index)) ? Number(storedWorker.input_index) : 0,
      tabId,
      profileUrl: normalizeScrapeText(storedWorker && storedWorker.profile_url) || buildScrapeProfileUrl(username),
      startedAt: Number.isFinite(Number(storedWorker && storedWorker.started_at)) ? Number(storedWorker.started_at) : Date.now(),
      timeoutId: null,
    };
    worker.timeoutId = createScrapeWorkerTimeout(worker);
    workers.set(key, worker);
    activeKeys.add(key);
  }

  const queueEntries = entries.filter((entry) => {
    const key = normalizeScrapeUsernameKey(entry && entry.username);
    return key && !checkedKeys.has(key) && !activeKeys.has(key);
  });

  activeScrapeRun = {
    runId: normalizeScrapeText(runnerState.run_id) || `${Date.now()}`,
    queue: queueEntries.slice(),
    workers,
    config: status.config || await getScrapeConfig(),
  };

  // A paused session stays paused across a service-worker restart; only revive
  // genuinely-interrupted (non-paused, non-stopped) sessions back to running.
  if (
    currentStatus !== SCRAPE_RUNNER_RUNNING
    && currentStatus !== SCRAPE_RUNNER_PAUSED
    && (queueEntries.length || workers.size)
  ) {
    await appendScrapeEvent("Recovered incomplete username scrape session.");
    await setScrapeRunnerState({
      status: SCRAPE_RUNNER_RUNNING,
      run_id: activeScrapeRun.runId,
    });
  }
  await syncScrapeRunnerLiveState();
  return activeScrapeRun;
}

function getScrapeWorkerEntryByTabId(tabId) {
  if (!activeScrapeRun || !tabId) {
    return null;
  }

  for (const entry of activeScrapeRun.workers.entries()) {
    if (entry[1] && entry[1].tabId === tabId) {
      return {
        key: entry[0],
        worker: entry[1],
      };
    }
  }

  return null;
}

async function settleScrapeWorker(usernameKey, resultPayload) {
  await hydrateScrapeRunFromStorage();
  if (!activeScrapeRun) {
    return;
  }

  const worker = activeScrapeRun.workers.get(usernameKey);
  if (!worker) {
    return;
  }

  if (worker.timeoutId) {
    clearTimeout(worker.timeoutId);
  }

  activeScrapeRun.workers.delete(usernameKey);

  const payload = {
    ...(resultPayload || {}),
    username: normalizeScrapeText((resultPayload && resultPayload.username) || worker.username),
    input_index: worker.inputIndex,
  };
  await upsertScrapeResult(payload);
  await recomputeScrapeCounters();
  await closeScrapeWorkerTab(worker);
  await syncScrapeRunnerLiveState();
  await maybeProcessScrapeQueue();
}

async function launchScrapeWorker(entry) {
  if (!activeScrapeRun) {
    return;
  }

  const usernameText = normalizeScrapeText(entry && entry.username);
  const usernameKey = normalizeScrapeUsernameKey(usernameText);
  const inputIndex = Number.isFinite(Number(entry && entry.input_index)) ? Number(entry.input_index) : 0;
  if (!usernameKey) {
    return;
  }

  const profileUrl = buildScrapeProfileUrl(usernameText);
  const tab = await chrome.tabs.create({
    url: profileUrl,
    active: false,
  });

  const worker = {
    username: usernameText,
    usernameKey,
    inputIndex,
    tabId: tab.id,
    profileUrl,
    startedAt: Date.now(),
    timeoutId: null,
  };
  worker.timeoutId = createScrapeWorkerTimeout(worker);

  activeScrapeRun.workers.set(usernameKey, worker);
  await appendScrapeEvent(`Checking Snapchat profile for ${usernameText}.`);
  await syncScrapeRunnerLiveState();
}

async function maybeProcessScrapeQueue() {
  await hydrateScrapeRunFromStorage();
  if (!activeScrapeRun) {
    return;
  }

  const runnerState = await getScrapeRunnerState();
  if (runnerState.status !== SCRAPE_RUNNER_RUNNING) {
    return;
  }

  while (
    activeScrapeRun.queue.length
    && activeScrapeRun.workers.size < activeScrapeRun.config.maxParallelTabs
  ) {
    const entry = activeScrapeRun.queue.shift();
    await launchScrapeWorker(entry);
  }

  await syncScrapeRunnerLiveState();

  if (!activeScrapeRun.queue.length && activeScrapeRun.workers.size === 0) {
    // Make the final counters exact (derived from the saved results) before
    // declaring completion, so the summary never reads short of reality.
    await recomputeScrapeCounters();
    await appendScrapeEvent("Username scrape completed.");
    await finishScrapeRun(SCRAPE_RUNNER_IDLE);
  }
}

// Watchdog: settle workers whose tab was closed/crashed or that have been open
// past the hard deadline (their content script never reported and the in-memory
// timeout was lost to service-worker hibernation). Called from the 1-minute
// alarm and onRemoved so a single hung tab can never wedge the run at "finishing".
async function settleOverdueScrapeWorkers() {
  await hydrateScrapeRunFromStorage();
  if (!activeScrapeRun) {
    return;
  }

  const now = Date.now();
  const workers = Array.from(activeScrapeRun.workers.values());
  for (const worker of workers) {
    if (!worker) {
      continue;
    }
    let tabAlive = true;
    if (worker.tabId) {
      try {
        await chrome.tabs.get(worker.tabId);
      } catch (_error) {
        tabAlive = false;
      }
    } else {
      tabAlive = false;
    }

    const overdue = Number.isFinite(Number(worker.startedAt))
      && (now - Number(worker.startedAt)) > SCRAPE_WORKER_HARD_DEADLINE_MS;

    if (!tabAlive || overdue) {
      await settleScrapeWorker(worker.usernameKey, {
        username: worker.username,
        input_index: worker.inputIndex,
        profile_url: worker.profileUrl,
        has_bitmoji: false,
        status: "unknown",
        evidence: tabAlive ? "timeout" : "tab-closed",
      });
    }
  }
}

async function startScrapeRunFromInput(rawInputText, configPatch) {
  if (activeScrapeRun) {
    throw new Error("Username scrape is already running. Stop it before starting a new one.");
  }

  if (configPatch && Object.keys(configPatch).length) {
    await saveScrapeConfig(configPatch);
  }

  const config = await getScrapeConfig();
  if (!config.enabled) {
    throw new Error("Username scrape is disabled.");
  }

  const inputText = String(rawInputText || "");
  const entries = parseScrapeInputUsernames(inputText);
  if (!entries.length) {
    throw new Error("Paste at least one valid Snapchat username.");
  }

  await chrome.storage.local.set({
    [SCRAPE_STORAGE_KEYS.inputText]: inputText,
    [SCRAPE_STORAGE_KEYS.scrapeResults]: [],
    [SCRAPE_STORAGE_KEYS.eventLog]: [],
  });

  activeScrapeRun = {
    runId: `${Date.now()}`,
    queue: entries.slice(),
    workers: new Map(),
    config,
  };

  await setScrapeRunnerState({
    status: SCRAPE_RUNNER_RUNNING,
    run_id: activeScrapeRun.runId,
    total: entries.length,
    completed: 0,
    has_bitmoji: 0,
    no_bitmoji: 0,
    not_found: 0,
    unknown: 0,
    current_username: "",
    queue: entries.map((entry) => entry.username),
    queue_entries: serializeScrapeQueueEntries(entries),
    active_usernames: [],
    active_workers: [],
    active_count: 0,
    run_started_at: new Date().toISOString(),
  });
  await appendScrapeEvent(`Started username scrape for ${entries.length} username(s).`);
  await maybeProcessScrapeQueue();
  return getScrapeRunnerState();
}

async function pauseScrapeRun() {
  await hydrateScrapeRunFromStorage();
  const runnerState = await getScrapeRunnerState();
  if (!activeScrapeRun || runnerState.status !== SCRAPE_RUNNER_RUNNING) {
    return getScrapeRunnerState();
  }
  // In-flight tabs are left to finish; no new tabs open while paused.
  await setScrapeRunnerState({ status: SCRAPE_RUNNER_PAUSED });
  await appendScrapeEvent("Paused username scrape.");
  return getScrapeRunnerState();
}

async function resumeScrapeRun() {
  await hydrateScrapeRunFromStorage();
  const runnerState = await getScrapeRunnerState();
  if (runnerState.status !== SCRAPE_RUNNER_PAUSED) {
    return getScrapeRunnerState();
  }
  await setScrapeRunnerState({ status: SCRAPE_RUNNER_RUNNING });
  await appendScrapeEvent("Resumed username scrape.");
  await maybeProcessScrapeQueue();
  return getScrapeRunnerState();
}

async function stopScrapeRun() {
  const runnerState = await getScrapeRunnerState();

  await hydrateScrapeRunFromStorage();
  if (activeScrapeRun) {
    const workers = Array.from(activeScrapeRun.workers.values());
    workers.forEach((worker) => {
      if (worker && worker.timeoutId) {
        clearTimeout(worker.timeoutId);
      }
    });
    activeScrapeRun = null;
    await Promise.all(workers.map((worker) => closeScrapeWorkerTab(worker)));
  }

  await setScrapeRunnerState({
    status: SCRAPE_RUNNER_STOPPED,
    current_username: "",
    queue: [],
    queue_entries: [],
    active_usernames: [],
    active_workers: [],
    active_count: 0,
  });
  await appendScrapeEvent("Stopped username scrape.");
  return {
    ...(runnerState || {}),
    status: SCRAPE_RUNNER_STOPPED,
    current_username: "",
    queue: [],
    queue_entries: [],
    active_usernames: [],
    active_workers: [],
    active_count: 0,
  };
}

async function clearScrapeData() {
  await stopScrapeRun().catch(() => null);
  await chrome.storage.local.set({
    [SCRAPE_STORAGE_KEYS.inputText]: "",
    [SCRAPE_STORAGE_KEYS.scrapeResults]: [],
    [SCRAPE_STORAGE_KEYS.eventLog]: [],
    [SCRAPE_STORAGE_KEYS.runnerState]: getDefaultScrapeRunnerState(),
  });
  return { ok: true };
}

async function appendEventLog(message) {
  const localData = await chrome.storage.local.get(STORAGE_KEYS.eventLog);
  const current = localData[STORAGE_KEYS.eventLog] || [];
  const nextLog = [{
    message: String(message || "").trim(),
    at: new Date().toISOString(),
  }].concat(current).slice(0, 60);

  await chrome.storage.local.set({
    [STORAGE_KEYS.eventLog]: nextLog,
  });
}

function clearPopupStatusTimer() {
  if (popupStatusTimer) {
    clearTimeout(popupStatusTimer);
    popupStatusTimer = null;
  }
}

function invalidatePopupStatusCache() {
  popupStatusCache = {
    at: 0,
    status: null,
  };
}

function getLoadingRunnerStatus() {
  return {
    loading: true,
    counts: {},
    rows: [],
    bot: {
      state: "CHECKING",
      detail: "Checking Nyxify runner...",
      pid: null,
    },
    adspower_usage: {},
  };
}

function getCachedRunnerStatus(force) {
  const now = Date.now();
  if (!force && runnerStatusCache.status && (now - runnerStatusCache.at) < RUNNER_STATUS_CACHE_TTL_MS) {
    return runnerStatusCache.status;
  }

  requestRunnerStatusRefresh(Boolean(force));
  return runnerStatusCache.status || getLoadingRunnerStatus();
}

function requestRunnerStatusRefresh(force) {
  const now = Date.now();
  if (!force && runnerStatusCache.status && (now - runnerStatusCache.at) < RUNNER_STATUS_CACHE_TTL_MS) {
    return runnerStatusRequest || Promise.resolve(runnerStatusCache.status);
  }
  if (runnerStatusRequest) {
    return runnerStatusRequest;
  }

  runnerStatusRequest = callLocalNyxify("GET", "/status")
    .then((payload) => {
      const status = payload && payload.status ? payload.status : {};
      runnerStatusCache = {
        at: Date.now(),
        status,
      };
      return status;
    })
    .catch((error) => {
      const status = {
        unavailable: true,
        error: error.message,
        counts: {},
        rows: [],
        bot: {
          state: "UNAVAILABLE",
          detail: "Nyxify runner unavailable.",
          pid: null,
        },
        adspower_usage: {},
      };
      runnerStatusCache = {
        at: Date.now(),
        status,
      };
      return status;
    })
    .finally(() => {
      runnerStatusRequest = null;
      invalidatePopupStatusCache();
      if (popupPorts.size) {
        pushPopupStatus(false);
      }
    });

  return runnerStatusRequest;
}

function postStatusToPopupPorts(payload) {
  Array.from(popupPorts).forEach((port) => {
    try {
      port.postMessage(payload);
    } catch (error) {
      popupPorts.delete(port);
    }
  });
}

function buildPopupStatusSignature(status) {
  try {
    // Compact signature over only the fields the popup renders, avoiding
    // JSON.stringify-ing the full (huge) runner status on every poll.
    const s = status || {};
    const bot = s.bot || {};
    const counts = s.counts || {};
    const cfg = s.config || {};
    return [
      bot.state || "", bot.detail || "",
      counts.pending || 0, counts.running || 0,
      counts.failed || 0, counts.done || 0,
      counts.waiting || 0, counts.ready || 0,
      cfg.pending_threshold || "", cfg.max_parallel_profiles || "",
    ].join("||");
  } catch (error) {
    return `status-error:${String(error && error.message || error)}`;
  }
}

// Pull the per-install token from the bridge's unauthenticated /token endpoint
// so Nyxify connects on its own once the bridge is up — no need to open the
// Nyxify popup. Starting the bridge from the Nyx extension's "Connect" button is
// enough for both products to come online (one bridge serves :8865 and :8866).
async function maybeFetchBridgeToken() {
  try {
    const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
    const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
    if (config.localToken) {
      return; // already connected — the token is stable across bridge restarts
    }
    const apiUrl = config.localApiUrl || "http://127.0.0.1:8866";
    const response = await fetch(`${apiUrl}/token`);
    const data = await response.json();
    if (data && data.ok && data.token) {
      const next = normalizeConfig({ ...config, localToken: data.token, localApiUrl: apiUrl });
      await chrome.storage.sync.set({ [STORAGE_KEYS.config]: next });
    }
  } catch (error) {
    // Bridge not up yet — retry on the next alarm tick.
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  await maybeFetchBridgeToken().catch(() => null);
  await updateBadge();
});

chrome.runtime.onStartup.addListener(async () => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  await maybeFetchBridgeToken().catch(() => null);
  await hydrateScrapeRunFromStorage().catch(() => null);
  await maybeProcessScrapeQueue().catch(() => null);
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === FLUSH_ALARM) {
    await maybeFetchBridgeToken().catch(() => null);
    await processSnapboardRefreshRequest().catch(() => null);
    await flushPendingEntries();
    await hydrateScrapeRunFromStorage().catch(() => null);
    // Settle hung/closed worker tabs (hibernation-safe) before re-driving so a
    // single stuck tab can't wedge the run, then keep the queue moving.
    await settleOverdueScrapeWorkers().catch(() => null);
    await maybeProcessScrapeQueue().catch(() => null);
  }
});

// If the user (or a crash) closes a worker tab, settle it immediately instead of
// waiting for the timeout/alarm — otherwise the run could hang near "finishing".
chrome.tabs.onRemoved.addListener((tabId) => {
  hydrateScrapeRunFromStorage()
    .then(() => {
      if (!activeScrapeRun) {
        return null;
      }
      const entry = getScrapeWorkerEntryByTabId(tabId);
      if (!entry) {
        return null;
      }
      return settleScrapeWorker(entry.key, {
        username: entry.worker.username,
        input_index: entry.worker.inputIndex,
        profile_url: entry.worker.profileUrl,
        has_bitmoji: false,
        status: "unknown",
        evidence: "tab-closed",
      });
    })
    .catch(() => null);
});

chrome.runtime.onConnect.addListener((port) => {
  if (!port) {
    return;
  }

  if (port.name === POPUP_PORT_NAME) {
    popupPorts.add(port);
    port.onDisconnect.addListener(() => {
      popupPorts.delete(port);
      if (!popupPorts.size) {
        clearPopupStatusTimer();
      }
    });
    port.onMessage.addListener((message) => {
      if (message && message.type === "refresh") {
        invalidatePopupStatusCache();
        pushPopupStatus(true);
      }
    });
    invalidatePopupStatusCache();
    pushPopupStatus(true);
    return;
  }

  if (port.name !== "nyxify-snapboard-bridge") {
    return;
  }
  const tabId = port.sender && port.sender.tab ? port.sender.tab.id : null;
  if (tabId == null) {
    return;
  }
  snapboardPorts.set(tabId, port);
  ensureBridgeLoop();
  port.onDisconnect.addListener(() => {
    // A reload can connect the new content script before the old port's
    // disconnect event arrives. Never let that late event delete the live port.
    if (snapboardPorts.get(tabId) === port) {
      snapboardPorts.delete(tabId);
    }
  });
});

chrome.storage.onChanged.addListener((_changes, areaName) => {
  if (areaName !== "local" && areaName !== "sync") {
    return;
  }
  invalidatePopupStatusCache();
  if (popupPorts.size) {
    pushPopupStatus(true);
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "NYXIFY_SCRAPE_SNAPCHAT_RESULT") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    hydrateScrapeRunFromStorage()
      .catch(() => null)
      .then(() => {
        const workerEntry = getScrapeWorkerEntryByTabId(tabId);
    const payload = message.payload || {};
        const workerKey = workerEntry ? workerEntry.key : normalizeScrapeUsernameKey(payload && payload.username);

        return upsertScrapeResult({
          ...payload,
          input_index: workerEntry ? workerEntry.worker.inputIndex : payload && payload.input_index,
        })
          .then(async (result) => {
            const eventUsername = normalizeScrapeText(result && result.username);
            const eventStatus = normalizeScrapeText(result && result.status) || "unknown";
            if (activeScrapeRun && workerKey && activeScrapeRun.workers.has(workerKey)) {
              await appendScrapeEvent(`Checked Snapchat profile: ${eventUsername} (${eventStatus}).`);
              await settleScrapeWorker(workerKey, result || payload || {});
            }
            sendResponse({ ok: true, result });
          });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_DETECTED_ROWS") {
    chrome.storage.sync.get(STORAGE_KEYS.config)
      .then(async (syncData) => {
        const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
        if (!config.enabled) {
          sendResponse({ ok: true, count: 0, skipped: true });
          return;
        }

        const count = await mergeDetectedEntries(message.rows || [], sender && sender.tab ? sender.tab.url : "");
        await flushPendingEntries();
        await maybeRunFullAutoForRows(message.rows || [], config);
        if (count > 0) {
          await appendEventLog(`Detected ${count} new Nyxify row(s).`);
        }
        sendResponse({ ok: true, count });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SNAPBOARD_STATUS_ROWS") {
    callLocalNyxify("POST", "/replace_banned/snapshot", {
      rows: message.rows || [],
    })
      .then((payload) => sendResponse({ ok: true, payload }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCAN_BANNED_ROWS") {
    sendMessageToSnapboardTab({
      type: "NYXIFY_SCAN_BANNED_ROWS",
      count: message.count || 100000,
    })
      .then(async (scan) => {
        if (!scan || !scan.ok) {
          sendResponse({ ok: false, error: (scan && scan.error) || "Could not scan SnapBoard rows." });
          return;
        }
        const payload = await callLocalNyxify("POST", "/replace_banned/snapshot", {
          rows: scan.rows || [],
        });
        sendResponse({
          ok: true,
          rows: payload.rows || scan.banned || [],
          count: Number(payload.count || (scan.banned || []).length || 0),
          message: payload.message || `Found ${Number(payload.count || 0)} banned row(s).`,
        });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_REMOVE_BANNED_ROWS") {
    removeBannedRowsDirectly(Array.isArray(message.rows) ? message.rows : [])
      .then(async (payload) => {
        await appendEventLog(payload.message || "Remove banned finished.");
        invalidatePopupStatusCache();
        sendResponse({ ok: payload.ok !== false, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_WARMUP_BANNED_ROWS") {
    callLocalNyxify("POST", "/replace_banned/warmup", {
      rows: Array.isArray(message.rows) ? message.rows : undefined,
    })
      .then(async (payload) => {
        await appendEventLog(payload.message || "Warm Up banned finished.");
        invalidatePopupStatusCache();
        sendResponse({ ok: payload.ok !== false, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_GET_STATUS") {
    getStatusSnapshotCached(Boolean(message.force))
      .then((status) => sendResponse({ ok: true, status }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_AUTO_FILL_RESERVE_CLICK") {
    reserveAutoFillClick()
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_GET_STATUS") {
    hydrateScrapeRunFromStorage()
      .catch(() => null)
      .then(() => getScrapeStatus())
      .then((status) => sendResponse({ ok: true, status }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SAVE_CONFIG") {
    saveConfigAndRunner(message || {})
      .then((config) => sendResponse({ ok: true, config }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_SAVE_INPUT") {
    chrome.storage.local.set({
      [SCRAPE_STORAGE_KEYS.inputText]: String(message.inputText || ""),
    })
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_SAVE_CONFIG") {
    saveScrapeConfig(message.config || {})
      .then((config) => sendResponse({ ok: true, config }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_START") {
    startScrapeRunFromInput(
      message.inputText || "",
      message.config || {}
    )
      .then((runnerState) => sendResponse({ ok: true, runnerState }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_PAUSE") {
    pauseScrapeRun()
      .then((runnerState) => sendResponse({ ok: true, runnerState }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_RESUME") {
    resumeScrapeRun()
      .then((runnerState) => sendResponse({ ok: true, runnerState }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_STOP") {
    stopScrapeRun()
      .then((runnerState) => sendResponse({ ok: true, runnerState }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_CLEAR") {
    clearScrapeData()
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SCRAPE_OPEN_PAGE") {
    chrome.tabs.create({
      url: chrome.runtime.getURL("scrape.html"),
      active: true,
    })
      .then((tab) => sendResponse({ ok: true, tabId: tab && tab.id ? tab.id : null }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_SET_ENABLED") {
    chrome.storage.sync.get(STORAGE_KEYS.config).then((data) => {
      const nextConfig = normalizeConfig({
        ...(data[STORAGE_KEYS.config] || {}),
        enabled: message.enabled,
      });
      return chrome.storage.sync.set({
        [STORAGE_KEYS.config]: nextConfig,
      }).then(async () => {
        await updateBadge();
        sendResponse({ ok: true, enabled: nextConfig.enabled });
      });
    }).catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_BOT_ACTION") {
    callLocalNyxify("POST", `/bot/${message.action}`, message.payload || {})
      .then(async (payload) => {
        await appendEventLog(payload.message || `Ran Nyxify action: ${message.action}`);
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_REMOVE_QUEUE_ROW") {
    callLocalNyxify("POST", "/queue/remove", {
      row_key: message.rowKey,
    })
      .then(async (payload) => {
        await appendEventLog(`Removed Nyxify row ${String(message.rowKey || "").trim()}.`);
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYXIFY_BAN_PROXY") {
    banProxy(message.proxyValue)
      .then((config) => sendResponse({ ok: true, config }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
});

async function saveConfigAndRunner(patch) {
  const existingData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const normalizedPatch = { ...(patch || {}) };
  // A control flag (not a config field): true only when the caller edited the
  // banned-proxies field, so the runner is allowed to replace its stored list.
  const replaceBlocked = normalizedPatch.blockedProxiesReplace === true;
  delete normalizedPatch.blockedProxiesReplace;
  delete normalizedPatch.type;
  if (Object.prototype.hasOwnProperty.call(normalizedPatch, "bannedProxies") && !Object.prototype.hasOwnProperty.call(normalizedPatch, "blockedProxies")) {
    normalizedPatch.blockedProxies = normalizedPatch.bannedProxies;
  }
  if (Object.prototype.hasOwnProperty.call(normalizedPatch, "blockedProxies") && !Object.prototype.hasOwnProperty.call(normalizedPatch, "bannedProxies")) {
    normalizedPatch.bannedProxies = normalizedPatch.blockedProxies;
  }
  const existingConfig = normalizeConfig(existingData[STORAGE_KEYS.config] || {});
  const nextConfig = normalizeConfig({
    ...existingConfig,
    ...normalizedPatch,
  });
  const targetChanged = existingConfig.autoFillAccountTarget !== nextConfig.autoFillAccountTarget;
  const autoFillTurnedOn = !existingConfig.autoFillRow && nextConfig.autoFillRow;

  await chrome.storage.sync.set({
    [STORAGE_KEYS.config]: nextConfig,
  });

  if (targetChanged || autoFillTurnedOn) {
    await resetAutoFillProgress(nextConfig.autoFillAccountTarget);
  }

  await updateBadge();
  await syncConfigToRunner(nextConfig, replaceBlocked);
  return nextConfig;
}

async function getStatusSnapshot(forceRunnerRefresh = false) {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const localData = await chrome.storage.local.get([
    STORAGE_KEYS.pending,
    STORAGE_KEYS.lastSeen,
    STORAGE_KEYS.lastSync,
    STORAGE_KEYS.eventLog,
    STORAGE_KEYS.autoFillProgress,
  ]);

  const runnerStatus = getCachedRunnerStatus(Boolean(forceRunnerRefresh));
  let scrapeStatus = null;

  try {
    await hydrateScrapeRunFromStorage().catch(() => null);
    scrapeStatus = await getScrapeStatus();
  } catch (error) {
    scrapeStatus = {
      unavailable: true,
      error: error.message,
    };
  }

  let config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  try {
    const configPayload = await callLocalNyxify("GET", "/config");
    if (configPayload && configPayload.config) {
      config = extensionConfigFromRunnerConfig(configPayload.config, config);
      await chrome.storage.sync.set({ [STORAGE_KEYS.config]: config });
    }
  } catch (error) {
    // Offline popup use: keep the cached extension config until the bridge is reachable.
  }
  const autoFillProgress = normalizeAutoFillProgress(
    localData[STORAGE_KEYS.autoFillProgress],
    config.autoFillAccountTarget,
  );

  return {
    config,
    autoFillProgress,
    pendingEntries: localData[STORAGE_KEYS.pending] || [],
    lastSeenEntries: localData[STORAGE_KEYS.lastSeen] || [],
    lastSync: localData[STORAGE_KEYS.lastSync] || null,
    eventLog: localData[STORAGE_KEYS.eventLog] || [],
    runnerStatus,
    scrapeStatus,
  };
}

async function getStatusSnapshotCached(force) {
  const now = Date.now();
  if (!force && popupStatusCache.status && (now - popupStatusCache.at) < STATUS_CACHE_TTL_MS) {
    return popupStatusCache.status;
  }

  if (!force && popupStatusRequest) {
    return popupStatusRequest;
  }

  popupStatusRequest = getStatusSnapshot(Boolean(force))
    .then((status) => {
      popupStatusCache = {
        at: Date.now(),
        status,
      };
      popupStatusRequest = null;
      return status;
    })
    .catch((error) => {
      popupStatusRequest = null;
      throw error;
    });

  return popupStatusRequest;
}

async function pushPopupStatus(force) {
  if (!popupPorts.size) {
    clearPopupStatusTimer();
    return;
  }

  try {
    const status = await getStatusSnapshotCached(Boolean(force));
    const nextSignature = buildPopupStatusSignature(status);
    if (force || nextSignature !== popupStatusSignature) {
      popupStatusSignature = nextSignature;
      postStatusToPopupPorts({ type: "status", status });
    }
  } catch (error) {
    const message = String(error && error.message || error || "Could not load Nyxify status.");
    const nextSignature = `error:${message}`;
    if (force || nextSignature !== popupStatusSignature) {
      popupStatusSignature = nextSignature;
      postStatusToPopupPorts({ type: "status-error", error: message });
    }
  } finally {
    clearPopupStatusTimer();
    if (popupPorts.size) {
      popupStatusTimer = setTimeout(() => {
        pushPopupStatus(false);
      }, POPUP_LIVE_POLL_MS);
    }
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getAvailableSnapboardTabId() {
  const entries = Array.from(snapboardPorts.entries());
  if (!entries.length) {
    return null;
  }
  return entries[0][0];
}

async function findSnapboardTabId() {
  const connectedTabId = getAvailableSnapboardTabId();
  if (connectedTabId != null) {
    return connectedTabId;
  }
  try {
    const tabs = await chrome.tabs.query({ url: "https://snapboard.onrender.com/*" });
    const tab = (tabs || []).find((candidate) => candidate && candidate.id != null);
    return tab ? tab.id : null;
  } catch (_error) {
    return null;
  }
}

function sendMessageToSnapboardTab(message) {
  return new Promise((resolve) => {
    const tabId = getAvailableSnapboardTabId();
    if (tabId == null) {
      resolve({ ok: false, error: "No SnapBoard tab bridge connected." });
      return;
    }
    chrome.tabs.sendMessage(tabId, message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message || "SnapBoard messaging failed." });
        return;
      }
      resolve(response || { ok: false, error: "Empty SnapBoard response." });
    });
  });
}

function normalizeBannedRowForRemoval(row) {
  const safe = row || {};
  const rowKey = String(safe.row_key || "").trim().toLowerCase();
  return {
    ...safe,
    row_key: rowKey,
    adspower_id: String(safe.adspower_id || safe.adspower_profile_id || safe.profile_id || "").trim(),
  };
}

async function removeBannedRowsDirectly(rows) {
  const candidates = (Array.isArray(rows) ? rows : [])
    .map((row) => normalizeBannedRowForRemoval(row))
    .filter((row) => row.row_key);
  if (!candidates.length) {
    return {
      ok: false,
      count: 0,
      removed: 0,
      failed: 0,
      results: [],
      message: "Scan banned rows first.",
    };
  }

  const results = candidates.map((row) => ({
    ok: true,
    row_key: row.row_key,
    adspower_id: row.adspower_id,
    proxy: "",
    errors: [],
  }));

  for (const result of results) {
    const response = await sendMessageToSnapboardTab({
      type: "NYXIFY_SNAPBOARD_ACTION",
      action: "adspower_update",
      row_key: result.row_key,
      adspower_id: "",
    });
    if (!response || !response.ok) {
      result.ok = false;
      result.errors.push((response && response.error) || "SnapBoard AdsPower id update failed.");
    }
  }

  for (const result of results) {
    const response = await sendMessageToSnapboardTab({
      type: "NYXIFY_SNAPBOARD_ACTION",
      action: "proxy_rotate",
      row_key: result.row_key,
      max_clicks: 3,
      force: true,
    });
    if (response && response.ok && response.proxy) {
      result.proxy = response.proxy;
      try {
        await callLocalNyxify("POST", "/replace_banned/remove_result", {
          row_key: result.row_key,
          proxy: result.proxy,
        });
      } catch (error) {
        result.ok = false;
        result.errors.push(error.message || "Local remove-banned result update failed.");
      }
    } else {
      result.ok = false;
      result.errors.push((response && response.error) || "SnapBoard proxy rotation failed.");
    }
  }

  const failed = results.filter((result) => !result.ok).length;
  const removed = results.length - failed;
  return {
    ok: failed === 0,
    count: results.length,
    removed,
    failed,
    results,
    message: `Remove banned finished: ${removed} removed, ${failed} failed.`,
  };
}

// --- SnapBoard staleness recovery ------------------------------------------
// A SnapBoard tab that drifts out of sync stops handing out emails/numbers
// ("no pending order" even when an order exists) and drops row updates. A plain
// page refresh re-syncs it. We reload reactively when a fetch comes back empty,
// rate-limited so a burst of failures can't reload-loop and interrupt healthy
// concurrent work.
let lastSnapboardReloadAt = 0;
const SNAPBOARD_RELOAD_COOLDOWN_MS = 20000;
const SNAPBOARD_RECONNECT_TIMEOUT_MS = 15000;

// Ask the content bridge to re-login a logged-out SnapBoard. A logged-out board
// silently drops its rows and stops handing out emails/numbers/OTPs, so we drive
// its Sign In button (credentials are typed from the extension options) before
// falling back to a heavier full reload. Returns { loggedIn, wasLoggedOut } so
// callers can distinguish a recovered session from a board that was fine.
async function ensureSnapboardLoggedIn(timeoutMs) {
  try {
    const resp = await sendMessageToSnapboardTab({
      type: "NYXIFY_SNAPBOARD_ACTION",
      action: "ensure_logged_in",
      timeout_ms: timeoutMs || 15000,
    });
    return {
      loggedIn: !!(resp && resp.ok && resp.logged_in),
      wasLoggedOut: !!(resp && resp.was_logged_out),
    };
  } catch (error) {
    return { loggedIn: false, wasLoggedOut: false };
  }
}

async function refreshSnapboardTab(options) {
  const force = !!(options && options.force);
  const now = Date.now();
  if (!force && now - lastSnapboardReloadAt < SNAPBOARD_RELOAD_COOLDOWN_MS) {
    return false;
  }
  const tabId = await findSnapboardTabId();
  if (tabId == null) {
    return false;  // no tab to reload
  }
  lastSnapboardReloadAt = now;
  const oldPort = snapboardPorts.get(tabId) || null;
  try {
    await appendEventLog("Refreshing SnapBoard to recover from a stale/no-update response.");
    await chrome.tabs.reload(tabId);
  } catch (error) {
    return false;
  }
  // Wait for the reloaded page's content bridge to reconnect — a brand-new port
  // object replaces the old one (see onConnect for "nyxify-snapboard-bridge").
  const deadline = Date.now() + SNAPBOARD_RECONNECT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const current = snapboardPorts.get(tabId);
    if (current && current !== oldPort) {
      await delay(1500);  // let SnapBoard render its rows before we retry
      // A fresh load can come up logged out (expired session) — recover the
      // session before the caller retries, or the rows won't be there yet.
      const recovered = await ensureSnapboardLoggedIn();
      if (recovered.wasLoggedOut) {
        await delay(1500);  // give the re-login'd board a moment to render rows
      }
      return true;
    }
    await delay(300);
  }
  return false;
}

// Send a fetch to SnapBoard and, if it comes back empty/failed, recover the
// board and retry — "refresh / re-login the SnapBoard first before retrying",
// since a stale OR logged-out board is a common cause of a missing
// email/number/OTP. Try the cheap in-place re-login first, then the reload.
async function snapboardFetchWithRefresh(message) {
  const response = await sendMessageToSnapboardTab(message);
  if (response && response.ok) {
    return response;
  }
  // Cheap path: a logged-out board answers empty for everything — sign back in
  // and retry without a full reload.
  const recovered = await ensureSnapboardLoggedIn();
  if (recovered.loggedIn) {
    const afterLogin = await sendMessageToSnapboardTab(message);
    if (afterLogin && afterLogin.ok) {
      return afterLogin;
    }
  }
  const refreshed = await refreshSnapboardTab();
  if (refreshed) {
    const retry = await sendMessageToSnapboardTab(message);
    if (retry) {
      return retry;
    }
  }
  return response;
}

// Lighter recovery for OTP/SMS: an empty code usually just means "not landed
// yet" (normal), so we must NOT reload the whole board — that would disrupt
// every other account's in-flight fetch. Only retry when the board was actually
// signed out, which is the real "OTP unresponsive because logged out" case.
async function snapboardFetchWithRelogin(message) {
  const response = await sendMessageToSnapboardTab(message);
  if (response && response.ok) {
    return response;
  }
  const recovered = await ensureSnapboardLoggedIn();
  if (recovered.wasLoggedOut && recovered.loggedIn) {
    const retry = await sendMessageToSnapboardTab(message);
    if (retry) {
      return retry;
    }
  }
  return response;
}

async function reserveAutoFillClickInternal() {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  const target = normalizePositiveInteger(config.autoFillAccountTarget, 0);
  const localData = await chrome.storage.local.get(STORAGE_KEYS.autoFillProgress);
  let progress = normalizeAutoFillProgress(localData[STORAGE_KEYS.autoFillProgress], target);

  if (!config.autoFillRow) {
    return {
      ok: true,
      shouldClick: false,
      targetReached: false,
      progress,
      reason: "auto_fill_disabled",
    };
  }

  if (!target) {
    return {
      ok: true,
      shouldClick: true,
      targetReached: false,
      unlimited: true,
      progress,
    };
  }

  if (progress.count >= target) {
    await chrome.storage.sync.set({
      [STORAGE_KEYS.config]: normalizeConfig({
        ...config,
        autoFillRow: false,
      }),
    });
    await chrome.storage.local.set({
      [STORAGE_KEYS.autoFillProgress]: progress,
    });
    await appendEventLog(`Auto-Fill Row target ${target} already reached; Auto-Fill Row disabled.`);
    await updateBadge();
    return {
      ok: true,
      shouldClick: false,
      targetReached: true,
      disabled: true,
      progress,
    };
  }

  progress = {
    target,
    count: progress.count + 1,
  };
  const targetReached = progress.count >= target;
  const nextConfig = targetReached
    ? normalizeConfig({ ...config, autoFillRow: false })
    : config;

  const updates = {
    [STORAGE_KEYS.autoFillProgress]: progress,
  };
  if (targetReached) {
    await chrome.storage.sync.set({
      [STORAGE_KEYS.config]: nextConfig,
    });
    await appendEventLog(`Auto-Fill Row target reached (${progress.count}/${target}); Auto-Fill Row disabled.`);
    await updateBadge();
  }
  await chrome.storage.local.set(updates);

  return {
    ok: true,
    shouldClick: true,
    targetReached,
    disabled: targetReached,
    progress,
  };
}

function reserveAutoFillClick() {
  const run = autoFillReserveInFlight
    .catch(() => null)
    .then(() => reserveAutoFillClickInternal());
  autoFillReserveInFlight = run;
  return run;
}

async function collectPendingBridgeRequests(path, maxRequests) {
  const requests = [];
  const seenRowKeys = new Set();
  const limit = Math.max(1, Number(maxRequests) || 1);

  for (let index = 0; index < limit; index += 1) {
    const payload = await callLocalNyxify("GET", path);
    const request = payload && payload.request ? payload.request : null;
    const rowKey = String(request && request.row_key || "").trim();
    if (!rowKey || seenRowKeys.has(rowKey)) {
      break;
    }
    seenRowKeys.add(rowKey);
    requests.push(request);
  }

  return requests;
}

async function processSnapboardRefreshRequest() {
  const payload = await callLocalNyxify("GET", "/snapboard_refresh/pending");
  const request = payload && payload.request ? payload.request : null;
  if (!request || !request.request_id) {
    return false;
  }

  const refreshed = await refreshSnapboardTab({ force: true });
  await callLocalNyxify("POST", "/snapboard_refresh/result", {
    request_id: request.request_id,
    success: refreshed,
    error: refreshed ? "" : "No SnapBoard tab could be refreshed.",
  });
  return refreshed;
}

async function processBridgeActionsOnce() {
  try {
    await processSnapboardRefreshRequest();
  } catch (error) {
    await appendEventLog(`Nyxify SnapBoard refresh bridge error: ${error.message}`);
  }

  if (!snapboardPorts.size) {
    return;
  }

  // SMS / OTP first: a pending signup verification code must NOT wait behind a
  // long email/metadata batch. Each stage is wrapped in its own try/catch so one
  // failed request is logged and the bridge loop keeps going.
  try {
    const otpPayload = await callLocalNyxify("GET", "/otp/pending");
    const otpRequest = otpPayload && otpPayload.request ? otpPayload.request : null;
    if (otpRequest && otpRequest.row_key) {
      const otpResponse = await snapboardFetchWithRelogin({
        type: "NYXIFY_SNAPBOARD_ACTION",
        action: "otp",
        row_key: otpRequest.row_key,
        email: otpRequest.email || "",
      });
      if (otpResponse.ok && otpResponse.code) {
        await callLocalNyxify("POST", "/otp/result", {
          row_key: otpRequest.row_key,
          code: otpResponse.code,
        });
      } else if (otpResponse && !otpResponse.ok) {
        await callLocalNyxify("POST", "/otp/result", {
          row_key: otpRequest.row_key,
          code: "",
          error: otpResponse.error || "SnapBoard OTP fetch failed.",
        });
      }
    }
  } catch (error) {
    await appendEventLog(`Nyxify OTP bridge error: ${error.message}`);
  }

  try {
    const smsPayload = await callLocalNyxify("GET", "/sms/pending");
    const smsRequest = smsPayload && smsPayload.request ? smsPayload.request : null;
    if (smsRequest && smsRequest.row_key) {
      const smsResponse = await snapboardFetchWithRelogin({
        type: "NYXIFY_SNAPBOARD_ACTION",
        action: "sms",
        row_key: smsRequest.row_key,
        phone: smsRequest.phone || "",
      });
      await callLocalNyxify("POST", "/sms/result", {
        row_key: smsRequest.row_key,
        code: smsResponse.ok ? (smsResponse.code || "") : "",
        error: smsResponse.ok ? "" : (smsResponse.error || "SnapBoard SMS fetch failed."),
      });
    }
  } catch (error) {
    await appendEventLog(`Nyxify SMS bridge error: ${error.message}`);
  }

  try {
    const emailRequests = await collectPendingBridgeRequests("/email/pending", MAX_EMAIL_BRIDGE_BATCH);
    if (emailRequests.length) {
      await Promise.all(emailRequests.map(async (emailRequest) => {
        try {
          const emailResponse = await snapboardFetchWithRefresh({
            type: "NYXIFY_SNAPBOARD_ACTION",
            action: "email_fetch",
            row_key: emailRequest.row_key,
            force_new: !!emailRequest.force_new,
          });
          await callLocalNyxify("POST", "/email/result", {
            row_key: emailRequest.row_key,
            email: emailResponse.ok ? (emailResponse.email || "") : "",
            error: emailResponse.ok ? "" : (emailResponse.error || "SnapBoard email fetch failed."),
          });
        } catch (error) {
          await appendEventLog(`Nyxify email bridge error for ${emailRequest.row_key}: ${error.message}`);
        }
      }));
    }
  } catch (error) {
    await appendEventLog(`Nyxify email bridge error: ${error.message}`);
  }

  try {
    const phoneRequests = await collectPendingBridgeRequests("/phone/pending", MAX_EMAIL_BRIDGE_BATCH);
    if (phoneRequests.length) {
      await Promise.all(phoneRequests.map(async (phoneRequest) => {
        try {
          const phoneResponse = await snapboardFetchWithRefresh({
            type: "NYXIFY_SNAPBOARD_ACTION",
            action: "phone_fetch",
            row_key: phoneRequest.row_key,
            force_new: !!phoneRequest.force_new,
          });
          await callLocalNyxify("POST", "/phone/result", {
            row_key: phoneRequest.row_key,
            phone: phoneResponse.ok ? (phoneResponse.phone || "") : "",
            error: phoneResponse.ok ? "" : (phoneResponse.error || "SnapBoard phone fetch failed."),
          });
        } catch (error) {
          await appendEventLog(`Nyxify phone bridge error for ${phoneRequest.row_key}: ${error.message}`);
        }
      }));
    }
  } catch (error) {
    await appendEventLog(`Nyxify phone bridge error: ${error.message}`);
  }

  try {
    const usernamePayload = await callLocalNyxify("GET", "/username_update/pending");
    const usernameRequest = usernamePayload && usernamePayload.request ? usernamePayload.request : null;
    if (usernameRequest && usernameRequest.row_key) {
      const usernameResponse = await sendMessageToSnapboardTab({
        type: "NYXIFY_SNAPBOARD_ACTION",
        action: "username_update",
        row_key: usernameRequest.row_key,
        username: usernameRequest.username,
      });
      await callLocalNyxify("POST", "/username_update/result", {
        row_key: usernameRequest.row_key,
        success: !!usernameResponse.ok,
        error: usernameResponse.ok ? "" : (usernameResponse.error || "SnapBoard username update failed."),
      });
    }
  } catch (error) {
    await appendEventLog(`Nyxify username bridge error: ${error.message}`);
  }

  try {
    const proxyPayload = await callLocalNyxify("GET", "/proxy/rotate_pending");
    if (proxyPayload && proxyPayload.row_key) {
      const proxyResponse = await sendMessageToSnapboardTab({
        type: "NYXIFY_SNAPBOARD_ACTION",
        action: "proxy_rotate",
        row_key: proxyPayload.row_key,
        max_clicks: proxyPayload.max_clicks,
        force: !!proxyPayload.force,
      });
      await callLocalNyxify("POST", "/proxy/rotate_result", {
        row_key: proxyPayload.row_key,
        proxy: proxyResponse.ok ? (proxyResponse.proxy || "") : "",
        error: proxyResponse.ok ? "" : (proxyResponse.error || "SnapBoard proxy rotation failed."),
      });
    }
  } catch (error) {
    await appendEventLog(`Nyxify proxy bridge error: ${error.message}`);
  }

  try {
    const adspowerPayload = await callLocalNyxify("GET", "/adspower_update/pending");
    const adspowerRequest = adspowerPayload && adspowerPayload.request ? adspowerPayload.request : null;
    if (adspowerRequest && adspowerRequest.row_key) {
      const adspowerResponse = await sendMessageToSnapboardTab({
        type: "NYXIFY_SNAPBOARD_ACTION",
        action: "adspower_update",
        row_key: adspowerRequest.row_key,
        adspower_id: adspowerRequest.adspower_id,
      });
      await callLocalNyxify("POST", "/adspower_update/result", {
        row_key: adspowerRequest.row_key,
        success: !!adspowerResponse.ok,
        error: adspowerResponse.ok ? "" : (adspowerResponse.error || "SnapBoard AdsPower id update failed."),
      });
    }
  } catch (error) {
    await appendEventLog(`Nyxify AdsPower bridge error: ${error.message}`);
  }

  try {
    const adspowerNamePayload = await callLocalNyxify("GET", "/adspower_name_update/pending");
    const adspowerNameRequest = adspowerNamePayload && adspowerNamePayload.request ? adspowerNamePayload.request : null;
    if (adspowerNameRequest && adspowerNameRequest.row_key) {
      const adspowerNameResponse = await sendMessageToSnapboardTab({
        type: "NYXIFY_SNAPBOARD_ACTION",
        action: "adspower_name_update",
        row_key: adspowerNameRequest.row_key,
        adspower_name: adspowerNameRequest.adspower_name,
      });
      await callLocalNyxify("POST", "/adspower_name_update/result", {
        row_key: adspowerNameRequest.row_key,
        success: !!adspowerNameResponse.ok,
        error: adspowerNameResponse.ok ? "" : (adspowerNameResponse.error || "SnapBoard AdsPower name update failed."),
      });
    }
  } catch (error) {
    await appendEventLog(`Nyxify AdsPower name bridge error: ${error.message}`);
  }
}

function ensureBridgeLoop() {
  if (bridgeLoopPromise) {
    return bridgeLoopPromise;
  }
  bridgeLoopPromise = (async () => {
    try {
      while (snapboardPorts.size) {
        await processBridgeActionsOnce();
        await delay(700);
      }
    } finally {
      bridgeLoopPromise = null;
    }
  })();
  return bridgeLoopPromise;
}

async function requestSnapboardUsernameUpdate(rowKey, username) {
  if (!rowKey || !username) {
    return false;
  }
  try {
    await callLocalNyxify("POST", "/username_update/request", {
      row_key: rowKey,
      username,
    });
    return true;
  } catch (_error) {
    return false;
  }
}

async function waitForSnapboardUsernameUpdate(rowKey, timeoutMs) {
  const normalizedRowKey = String(rowKey || "").trim();
  const maxWaitMs = Math.max(1000, Number(timeoutMs) || 30000);
  const startedAt = Date.now();
  let lastError = "";

  while ((Date.now() - startedAt) < maxWaitMs) {
    try {
      const payload = await callLocalNyxify(
        "GET",
        `/username_update/status?row_key=${encodeURIComponent(normalizedRowKey)}`
      );
      if (payload && payload.done) {
        if (payload.success) {
          return { ok: true };
        }
        return {
          ok: false,
          error: String(payload.error || "SnapBoard username update failed.").trim(),
        };
      }
      lastError = String(payload && payload.error || "").trim();
    } catch (error) {
      lastError = String(error && error.message || error || "").trim();
    }
    await delay(500);
  }

  return {
    ok: false,
    error: lastError || "Timed out waiting for SnapBoard username update.",
  };
}

async function reserveFullAutoUsername(row, reason) {
  return callLocalNyxify("POST", "/full_auto/reserve", {
    row_key: row && row.row_key,
    model: row && row.model,
    current_username: row && row.username,
    reason: reason || "",
  });
}

async function commitFullAutoUsername(reservation, success, errorMessage) {
  return callLocalNyxify("POST", "/full_auto/commit", {
    row_key: reservation && reservation.row_key,
    reservation_id: reservation && reservation.reservation_id,
    username: reservation && reservation.username,
    model: reservation && reservation.model,
    success: !!success,
    error: success ? "" : String(errorMessage || "").trim(),
  });
}

async function replaceTempUsernameViaFullAuto(row, reason) {
  const rowKey = String(row && row.row_key || "").trim();
  const currentUsername = normalizeUsername(row && row.username);
  const model = String(row && row.model || "").trim();

  if (!rowKey || !model || !isTempUsername(currentUsername)) {
    return { ok: false, skipped: true };
  }
  if (fullAutoRowsInFlight.has(rowKey)) {
    return { ok: false, skipped: true };
  }
  if (!snapboardPorts.size) {
    return { ok: false, error: "No SnapBoard bridge connected." };
  }

  fullAutoRowsInFlight.add(rowKey);
  let reservation = null;
  try {
    reservation = await reserveFullAutoUsername(row, reason || "snapboard_temp_username_detected");
    const nextUsername = normalizeUsername(reservation && reservation.username);
    if (!nextUsername) {
      return {
        ok: false,
        error: String(
          reservation && reservation.message
          || `No Full Auto username available for model ${model}.`
        ).trim(),
      };
    }

    const requested = await requestSnapboardUsernameUpdate(rowKey, nextUsername);
    if (!requested) {
      await commitFullAutoUsername(reservation, false, "Could not dispatch SnapBoard username update.");
      return {
        ok: false,
        error: "Could not dispatch SnapBoard username update.",
      };
    }

    const updateResult = await waitForSnapboardUsernameUpdate(rowKey, 30000);
    if (!updateResult.ok) {
      await commitFullAutoUsername(
        reservation,
        false,
        updateResult.error || "SnapBoard username update failed."
      );
      return updateResult;
    }

    await commitFullAutoUsername(reservation, true, "");
    await appendEventLog(`Full Auto Mode set ${model} row ${rowKey} to ${nextUsername}.`);
    return { ok: true, username: nextUsername };
  } catch (error) {
    if (reservation) {
      try {
        await commitFullAutoUsername(reservation, false, error.message || String(error));
      } catch (_commitError) {
      }
    }
    return { ok: false, error: error.message || String(error) };
  } finally {
    fullAutoRowsInFlight.delete(rowKey);
  }
}

async function maybeRunFullAutoForRows(rows, config) {
  if (!config || !config.fullAutoModeEnabled) {
    return;
  }

  const sanitizedRows = sanitizeEntries(rows || []);
  for (const row of sanitizedRows) {
    if (!isTempUsername(row.username)) {
      continue;
    }
    const result = await replaceTempUsernameViaFullAuto(row, "snapboard_temp_username_detected");
    if (!result.ok && result.error) {
      await appendEventLog(`Full Auto Mode could not replace ${row.model} row ${row.row_key}: ${result.error}`);
    }
  }
}

async function mergeDetectedEntries(rows, sourceUrl) {
  const sanitizedRows = sanitizeEntries(rows);
  const localData = await chrome.storage.local.get([STORAGE_KEYS.pending, STORAGE_KEYS.lastSeen]);
  const currentPending = localData[STORAGE_KEYS.pending] || [];
  const lastSeenEntries = localData[STORAGE_KEYS.lastSeen] || [];
  const currentDetectedRowKeys = new Set(sanitizedRows.map((entry) => entry.row_key));
  const mergedMap = new Map(
    currentPending
      .filter((entry) => currentDetectedRowKeys.has(entry.row_key))
      .map((entry) => [entry.row_key, entry])
  );
  const lastSeenMap = new Map(lastSeenEntries.map((entry) => [entry.row_key, entry]));
  let runnerQueueMap = null;
  let addedCount = 0;

  try {
    const payload = await callLocalNyxify("GET", "/queue");
    const queueRows = Array.isArray(payload && payload.rows) ? payload.rows : [];
    runnerQueueMap = new Map(queueRows.map((entry) => [String(entry.row_key || "").trim(), entry]));
  } catch (error) {
    runnerQueueMap = null;
  }

  for (const row of sanitizedRows) {
    const pendingRow = mergedMap.get(row.row_key);
    const previousRow = lastSeenMap.get(row.row_key);
    const runnerRow = runnerQueueMap ? runnerQueueMap.get(row.row_key) : null;
    const sameAsPrevious = previousRow
      && previousRow.model === row.model
      && previousRow.ip_address === row.ip_address
      && previousRow.proxy_address === row.proxy_address
      && String(previousRow.username || "").trim() === row.username
      && String(previousRow.email || "").trim() === row.email
      && String(previousRow.password || "").trim() === row.password;
    const sameAsPending = pendingRow
      && pendingRow.model === row.model
      && pendingRow.ip_address === row.ip_address
      && pendingRow.proxy_address === row.proxy_address
      && String(pendingRow.username || "").trim() === row.username
      && String(pendingRow.email || "").trim() === row.email
      && String(pendingRow.password || "").trim() === row.password;
    const sameAsRunner = runnerRow
      && String(runnerRow.model || "").trim() === row.model
      && String(runnerRow.ip_address || "").trim() === row.ip_address
      && String(runnerRow.proxy_address || "").trim() === row.proxy_address
      && String(runnerRow.username || "").trim() === row.username
      && String(runnerRow.email || "").trim() === row.email
      && String(runnerRow.password || "").trim() === row.password;

    if (sameAsPending || (sameAsPrevious && sameAsRunner)) {
      continue;
    }

    mergedMap.set(row.row_key, {
      ...(pendingRow || {}),
      ...row,
      source: "nyxify-extension",
      source_url: sourceUrl || "",
      queued_at: new Date().toISOString(),
    });
    addedCount += 1;
  }

  await chrome.storage.local.set({
    [STORAGE_KEYS.pending]: Array.from(mergedMap.values()),
    [STORAGE_KEYS.lastSeen]: sanitizedRows,
  });
  await updateBadge();
  return addedCount;
}

function sanitizeEntries(rows) {
  const unique = new Map();
  for (const row of rows || []) {
    const rowKey = String(row.row_key || "").trim().toLowerCase();
    const model = String(row.model || "").trim();
    const ipAddress = String(row.ip_address || "").trim();
    const proxyAddress = String(row.proxy_address || "").trim() || ipAddress;
    const username = String(row.username || "").trim();
    const email = String(row.email || "").trim();
    const password = String(row.password || "").trim();
    const adspowerId = String(row.adspower_id || "").trim();
    if (!rowKey || !model || !ipAddress || adspowerId) {
      continue;
    }
    unique.set(rowKey, {
      row_key: rowKey,
      model,
      ip_address: ipAddress,
      proxy_address: proxyAddress,
      username,
      email,
      password,
      adspower_id: adspowerId,
      status: "PENDING",
    });
  }
  return Array.from(unique.values());
}

async function flushPendingEntries() {
  if (flushInFlight) {
    return flushInFlight;
  }
  flushInFlight = flushPendingEntriesInternal();
  try {
    return await flushInFlight;
  } finally {
    flushInFlight = null;
  }
}

async function flushPendingEntriesInternal() {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  const localData = await chrome.storage.local.get(STORAGE_KEYS.pending);
  const pendingEntries = localData[STORAGE_KEYS.pending] || [];

  if (!config.enabled || !config.localApiUrl || pendingEntries.length === 0) {
    await updateBadge();
    return;
  }

  // Do not drop blocked proxies here. Nyxify's runner now needs to receive the
  // row so it can rotate the proxy on SnapBoard until a usable proxy is found.
  const filteredEntries = pendingEntries;

  try {
    const payload = await callLocalNyxify("POST", "/queue/upsert", {
      entries: filteredEntries.map((entry) => ({
        row_key: entry.row_key,
        model: entry.model,
        ip_address: entry.ip_address,
        proxy_address: entry.proxy_address,
        username: entry.username,
        email: entry.email,
        password: entry.password,
        adspower_id: entry.adspower_id || "",
      })),
    });

    await chrome.storage.local.set({
      [STORAGE_KEYS.pending]: [],
      [STORAGE_KEYS.lastSync]: {
        syncedAt: new Date().toISOString(),
        count: Number(payload.count || 0),
        message: payload.message || "Nyxify rows synced.",
      },
    });
  } catch (error) {
    await chrome.storage.local.set({
      [STORAGE_KEYS.lastSync]: {
        syncedAt: new Date().toISOString(),
        count: 0,
        message: error.message,
        failed: true,
      },
    });
    await appendEventLog(`Nyxify sync failed: ${error.message}`);
  }

  await updateBadge();
}

async function banProxy(proxyValue) {
  const normalizedProxy = String(proxyValue || "").trim();
  if (!normalizedProxy) {
    throw new Error("Proxy value is required.");
  }

  const data = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const config = normalizeConfig(data[STORAGE_KEYS.config] || {});
  const nextBanned = Array.from(new Set(config.bannedProxies.concat([normalizedProxy])));
  const nextConfig = normalizeConfig({
    ...config,
    bannedProxies: nextBanned,
    blockedProxies: nextBanned,
  });

  await chrome.storage.sync.set({
    [STORAGE_KEYS.config]: nextConfig,
  });
  try {
    // Append via the additive ban endpoint (not a full-config replace) so this
    // never drops bans added from the Proxy Ranking table or another surface.
    await callLocalNyxify("POST", "/proxy_ranking/ban", { value: normalizedProxy });
  } catch (error) {
    await appendEventLog(`Proxy was banned locally. Runner sync pending: ${error.message}`);
  }
  await appendEventLog(`Banned proxy: ${normalizedProxy}`);
  return nextConfig;
}

async function syncConfigToRunner(nextConfig, replaceBlocked = false) {
  try {
    await callLocalNyxify("POST", "/config", runnerConfigPayloadFromExtensionConfig(nextConfig, replaceBlocked));
    await flushPendingEntries();
  } catch (error) {
    await appendEventLog(`Saved Nyxify extension settings locally. Runner sync pending: ${error.message}`);
  } finally {
    requestRunnerStatusRefresh(true).catch(() => null);
  }
}

async function callLocalNyxify(method, path, payload) {
  const config = await getLocalConfig();

  if (!config.localApiUrl) {
    throw new Error("Nyxify local API missing.");
  }

  const headers = {
    "Content-Type": "application/json",
  };

  const bodyPayload = { ...(payload || {}) };

  if (config.localToken) {
    headers["X-Nyxify-Token"] = config.localToken;
    bodyPayload.token = config.localToken;
  }

  const response = await fetchWithTimeout(`${config.localApiUrl}${path}`, {
    method,
    headers,
    body: method === "GET" ? undefined : JSON.stringify(bodyPayload),
  });

  const result = await response.json();
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || `Request failed with status ${response.status}`);
  }
  return result;
}

async function updateBadge() {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  const localData = await chrome.storage.local.get(STORAGE_KEYS.pending);
  const pendingCount = (localData[STORAGE_KEYS.pending] || []).length;
  await chrome.action.setBadgeBackgroundColor({ color: "#0a1220" });
  await chrome.action.setBadgeText({ text: config.enabled ? (pendingCount ? String(Math.min(pendingCount, 99)) : "") : "OFF" });
}
