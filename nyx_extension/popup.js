let isHeaderRefreshing = false;
let lastRunnerStatusSignature = "";
let lastEventLogSignature = "";
let lastLastSeenSignature = "";
let lastSyncSignature = "";
let lastPopupSettingsSignature = "";
let latestDailyRows = [];
let latestScrapeConfig = {};
let lastDailyRenderSignature = "";
let popupLivePort = null;
let popupLiveReconnectTimer = null;
let dailyRowsRefreshInFlight = false;
let lastDailyRowsRefreshAt = 0;
let lastDailyRowsDataSignature = "";
let popupSettingsSaveTimer = null;
let popupSettingsDirty = false;
let bitmojiIndicatorsVisible = false;
const DAILY_ROWS_AUTO_REFRESH_MS = 15000;
// Last-known runner status, persisted by the popup on each successful refresh so
// the next open can paint an immediate "connected/running" card before the live
// refresh lands. Kept tiny (runnerStatus is now a compact bot+counts+config).
const LAST_RUNNER_STATUS_KEY = "nyxLastRunnerStatus";

// Redacted timing diagnostics (baseline). Inert unless window.__NYX_TIMING__ is
// set from the DevTools console. Logs only a label + ms — never credentials.
function diagTiming(label, start) {
  if (!window || !window.__NYX_TIMING__) return;
  try {
    console.debug("[nyx-timing] " + label + " | " + (performance.now() - start).toFixed(1) + " ms");
  } catch (e) {
    /* best-effort */
  }
}

function setHeaderRefreshLoading(isLoading) {
  const refreshButton = document.getElementById("headerRefreshButton");
  if (!refreshButton) {
    return;
  }

  isHeaderRefreshing = isLoading;
  refreshButton.disabled = isLoading;
  refreshButton.classList.toggle("is-loading", isLoading);
}

function setPrimaryStatus(message, holdMs = 0) {
  const statusLine = document.getElementById("statusLine");
  if (!statusLine) {
    return;
  }

  statusLine.textContent = String(message || "");
  statusLine.dataset.holdUntil = holdMs > 0 ? String(Date.now() + holdMs) : "0";
}

function applyPrimaryStatus(message) {
  const statusLine = document.getElementById("statusLine");
  if (!statusLine) {
    return;
  }

  const holdUntil = Number(statusLine.dataset.holdUntil || 0);
  if (holdUntil > Date.now()) {
    return;
  }

  statusLine.textContent = String(message || "");
}

function setBitmojiShowButtonState(isVisible) {
  const button = document.getElementById("showBitmojiStatusButton");
  bitmojiIndicatorsVisible = !!isVisible;
  if (!button) {
    return;
  }
  button.classList.toggle("button-toggle-active", bitmojiIndicatorsVisible);
  button.textContent = bitmojiIndicatorsVisible ? "Hide" : "Show";
  button.title = bitmojiIndicatorsVisible ? "Hide SnapBoard Bitmoji indicators" : "Show SnapBoard Bitmoji indicators";
}

function getActiveSnapboardTab(callback) {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const activeTab = tabs && tabs.length ? tabs[0] : null;
    const activeUrl = String((activeTab && activeTab.url) || "");

    if (!activeTab || !activeTab.id || activeUrl.indexOf("snapboard.onrender.com") === -1) {
      callback(null);
      return;
    }

    callback(activeTab);
  });
}

function syncBitmojiShowButtonState() {
  getActiveSnapboardTab((activeTab) => {
    if (!activeTab) {
      setBitmojiShowButtonState(false);
      return;
    }

    chrome.tabs.sendMessage(activeTab.id, { type: "NYX_GET_BITMOJI_INDICATOR_STATE" }, (response) => {
      if (chrome.runtime.lastError || !response || !response.ok) {
        setBitmojiShowButtonState(false);
        return;
      }
      setBitmojiShowButtonState(response.visible === true);
    });
  });
}

function renderEventLog(events) {
  const eventLog = document.getElementById("eventLog");
  const nextSignature = (events || []).map((entry) => `${entry && entry.at ? entry.at : ""}|${entry && entry.message ? entry.message : ""}`).join("\n");
  if (!eventLog) {
    return;
  }
  if (nextSignature === lastEventLogSignature) {
    return;
  }
  lastEventLogSignature = nextSignature;
  if (!events || !events.length) {
    eventLog.textContent = "No events yet.";
    return;
  }

  eventLog.textContent = events.map((entry) => {
    const timestamp = entry && entry.at ? new Date(entry.at).toLocaleString() : "-";
    return `[${timestamp}] ${entry && entry.message ? entry.message : "-"}`;
  }).join("\n");
}

function normalizeDailyAdsPowerId(value) {
  const normalized = String(value || "")
    .trim()
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

function getSortableDailyRank(value, fallbackIndex) {
  const numericRank = Number(value);
  if (Number.isFinite(numericRank) && numericRank >= 0) {
    return numericRank;
  }
  return 1000000 + fallbackIndex;
}

function normalizeDailyRows(rows) {
  return (rows || [])
    .map((row, index) => {
      const username = String(row && row.username || "").trim();
      const model = String(row && (row.last_known_model || row.model) || "").trim();
      const profileId = normalizeDailyAdsPowerId(row && (row.last_known_adspower_id || row.profile_id));
      const sourceRank = getSortableDailyRank(row && row.source_rank, index);
      return {
        username,
        last_known_model: model || "Unknown",
        last_known_adspower_id: profileId,
        source_rank: sourceRank,
      };
    })
    .filter((row) => row.last_known_adspower_id);
}

function getDailyRowsSignature(rows) {
  return JSON.stringify(
    (rows || []).map((row) => [
      String(row && row.username || "").trim(),
      String(row && row.last_known_model || row && row.model || "").trim(),
      normalizeDailyAdsPowerId(row && (row.last_known_adspower_id || row.profile_id)),
      getSortableDailyRank(row && row.source_rank, 0),
    ])
  );
}

function syncLatestDailyRows(rows) {
  const normalizedRows = normalizeDailyRows(rows || []);
  const nextSignature = getDailyRowsSignature(normalizedRows);
  if (nextSignature === lastDailyRowsDataSignature) {
    return false;
  }

  latestDailyRows = normalizedRows;
  lastDailyRowsDataSignature = nextSignature;
  lastDailyRenderSignature = "";
  return true;
}

const DEFAULT_ACCOUNTS_PER_HOUR = 7;

function normalizeOutfitStyle(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "mixed") return "mix";
  return ["default", "mix", "casual", "sexy", "custom"].includes(normalized) ? normalized : "default";
}

function updatePopupCustomPresetVisibility() {
  const field = document.getElementById("popupCustomPresetField");
  if (!field) return;
  const styleSelect = document.getElementById("popupOutfitStyle");
  field.style.display = styleSelect && normalizeOutfitStyle(styleSelect.value) === "custom" ? "" : "none";
}

function normalizeAccountsPerHour(value) {
  const parsed = parseFloat(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return DEFAULT_ACCOUNTS_PER_HOUR;
  }
  // Clamp to a sane range and keep at most one decimal place.
  return Math.min(1000, Math.round(parsed * 10) / 10);
}

function activeAccountsPerHour() {
  const perHourInput = document.getElementById("dailyAccountsPerHourInput");
  const stored = latestScrapeConfig && latestScrapeConfig.dailyAccountsPerHour;
  return normalizeAccountsPerHour(
    (perHourInput && perHourInput.value) || stored
  );
}

