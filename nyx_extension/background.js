const FLUSH_ALARM = "nyx-flush-queue";
const STORAGE_KEYS = {
  config: "nyxConfig",
  pending: "nyxPendingEntries",
  lastSeen: "nyxLastSeenEntries",
  lastSync: "nyxLastSync",
  eventLog: "nyxEventLog",
};
const SCRAPE_STORAGE_KEYS = {
  config: "nyxScrapeConfig",
  snapboardRows: "nyxScrapeSnapboardRows",
  scrapeResults: "nyxScrapeResults",
  eventLog: "nyxScrapeEventLog",
  runnerState: "nyxScrapeRunnerState",
};
const SCRAPE_RUNNER_IDLE = "idle";
const SCRAPE_RUNNER_RUNNING = "running";
const SCRAPE_RUNNER_PAUSED = "paused";
const SCRAPE_RUNNER_STOPPED = "stopped";
const POPUP_PORT_NAME = "nyx-popup-live";
const POPUP_LIVE_POLL_MS = 1100;
const STATUS_CACHE_TTL_MS = 700;
let flushInFlight = null;
let activeScrapeRun = null;
let scrapeHydrationInFlight = null;
let scrapePageTabId = null;
const popupPorts = new Set();
let popupStatusTimer = null;
let popupStatusSignature = "";
let popupStatusRequest = null;
let popupStatusCache = {
  at: 0,
  status: null,
};

function normalizeConfig(config) {
  const safeConfig = config || {};
  const parsedRowLimit = parseInt(safeConfig.rowLimit, 10);
  const parsedRenameTopCount = parseInt(safeConfig.renameTopCount, 10);
  return {
    localApiUrl: String(safeConfig.localApiUrl || safeConfig.googleAppsScriptUrl || "http://127.0.0.1:8865").trim(),
    localToken: String(safeConfig.localToken || safeConfig.sharedSecret || "").trim(),
    remoteConfigUrl: String(
      safeConfig.remoteConfigUrl || "https://drive.google.com/drive/folders/1wSJiQyVUQvVb3UKvOWwrSFPEFMBIhKyy?usp=sharing"
    ).trim(),
    enabled: safeConfig.enabled !== false,
    rowLimit: Number.isFinite(parsedRowLimit) && parsedRowLimit > 0 ? parsedRowLimit : 100,
    renameTopCount: Number.isFinite(parsedRenameTopCount) && parsedRenameTopCount > 0 ? parsedRenameTopCount : 10,
    autoRenameEnabled: safeConfig.autoRenameEnabled === true,
  };
}

function normalizeText(value) {
  return String(value || "").trim();
}

function normalizeUsernameKey(value) {
  return normalizeText(value).toLowerCase();
}