function getDailyUpdateStats(rows, startAdspowerId, topAdspowerIdOverride, accountsPerHour) {
  const perHour = normalizeAccountsPerHour(accountsPerHour);
  const normalizedStartId = String(startAdspowerId || "").trim();
  const normalizedStartIdLower = normalizedStartId.toLowerCase();
  const normalizedTopOverride = normalizeDailyAdsPowerId(topAdspowerIdOverride);
  const normalizedTopOverrideLower = normalizedTopOverride.toLowerCase();
  const candidates = normalizeDailyRows(rows)
    .map((row, index) => ({
      row,
      sourceRank: getSortableDailyRank(row && row.source_rank, index),
      index,
      profileId: normalizeDailyAdsPowerId(row && row.last_known_adspower_id),
    }))
    .filter((entry) => entry.profileId)
    .sort((a, b) => {
      if (a.sourceRank !== b.sourceRank) {
        return a.sourceRank - b.sourceRank;
      }
      return a.index - b.index;
    });

  if (!candidates.length) {
    return {
      found: false,
      total: 0,
      topId: "",
      startId: normalizedStartId,
      counts: {},
      rangeRows: [],
      message: "No SnapBoard rows with valid AdsPower IDs are available.",
    };
  }

  const detectedTopId = candidates[0].profileId;
  const topIndex = normalizedTopOverride
    ? candidates.findIndex((entry) => entry.profileId.toLowerCase() === normalizedTopOverrideLower)
    : 0;
  const hasManualTop = Boolean(normalizedTopOverride);

  if (hasManualTop && topIndex === -1) {
    return {
      found: false,
      total: 0,
      topId: detectedTopId,
      startId: normalizedStartId,
      counts: {},
      rangeRows: [],
      message: "Top AdsPower ID was not found in the current SnapBoard rows.",
    };
  }

  const resolvedTopIndex = topIndex >= 0 ? topIndex : 0;
  const topId = candidates[resolvedTopIndex].profileId;
  if (!normalizedStartId) {
    return {
      found: false,
      total: 0,
      topId,
      startId: "",
      counts: {},
      rangeRows: [],
      message: "Enter a start AdsPower ID.",
    };
  }

  const endIndex = candidates.findIndex((entry) => entry.profileId.toLowerCase() === normalizedStartIdLower);
  if (endIndex === -1) {
    return {
      found: false,
      total: 0,
      topId,
      startId: normalizedStartId,
      counts: {},
      rangeRows: [],
      message: "Start AdsPower ID was not found in the current SnapBoard rows.",
    };
  }

  if (endIndex < resolvedTopIndex) {
    return {
      found: false,
      total: 0,
      topId,
      startId: normalizedStartId,
      counts: {},
      rangeRows: [],
      message: "Start AdsPower ID is above the selected Top AdsPower ID.",
    };
  }

  const rangeRows = candidates.slice(resolvedTopIndex, endIndex + 1).map((entry) => entry.row);
  const counts = rangeRows.reduce((accumulator, row) => {
    const model = String(row && row.last_known_model || "").trim() || "Unknown";
    accumulator[model] = (accumulator[model] || 0) + 1;
    return accumulator;
  }, {});
  const expectedHours = rangeRows.length / perHour;

  return {
    found: true,
    total: rangeRows.length,
    topId,
    startId: normalizedStartId,
    counts,
    expectedHours,
    accountsPerHour: perHour,
    rangeRows,
    message: `Counting from ${normalizedStartId} up to current top ${topId}.`,
  };
}

function buildDailyReportText(stats) {
  if (!stats || !stats.found) {
    return "";
  }

  const lines = [
    "Daily Report",
    `Total working hours: ${stats.expectedHours.toFixed(1)} hours`,
    "",
    `✅ Snapchat created accounts: ${stats.total}`,
  ].concat(
    Object.entries(stats.counts)
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([model, count]) => `${model}: ${count}`)
  );

  return lines.join("\n");
}

function renderDailyUpdatePanel(rows, scrapeConfig) {
  const input = document.getElementById("dailyStartAdspowerIdInput");
  const topInput = document.getElementById("dailyTopAdspowerIdInput");
  const perHourInput = document.getElementById("dailyAccountsPerHourInput");
  const summaryElement = document.getElementById("dailyUpdateSummary");
  const previewElement = document.getElementById("dailyUpdatePreview");
  if (!input || !topInput || !summaryElement || !previewElement) {
    return;
  }

  const storedStartId = String(scrapeConfig && scrapeConfig.dailyStartAdspowerId || "").trim();
  if (document.activeElement !== input) {
    input.value = storedStartId;
  }

  const storedPerHour = normalizeAccountsPerHour(scrapeConfig && scrapeConfig.dailyAccountsPerHour);
  if (perHourInput && document.activeElement !== perHourInput) {
    perHourInput.value = String(storedPerHour);
  }
  const activePerHour = perHourInput
    ? normalizeAccountsPerHour(perHourInput.value || storedPerHour)
    : storedPerHour;

  const manualTopInput = String(topInput.value || "").trim();
  const stats = getDailyUpdateStats(rows, input.value || storedStartId, manualTopInput, activePerHour);
  if (document.activeElement !== topInput) {
    topInput.value = stats.topId || "";
  }
  summaryElement.textContent = stats.found
    ? `${stats.total} total | top ${stats.topId}`
    : stats.message;

  const previewLines = stats.found
    ? [
        `Top ID: ${stats.topId}`,
        `Start ID: ${stats.startId}`,
        `Total: ${stats.total}`,
        `Accounts/hour: ${stats.accountsPerHour}`,
        `Expected hour: ${stats.expectedHours.toFixed(2)}`,
        "",
      ].concat(
        Object.entries(stats.counts)
          .sort((a, b) => a[0].localeCompare(b[0]))
          .map(([model, count]) => `${model}: ${count}`)
      )
    : [];

  const signature = `${stats.found}|${stats.total}|${stats.topId}|${stats.startId}|${stats.accountsPerHour}|${previewLines.join("\n")}`;
  if (signature === lastDailyRenderSignature) {
    return;
  }
  lastDailyRenderSignature = signature;

  previewElement.textContent = previewLines.length
    ? previewLines.join("\n")
    : latestDailyRows.length
      ? "Enter a start AdsPower ID to calculate the daily update."
      : "Open SnapBoard and click Refresh Rows to load the latest AdsPower IDs.";
}

function finishDailyRowsRefresh(ok, message, onComplete) {
  dailyRowsRefreshInFlight = false;
  lastDailyRowsRefreshAt = Date.now();
  if (typeof onComplete === "function") {
    onComplete(ok, message || "");
  }
}

function refreshDailyRowsFromActiveTab(onComplete, shouldShowError, options = {}) {
  const { force = false, saveToStorage = true } = options;
  if (dailyRowsRefreshInFlight && !force) {
    if (typeof onComplete === "function") {
      onComplete(false, "Daily rows refresh already in progress.");
    }
    return;
  }

  dailyRowsRefreshInFlight = true;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const activeTab = tabs && tabs.length ? tabs[0] : null;
    const activeUrl = String((activeTab && activeTab.url) || "");

    if (!activeTab || !activeTab.id || activeUrl.indexOf("snapboard.onrender.com") === -1) {
      if (shouldShowError) {
        document.getElementById("dailyUpdateStatusLine").textContent = "Open a SnapBoard tab to refresh daily update rows.";
      }
      finishDailyRowsRefresh(false, "SnapBoard tab is not active.", onComplete);
      return;
    }

    // Daily Update needs every row in the SnapBoard table, not just the
    // top N. Ignore popupRowLimit (which caps at 200 for the bot's regular
    // top-row detection) and ask the content script for an effectively
    // unbounded count so the very last row is included.
    const requestedCount = 100000;
    chrome.tabs.sendMessage(activeTab.id, { type: "NYX_GET_DAILY_UPDATE_ROWS", count: requestedCount }, (response) => {
      if (chrome.runtime.lastError) {
        if (shouldShowError) {
          document.getElementById("dailyUpdateStatusLine").textContent = "Could not reach SnapBoard. Refresh the tab and try again.";
        }
        finishDailyRowsRefresh(false, "Could not reach SnapBoard.", onComplete);
        return;
      }

      if (!response || !response.ok) {
        if (shouldShowError) {
          document.getElementById("dailyUpdateStatusLine").textContent = (response && response.error) || "Could not load daily update rows.";
        }
        finishDailyRowsRefresh(false, (response && response.error) || "Could not load rows.", onComplete);
        return;
      }

      const normalizedRows = normalizeDailyRows(response.rows || []);
      if (normalizedRows.length || !latestDailyRows.length) {
        syncLatestDailyRows(normalizedRows);
        renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
      }

      if (!saveToStorage) {
        finishDailyRowsRefresh(true, "", onComplete);
        return;
      }

      chrome.runtime.sendMessage({ type: "NYX_SCRAPE_CAPTURE_ROWS", rows: normalizedRows }, (captureResponse) => {
        if (captureResponse && captureResponse.ok && Array.isArray(captureResponse.rows)) {
          syncLatestDailyRows(captureResponse.rows);
          renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
        }
        finishDailyRowsRefresh(true, "", onComplete);
      });
    });
  });
}

function maybeAutoRefreshDailyRows(force = false) {
  const now = Date.now();
  if (dailyRowsRefreshInFlight) {
    return;
  }
  if (!force && latestDailyRows.length && now - lastDailyRowsRefreshAt < DAILY_ROWS_AUTO_REFRESH_MS) {
    return;
  }
  refreshDailyRowsFromActiveTab(() => {}, false, { force, saveToStorage: true });
}

function saveDailyStartAdspowerId() {
  const statusLine = document.getElementById("dailyUpdateStatusLine");
  const startId = String(document.getElementById("dailyStartAdspowerIdInput").value || "").trim();
  chrome.runtime.sendMessage({ type: "NYX_SCRAPE_SAVE_CONFIG", config: { dailyStartAdspowerId: startId } }, (response) => {
    if (!response || !response.ok) {
      statusLine.textContent = (response && response.error) || "Could not save the start AdsPower ID.";
      return;
    }
    latestScrapeConfig = response.config || latestScrapeConfig;
    statusLine.textContent = "Saved daily update start AdsPower ID.";
    renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
  });
}

function openScrapePage() {
  chrome.runtime.sendMessage({ type: "NYX_SCRAPE_OPEN_PAGE" }, (response) => {
    if (!response || !response.ok) {
      document.getElementById("dailyUpdateStatusLine").textContent = (response && response.error) || "Could not open scrape page.";
    }
  });
}

function runDailyRangeScrape() {
  const statusLine = document.getElementById("dailyUpdateStatusLine");
  statusLine.textContent = "Preparing daily range scrape...";

  refreshDailyRowsFromActiveTab((ok) => {
    if (!ok && !latestDailyRows.length) {
      statusLine.textContent = "Open a SnapBoard tab first, then click Scrape.";
      return;
    }

    const startId = String(document.getElementById("dailyStartAdspowerIdInput").value || "").trim();
    const manualTopId = String(document.getElementById("dailyTopAdspowerIdInput").value || "").trim();
    const stats = getDailyUpdateStats(latestDailyRows, startId, manualTopId, activeAccountsPerHour());
    if (!stats.found) {
      statusLine.textContent = stats.message;
      return;
    }

    chrome.runtime.sendMessage({ type: "NYX_SCRAPE_SAVE_CONFIG", config: { dailyStartAdspowerId: startId } }, () => {
      chrome.runtime.sendMessage({ type: "NYX_SCRAPE_OPEN_PAGE" }, (openResponse) => {
        if (!openResponse || !openResponse.ok) {
          statusLine.textContent = (openResponse && openResponse.error) || "Could not open scrape page.";
          return;
        }

        chrome.runtime.sendMessage({ type: "NYX_SCRAPE_START_RANGE", rows: stats.rangeRows }, (response) => {
          if (!response || !response.ok) {
            statusLine.textContent = (response && response.error) || "Could not start daily range scrape.";
            return;
          }
          statusLine.textContent = `Started scraping ${stats.total} profile(s) from ${stats.startId} up to ${stats.topId}.`;
          refreshPopupStatus();
        });
      });
    });
  }, true);
}

function scrollToPopupSection(sectionId) {
  const section = document.getElementById(sectionId);
  if (!section) {
    return;
  }
  section.scrollIntoView({ behavior: "smooth", block: "start" });
}

// --- Nyx Scrape (independent popup section) ---------------------------------
function renderNyxScrapeSection(scrapeStatus) {
  const rs = (scrapeStatus && scrapeStatus.runnerState) || {};
  const status = String(rs.status || "idle");
  const total = Number(rs.total || 0);
  const completed = Number(rs.completed || 0);
  const active = total > 0 || completed > 0;

  const pauseBtn = document.getElementById("nyxScrapePauseButton");
  const resumeBtn = document.getElementById("nyxScrapeResumeButton");
  if (pauseBtn) { pauseBtn.style.display = status === "running" ? "" : "none"; }
  if (resumeBtn) { resumeBtn.style.display = status === "paused" ? "" : "none"; }

  const summaryEl = document.getElementById("nyxScrapeSummary");
  if (summaryEl) {
    summaryEl.textContent = active
      ? `State: ${status} · ${completed}/${total} checked · Has Bitmoji ${Number(rs.has_bitmoji || 0)} · No Bitmoji ${Number(rs.no_bitmoji || 0)} · Not Found ${Number(rs.not_found || 0)} · Unknown ${Number(rs.unknown || 0)}`
      : "Scrape is idle.";
  }

  const statusLineEl = document.getElementById("nyxScrapeStatusLine");
  if (statusLineEl) {
    if (status === "running") {
      statusLineEl.textContent = `Scanning… ${completed}/${total}.`;
    } else if (status === "paused") {
      statusLineEl.textContent = `Paused at ${completed}/${total}. Click Resume.`;
    } else if (!active) {
      statusLineEl.textContent = "Open a SnapBoard tab, then click Scan All.";
    } else {
      statusLineEl.textContent = `Done. Checked ${completed} of ${total}.`;
    }
  }
}

function scanAllSnapboardRows() {
  const statusLine = document.getElementById("nyxScrapeStatusLine");
  statusLine.textContent = "Reading all SnapBoard rows…";
  // Capture every row from the active SnapBoard tab into storage, then scan all.
  refreshDailyRowsFromActiveTab((ok) => {
    if (!ok && !latestDailyRows.length) {
      statusLine.textContent = "Open a SnapBoard tab first, then click Scan All.";
      return;
    }
    chrome.runtime.sendMessage({ type: "NYX_SCRAPE_START_ALL" }, (response) => {
      if (!response || !response.ok) {
        statusLine.textContent = (response && response.error) || "Could not start the scan.";
        return;
      }
      const total = Number((response.runnerState && response.runnerState.total) || 0);
      statusLine.textContent = `Scanning all ${total} SnapBoard row(s)…`;
    });
  }, false, { force: true, saveToStorage: true });
}

function nyxScrapeRunnerAction(action, busyMsg, doneMsg) {
  const statusLine = document.getElementById("nyxScrapeStatusLine");
  statusLine.textContent = busyMsg;
  chrome.runtime.sendMessage({ type: "NYX_SCRAPE_RUNNER_ACTION", action }, (response) => {
    if (!response || !response.ok) {
      statusLine.textContent = (response && response.error) || ("Could not " + action + " the scan.");
      return;
    }
    statusLine.textContent = doneMsg;
  });
}

function clearNyxScrapeData() {
  const statusLine = document.getElementById("nyxScrapeStatusLine");
  statusLine.textContent = "Clearing scrape data…";
  chrome.runtime.sendMessage({ type: "NYX_SCRAPE_CLEAR_ALL" }, (response) => {
    statusLine.textContent = response && response.ok
      ? "Scrape data cleared."
      : ((response && response.error) || "Could not clear scrape data.");
  });
}

function getBotStateMeta(runnerStatus) {
  if (!runnerStatus || runnerStatus.unavailable) {
    return { key: "offline", label: "Offline", title: "NyxSuite disconnected" };
  }

  const bot = runnerStatus.bot || {};
  const rawState = String(bot.state || "stopped").trim().toLowerCase();
  const stateKey = ["running", "paused", "waiting", "stopped"].includes(rawState)
    ? rawState
    : "stopped";
  const labels = {
    running: "Running",
    paused: "Paused",
    waiting: "Waiting",
    stopped: "Stopped",
  };

  return {
    key: stateKey,
    label: labels[stateKey] || "Stopped",
    title: String(bot.detail || labels[stateKey] || "Nyx runner state"),
  };
}