function normalizeAdsPowerId(value) {
  const normalized = normalizeText(value)
    .replace(/^adspower\s*id[:#-]?\s*/i, "")
    .replace(/^profile\s*id[:#-]?\s*/i, "")
    .trim();
  const lowered = normalized.toLowerCase();

  if (!normalized) {
    return "";
  }

  if (
    lowered === "id"
    || lowered === "adspower id"
    || lowered === "ads power id"
    || lowered === "profile id"
    || lowered === "n/a"
    || lowered === "na"
    || lowered === "none"
    || lowered === "null"
    || lowered === "undefined"
    || lowered === "-"
    || lowered === "--"
  ) {
    return "";
  }

  if (/^\d{4,}$/.test(normalized)) {
    return normalized;
  }

  if (/^(?=.*\d)[A-Za-z0-9_-]{4,}$/.test(normalized)) {
    return normalized;
  }

  return "";
}

function buildAdsPowerProfileName(username) {
  const normalizedUsername = normalizeText(username);
  return normalizedUsername ? `Snapchat: ${normalizedUsername}` : "";
}

function createExportConfig(extensionConfig, runnerConfig) {
  const safeExtensionConfig = normalizeConfig(extensionConfig || {});
  const safeRunnerConfig = runnerConfig || {};
  const outfitStyle = String(safeRunnerConfig.outfit_style || "").trim().toLowerCase() === "mixed"
    ? "mix"
    : String(safeRunnerConfig.outfit_style || "default").trim().toLowerCase();
  return {
    version: 1,
    exportedAt: new Date().toISOString(),
    extension: {
      enabled: safeExtensionConfig.enabled,
      localApiUrl: safeExtensionConfig.localApiUrl,
      localToken: safeExtensionConfig.localToken,
      onlineConfigUrl: safeExtensionConfig.remoteConfigUrl,
      rowLimit: safeExtensionConfig.rowLimit,
      renameTopCount: safeExtensionConfig.renameTopCount,
      autoRenameEnabled: safeExtensionConfig.autoRenameEnabled,
    },
    runner: {
      pendingThreshold: Number(safeRunnerConfig.pending_threshold || 10),
      maxParallelProfiles: Number(safeRunnerConfig.max_parallel_profiles || 5),
      ignoreDoneProfiles: safeRunnerConfig.ignore_done_profiles !== false,
      outfitStyle: ["default", "mix", "casual", "sexy", "custom"].includes(outfitStyle) ? outfitStyle : "default",
      automationSpeed: Number(safeRunnerConfig.automation_speed || 1),
      hairRandomizerEnabled: safeRunnerConfig.hair_randomizer_enabled === true,

      launchOnWindowsStartup: safeRunnerConfig.launch_on_windows_startup === true,
    },
  };
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
    return JSON.stringify(status || {});
  } catch (error) {
    return `status-error:${String(error && error.message || error)}`;
  }
}

async function appendEventLog(message) {
  const localData = await chrome.storage.local.get(STORAGE_KEYS.eventLog);
  const currentLog = localData[STORAGE_KEYS.eventLog] || [];
  const nextLog = [{
    message: String(message || "").trim(),
    at: new Date().toISOString(),
  }].concat(currentLog).slice(0, 60);

  await chrome.storage.local.set({
    [STORAGE_KEYS.eventLog]: nextLog,
  });
}

async function renameProfiles(rows) {
  const sanitizedRows = Array.isArray(rows) ? rows : [];
  if (!sanitizedRows.length) {
    throw new Error("No SnapBoard rows were provided for rename.");
  }

  const successes = [];
  const failures = [];

  for (const row of sanitizedRows) {
    const profileId = normalizeText(row && row.profile_id);
    const username = normalizeText(row && row.username);
    const targetName = buildAdsPowerProfileName(username);
    if (!profileId || !username || !targetName) {
      failures.push({
        profile_id: profileId,
        username: username,
        rename_error: "Missing username or AdsPower ID.",
      });
      continue;
    }

    try {
      await callLocalNyx("POST", "/bot/rename_profile", {
        profile_id: profileId,
        new_name: targetName,
      });
      successes.push({
        profile_id: profileId,
        username: username,
        rename_target_name: targetName,
      });
    } catch (error) {
      failures.push({
        profile_id: profileId,
        username: username,
        rename_target_name: targetName,
        rename_error: normalizeText(error && error.message),
      });
    }
  }

  let message = `Renamed ${successes.length} AdsPower profile(s).`;
  if (failures.length) {
    message += ` ${failures.length} failed.`;
  }
  await appendEventLog(message);
  return {
    count: successes.length,
    failed: failures.length,
    successes,
    failures,
    message,
  };
}

function sanitizeAutoRenameRows(rows) {
  const sortedRows = (Array.isArray(rows) ? rows : [])
    .map((row, index) => {
      const safeRow = row || {};
      const sourceRank = Number(safeRow.source_rank);
      return {
        profile_id: normalizeAdsPowerId(safeRow.profile_id),
        username: normalizeText(safeRow.username),
        source_rank: Number.isFinite(sourceRank) && sourceRank >= 0 ? sourceRank : index,
      };
    })
    .filter((row) => row.profile_id)
    .sort((a, b) => a.source_rank - b.source_rank);

  const unique = new Map();
  for (const row of sortedRows) {
    if (!unique.has(row.profile_id)) {
      unique.set(row.profile_id, row);
    }
  }

  return Array.from(unique.values());
}

function getAutoRenameTopInsertRows(currentDetectedRows, previousRows, maxRows) {
  const currentRows = sanitizeAutoRenameRows(currentDetectedRows);
  const lastSeenRows = sanitizeAutoRenameRows(previousRows);
  if (!currentRows.length || !lastSeenRows.length) {
    return [];
  }

  const lastSeenIds = new Set(lastSeenRows.map((row) => row.profile_id));
  const previousTopId = lastSeenRows[0].profile_id;
  const anchorIndex = currentRows.findIndex((row) => row.profile_id === previousTopId);
  let insertedRows = [];

  if (anchorIndex > 0) {
    insertedRows = currentRows.slice(0, anchorIndex);
  }

  const safeMaxRows = Number.isFinite(Number(maxRows)) && Number(maxRows) > 0
    ? Number(maxRows)
    : insertedRows.length;

  return insertedRows
    .filter((row) => !lastSeenIds.has(row.profile_id) && row.username)
    .slice(0, safeMaxRows)
    .map((row) => ({
      profile_id: row.profile_id,
      username: row.username,
    }));
}

// Editable Daily-Report rate (accounts created per working hour). Kept in
// sync with the popup's normalizeAccountsPerHour: a non-positive/blank value
// falls back to the historical fixed rate of 7.
function normalizeAccountsPerHour(value) {
  const parsed = parseFloat(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 7;
  }
  return Math.min(1000, Math.round(parsed * 10) / 10);
}

function normalizeScrapeConfig(config, mainConfig) {
  const safe = config || {};
  const fallbackMainConfig = mainConfig || normalizeConfig({});

  return {
    enabled: safe.enabled !== false,
    maxProfilesPerRun: Math.max(1, parseInt(safe.maxProfilesPerRun, 10) || 25),
    maxParallelTabs: Math.max(1, Math.min(10, parseInt(safe.maxParallelTabs, 10) || 3)),
    profileTimeoutMs: Math.max(4000, Math.min(60000, parseInt(safe.profileTimeoutMs, 10) || 12000)),
    nyxLocalApiUrl: normalizeText(safe.nyxLocalApiUrl || fallbackMainConfig.localApiUrl || "http://127.0.0.1:8865"),
    nyxSharedSecret: normalizeText(safe.nyxSharedSecret || fallbackMainConfig.localToken || ""),
    dailyStartAdspowerId: normalizeText(safe.dailyStartAdspowerId || ""),
    dailyAccountsPerHour: normalizeAccountsPerHour(safe.dailyAccountsPerHour),
  };
}

function buildScrapeProfileUrl(username) {
  return `https://www.snapchat.com/@${encodeURIComponent(username)}`;
}

async function getScrapeConfig() {
  const syncData = await chrome.storage.sync.get([STORAGE_KEYS.config, SCRAPE_STORAGE_KEYS.config]);
  const mainConfig = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  return normalizeScrapeConfig(syncData[SCRAPE_STORAGE_KEYS.config] || {}, mainConfig);
}

// Serialize scrape storage read-modify-writes so parallel worker settles can't
// clobber each other (lost results / undercounted totals). See the matching
// helper in nyxify_extension/background.js.
let scrapeStorageMutex = Promise.resolve();
function withScrapeStorageLock(fn) {
  const run = scrapeStorageMutex.then(() => fn());
  scrapeStorageMutex = run.then(() => undefined, () => undefined);
  return run;
}

async function appendScrapeEvent(message) {
  return withScrapeStorageLock(async () => {
    const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.eventLog);
    const current = Array.isArray(data[SCRAPE_STORAGE_KEYS.eventLog]) ? data[SCRAPE_STORAGE_KEYS.eventLog] : [];
    const next = [{
      at: new Date().toISOString(),
      message: normalizeText(message),
    }].concat(current).slice(0, 120);

    await chrome.storage.local.set({
      [SCRAPE_STORAGE_KEYS.eventLog]: next,
    });
  });
}

function getDefaultScrapeRunnerState() {
  return {
    status: SCRAPE_RUNNER_IDLE,
    total: 0,
    completed: 0,
    has_bitmoji: 0,
    no_bitmoji: 0,
    not_found: 0,
    unknown: 0,
    current_username: "",
    queue: [],
    active_usernames: [],
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
      ...patch,
      updated_at: new Date().toISOString(),
    };
    await chrome.storage.local.set({
      [SCRAPE_STORAGE_KEYS.runnerState]: next,
    });
    return next;
  });
}

function normalizeScrapeRows(rows) {
  const byKey = new Map();

  (rows || []).forEach((row, index) => {
    const username = normalizeText(row && row.username);
    const profileId = normalizeAdsPowerId(
      row && (
        row.last_known_adspower_id
        || row.profile_id
      )
    );
    const model = normalizeText(row && (row.last_known_model || row.model)) || "Unknown";
    const sourceRankRaw = Number(row && row.source_rank);
    const sourceRank = Number.isFinite(sourceRankRaw) && sourceRankRaw >= 0 ? sourceRankRaw : index;

    if (!username || !profileId) {
      if (!profileId) {
        return;
      }
    }

    const key = profileId || username;

    if (!byKey.has(key)) {
      byKey.set(key, {
        username,
        last_known_model: model,
        last_known_adspower_id: profileId,
        source_rank: sourceRank,
        collected_at: new Date().toISOString(),
      });
    }
  });

  return Array.from(byKey.values()).sort((a, b) => Number(a.source_rank) - Number(b.source_rank));
}

async function captureScrapeRows(rows) {
  const normalizedRows = normalizeScrapeRows(rows || []);
  if (!normalizedRows.length) {
    const localData = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.snapboardRows);
    return localData[SCRAPE_STORAGE_KEYS.snapboardRows] || [];
  }
  await chrome.storage.local.set({
    [SCRAPE_STORAGE_KEYS.snapboardRows]: normalizedRows,
  });
  return normalizedRows;
}

async function getScrapeStatus() {
  const localData = await chrome.storage.local.get([
    SCRAPE_STORAGE_KEYS.snapboardRows,
    SCRAPE_STORAGE_KEYS.scrapeResults,
    SCRAPE_STORAGE_KEYS.eventLog,
    SCRAPE_STORAGE_KEYS.runnerState,
  ]);
  const config = await getScrapeConfig();

  return {
    config,
    snapboardRows: localData[SCRAPE_STORAGE_KEYS.snapboardRows] || [],
    scrapeResults: localData[SCRAPE_STORAGE_KEYS.scrapeResults] || [],
    eventLog: localData[SCRAPE_STORAGE_KEYS.eventLog] || [],
    runnerState: localData[SCRAPE_STORAGE_KEYS.runnerState] || await getScrapeRunnerState(),
  };
}