function updateBotStateIndicator(runnerStatus) {
  const panel = document.getElementById("botControlsPanel");
  const actionRow = document.getElementById("botActionRow");
  const pill = document.getElementById("botStatePill");
  const text = document.getElementById("botStateText");
  if (!panel || !actionRow || !pill || !text) {
    return;
  }

  const meta = getBotStateMeta(runnerStatus);
  const stateClasses = [
    "bot-state-running",
    "bot-state-paused",
    "bot-state-waiting",
    "bot-state-stopped",
    "bot-state-offline",
  ];
  panel.classList.remove(...stateClasses);
  actionRow.classList.remove(...stateClasses);
  panel.classList.add(`bot-state-${meta.key}`);
  actionRow.classList.add(`bot-state-${meta.key}`);
  pill.title = meta.title;
  text.textContent = meta.label;
  updateBotActionButtons(meta);
}

function updateBotActionButtons(meta) {
  const startStopButton = document.getElementById("startStopBotButton");
  const pauseResumeButton = document.getElementById("pauseResumeBotButton");
  if (!startStopButton || !pauseResumeButton) {
    return;
  }

  const isOffline = meta.key === "offline";
  const isActive = ["running", "waiting", "paused"].includes(meta.key);
  const isPaused = meta.key === "paused";
  const startStopAction = isActive ? "stop" : "start";
  const pauseResumeAction = isPaused ? "resume" : "pause";

  startStopButton.dataset.action = startStopAction;
  startStopButton.title = `${startStopAction === "stop" ? "Stop" : "Start"} Nyx`;
  startStopButton.querySelector(".bot-action-label").textContent = startStopAction === "stop" ? "Stop" : "Start";
  startStopButton.querySelector(".bot-action-icon").textContent = startStopAction === "stop" ? "\u25a0" : "\u25b6";
  startStopButton.disabled = isOffline;
  startStopButton.classList.toggle("bot-action-stop-active", startStopAction === "stop");

  pauseResumeButton.dataset.action = pauseResumeAction;
  pauseResumeButton.title = `${pauseResumeAction === "resume" ? "Resume" : "Pause"} Nyx`;
  pauseResumeButton.querySelector(".bot-action-label").textContent = pauseResumeAction === "resume" ? "Resume" : "Pause";
  pauseResumeButton.querySelector(".bot-action-icon").textContent = pauseResumeAction === "resume" ? "\u25b6" : "\u275a\u275a";
  pauseResumeButton.disabled = isOffline;
  pauseResumeButton.classList.toggle("bot-action-resume-active", pauseResumeAction === "resume");
}

function automationSpeedToPercent(value) {
  const parsed = Number(value);
  const speed = Number.isFinite(parsed) ? parsed : 1;
  return Math.max(5, Math.min(100, Math.round(speed * 50)));
}

function setAutomationSpeedDisplay(value) {
  const label = document.getElementById("popupAutomationSpeedLabel");
  if (!label) {
    return;
  }
  const percent = Math.max(5, Math.min(100, Math.round(Number(value) || 50)));
  label.textContent = `${percent}%`;
}

function getPopupAutomationSpeedValue() {
  const speedInput = document.getElementById("popupAutomationSpeed");
  const percent = Math.max(5, Math.min(100, Math.round(Number(speedInput && speedInput.value) || 50)));
  return Math.max(0.1, Math.min(2.0, Math.round((percent / 50) * 100) / 100));
}

function renderRunnerStatus(runnerStatus) {
  const runnerLine = document.getElementById("runnerLine");
  const runnerStatusBlock = document.getElementById("runnerStatus");
  const runnerDot = document.getElementById("runnerDot");
  const runnerConnectionText = document.getElementById("runnerConnectionText");

  updateBotStateIndicator(runnerStatus);

  if (!runnerStatus || runnerStatus.unavailable) {
    const message = "NyxSuite is not connected. Turn on NyxSuite to start the bridge.";
    runnerDot.classList.remove("runner-dot-online");
    runnerDot.classList.add("runner-dot-offline");
    runnerConnectionText.textContent = "NyxSuite disconnected";
    runnerLine.textContent = runnerStatus && runnerStatus.error ? runnerStatus.error : message;
    runnerStatusBlock.textContent = message;
    return;
  }

  const bot = runnerStatus.bot || {};
  const counts = runnerStatus.counts || {};
  const config = runnerStatus.config || {};
  const signature = [
    bot.state || "",
    bot.detail || "",
    counts.pending || 0,
    counts.running || 0,
    counts.failed || 0,
    counts.done || 0,
    config.pending_threshold || "",
    config.max_parallel_profiles || "",
    config.automation_speed || "",
  ].join("||");

  if (signature === lastRunnerStatusSignature) {
    return;
  }
  lastRunnerStatusSignature = signature;

  runnerDot.classList.remove("runner-dot-offline");
  runnerDot.classList.add("runner-dot-online");
  runnerConnectionText.textContent = "NyxSuite connected";
  runnerLine.textContent = bot.detail || "Nyx runner connected.";
  runnerStatusBlock.textContent = [
    `State: ${bot.state || "-"}`,
    `Pending: ${counts.pending || 0}`,
    `Running: ${counts.running || 0}`,
    `Failed: ${counts.failed || 0}`,
    `Done: ${counts.done || 0}`,
    `Start threshold: ${config.pending_threshold || "-"}`,
    `Parallel: ${config.max_parallel_profiles || "-"}`,
    `Speed: ${automationSpeedToPercent(config.automation_speed)}%`,
  ].join("\n");

}

function applyPopupStatusSnapshot(status) {
  const lastSeen = document.getElementById("lastSeen");
  const syncLine = document.getElementById("syncLine");
  const safeStatus = status || {};
  const config = safeStatus.config || {};
  const pendingEntries = safeStatus.pendingEntries || [];
  const lastSeenEntries = safeStatus.lastSeenEntries || [];
  const lastSync = safeStatus.lastSync || null;
const runnerStatus = safeStatus.runnerStatus || {};
  const runnerConfig = runnerStatus.config || {};
  const eventLog = safeStatus.eventLog || [];
  const scrapeStatus = safeStatus.scrapeStatus || {};
  const scrapeConfig = scrapeStatus && scrapeStatus.config ? scrapeStatus.config : {};
  const scrapeRows = scrapeStatus && Array.isArray(scrapeStatus.snapboardRows) ? scrapeStatus.snapboardRows : [];

  const popupSettingsSignature = [
    config.enabled !== false,
    normalizeOutfitStyle(runnerConfig.outfit_style),
    config.rowLimit || 100,
    runnerConfig.pending_threshold || 1,
    runnerConfig.max_parallel_profiles || 5,
    automationSpeedToPercent(runnerConfig.automation_speed),
    runnerConfig.hair_randomizer_enabled === true,
  ].join("|");
  if (popupSettingsSignature !== lastPopupSettingsSignature) {
    lastPopupSettingsSignature = popupSettingsSignature;
    document.getElementById("popupOutfitStyle").value = normalizeOutfitStyle(runnerConfig.outfit_style);
    document.getElementById("popupRowLimit").value = config.rowLimit || 100;
    document.getElementById("popupPendingThreshold").value = runnerConfig.pending_threshold || 1;
    document.getElementById("popupMaxParallel").value = runnerConfig.max_parallel_profiles || 5;
    const speedInput = document.getElementById("popupAutomationSpeed");
    const speedPercent = automationSpeedToPercent(runnerConfig.automation_speed);
    if (speedInput && document.activeElement !== speedInput) {
      speedInput.value = speedPercent;
    }
    setAutomationSpeedDisplay(speedInput ? speedInput.value : speedPercent);
    document.getElementById("popupHairRandomizer").checked = runnerConfig.hair_randomizer_enabled === true;
  }
  updatePopupCustomPresetVisibility();

  applyPrimaryStatus(config.enabled === false ? "Nyx is off." : `${pendingEntries.length} pending to sync from extension.`);

  if (lastSeen) {
    const nextLastSeenSignature = lastSeenEntries.map((entry) => `${entry.profile_id}|${entry.model}`).join("\n");
    if (nextLastSeenSignature !== lastLastSeenSignature) {
      lastLastSeenSignature = nextLastSeenSignature;
      lastSeen.textContent = lastSeenEntries.length
        ? lastSeenEntries.map((entry) => `${entry.profile_id} | ${entry.model}`).join("\n")
        : "No dashboard rows detected yet.";
    }
  }

  const nextSyncSignature = lastSync
    ? `${lastSync.syncedAt || ""}|${lastSync.message || ""}|${lastSync.failed === true}|${lastSync.count || 0}`
    : "empty";
  if (nextSyncSignature !== lastSyncSignature) {
    lastSyncSignature = nextSyncSignature;
    if (!lastSync) {
      syncLine.textContent = "No sync has run yet.";
    } else {
      syncLine.textContent = `${lastSync.failed ? "Last sync failed" : "Last sync ok"} at ${new Date(lastSync.syncedAt).toLocaleString()}${lastSync.message ? `: ${lastSync.message}` : ""}`;
    }
  }

  renderRunnerStatus(runnerStatus);
  renderEventLog(eventLog);
  if (scrapeConfig && Object.keys(scrapeConfig).length) {
    latestScrapeConfig = scrapeConfig;
  }
  if (scrapeRows.length) {
    syncLatestDailyRows(scrapeRows);
  }
  renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
  renderNyxScrapeSection(scrapeStatus);
  persistLastRunnerStatus(runnerStatus);
  if (!latestDailyRows.length || Date.now() - lastDailyRowsRefreshAt > DAILY_ROWS_AUTO_REFRESH_MS) {
    maybeAutoRefreshDailyRows(false);
  }
}