async function callLocalNyxForScrape(method, path, payload) {
  const config = await getScrapeConfig();

  if (!config.nyxLocalApiUrl) {
    throw new Error("Nyx local API URL is missing for scrape features.");
  }

  const headers = {
    "Content-Type": "application/json",
  };

  if (config.nyxSharedSecret) {
    headers["X-Nyx-Token"] = config.nyxSharedSecret;
  }

  const response = await fetch(`${config.nyxLocalApiUrl}${path}`, {
    method,
    headers,
    body: method === "GET" ? undefined : JSON.stringify(payload || {}),
  });

  const result = await response.json();
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || `Request failed with status ${response.status}`);
  }

  return result;
}

async function upsertScrapeResult(payload) {
  const rawUsername = normalizeText(payload && payload.username);
  const usernameKey = normalizeUsernameKey(rawUsername);
  if (!usernameKey) {
    return null;
  }

  return withScrapeStorageLock(async () => {
    const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.scrapeResults);
    const current = Array.isArray(data[SCRAPE_STORAGE_KEYS.scrapeResults]) ? data[SCRAPE_STORAGE_KEYS.scrapeResults] : [];
    const map = new Map(current.map((entry) => [normalizeUsernameKey(entry && entry.username), entry]));
    const normalizedStatus = normalizeText(payload && payload.status) || "unknown";
    const existing = map.get(usernameKey);
    const username = rawUsername || normalizeText(existing && existing.username);

    const result = {
      username,
      checked_at: new Date().toISOString(),
      profile_url: normalizeText(payload && payload.profile_url) || buildScrapeProfileUrl(username),
      has_bitmoji: payload && payload.has_bitmoji === true,
      status: normalizedStatus,
      evidence: normalizeText(payload && payload.evidence),
    };

    map.set(usernameKey, result);
    await chrome.storage.local.set({
      [SCRAPE_STORAGE_KEYS.scrapeResults]: Array.from(map.values()).sort((a, b) => a.username.localeCompare(b.username)),
    });
    return result;
  });
}

async function getQueuedUsernamesForScrapeRun() {
  const config = await getScrapeConfig();
  const localData = await chrome.storage.local.get([
    SCRAPE_STORAGE_KEYS.snapboardRows,
    SCRAPE_STORAGE_KEYS.scrapeResults,
  ]);
  const rows = localData[SCRAPE_STORAGE_KEYS.snapboardRows] || [];
  const results = localData[SCRAPE_STORAGE_KEYS.scrapeResults] || [];
  const checked = new Set(results.map((entry) => normalizeUsernameKey(entry && entry.username)).filter(Boolean));

  return rows
    .map((row) => normalizeText(row && row.username))
    .filter((username) => username && !checked.has(normalizeUsernameKey(username)))
    .slice(0, config.maxProfilesPerRun);
}

async function getTimeoutUsernamesForScrapeRun() {
  const config = await getScrapeConfig();
  const localData = await chrome.storage.local.get([
    SCRAPE_STORAGE_KEYS.scrapeResults,
  ]);
  const results = localData[SCRAPE_STORAGE_KEYS.scrapeResults] || [];

  return results
    .filter((entry) => normalizeText(entry && entry.evidence).toLowerCase() === "timeout")
    .map((entry) => normalizeText(entry && entry.username))
    .filter(Boolean)
    .slice(0, config.maxProfilesPerRun);
}

function getActiveScrapeUsernames() {
  if (!activeScrapeRun) {
    return [];
  }
  return Array.from(activeScrapeRun.workers.values()).map((worker) => worker.username);
}

async function syncScrapeRunnerLiveState(extraPatch) {
  const patch = extraPatch || {};
  const activeWorkers = activeScrapeRun
    ? Array.from(activeScrapeRun.workers.values()).map((worker) => ({
        username: worker.username,
        tabId: worker.tabId,
        profileUrl: worker.profileUrl || buildScrapeProfileUrl(worker.username),
      }))
    : [];
  await setScrapeRunnerState({
    queue: activeScrapeRun ? activeScrapeRun.queue.slice() : [],
    active_usernames: getActiveScrapeUsernames(),
    active_count: activeScrapeRun ? activeScrapeRun.workers.size : 0,
    active_workers: activeWorkers,
    current_username: getActiveScrapeUsernames().join(", "),
    ...patch,
  });
}