// Persist the last-known runner status so a future popup open can paint it
// immediately instead of waiting for the first live refresh. Fire-and-forget.
function persistLastRunnerStatus(runnerStatus) {
  if (!runnerStatus || (!runnerStatus.bot && !runnerStatus.unavailable)) return;
  try {
    chrome.storage.local.set({
      [LAST_RUNNER_STATUS_KEY]: { runnerStatus, at: Date.now() },
    });
  } catch (error) {
    /* storage is best-effort */
  }
}

// Paint the last-known cached status immediately on open, then the live refresh
// overwrites it. Falls back to the disconnected hint if nothing was cached.
function renderStoredRunnerStatus() {
  chrome.storage.local.get(LAST_RUNNER_STATUS_KEY, (data) => {
    const entry = data && data[LAST_RUNNER_STATUS_KEY];
    if (entry && entry.runnerStatus) {
      renderRunnerStatus(entry.runnerStatus);
    }
  });
}

function refreshPopupStatus(statusMessage, options = {}) {
  const { onComplete, force = false } = options;
  if (statusMessage) {
    setPrimaryStatus(statusMessage, 1500);
  }

  const diagStart = performance.now();
  chrome.runtime.sendMessage({ type: "NYX_GET_STATUS", force }, (response) => {
    diagTiming("popup.status", diagStart);
    if (!response || !response.ok) {
      updateBotStateIndicator({ unavailable: true });
      setPrimaryStatus((response && response.error) || "Could not load Nyx status.", 2500);
      if (typeof onComplete === "function") {
        onComplete(false);
      }
      return;
    }

    applyPopupStatusSnapshot(response.status || {});
    if (typeof onComplete === "function") {
      onComplete(true);
    }
  });
}

function scheduleLiveStatusReconnect() {
  if (popupLiveReconnectTimer) {
    window.clearTimeout(popupLiveReconnectTimer);
  }
  popupLiveReconnectTimer = window.setTimeout(() => {
    popupLiveReconnectTimer = null;
    connectLiveStatus();
  }, 600);
}

function connectLiveStatus() {
  if (popupLivePort) {
    return;
  }

  try {
    popupLivePort = chrome.runtime.connect({ name: "nyx-popup-live" });
  } catch (error) {
    scheduleLiveStatusReconnect();
    return;
  }

  popupLivePort.onMessage.addListener((message) => {
    if (!message) {
      return;
    }
    if (message.type === "status") {
      applyPopupStatusSnapshot(message.status || {});
      return;
    }
    if (message.type === "status-error") {
      updateBotStateIndicator({ unavailable: true });
      applyPrimaryStatus(message.error || "Could not load Nyx status.");
    }
  });

  popupLivePort.onDisconnect.addListener(() => {
    popupLivePort = null;
    if (!document.hidden) {
      scheduleLiveStatusReconnect();
    }
  });
}

function handleHeaderRefreshClick() {
  if (isHeaderRefreshing) {
    return;
  }

  setHeaderRefreshLoading(true);
  refreshPopupStatus("Refreshing Nyx status...", {
    force: true,
    onComplete() {
      setHeaderRefreshLoading(false);
    },
  });
  if (popupLivePort) {
    try {
      popupLivePort.postMessage({ type: "refresh" });
    } catch (error) {
    }
  }
}

function savePopupSettings(options = {}) {
  const statusMessage = options.statusMessage === undefined ? "Saving dashboard settings..." : options.statusMessage;
  const successMessage = options.successMessage || "Dashboard settings saved.";
  if (statusMessage) {
    setPrimaryStatus(statusMessage, 2000);
  }

  chrome.runtime.sendMessage(
    {
      type: "NYX_SAVE_CONFIG",
      localApiUrl: undefined,
      localToken: undefined,
      enabled: true,
outfitStyle: normalizeOutfitStyle(document.getElementById("popupOutfitStyle").value),
      rowLimit: document.getElementById("popupRowLimit").value,
      pendingThreshold: document.getElementById("popupPendingThreshold").value,
      maxParallelProfiles: document.getElementById("popupMaxParallel").value,
      automationSpeed: getPopupAutomationSpeedValue(),
      hairRandomizerEnabled: document.getElementById("popupHairRandomizer").checked,
    },
    (response) => {
      if (!response || !response.ok) {
        setPrimaryStatus((response && response.error) || "Could not save popup settings.", 2500);
        return;
      }

      refreshPopupStatus(successMessage);
    }
  );
}

function schedulePopupSettingsSave() {
  popupSettingsDirty = true;
  if (popupSettingsSaveTimer) {
    window.clearTimeout(popupSettingsSaveTimer);
  }
  popupSettingsSaveTimer = window.setTimeout(() => {
    popupSettingsSaveTimer = null;
    popupSettingsDirty = false;
    savePopupSettings({
      statusMessage: "Applying dashboard setting...",
      successMessage: "Dashboard setting applied.",
    });
  }, 250);
}

function flushPopupSettingsSave() {
  if (!popupSettingsDirty && !popupSettingsSaveTimer) {
    return;
  }
  if (popupSettingsSaveTimer) {
    window.clearTimeout(popupSettingsSaveTimer);
    popupSettingsSaveTimer = null;
  }
  popupSettingsDirty = false;
  savePopupSettings({
    statusMessage: "",
    successMessage: "",
  });
}

function runBotAction(action, statusMessage, successFormatter) {
  setPrimaryStatus(statusMessage, 2000);

  chrome.runtime.sendMessage({ type: "NYX_BOT_ACTION", action }, (response) => {
    if (!response || !response.ok) {
      setPrimaryStatus((response && response.error) || "Nyx action failed.", 2500);
      return;
    }

    const payload = response.payload || {};
    refreshPopupStatus(typeof successFormatter === "function" ? successFormatter(payload) : (payload.message || "Nyx action completed."));
  });
}

function warmupAllInaccessibleFromSnapboard() {
  setPrimaryStatus("Changing inaccessible SnapBoard rows to Warm Up...", 2000);

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const activeTab = tabs && tabs.length ? tabs[0] : null;
    const activeUrl = String((activeTab && activeTab.url) || "");

    if (!activeTab || !activeTab.id || !activeUrl.includes("snapboard.onrender.com")) {
      setPrimaryStatus("Open a SnapBoard tab first, then use Warm Up All.", 2500);
      return;
    }

    chrome.tabs.sendMessage(activeTab.id, { type: "NYX_WARMUP_ALL_INACCESSIBLE" }, (response) => {
      if (chrome.runtime.lastError) {
        setPrimaryStatus("Could not reach SnapBoard. Refresh the tab and try again.", 2500);
        return;
      }

      if (!response || !response.ok) {
        setPrimaryStatus((response && response.error) || "Warm Up All failed.", 2500);
        return;
      }

      setPrimaryStatus(`Changed ${Number(response.updated || 0)} row(s) to Warm Up.`, 2500);
    });
  });
}

function removeMissingProfileRow() {
  setPrimaryStatus("Removing missing profile rows...", 2000);
  chrome.runtime.sendMessage({ type: "NYX_REMOVE_MISSING_PROFILE" }, (response) => {
    if (!response || !response.ok) {
      setPrimaryStatus((response && response.error) || "Could not remove missing profile rows.", 2500);
      return;
    }

    const payload = response.payload || {};
    refreshPopupStatus(payload.message || "Missing profile cleanup completed.");
  });
}

function showBitmojiIndicatorsOnSnapboard() {
  setPrimaryStatus("Showing Bitmoji status on SnapBoard...", 2000);

  getActiveSnapboardTab((activeTab) => {
    if (!activeTab) {
      setPrimaryStatus("Open a SnapBoard tab first, then click Show.", 2500);
      return;
    }

    chrome.tabs.sendMessage(activeTab.id, { type: "NYX_GET_DAILY_UPDATE_ROWS", count: 100000 }, (rowsResponse) => {
      if (chrome.runtime.lastError) {
        setPrimaryStatus("Could not reach SnapBoard. Refresh the tab and try again.", 2500);
        return;
      }

      if (!rowsResponse || !rowsResponse.ok) {
        setPrimaryStatus((rowsResponse && rowsResponse.error) || "Could not read SnapBoard rows.", 2500);
        return;
      }

      const rows = normalizeDailyRows(rowsResponse.rows || []).map((row) => ({
        username: row.username,
        profile_id: row.last_known_adspower_id,
      }));
      if (!rows.length) {
        setPrimaryStatus("No SnapBoard rows with AdsPower IDs were found.", 2500);
        return;
      }

      chrome.runtime.sendMessage({ type: "NYX_GET_BITMOJI_STATUS", rows }, (statusResponse) => {
        if (!statusResponse || !statusResponse.ok) {
          setPrimaryStatus((statusResponse && statusResponse.error) || "Could not read Bitmoji statuses.", 2500);
          return;
        }

        chrome.tabs.sendMessage(
          activeTab.id,
          { type: "NYX_SHOW_BITMOJI_INDICATORS", statuses: statusResponse.statuses || [] },
          (showResponse) => {
            if (chrome.runtime.lastError) {
              setPrimaryStatus("Could not update SnapBoard indicators. Refresh the tab and try again.", 2500);
              return;
            }
            if (!showResponse || !showResponse.ok) {
              setPrimaryStatus((showResponse && showResponse.error) || "Could not show SnapBoard indicators.", 2500);
              return;
            }
            setBitmojiShowButtonState(true);
            setPrimaryStatus(`Shown Bitmoji indicators for ${Number(showResponse.count || 0)} SnapBoard row(s).`, 2500);
          }
        );
      });
    });
  });
}

function hideBitmojiIndicatorsOnSnapboard() {
  setPrimaryStatus("Hiding Bitmoji status on SnapBoard...", 2000);

  getActiveSnapboardTab((activeTab) => {
    if (!activeTab) {
      setBitmojiShowButtonState(false);
      setPrimaryStatus("Open a SnapBoard tab first, then click Show.", 2500);
      return;
    }

    chrome.tabs.sendMessage(activeTab.id, { type: "NYX_HIDE_BITMOJI_INDICATORS" }, (response) => {
      if (chrome.runtime.lastError) {
        setPrimaryStatus("Could not reach SnapBoard. Refresh the tab and try again.", 2500);
        return;
      }
      if (!response || !response.ok) {
        setPrimaryStatus((response && response.error) || "Could not hide SnapBoard indicators.", 2500);
        return;
      }
      setBitmojiShowButtonState(false);
      setPrimaryStatus(`Hidden Bitmoji indicators from ${Number(response.count || 0)} SnapBoard row(s).`, 2500);
    });
  });
}

function toggleBitmojiIndicatorsOnSnapboard() {
  if (bitmojiIndicatorsVisible) {
    hideBitmojiIndicatorsOnSnapboard();
    return;
  }
  showBitmojiIndicatorsOnSnapboard();
}

// Nyx is always enabled now — the NyxSuite (bridge) toggle is the master switch.
// Heal any previously-disabled state so SnapBoard syncing always runs.
chrome.runtime.sendMessage({ type: "NYX_SET_ENABLED", enabled: true }, () => {});

document.getElementById("startStopBotButton").addEventListener("click", (event) => {
  const action = event.currentTarget.dataset.action === "stop" ? "stop" : "start";
  runBotAction(
    action,
    action === "stop" ? "Stopping Nyx bot..." : "Starting Nyx bot...",
    (payload) => payload.message || (action === "stop" ? "Nyx stopped." : "Nyx started.")
  );
});

document.getElementById("pauseResumeBotButton").addEventListener("click", (event) => {
  const action = event.currentTarget.dataset.action === "resume" ? "resume" : "pause";
  runBotAction(
    action,
    action === "resume" ? "Resuming Nyx bot..." : "Pausing Nyx bot...",
    (payload) => payload.message || (action === "resume" ? "Nyx resumed." : "Nyx paused.")
  );
});

document.getElementById("resetStuckButton").addEventListener("click", () => {
  runBotAction("reset_stuck", "Resetting stuck Nyx rows...", (payload) => payload.message || "Reset stuck completed.");
});

document.getElementById("rerunFailedButton").addEventListener("click", () => {
  setPrimaryStatus("Resetting failed Nyx rows...", 2000);

  chrome.runtime.sendMessage({ type: "NYX_RERUN_FAILED" }, (response) => {
    if (!response || !response.ok) {
      setPrimaryStatus((response && response.error) || "Could not rerun failed rows.", 2500);
      return;
    }

    refreshPopupStatus(response.message || `Reset ${response.count} failed row(s) to PENDING.`);
  });
});

document.getElementById("removeMissingProfileButton").addEventListener("click", removeMissingProfileRow);

document.getElementById("clearCompletedButton").addEventListener("click", () => {
  runBotAction("clear_completed", "Clearing completed Nyx rows...", (payload) => payload.message || "Completed rows cleared.");
});

document.getElementById("showBitmojiStatusButton").addEventListener("click", toggleBitmojiIndicatorsOnSnapboard);

document.getElementById("finishRemainingButton").addEventListener("click", () => {
  runBotAction("finish_remaining", "Flushing remaining Nyx rows...", (payload) => payload.message || "Nyx will finish the remaining rows.");
});
document.getElementById("warmupAllButton").addEventListener("click", warmupAllInaccessibleFromSnapboard);