function createScrapeWorkerTimeout(worker) {
  const timeoutMs = Math.max(
    (activeScrapeRun && activeScrapeRun.config && activeScrapeRun.config.profileTimeoutMs) || 12000,
    12000,
  );
  return setTimeout(async () => {
    await appendScrapeEvent(`Timed out while checking ${worker.username}.`);
    await settleScrapeWorker(worker.usernameKey, {
      username: worker.username,
      profile_url: worker.profileUrl || buildScrapeProfileUrl(worker.username),
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
    const statusKey = normalizeText(entry && entry.status) || "unknown";
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

  const runnerState = await getScrapeRunnerState();
  const status = String(runnerState.status || "");
  if (status !== SCRAPE_RUNNER_RUNNING && status !== SCRAPE_RUNNER_PAUSED) {
    return null;
  }

  const total = Number(runnerState.total || 0);
  const queueFromState = Array.isArray(runnerState.queue) ? runnerState.queue.slice() : [];
  const storedWorkers = Array.isArray(runnerState.active_workers) ? runnerState.active_workers : [];
  const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.scrapeResults);
  const existingResults = Array.isArray(data[SCRAPE_STORAGE_KEYS.scrapeResults])
    ? data[SCRAPE_STORAGE_KEYS.scrapeResults]
    : [];

  // Reconcile counters against the actual saved results before deciding
  // whether to rehydrate. Hibernation can drop recordScrapeRunnerResult
  // calls so completed lags behind reality.
  await _reconcileScrapeCountersFromResults(existingResults, runnerState);
  const reconciledRunnerState = await getScrapeRunnerState();
  const completed = Number(reconciledRunnerState.completed || 0);

  if (!total) {
    return null;
  }
  if (completed >= total && !queueFromState.length && !storedWorkers.length) {
    return null;
  }

  const config = await getScrapeConfig();
  const checkedKeys = new Set(
    existingResults
      .map((entry) => normalizeUsernameKey(entry && entry.username))
      .filter(Boolean),
  );

  const workers = new Map();
  const activeKeys = new Set();
  const orphanedUsernames = [];

  for (const stored of storedWorkers) {
    const username = normalizeText(stored && stored.username);
    const key = normalizeUsernameKey(username);
    const tabId = Number(stored && stored.tabId);
    if (!username || !key || checkedKeys.has(key) || activeKeys.has(key)) {
      continue;
    }
    if (!Number.isFinite(tabId) || tabId <= 0) {
      orphanedUsernames.push(username);
      continue;
    }
    try {
      await chrome.tabs.get(tabId);
    } catch (_error) {
      orphanedUsernames.push(username);
      continue;
    }
    workers.set(key, {
      username,
      usernameKey: key,
      tabId,
      profileUrl: normalizeText(stored && stored.profileUrl) || buildScrapeProfileUrl(username),
      timeoutId: null,
    });
    activeKeys.add(key);
  }

  const filteredQueue = queueFromState.filter((rawUsername) => {
    const key = normalizeUsernameKey(rawUsername);
    return key && !checkedKeys.has(key) && !activeKeys.has(key);
  });

  // Re-queue any worker whose tab is gone so we don't lose those usernames.
  const recoveredQueue = orphanedUsernames.concat(filteredQueue);

  activeScrapeRun = {
    queue: recoveredQueue,
    workers,
    config,
  };

  for (const worker of workers.values()) {
    worker.timeoutId = createScrapeWorkerTimeout(worker);
  }

  if (storedWorkers.length || filteredQueue.length || orphanedUsernames.length) {
    await appendScrapeEvent(
      `Recovered scrape session: ${workers.size} active worker(s), ${recoveredQueue.length} queued, ${orphanedUsernames.length} requeued.`,
    );
  }

  if (status === SCRAPE_RUNNER_PAUSED) {
    await syncScrapeRunnerLiveState();
  } else {
    await syncScrapeRunnerLiveState({ status: SCRAPE_RUNNER_RUNNING });
  }
  return activeScrapeRun;
}

async function finishScrapeRun(status) {
  activeScrapeRun = null;
  await setScrapeRunnerState({
    status: status || SCRAPE_RUNNER_IDLE,
    current_username: "",
    queue: [],
    active_usernames: [],
    active_count: 0,
  });
}

async function closeScrapeWorkerTab(worker) {
  if (!worker || !worker.tabId) {
    return;
  }

  try {
    await chrome.tabs.remove(worker.tabId);
  } catch (error) {
  }
}

// Derive counters from the saved results (authoritative) instead of
// incrementing — incrementing raced under parallel settles and drifted low.
async function recomputeScrapeCounters() {
  const data = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.scrapeResults);
  const results = Array.isArray(data[SCRAPE_STORAGE_KEYS.scrapeResults]) ? data[SCRAPE_STORAGE_KEYS.scrapeResults] : [];
  const counters = { has_bitmoji: 0, no_bitmoji: 0, not_found: 0, unknown: 0 };
  for (const entry of results) {
    const statusKey = normalizeText(entry && entry.status) || "unknown";
    if (Object.prototype.hasOwnProperty.call(counters, statusKey)) {
      counters[statusKey] += 1;
    } else {
      counters.unknown += 1;
    }
  }
  return setScrapeRunnerState({ completed: results.length, ...counters });
}

function getScrapeWorkerEntryByTabId(tabId) {
  if (!activeScrapeRun || !tabId) {
    return null;
  }

  for (const entry of activeScrapeRun.workers.entries()) {
    const key = entry[0];
    const worker = entry[1];
    if (worker && worker.tabId === tabId) {
      return { key, worker };
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
    username: normalizeText((resultPayload && resultPayload.username) || worker.username),
  };
  await upsertScrapeResult(payload);
  await recomputeScrapeCounters();
  await closeScrapeWorkerTab(worker);
  await syncScrapeRunnerLiveState();
  await maybeProcessScrapeQueue();
}

async function launchScrapeWorker(username) {
  if (!activeScrapeRun) {
    return;
  }

  const usernameText = normalizeText(username);
  const usernameKey = normalizeUsernameKey(usernameText);
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
    tabId: tab.id,
    profileUrl,
    timeoutId: null,
  };
  worker.timeoutId = createScrapeWorkerTimeout(worker);

  activeScrapeRun.workers.set(usernameKey, worker);
  await appendScrapeEvent(`Opening Snapchat profile for ${usernameText}.`);
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

  while (activeScrapeRun.queue.length && activeScrapeRun.workers.size < activeScrapeRun.config.maxParallelTabs) {
    const username = activeScrapeRun.queue.shift();
    await launchScrapeWorker(username);
  }

  await syncScrapeRunnerLiveState();

  if (!activeScrapeRun.queue.length && activeScrapeRun.workers.size === 0) {
    await recomputeScrapeCounters();
    await appendScrapeEvent("Scrape run completed.");
    await finishScrapeRun(SCRAPE_RUNNER_IDLE);
  }
}

async function startScrapeRunWithQueue(queue, config, modeLabel) {
  if (!config.enabled) {
    throw new Error("Scrape features are disabled in settings.");
  }

  if (activeScrapeRun) {
    return getScrapeRunnerState();
  }

  if (!queue.length) {
    await setScrapeRunnerState({
      status: SCRAPE_RUNNER_IDLE,
      total: 0,
      completed: 0,
      has_bitmoji: 0,
      no_bitmoji: 0,
      not_found: 0,
      unknown: 0,
      current_username: "",
      queue: [],
      active_usernames: [],
      active_count: 0,
      run_started_at: "",
    });
    throw new Error(`No usernames are available for ${modeLabel}.`);
  }

  const dedupedQueue = [];
  const seenKeys = new Set();
  (queue || []).forEach((username) => {
    const usernameText = normalizeText(username);
    const usernameKey = normalizeUsernameKey(usernameText);
    if (!usernameKey || seenKeys.has(usernameKey)) {
      return;
    }
    seenKeys.add(usernameKey);
    dedupedQueue.push(usernameText);
  });

  activeScrapeRun = {
    queue: dedupedQueue.slice(),
    workers: new Map(),
    config,
  };

  await setScrapeRunnerState({
    status: SCRAPE_RUNNER_RUNNING,
    total: dedupedQueue.length,
    completed: 0,
    has_bitmoji: 0,
    no_bitmoji: 0,
    not_found: 0,
    unknown: 0,
    current_username: "",
    queue: dedupedQueue.slice(),
    active_usernames: [],
    active_count: 0,
    run_started_at: new Date().toISOString(),
  });
  await appendScrapeEvent(`Started ${modeLabel} scrape run for ${dedupedQueue.length} username(s).`);
  await maybeProcessScrapeQueue();
  return getScrapeRunnerState();
}

async function startScrapeRun() {
  const config = await getScrapeConfig();
  const queue = await getQueuedUsernamesForScrapeRun();
  return startScrapeRunWithQueue(queue, config, "standard");
}

async function startScrapeRangeRun(rangeRows) {
  if (activeScrapeRun) {
    throw new Error("Scrape runner is already active. Stop it before starting a new daily range scrape.");
  }

  const config = await getScrapeConfig();
  const normalizedRows = normalizeScrapeRows(rangeRows || []);

  if (!normalizedRows.length) {
    throw new Error("No valid SnapBoard rows were provided for daily range scrape.");
  }

  await chrome.storage.local.remove([
    SCRAPE_STORAGE_KEYS.scrapeResults,
    SCRAPE_STORAGE_KEYS.eventLog,
    SCRAPE_STORAGE_KEYS.runnerState,
  ]);

  await chrome.storage.local.set({
    [SCRAPE_STORAGE_KEYS.snapboardRows]: normalizedRows,
  });

  await appendScrapeEvent(`Loaded ${normalizedRows.length} row(s) for daily update range scrape.`);
  const queue = normalizedRows.map((row) => row.username).filter(Boolean);
  if (!queue.length) {
    throw new Error("Daily update rows were loaded, but no usernames are available for scrape run.");
  }
  return startScrapeRunWithQueue(queue, config, "daily range");
}

// Scan EVERY captured SnapBoard row (no maxProfilesPerRun cap). The popup
// captures all rows first (NYX_SCRAPE_CAPTURE_ROWS), then calls this for a fresh
// full sweep of the board.
async function startScrapeAllRun() {
  if (activeScrapeRun) {
    throw new Error("Scrape runner is already active. Stop it before starting a new scan.");
  }

  const config = await getScrapeConfig();
  const localData = await chrome.storage.local.get(SCRAPE_STORAGE_KEYS.snapboardRows);
  const rows = normalizeScrapeRows(localData[SCRAPE_STORAGE_KEYS.snapboardRows] || []);
  if (!rows.length) {
    throw new Error("No SnapBoard rows captured. Open a SnapBoard tab and try again.");
  }

  await chrome.storage.local.remove([
    SCRAPE_STORAGE_KEYS.scrapeResults,
    SCRAPE_STORAGE_KEYS.eventLog,
    SCRAPE_STORAGE_KEYS.runnerState,
  ]);

  await appendScrapeEvent(`Scanning all ${rows.length} SnapBoard row(s).`);
  const queue = rows.map((row) => row.username).filter(Boolean);
  if (!queue.length) {
    throw new Error("Captured SnapBoard rows have no usernames to scan.");
  }
  return startScrapeRunWithQueue(queue, config, "all SnapBoard rows");
}

async function rescrapeTimeoutRun() {
  const config = await getScrapeConfig();
  const queue = await getTimeoutUsernamesForScrapeRun();
  return startScrapeRunWithQueue(queue, config, "timeout-rescrape");
}

async function pauseScrapeRun() {
  if (!activeScrapeRun) {
    return getScrapeRunnerState();
  }
  await setScrapeRunnerState({
    status: SCRAPE_RUNNER_PAUSED,
  });
  await appendScrapeEvent("Paused scrape run.");
  return getScrapeRunnerState();
}

async function resumeScrapeRun() {
  if (!activeScrapeRun) {
    return startScrapeRun();
  }
  await setScrapeRunnerState({
    status: SCRAPE_RUNNER_RUNNING,
  });
  await appendScrapeEvent("Resumed scrape run.");
  await maybeProcessScrapeQueue();
  return getScrapeRunnerState();
}

async function stopScrapeRun() {
  const runnerState = await getScrapeRunnerState();
  if (activeScrapeRun) {
    const workers = Array.from(activeScrapeRun.workers.values());
    workers.forEach((worker) => {
      if (worker.timeoutId) {
        clearTimeout(worker.timeoutId);
      }
    });
    await Promise.all(workers.map((worker) => closeScrapeWorkerTab(worker)));
  }
  activeScrapeRun = null;
  await setScrapeRunnerState({
    status: SCRAPE_RUNNER_STOPPED,
    current_username: "",
    queue: [],
    active_usernames: [],
    active_count: 0,
  });
  await appendScrapeEvent("Stopped scrape run.");
  return {
    ...runnerState,
    status: SCRAPE_RUNNER_STOPPED,
    current_username: "",
    queue: [],
    active_usernames: [],
    active_count: 0,
  };
}

async function clearScrapeData() {
  await stopScrapeRun().catch(() => null);
  await chrome.storage.local.remove([
    SCRAPE_STORAGE_KEYS.snapboardRows,
    SCRAPE_STORAGE_KEYS.scrapeResults,
    SCRAPE_STORAGE_KEYS.eventLog,
    SCRAPE_STORAGE_KEYS.runnerState,
  ]);
}

chrome.runtime.onInstalled.addListener(async () => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  await hydrateScrapeRunFromStorage().catch(() => null);
  await maybeProcessScrapeQueue().catch(() => null);
  await updateBadge();
});

chrome.runtime.onStartup.addListener(async () => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  await hydrateScrapeRunFromStorage().catch(() => null);
  await maybeProcessScrapeQueue().catch(() => null);
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === FLUSH_ALARM) {
    await flushPendingEntries();
    await hydrateScrapeRunFromStorage().catch(() => null);
    await maybeProcessScrapeQueue().catch(() => null);
  }
});