document.getElementById("popupAutomationSpeed").addEventListener("input", (event) => {
  setAutomationSpeedDisplay(event.target.value);
  schedulePopupSettingsSave();
});
document.getElementById("popupAutomationSpeed").addEventListener("change", flushPopupSettingsSave);
document.getElementById("popupAutomationSpeed").addEventListener("blur", flushPopupSettingsSave);
[
  "popupOutfitStyle",
  "popupCustomPreset",
  "popupRowLimit",
  "popupPendingThreshold",
  "popupMaxParallel",
  "popupHairRandomizer",
].forEach((id) => {
  const element = document.getElementById(id);
  if (element) {
    if (element.matches('input[type="number"], input[type="text"]')) {
      element.addEventListener("input", schedulePopupSettingsSave);
    }
    element.addEventListener("change", () => {
      popupSettingsDirty = true;
      if (id === "popupOutfitStyle") updatePopupCustomPresetVisibility();
    });
    element.addEventListener("change", flushPopupSettingsSave);
    element.addEventListener("blur", flushPopupSettingsSave);
  }
});
document.getElementById("headerRefreshButton").addEventListener("click", handleHeaderRefreshClick);
let dailyStartSaveTimer = null;
const dailyStartInputEl = document.getElementById("dailyStartAdspowerIdInput");
dailyStartInputEl.addEventListener("input", () => {
  lastDailyRenderSignature = "";
  renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
  // Auto-save (debounced) so the start AdsPower ID persists without clicking
  // Save. renderDailyUpdatePanel resets the field to the last-saved value
  // whenever it isn't focused, so a periodic refresh (or blur) would otherwise
  // drop whatever was typed but not yet saved.
  if (dailyStartSaveTimer) {
    clearTimeout(dailyStartSaveTimer);
  }
  dailyStartSaveTimer = setTimeout(() => {
    const startId = String(dailyStartInputEl.value || "").trim();
    chrome.runtime.sendMessage(
      { type: "NYX_SCRAPE_SAVE_CONFIG", config: { dailyStartAdspowerId: startId } },
      (response) => {
        if (response && response.ok) {
          latestScrapeConfig = response.config || latestScrapeConfig;
        }
      }
    );
  }, 400);
});
// Persist immediately when focus leaves the field, covering the case where the
// popup is closed right after typing, before the debounce timer fires.
dailyStartInputEl.addEventListener("change", () => {
  if (dailyStartSaveTimer) {
    clearTimeout(dailyStartSaveTimer);
    dailyStartSaveTimer = null;
  }
  saveDailyStartAdspowerId();
});
document.getElementById("dailyTopAdspowerIdInput").addEventListener("input", () => {
  lastDailyRenderSignature = "";
  renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
});
let dailyPerHourSaveTimer = null;
const dailyPerHourInputEl = document.getElementById("dailyAccountsPerHourInput");
if (dailyPerHourInputEl) {
  dailyPerHourInputEl.addEventListener("input", () => {
    lastDailyRenderSignature = "";
    renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
    // Debounce the persist so every keystroke doesn't hit storage.
    if (dailyPerHourSaveTimer) {
      clearTimeout(dailyPerHourSaveTimer);
    }
    dailyPerHourSaveTimer = setTimeout(() => {
      const perHour = normalizeAccountsPerHour(dailyPerHourInputEl.value);
      chrome.runtime.sendMessage(
        { type: "NYX_SCRAPE_SAVE_CONFIG", config: { dailyAccountsPerHour: perHour } },
        (response) => {
          if (response && response.ok) {
            latestScrapeConfig = response.config || latestScrapeConfig;
          }
        }
      );
    }, 600);
  });
  // Normalize the field to the stored/clamped value once focus leaves it.
  dailyPerHourInputEl.addEventListener("blur", () => {
    dailyPerHourInputEl.value = String(activeAccountsPerHour());
    lastDailyRenderSignature = "";
    renderDailyUpdatePanel(latestDailyRows, latestScrapeConfig);
  });
}
document.getElementById("refreshDailyRowsButton").addEventListener("click", () => {
  document.getElementById("dailyUpdateStatusLine").textContent = "Refreshing daily update rows from SnapBoard...";
  refreshDailyRowsFromActiveTab((ok, message) => {
    if (ok) {
      const count = latestDailyRows.length;
      document.getElementById("dailyUpdateStatusLine").textContent = count
        ? `Loaded ${count} SnapBoard row(s) for daily update.`
        : "No valid AdsPower IDs were found in the visible SnapBoard rows.";
      return;
    }
    document.getElementById("dailyUpdateStatusLine").textContent = message || "Could not refresh daily update rows.";
  }, true, { force: true, saveToStorage: true });
});
document.getElementById("copyDailyUpdateButton").addEventListener("click", async () => {
  const startId = String(document.getElementById("dailyStartAdspowerIdInput").value || "").trim();
  const manualTopId = String(document.getElementById("dailyTopAdspowerIdInput").value || "").trim();
  const stats = getDailyUpdateStats(latestDailyRows, startId, manualTopId, activeAccountsPerHour());
  const statusLine = document.getElementById("dailyUpdateStatusLine");
  if (!stats.found) {
    statusLine.textContent = stats.message || "Daily report is not ready yet.";
    return;
  }

  const reportText = buildDailyReportText(stats);
  try {
    await navigator.clipboard.writeText(reportText);
    statusLine.textContent = "Daily report copied.";
  } catch (error) {
    statusLine.textContent = "Could not copy daily report.";
  }
});
document.getElementById("saveDailyStartButton").addEventListener("click", saveDailyStartAdspowerId);
document.getElementById("dailyScrapeButton").addEventListener("click", runDailyRangeScrape);
document.getElementById("openScrapePageButton").addEventListener("click", openScrapePage);
document.getElementById("nyxScanAllButton").addEventListener("click", scanAllSnapboardRows);
document.getElementById("nyxScrapePauseButton").addEventListener("click", () => nyxScrapeRunnerAction("pause", "Pausing scan…", "Scan paused."));
document.getElementById("nyxScrapeResumeButton").addEventListener("click", () => nyxScrapeRunnerAction("resume", "Resuming scan…", "Scan resumed."));
document.getElementById("nyxScrapeStopButton").addEventListener("click", () => nyxScrapeRunnerAction("stop", "Stopping scan…", "Scan stopped."));
document.getElementById("nyxScrapeClearButton").addEventListener("click", clearNyxScrapeData);
document.getElementById("nyxScrapeOpenPageButton").addEventListener("click", openScrapePage);
document.getElementById("jumpDailyUpdateButton").addEventListener("click", () => {
  scrollToPopupSection("dailyUpdateSection");
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    flushPopupSettingsSave();
    return;
  }
  if (!document.hidden) {
    connectLiveStatus();
    refreshPopupStatus("Refreshing Nyx queue...", { force: true });
    syncBitmojiShowButtonState();
    maybeAutoRefreshDailyRows(true);
  }
});

window.addEventListener("beforeunload", () => {
  flushPopupSettingsSave();
  if (popupLiveReconnectTimer) {
    window.clearTimeout(popupLiveReconnectTimer);
    popupLiveReconnectTimer = null;
  }
  if (popupLivePort) {
    try {
      popupLivePort.disconnect();
    } catch (error) {
    }
    popupLivePort = null;
  }
});

// ---------- Bridge controls ----------
const DASHBOARD_URL = "http://127.0.0.1:8870/";
const NYX_API_URL = "http://127.0.0.1:8865";

// Open the dashboard, reusing an existing tab so we never stack duplicate pages.
function focusOrCreateDashboard(url, setUrl) {
  chrome.tabs.query({}, (tabs) => {
    const existing = (tabs || []).find((t) => t.url && t.url.indexOf(DASHBOARD_URL) === 0);
    if (existing) {
      const upd = { active: true };
      if (setUrl) upd.url = url;
      chrome.tabs.update(existing.id, upd);
      if (existing.windowId != null) chrome.windows.update(existing.windowId, { focused: true });
    } else {
      chrome.tabs.create({ url: url });
    }
  });
}
function openWebApp() { focusOrCreateDashboard(DASHBOARD_URL + "#nyx", true); }

// Silent token fetch from the unauthenticated /token endpoint, so SnapBoard sync
// keeps working without a manual "Connect" step.
function fetchTokenFromApi(onDone) {
  fetch(NYX_API_URL + "/token")
    .then((r) => r.json())
    .then((data) => {
      if (data && data.ok && data.token) {
        chrome.runtime.sendMessage(
          { type: "NYX_SAVE_CONFIG", localToken: data.token, localApiUrl: NYX_API_URL },
          () => { if (onDone) onDone(true); }
        );
      } else if (onDone) { onDone(false); }
    })
    .catch(() => { if (onDone) onDone(false); });
}

function startBridgeViaNative() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage("com.nyxsuite.agent", { type: "start_agent" }, (resp) => {
        if (chrome.runtime.lastError) { resolve({ ok: false, error: chrome.runtime.lastError.message }); return; }
        resolve(resp || { ok: false });
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });
}

// Lightweight liveness probe for the native host itself (does NOT start the
// bridge). Used to tell "host genuinely not registered" apart from a transient
// start failure.
function pingNativeHost() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage("com.nyxsuite.agent", { type: "ping" }, (resp) => {
        if (chrome.runtime.lastError) { resolve({ ok: false, error: chrome.runtime.lastError.message }); return; }
        resolve(resp || { ok: false });
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });
}

// True only when the error clearly means the native messaging host is not
// registered for this browser ("Specified native messaging host not found",
// forbidden origin, etc.) — NOT a blank error or a host-crash, which are
// transient and must not send the user back to the installer.
function isHostMissingError(errStr) {
  const err = String(errStr || "").toLowerCase();
  if (!err) return false;
  return err.includes("not found")
      || err.includes("forbidden")
      || err.includes("not registered")
      || err.includes("native messaging host");
}

function checkAgentRunning() {
  return fetch(DASHBOARD_URL, { method: "HEAD", cache: "no-store" }).then(() => true).catch(() => false);
}

async function stopBridge() {
  const token = await new Promise((res) =>
    chrome.storage.sync.get(["nyxConfig"], (d) => res(((d && d.nyxConfig) || {}).localToken || "")));
  try {
    await fetch(DASHBOARD_URL + "bridge/shutdown", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Nyx-Token": token },
      body: JSON.stringify({ token: token }),
    });
  } catch (e) { /* already down */ }
}

// Single on/off toggle for the NyxSuite background bridge server. This is the
// master switch — the runner connection indicator mirrors its state.
const bridgeToggle = document.getElementById("bridgePowerToggle");
const nyxsuiteToggleText = document.getElementById("nyxsuiteToggleText");

function setNyxsuiteIndicator(running, busy) {
  if (nyxsuiteToggleText) {
    nyxsuiteToggleText.textContent = busy ? "NyxSuite …" : (running ? "NyxSuite on" : "NyxSuite off");
  }
  if (busy) return;
  const dot = document.getElementById("runnerDot");
  const text = document.getElementById("runnerConnectionText");
  if (dot) {
    dot.classList.toggle("runner-dot-online", !!running);
    dot.classList.toggle("runner-dot-offline", !running);
  }
  if (text) text.textContent = running ? "NyxSuite connected" : "NyxSuite disconnected";
}

function renderBridgeToggle(running, busy) {
  bridgeToggle.dataset.running = running ? "true" : "false";
  bridgeToggle.disabled = !!busy;
  if (!busy) bridgeToggle.checked = !!running;
  setNyxsuiteIndicator(running, busy);
}

function refreshBridgeToggle() {
  if (bridgeToggle.dataset.busy === "true") return;
  checkAgentRunning().then((running) => {
    if (bridgeToggle.dataset.busy === "true") return;
    renderBridgeToggle(running, false);
    if (running) fetchTokenFromApi();
  });
}

bridgeToggle.addEventListener("change", async () => {
  const wantOn = bridgeToggle.checked;
  bridgeToggle.dataset.busy = "true";
  bridgeToggle.disabled = true;
  setNyxsuiteIndicator(wantOn, true);
  if (!wantOn) {
    await stopBridge();
    setPrimaryStatus("NyxSuite stopping…", 2500);
    setTimeout(() => { bridgeToggle.dataset.busy = "false"; refreshBridgeToggle(); }, 2500);
  } else {
    setPrimaryStatus("Starting NyxSuite…", 2500);
    // Already up (e.g. toggled off then on faster than it shut down)? Just
    // reflect it instead of trying to start a duplicate.
    if (await checkAgentRunning()) {
      bridgeToggle.dataset.busy = "false";
      renderBridgeToggle(true, false);
      fetchTokenFromApi(() => refreshPopupStatus("NyxSuite running.", { force: true }));
      return;
    }
    const r = await startBridgeViaNative();
    if (!r.ok) {
      // Decide whether the host is genuinely unregistered or this is just a
      // transient start failure. The bridge registers the native host on its
      // first launch and the registration survives restarts, so a failure here
      // after a previous run is almost always transient (the just-stopped
      // bridge is still releasing its single-instance lock). Confirm with a
      // ping before sending the user back to the installer.
      let hostMissing = isHostMissingError(r.error);
      if (!hostMissing) {
        const probe = await pingNativeHost();
        hostMissing = !probe.ok && isHostMissingError(probe.error);
      }
      if (hostMissing) {
        setPrimaryStatus("NyxSuite isn't installed for this browser yet. Double-click run_nyx_suite once (opening Setup…).", 6000);
        openSetupInstall();
        bridgeToggle.dataset.busy = "false";
        renderBridgeToggle(false, false);
        return;
      }
      // Host is registered; the bridge may still be coming up (or a previous
      // instance is finishing). Fall through to polling instead of falsely
      // claiming it isn't installed.
    }
    let tries = 0;
    const poll = setInterval(async () => {
      tries += 1;
      const up = await checkAgentRunning();
      if (up) {
        clearInterval(poll);
        bridgeToggle.dataset.busy = "false";
        renderBridgeToggle(true, false);
        fetchTokenFromApi(() => refreshPopupStatus("NyxSuite running.", { force: true }));
      } else if (tries > 25) {
        clearInterval(poll);
        bridgeToggle.dataset.busy = "false";
        renderBridgeToggle(false, false);
        setPrimaryStatus("NyxSuite didn't come online — try Setup & Install.", 4500);
      }
    }, 800);
  }
});

document.getElementById("openWebAppButton").addEventListener("click", openWebApp);

// Setup & Install — reuse the dashboard tab if the bridge is up; else open the
// bundled setup page, which can start the bridge and guides first-run.
function openSetupInstall() {
  checkAgentRunning().then((running) => {
    if (running) focusOrCreateDashboard(DASHBOARD_URL + "#setup", true);
    else chrome.tabs.create({ url: chrome.runtime.getURL("setup.html") });
  });
}
document.getElementById("setupInstallButton").addEventListener("click", openSetupInstall);

// After an update the extension files change on disk, but Chrome keeps running
// the old loaded copy until you reload it. Detect that (bridge version newer
// than the loaded manifest) and prompt a one-click reload.
function compareVersions(a, b) {
  const pa = String(a || "0").split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b || "0").split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const da = pa[i] || 0, db = pb[i] || 0;
    if (da !== db) return da < db ? -1 : 1;
  }
  return 0;
}

function checkExtensionReload() {
  const banner = document.getElementById("extReloadBanner");
  if (!banner) return;
  const loaded = chrome.runtime.getManifest().version;
  chrome.storage.sync.get(["nyxConfig"], (d) => {
    const token = ((d && d.nyxConfig) || {}).localToken || "";
    fetch(DASHBOARD_URL + "bridge/status?token=" + encodeURIComponent(token))
      .then((r) => r.json())
      .then((s) => {
        const onDisk = s && s.bridge ? s.bridge.version : "";
        if (onDisk && compareVersions(loaded, onDisk) < 0) {
          const v = banner.querySelector("#extReloadVersion");
          if (v) v.textContent = "v" + onDisk;
          banner.hidden = false;
        } else {
          banner.hidden = true;
        }
      })
      .catch(() => {});
  });
}

const openExtensionsLink = document.getElementById("openExtensionsLink");
if (openExtensionsLink) {
  openExtensionsLink.addEventListener("click", (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: "chrome://extensions" });
  });
}

// Init — reflect the bridge state and fetch the token if it's running. We never
// auto-start the bridge and never auto-open the dashboard.
refreshBridgeToggle();
setInterval(refreshBridgeToggle, 5000);
checkExtensionReload();

document.getElementById("runnerStatusCard").open = false;
syncBitmojiShowButtonState();
renderStoredRunnerStatus();
connectLiveStatus();
maybeAutoRefreshDailyRows(true);