chrome.runtime.onConnect.addListener((port) => {
  if (!port || port.name !== POPUP_PORT_NAME) {
    return;
  }

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

chrome.tabs.onRemoved.addListener((tabId) => {
  if (!scrapePageTabId || tabId !== scrapePageTabId) {
    return;
  }

  scrapePageTabId = null;
  clearScrapeData().catch(() => null);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "NYX_DELETE_ADSPOWER_PROFILE") {
    callLocalNyx("POST", "/bot/delete_adspower_profile", { profile_id: message.profileId })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error && error.message ? error.message : "Delete failed." }));
    return true;
  }

  if (message && message.type === "NYX_REPLACE_SNAPBOARD_ROW") {
    callLocalNyxify("POST", "/replace_banned/replace", { rows: [message.row || {}] })
      .then((payload) => sendResponse({ ok: payload.ok !== false, payload }))
      .catch((error) => sendResponse({ ok: false, error: error && error.message ? error.message : "Replace failed." }));
    return true;
  }

  if (message && message.type === "NYX_ADD_TO_NYX_PENDING") {
    callLocalNyx("POST", "/queue/upsert", {
      allow_done_requeue: true,
      entries: [{
        profile_id: message.profileId,
        model: message.model,
        username: message.username,
        password: message.password,
      }],
    })
      .then((payload) => sendResponse({ ok: true, payload }))
      .catch((error) => sendResponse({ ok: false, error: error && error.message ? error.message : "Add to Nyx pending failed." }));
    return true;
  }

  if (message && message.type === "NYX_DETECTED_ROWS") {
    chrome.storage.sync.get(STORAGE_KEYS.config)
      .then(async (syncData) => {
        const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
        if (!config.enabled) {
          await appendEventLog("Ignored new detection because Nyx is disabled.");
          sendResponse({ ok: true, count: 0, skipped: true });
          return;
        }

        const sourceUrl = sender && sender.tab ? sender.tab.url : "";
        const localData = await chrome.storage.local.get(STORAGE_KEYS.lastSeen);
        const previousRows = localData[STORAGE_KEYS.lastSeen] || [];
        const autoRenameRows = config.autoRenameEnabled
          ? getAutoRenameTopInsertRows(message.rows || [], previousRows, config.renameTopCount)
          : [];

        const count = await mergeDetectedEntries(message.rows || [], sourceUrl);
        await flushPendingEntries();
        let autoRenameResult = null;
        if (autoRenameRows.length) {
          await appendEventLog(`Auto rename triggered for ${autoRenameRows.length} newly inserted top row(s).`);
          autoRenameResult = await renameProfiles(autoRenameRows);
        }
        if (count > 0) {
          await appendEventLog(`Detected ${count} new row(s) from SnapBoard.`);
        }
        sendResponse({ ok: true, count, autoRename: autoRenameResult });
      })
      .catch((error) => {
        sendResponse({ ok: false, error: error.message });
      });
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_SNAPCHAT_RESULT") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    const fallbackUsername = normalizeText(message && message.payload && message.payload.username);

    hydrateScrapeRunFromStorage()
      .catch(() => null)
      .then(() => {
        const workerEntry = getScrapeWorkerEntryByTabId(tabId);
        const worker = workerEntry ? workerEntry.worker : null;
        const workerKey = workerEntry ? workerEntry.key : normalizeUsernameKey(fallbackUsername);
        const payload = {
          ...(message.payload || {}),
          username: normalizeText((worker && worker.username) || fallbackUsername),
        };

        return upsertScrapeResult(payload).then(async (result) => {
          if (activeScrapeRun && workerKey && activeScrapeRun.workers.has(workerKey)) {
            const eventUsername = normalizeText((worker && worker.username) || (payload && payload.username));
            await appendScrapeEvent(`Checked Snapchat profile: ${eventUsername} (${normalizeText(result && result.status) || "unknown"}).`);
            await settleScrapeWorker(workerKey, result || payload || {});
          }
          sendResponse({ ok: true });
        });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_GET_STATUS") {
    hydrateScrapeRunFromStorage()
      .catch(() => null)
      .then(() => maybeProcessScrapeQueue().catch(() => null))
      .then(() => getScrapeStatus())
      .then((status) => sendResponse({ ok: true, status }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_SAVE_CONFIG") {
    chrome.storage.sync.get([STORAGE_KEYS.config, SCRAPE_STORAGE_KEYS.config])
      .then(async (syncData) => {
        const mainConfig = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
        const current = normalizeScrapeConfig(syncData[SCRAPE_STORAGE_KEYS.config] || {}, mainConfig);
        const next = normalizeScrapeConfig({
          ...current,
          ...(message.config || {}),
        }, mainConfig);

        await chrome.storage.sync.set({
          [SCRAPE_STORAGE_KEYS.config]: next,
        });
        await appendScrapeEvent("Saved scrape settings.");
        sendResponse({ ok: true, config: next });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_CAPTURE_ROWS") {
    captureScrapeRows(message.rows || [])
      .then((rows) => sendResponse({ ok: true, rows }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_RUNNER_ACTION") {
    const action = normalizeText(message.action).toLowerCase();
    let operation;

    if (action === "start") {
      operation = startScrapeRun();
    } else if (action === "rescrape_timeout") {
      operation = rescrapeTimeoutRun();
    } else if (action === "pause") {
      operation = pauseScrapeRun();
    } else if (action === "resume") {
      operation = resumeScrapeRun();
    } else if (action === "stop") {
      operation = stopScrapeRun();
    } else {
      sendResponse({ ok: false, error: "Unknown scrape runner action." });
      return false;
    }

    operation
      .then((runnerState) => sendResponse({ ok: true, runnerState }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_START_RANGE") {
    startScrapeRangeRun(message.rows || [])
      .then((runnerState) => sendResponse({ ok: true, runnerState }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_START_ALL") {
    startScrapeAllRun()
      .then((runnerState) => sendResponse({ ok: true, runnerState }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_OPEN_PAGE") {
    const scrapePageUrl = chrome.runtime.getURL("scrape.html");

    if (scrapePageTabId) {
      chrome.tabs.get(scrapePageTabId)
        .then((existingTab) => {
          if (!existingTab || !existingTab.id) {
            throw new Error("Missing scrape page tab.");
          }
          return chrome.tabs.update(existingTab.id, { active: true });
        })
        .then(() => sendResponse({ ok: true }))
        .catch(() => {
          chrome.tabs.create({
            url: scrapePageUrl,
            active: true,
          })
            .then((tab) => {
              scrapePageTabId = tab && tab.id ? tab.id : null;
              sendResponse({ ok: true });
            })
            .catch((error) => sendResponse({ ok: false, error: error.message }));
        });
      return true;
    }

    chrome.tabs.create({
      url: scrapePageUrl,
      active: true,
    })
      .then((tab) => {
        scrapePageTabId = tab && tab.id ? tab.id : null;
        sendResponse({ ok: true });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SCRAPE_CLEAR_ALL") {
    clearScrapeData()
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_GET_STATUS") {
    getStatusSnapshotCached(Boolean(message.force))
      .then((status) => sendResponse({ ok: true, status }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SAVE_CONFIG") {
    chrome.storage.sync.get(STORAGE_KEYS.config).then((existingData) => {
      const existingConfig = normalizeConfig(existingData[STORAGE_KEYS.config] || {});
      const configPatch = {};

      if (message.localApiUrl !== undefined) {
        configPatch.localApiUrl = message.localApiUrl;
      }
      if (message.googleAppsScriptUrl !== undefined) {
        configPatch.localApiUrl = message.googleAppsScriptUrl;
      }
      if (message.localToken !== undefined) {
        configPatch.localToken = message.localToken;
      }
      if (message.sharedSecret !== undefined) {
        configPatch.localToken = message.sharedSecret;
      }
      if (message.enabled !== undefined) {
        configPatch.enabled = message.enabled;
      }
      if (message.remoteConfigUrl !== undefined) {
        configPatch.remoteConfigUrl = message.remoteConfigUrl;
      }
      if (message.rowLimit !== undefined) {
        configPatch.rowLimit = message.rowLimit;
      }
      if (message.renameTopCount !== undefined) {
        configPatch.renameTopCount = message.renameTopCount;
      }
      if (message.autoRenameEnabled !== undefined) {
        configPatch.autoRenameEnabled = message.autoRenameEnabled;
      }

      const nextConfig = normalizeConfig({
        ...existingConfig,
        ...configPatch,
      });

      return chrome.storage.sync.set({
        [STORAGE_KEYS.config]: nextConfig,
      }).then(async () => {
        if (
          message.pendingThreshold !== undefined ||
          message.maxParallelProfiles !== undefined ||
          message.ignoreDoneProfiles !== undefined ||
          message.outfitStyle !== undefined ||
          message.automationSpeed !== undefined ||
          message.hairRandomizerEnabled !== undefined ||
          message.launchOnWindowsStartup !== undefined
        ) {
          await callLocalNyx("POST", "/config", {
            pending_threshold: message.pendingThreshold,
            max_parallel_profiles: message.maxParallelProfiles,
            ignore_done_profiles: message.ignoreDoneProfiles,
            outfit_style: message.outfitStyle,
            automation_speed: message.automationSpeed,
            hair_randomizer_enabled: message.hairRandomizerEnabled,

            launch_on_windows_startup: message.launchOnWindowsStartup,
          });
          await appendEventLog("Saved Nyx runner settings from the popup.");
        }
        await flushPendingEntries();
        sendResponse({ ok: true });
      });
    }).catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_SET_ENABLED") {
    chrome.storage.sync.get(STORAGE_KEYS.config).then((existingData) => {
      const existingConfig = normalizeConfig(existingData[STORAGE_KEYS.config] || {});
      const nextConfig = normalizeConfig({
        ...existingConfig,
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

  if (message && message.type === "NYX_CLEAR_ALL") {
    clearAllData()
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_CLEAR_CACHE_LOGS") {
    clearCacheAndLogs()
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_PRUNE_COMPLETED_KEEP_150") {
    callLocalNyx("POST", "/queue/prune_completed", {})
      .then(async (payload) => {
        await appendEventLog(payload.message || "Pruned old DONE rows.");
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_GET_BITMOJI_STATUS") {
    callLocalNyx("POST", "/queue/bitmoji_status", {
      entries: message.rows || [],
    })
      .then((payload) => sendResponse({ ok: true, statuses: payload.statuses || [] }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_CLEAR_DETECTED_DATA") {
    clearDetectedData()
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && (message.type === "NYX_REFRESH_QUEUE" || message.type === "NYX_REFRESH_SHEET")) {
    callLocalNyx("GET", "/queue")
      .then((payload) => sendResponse({ ok: true, rows: payload.rows || [] }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_RERUN_FAILED") {
    callLocalNyx("POST", "/queue/rerun_failed")
      .then(async (payload) => {
        await chrome.storage.local.set({
          [STORAGE_KEYS.lastSync]: {
            syncedAt: new Date().toISOString(),
            count: Number(payload.count || 0),
            message: payload.message || "Failed Nyx rows reset.",
          },
        });
        await appendEventLog(`Reset ${Number(payload.count || 0)} failed row(s) to PENDING.`);
        sendResponse({
          ok: true,
          count: Number(payload.count || 0),
          rows: payload.rows || [],
          message: payload.message || "Failed Nyx rows reset.",
        });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_BOT_ACTION") {
    callLocalNyx("POST", `/bot/${message.action}`, message.payload || {})
      .then(async (payload) => {
        await appendEventLog(payload.message || `Ran bot action: ${message.action}`);
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_MARK_DONE_PROFILE") {
    callLocalNyx("POST", "/queue/mark_done", {
      profile_id: message.profileId,
    })
      .then(async (payload) => {
        await appendEventLog(`Marked ${String(message.profileId || "").trim()} as DONE from popup.`);
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_REMOVE_QUEUE_PROFILE") {
    callLocalNyx("POST", "/queue/remove", {
      profile_id: message.profileId,
    })
      .then(async (payload) => {
        await appendEventLog(`Removed ${String(message.profileId || "").trim()} from Nyx queue.`);
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_REMOVE_MISSING_PROFILE") {
    callLocalNyx("POST", "/queue/remove_missing_profile", {})
      .then(async (payload) => {
        await appendEventLog(payload.message || "Remove missing profile action completed.");
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_RELAUNCH_QUEUE_PROFILE") {
    callLocalNyx("POST", "/queue/relaunch", {
      profile_id: message.profileId,
    })
      .then(async (payload) => {
        await appendEventLog(`Relaunched ${String(message.profileId || "").trim()} from Nyx queue.`);
        sendResponse({ ok: true, payload });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_EXPORT_CONFIG") {
    getStatusSnapshot()
      .then((status) => {
        sendResponse({
          ok: true,
          config: createExportConfig(status.config || {}, (status.runnerStatus && status.runnerStatus.config) || {}),
        });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_IMPORT_CONFIG") {
    importNyxConfig(message.config || {})
      .then((status) => sendResponse({ ok: true, status }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_GET_ONLINE_CONFIG_LINK") {
    chrome.storage.sync.get(STORAGE_KEYS.config)
      .then((syncData) => {
        const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
        sendResponse({ ok: true, url: config.remoteConfigUrl });
      })
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message && message.type === "NYX_RENAME_PROFILES") {
    renameProfiles(message.rows || [])
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
});

async function getStatusSnapshot() {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const localData = await chrome.storage.local.get([
    STORAGE_KEYS.pending,
    STORAGE_KEYS.lastSeen,
    STORAGE_KEYS.lastSync,
    STORAGE_KEYS.eventLog,
  ]);

  let runnerStatus = null;
  let scrapeStatus = null;
  try {
    const payload = await callLocalNyx("GET", "/status");
    runnerStatus = payload.status || null;
  } catch (error) {
    runnerStatus = {
      unavailable: true,
      error: error.message,
    };
  }

  try {
    scrapeStatus = await getScrapeStatus();
  } catch (error) {
    scrapeStatus = {
      unavailable: true,
      error: error.message,
    };
  }

  return {
    config: normalizeConfig(syncData[STORAGE_KEYS.config] || {}),
    pendingEntries: localData[STORAGE_KEYS.pending] || [],
    lastSeenEntries: localData[STORAGE_KEYS.lastSeen] || [],
    lastSync: localData[STORAGE_KEYS.lastSync] || null,
    eventLog: localData[STORAGE_KEYS.eventLog] || [],
    runnerStatus: runnerStatus,
    scrapeStatus: scrapeStatus,
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

  popupStatusRequest = getStatusSnapshot()
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
    const message = String(error && error.message || error || "Could not load Nyx status.");
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

async function mergeDetectedEntries(rows, sourceUrl) {
  const sanitizedRows = sanitizeEntries(rows);
  const localData = await chrome.storage.local.get([
    STORAGE_KEYS.pending,
    STORAGE_KEYS.lastSeen,
  ]);

  const currentPending = localData[STORAGE_KEYS.pending] || [];
  const lastSeenEntries = localData[STORAGE_KEYS.lastSeen] || [];
  const currentTopIds = new Set(sanitizedRows.map((entry) => entry.profile_id));
  const mergedMap = new Map(
    currentPending
      .filter((entry) => currentTopIds.has(entry.profile_id))
      .map((entry) => [entry.profile_id, entry])
  );
  const lastSeenMap = new Map(lastSeenEntries.map((entry) => [entry.profile_id, entry]));
  let runnerQueueMap = null;
  let addedCount = 0;

  try {
    const payload = await callLocalNyx("GET", "/queue");
    const queueRows = Array.isArray(payload && payload.rows) ? payload.rows : [];
    runnerQueueMap = new Map(
      queueRows
        .filter((entry) => entry && entry.profile_id)
        .map((entry) => [String(entry.profile_id).trim(), entry])
    );
  } catch (error) {
    runnerQueueMap = null;
  }

  for (const row of sanitizedRows) {
    const previousRow = lastSeenMap.get(row.profile_id);
    const alreadyPending = mergedMap.has(row.profile_id);
    const runnerRow = runnerQueueMap ? runnerQueueMap.get(row.profile_id) : null;
    const existsInRunnerQueue = Boolean(runnerRow);
    const sameAsPreviousSeen = previousRow && previousRow.model === row.model;
    const canBackfillPassword = Boolean(row.password && runnerRow && !Number(runnerRow.has_password || 0));

    if (alreadyPending || (sameAsPreviousSeen && existsInRunnerQueue && !canBackfillPassword)) {
      continue;
    }

    mergedMap.set(row.profile_id, {
      ...row,
      source: "nyx-extension",
      source_url: sourceUrl || "",
      queued_at: new Date().toISOString(),
    });
    addedCount += 1;
  }

  const pendingEntries = Array.from(mergedMap.values());
  await chrome.storage.local.set({
    [STORAGE_KEYS.pending]: pendingEntries,
    [STORAGE_KEYS.lastSeen]: sanitizedRows,
  });
  await updateBadge();
  return addedCount;
}

async function clearDetectedData() {
  let clearedQueue = false;
  let clearedRows = 0;

  try {
    const payload = await callLocalNyx("POST", "/queue/clear");
    clearedQueue = true;
    clearedRows = Number(payload.count || 0);
  } catch (error) {
    const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
    const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
    if (config.localApiUrl) {
      throw error;
    }
  }

  await chrome.storage.local.set({
    [STORAGE_KEYS.pending]: [],
    [STORAGE_KEYS.lastSeen]: [],
    [STORAGE_KEYS.lastSync]: {
      syncedAt: new Date().toISOString(),
      count: clearedRows,
      message: clearedQueue
        ? "Cleared recorded SnapBoard detections and Nyx queue rows."
        : "Cleared recorded SnapBoard detections.",
    },
  });

  await appendEventLog(
    clearedQueue
      ? `Cleared recorded SnapBoard detections and ${clearedRows} Nyx queue row(s).`
      : "Cleared recorded SnapBoard detections."
  );
  await updateBadge();

  return {
    message: clearedQueue
      ? "Cleared recorded IDs/models and the Nyx queue. The next scan will detect them as new again."
      : "Cleared recorded IDs and models. The next scan will detect them as new again."
  };
}

function sanitizeEntries(rows) {
  const unique = new Map();

  for (const row of rows || []) {
    const safeRow = row || {};
    const profileId = normalizeText(safeRow.profile_id);
    const model = normalizeText(safeRow.model);
    const username = normalizeText(safeRow.username);
    const password = normalizeText(safeRow.password);
    if (!profileId || !model) {
      continue;
    }

    unique.set(profileId, {
      profile_id: profileId,
      model,
      username,
      password,
      gender: "female",
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

  try {
    const payload = await callLocalNyx("POST", "/queue/upsert", {
      entries: pendingEntries.map((entry) => ({
        profile_id: entry.profile_id,
        model: entry.model,
        username: entry.username,
        password: entry.password,
      })),
    });

    const syncedCount = Number(payload.count || 0);
    const skippedDone = Number(payload.skipped_done || 0);
    const skippedMissing = Number(payload.skipped_missing || 0);
    const heldOnlyByNyxify = false;
    const skippedBits = [];
    if (skippedDone > 0) {
      skippedBits.push(`skipped ${skippedDone} row(s) already marked DONE`);
    }
    if (skippedMissing > 0) {
      skippedBits.push(`skipped ${skippedMissing} missing profile row(s)`);
    }
    const syncMessage = skippedBits.length
      ? `Synced ${syncedCount} row(s); ${skippedBits.join("; ")}.`
      : (payload.message || "Synced to local Nyx queue.");

    await chrome.storage.local.set({
      [STORAGE_KEYS.pending]: [],
      [STORAGE_KEYS.lastSync]: {
        syncedAt: new Date().toISOString(),
        count: syncedCount,
        message: syncMessage,
      },
    });
    if (!heldOnlyByNyxify) {
      await appendEventLog(syncMessage);
    }
  } catch (error) {
    await chrome.storage.local.set({
      [STORAGE_KEYS.lastSync]: {
        syncedAt: new Date().toISOString(),
        count: 0,
        message: error.message,
        failed: true,
      },
    });
    await appendEventLog(`Sync failed: ${error.message}`);
  }

  await updateBadge();
}

async function clearAllData() {
  let clearedQueue = false;
  let clearedRows = 0;

  try {
    const payload = await callLocalNyx("POST", "/queue/clear");
    clearedQueue = true;
    clearedRows = Number(payload.count || 0);
  } catch (error) {
    const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
    const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
    if (config.localApiUrl) {
      throw error;
    }
  }

  await chrome.storage.local.set({
    [STORAGE_KEYS.pending]: [],
    [STORAGE_KEYS.lastSeen]: [],
    [STORAGE_KEYS.lastSync]: {
      syncedAt: new Date().toISOString(),
      count: clearedRows,
      message: clearedQueue ? "Nyx extension and local queue cleared." : "Nyx extension queue cleared.",
    },
  });
  await appendEventLog(clearedQueue ? "Cleared extension queue and Nyx runner queue." : "Cleared extension queue.");

  await updateBadge();
  return { clearedQueue, clearedRows };
}

async function clearCacheAndLogs() {
  const payload = await callLocalNyx("POST", "/bot/clear_cache_logs", {});

  await chrome.storage.local.set({
    [STORAGE_KEYS.lastSeen]: [],
    [STORAGE_KEYS.lastSync]: null,
    [STORAGE_KEYS.eventLog]: [],
  });

  await updateBadge();
  return payload;
}

async function importNyxConfig(configPayload) {
  const payload = configPayload || {};
  const extensionConfig = normalizeConfig({
    localApiUrl: payload.extension ? payload.extension.localApiUrl : undefined,
    localToken: payload.extension ? payload.extension.localToken : undefined,
    remoteConfigUrl: payload.extension ? payload.extension.onlineConfigUrl : undefined,
    enabled: payload.extension ? payload.extension.enabled : undefined,
    rowLimit: payload.extension ? payload.extension.rowLimit : undefined,
    renameTopCount: payload.extension ? payload.extension.renameTopCount : undefined,
    autoRenameEnabled: payload.extension ? payload.extension.autoRenameEnabled : undefined,
  });

  await chrome.storage.sync.set({
    [STORAGE_KEYS.config]: extensionConfig,
  });

  await callLocalNyx("POST", "/config", {
    pending_threshold: payload.runner ? payload.runner.pendingThreshold : undefined,
    max_parallel_profiles: payload.runner ? payload.runner.maxParallelProfiles : undefined,
    ignore_done_profiles: payload.runner ? payload.runner.ignoreDoneProfiles : undefined,
    outfit_style: payload.runner ? payload.runner.outfitStyle : undefined,
    automation_speed: payload.runner ? payload.runner.automationSpeed : undefined,
    hair_randomizer_enabled: payload.runner ? payload.runner.hairRandomizerEnabled : undefined,

    launch_on_windows_startup: payload.runner ? payload.runner.launchOnWindowsStartup : undefined,
  });

  await appendEventLog("Imported Nyx config from settings.");
  await flushPendingEntries();
  return getStatusSnapshot();
}


// Fetch the bridge token from the unauthenticated /token endpoint and persist
// it. The v6 bridge requires a token on every local-API call; obtaining it this
// way (instead of relying on the native-messaging connect dance) keeps SnapBoard
// sync as reliable as v3.3.4 did without auth.
async function fetchAndSaveLocalToken(apiUrl) {
  try {
    const r = await fetch(`${apiUrl}/token`);
    const d = await r.json().catch(() => ({}));
    const token = d && d.ok && d.token ? String(d.token) : "";
    if (token) {
      const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
      const cfg = syncData[STORAGE_KEYS.config] || {};
      cfg.localToken = token;
      if (!cfg.localApiUrl) cfg.localApiUrl = apiUrl;
      await chrome.storage.sync.set({ [STORAGE_KEYS.config]: cfg });
    }
    return token;
  } catch (e) {
    return "";
  }
}

async function callLocalNyx(method, path, payload) {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});

  if (!config.localApiUrl) {
    throw new Error("Nyx local API missing.");
  }

  let token = config.localToken;
  if (!token) {
    token = await fetchAndSaveLocalToken(config.localApiUrl);
  }

  async function doRequest(tok) {
    const headers = { "Content-Type": "application/json" };
    const bodyPayload = { ...(payload || {}) };
    if (tok) {
      headers["X-Nyx-Token"] = tok;
      bodyPayload.token = tok;
    }
    return fetch(`${config.localApiUrl}${path}`, {
      method: method,
      headers: headers,
      body: method === "GET" ? undefined : JSON.stringify(bodyPayload),
    });
  }

  let response = await doRequest(token);
  // Self-heal: a stale/missing token gives 401 — re-fetch and retry once.
  if (response.status === 401) {
    const fresh = await fetchAndSaveLocalToken(config.localApiUrl);
    if (fresh && fresh !== token) {
      response = await doRequest(fresh);
    }
  }

  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || `Request failed with status ${response.status}`);
  }

  return result;
}

async function callLocalNyxify(method, path, payload) {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  const nyxBase = config.localApiUrl || "http://127.0.0.1:8865";
  const localApiUrl = nyxBase.replace(/:8865\b/, ":8866");
  let token = config.localToken;

  if (!token) {
    token = await fetchAndSaveLocalToken(localApiUrl);
  }

  const headers = { "Content-Type": "application/json" };
  const bodyPayload = { ...(payload || {}) };
  if (token) {
    headers["X-Nyxify-Token"] = token;
    bodyPayload.token = token;
  }

  const response = await fetch(`${localApiUrl}${path}`, {
    method,
    headers,
    body: method === "GET" ? undefined : JSON.stringify(bodyPayload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || `Request failed with status ${response.status}`);
  }
  return result;
}

async function updateBadge() {
  const syncData = await chrome.storage.sync.get(STORAGE_KEYS.config);
  const config = normalizeConfig(syncData[STORAGE_KEYS.config] || {});
  const localData = await chrome.storage.local.get(STORAGE_KEYS.pending);
  const localPendingCount = (localData[STORAGE_KEYS.pending] || []).length;
  let count = localPendingCount;

  if (config.enabled && config.localApiUrl) {
    try {
      const payload = await callLocalNyx("GET", "/status");
      const runnerPending = payload && payload.status && payload.status.counts
        ? Number(payload.status.counts.pending || 0)
        : 0;
      count = Number.isFinite(runnerPending) ? runnerPending : localPendingCount;
    } catch (error) {
      count = localPendingCount;
    }
  }

  await chrome.action.setBadgeBackgroundColor({ color: "#111111" });
  await chrome.action.setBadgeText({ text: config.enabled ? (count ? String(Math.min(count, 99)) : "") : "OFF" });
}
