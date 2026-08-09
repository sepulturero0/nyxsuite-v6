(function () {
  var debounceTimer = null;
  var CONFIG_KEY = "nyxifyConfig";
  // Separate local-storage key for the SnapBoard sign-in credentials — kept out
  // of the synced nyxifyConfig (and the runner config) so the password isn't
  // pushed around. The options page writes it; auto-login types it when the
  // board is signed out and the fields aren't already filled.
  var SNAPBOARD_LOGIN_KEY = "nyxifySnapboardLogin";
  var otpPollTimer = null;
  var otpPollInFlight = false;
  var proxyRotatePollTimer = null;
  var usernameUpdatePollTimer = null;
  var usernameUpdatePollInFlight = false;
  var adspowerUpdatePollTimer = null;
  var adspowerUpdatePollInFlight = false;
  var adspowerNameUpdatePollTimer = null;
  var adspowerNameUpdatePollInFlight = false;
  var statusUpdatePollTimer = null;
  var statusUpdatePollInFlight = false;
  var snapboardRefreshPollTimer = null;
  var snapboardRefreshPollInFlight = false;
  var configCache = null;
  var configCacheAt = 0;
  var ROW_SCAN_DEBOUNCE_MS = 800;
  var OTP_POLL_INTERVAL_MS = 900;
  var PROXY_ROTATE_POLL_INTERVAL_MS = 1500;
  var USERNAME_UPDATE_POLL_INTERVAL_MS = 1200;
  var SNAPBOARD_REFRESH_POLL_INTERVAL_MS = 1200;
  var SNAPBOARD_REFRESH_ACK_KEY = "nyxifySnapboardRefreshAck";
  var OTP_FETCH_TIMEOUT_MS = 60000;
  var EMAIL_FETCH_TIMEOUT_MS = 45000;
  // SnapBoard's "get new email / number" (redo) buttons enforce a ~60s cooldown
  // after each order. Wait a little past that so a reorder click isn't a no-op.
  var REDO_COOLDOWN_MAX_WAIT_MS = 72000;
  var OTP_CLICK_RETRY_INTERVAL_MS = 2500;
  var PROXY_ROTATE_WAIT_MS = 22000;
  var PROXY_ROTATE_CLICK_ATTEMPTS = 4;
  var bridgePort = null;
  var autoFillPollTimer = null;
  var AUTO_FILL_POLL_MS = 5000;
  var providerLockTimer = null;
  var PROVIDER_LOCK_POLL_MS = 1500;
  var autoLoginTimer = null;
  var AUTO_LOGIN_POLL_MS = 2000;
  var AUTO_LOGIN_MAX_ATTEMPTS = 5;
  var AUTO_LOGIN_MIN_GAP_MS = 4000;
  var autoLoginAttempts = 0;
  var autoLoginLastAttemptAt = 0;

  function toArray(nodeList) {
    return Array.prototype.slice.call(nodeList || []);
  }

  function normalizeText(value) {
    return String(value || "").trim();
  }

  function normalizeComparableEmail(value) {
    return normalizeText(value).toLowerCase();
  }

  function normalizeComparablePhone(value) {
    return normalizeText(value).replace(/[^\d]/g, "");
  }

  function extractEmailFromText(value) {
    var match = normalizeText(value).match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
    return match ? match[0] : "";
  }

  function extractPhoneFromText(value) {
    var text = stripLeadingNonAlphanumeric(normalizeText(value));
    var match = text.match(/\+?\d[\d\s().-]{7,}\d/);
    return match ? normalizeText(match[0]).replace(/[^\d+]/g, "") : "";
  }

  function normalizeHeaderKey(value) {
    return normalizeText(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  }

  function getRowCells(row) {
    return toArray(row ? row.children : []).filter(function (cell) {
      return cell && (cell.tagName === "TD" || cell.tagName === "TH");
    });
  }

  function stripLeadingNonAlphanumeric(value) {
    return String(value || "").replace(/^[^a-zA-Z0-9]+/, "").trim();
  }

  function readCellText(cell) {
    var inputLike;
    var selectedOption;
    var infoSpan;
    var credSpan;

    if (!cell) {
      return "";
    }

    inputLike = cell.querySelector("input, textarea, select");
    if (inputLike) {
      if (inputLike.tagName === "SELECT") {
        selectedOption = inputLike.options[inputLike.selectedIndex];
        return normalizeText((selectedOption && selectedOption.textContent) || inputLike.value || "");
      }
      return normalizeText(inputLike.value || "");
    }

    // Email badge: <span class="cred-email ...">📧 email@gmail.com</span>
    credSpan = cell.querySelector(".cred-email, [class*='email-badge']");
    if (credSpan) {
      return stripLeadingNonAlphanumeric(credSpan.textContent || "");
    }

    // Info cells (proxy, email): <span class="info-text">value</span>
    // Use this instead of full cell text to avoid picking up button labels (↻, 🔄 Check Code)
    infoSpan = cell.querySelector(".info-text");
    if (infoSpan) {
      return stripLeadingNonAlphanumeric(infoSpan.textContent || "");
    }

    return normalizeText(cell.textContent || "");
  }

  function getTableHeaderMap(root) {
    var table = root ? root.closest("table") : null;
    var headerCells = toArray((table && table.querySelectorAll("thead th")) || document.querySelectorAll("thead th, table th"));
    var headerMap = {};

    headerCells.forEach(function (cell, index) {
      var key = normalizeHeaderKey(cell.textContent || "");
      if (key && headerMap[key] === undefined) {
        headerMap[key] = index;
      }
    });

    return headerMap;
  }

  function findHeaderIndex(headerMap, aliases) {
    var i;
    var alias;
    for (i = 0; i < aliases.length; i += 1) {
      alias = normalizeHeaderKey(aliases[i]);
      if (Object.prototype.hasOwnProperty.call(headerMap, alias)) {
        return headerMap[alias];
      }
    }
    return -1;
  }

  function readValueFromAliases(row, headerMap, aliases) {
    var index = findHeaderIndex(headerMap, aliases);
    var cells = getRowCells(row);
    if (index >= 0 && cells[index]) {
      return readCellText(cells[index]);
    }
    return "";
  }

  function readEmailFromRowId(rowId) {
    var row = document.querySelector('tr[data-id="' + rowId + '"]');
    var headerMap;
    var email;
    if (!row) {
      return "";
    }
    headerMap = getTableHeaderMap(row);
    email = extractEmailFromText(readValueFromAliases(row, headerMap, ["email", "gmail", "google", "mail", "google mail"]));
    if (email) {
      return email;
    }
    return extractEmailFromText(row.innerText || row.textContent || "");
  }

  function readPhoneFromRowId(rowId) {
    var row = document.querySelector('tr[data-id="' + rowId + '"]');
    var headerMap;
    var phone;
    if (!row) {
      return "";
    }
    headerMap = getTableHeaderMap(row);
    phone = extractPhoneFromText(readValueFromAliases(row, headerMap, ["phone", "phone number", "sms", "mobile", "number"]));
    if (phone) {
      return phone;
    }
    return "";
  }

  function rowMatchesExpectedEmail(rowId, expectedEmail) {
    var expected = normalizeComparableEmail(expectedEmail);
    var row;
    var actual;

    if (!expected) {
      return false;
    }

    row = document.querySelector('tr[data-id="' + rowId + '"]');
    if (!row) {
      return false;
    }

    actual = normalizeComparableEmail(readEmailFromRowId(rowId));
    if (actual && actual === expected) {
      return true;
    }

    return normalizeComparableEmail(row.innerText || row.textContent || "").indexOf(expected) >= 0;
  }

  function rowMatchesExpectedPhone(rowId, expectedPhone) {
    var expected = normalizeComparablePhone(expectedPhone);
    var actual;

    if (!expected) {
      return false;
    }

    actual = normalizeComparablePhone(readPhoneFromRowId(rowId));
    if (actual && actual.endsWith(expected.slice(-10))) {
      return true;
    }

    return normalizeComparablePhone(
      (document.querySelector('tr[data-id="' + rowId + '"]') || {}).innerText || ""
    ).indexOf(expected.slice(-10)) >= 0;
  }

  function getRowRoot() {
    return document.querySelector("#tableBody")
      || document.querySelector("tbody")
      || document.querySelector("[data-table-body]")
      || document.body;
  }

  function getCandidateRows(root) {
    return toArray(root.querySelectorAll("tr[data-id], tr, [role='row'], [class*='table-row' i], [class*='row' i]")).filter(function (row) {
      return getRowCells(row).length >= 2 || !!row.querySelector("input, select, textarea");
    });
  }

  function getStableRowKey(row, ipAddress, model) {
    var rowId = normalizeText(
      (row && row.getAttribute && row.getAttribute("data-id"))
      || (row && row.dataset && row.dataset.id)
      || (row && row.id)
    );

    if (rowId) {
      return "snapboard:" + rowId.toLowerCase();
    }

    return ("snapboard:" + normalizeText(ipAddress) + "|" + normalizeText(model)).toLowerCase();
  }

  function extractRows(rowLimit) {
    var root = getRowRoot();
    var headerMap = getTableHeaderMap(root);
    var rows = getCandidateRows(root)
      .sort(function (a, b) {
        return a.getBoundingClientRect().top - b.getBoundingClientRect().top;
      })
      .filter(function (row) {
        return row && row.matches && row.matches("tr[data-id]");
      });
    var limit = Math.max(1, parseInt(rowLimit, 10) || 20);

    if (!rows.length) {
      return [];
    }

    return rows.slice(0, limit).map(function (row) {
      var model = readValueFromAliases(row, headerMap, ["model", "face model"]);
      var ipAddress = readValueFromAliases(row, headerMap, ["ip", "ip address", "proxy", "proxy ip", "proxy address"]);
      var proxyAddress = readValueFromAliases(row, headerMap, ["proxy", "proxy address", "ip", "ip address"]) || ipAddress;
      var adspowerId = readValueFromAliases(row, headerMap, ["adspower", "adspower id", "profile id"]);
      var username = readValueFromAliases(row, headerMap, ["username", "snap username", "snapchat username", "user", "snap user"]);
      var email = extractEmailFromText(readValueFromAliases(row, headerMap, ["email", "gmail", "google", "mail", "google mail"]));
      var password = readValueFromAliases(row, headerMap, ["password", "pass", "snap password", "snapchat password", "account password"]);

      if (!model || !ipAddress || adspowerId) {
        return null;
      }

      return {
        row_key: getStableRowKey(row, ipAddress, model),
        model: model,
        ip_address: ipAddress,
        proxy_address: proxyAddress,
        username: username,
        email: email,
        password: password,
        adspower_id: adspowerId,
      };
    }).filter(Boolean);
  }

  function readStatusFromRow(row, headerMap) {
    var status = readValueFromAliases(row, headerMap, ["status", "state"]);
    if (status) {
      return status;
    }
    var select = row.querySelector("select.cell-select.status-select")
      || row.querySelector("select.status-select");
    if (!select) {
      return "";
    }
    var selectedOption = select.options[select.selectedIndex];
    return normalizeText((selectedOption && (selectedOption.value || selectedOption.textContent)) || select.value || "");
  }

  function extractSnapboardStatusRows(rowLimit) {
    var root = getRowRoot();
    var headerMap = getTableHeaderMap(root);
    var rows = getCandidateRows(root)
      .sort(function (a, b) {
        return a.getBoundingClientRect().top - b.getBoundingClientRect().top;
      })
      .filter(function (row) {
        return row && row.matches && row.matches("tr[data-id]");
      });
    var limit = Math.max(1, parseInt(rowLimit, 10) || 100000);

    return rows.slice(0, limit).map(function (row, index) {
      var model = readValueFromAliases(row, headerMap, ["model", "face model"]);
      var ipAddress = readValueFromAliases(row, headerMap, ["ip", "ip address", "proxy", "proxy ip", "proxy address"]);
      var proxyAddress = readValueFromAliases(row, headerMap, ["proxy", "proxy address", "ip", "ip address"]) || ipAddress;
      var adspowerId = readValueFromAliases(row, headerMap, ["adspower", "adspower id", "profile id"]);
      var username = readValueFromAliases(row, headerMap, ["username", "snap username", "snapchat username", "user", "snap user"]);
      var email = extractEmailFromText(readValueFromAliases(row, headerMap, ["email", "gmail", "google", "mail", "google mail"]));
      var password = readValueFromAliases(row, headerMap, ["password", "pass", "snap password", "snapchat password", "account password"]);
      var status = readStatusFromRow(row, headerMap);

      if (!model || !ipAddress) {
        return null;
      }

      return {
        row_key: getStableRowKey(row, ipAddress, model),
        row_id: normalizeText(row.getAttribute("data-id") || ""),
        model: model,
        ip_address: ipAddress,
        proxy_address: proxyAddress,
        username: username,
        email: email,
        password: password,
        adspower_id: adspowerId,
        status: status,
        source_rank: index,
      };
    }).filter(Boolean);
  }

  function getRowLimit(callback) {
    chrome.storage.sync.get(CONFIG_KEY, function (result) {
      var config = result && result[CONFIG_KEY] ? result[CONFIG_KEY] : {};
      var parsed = parseInt(config.rowLimit, 10);
      callback(Number.isFinite(parsed) && parsed > 0 ? parsed : 20);
    });
  }

  function sendRows() {
    getRowLimit(function (rowLimit) {
      var rows = extractRows(rowLimit);
      var statusRows = extractSnapboardStatusRows(100000);
      if (statusRows.length) {
        chrome.runtime.sendMessage({
          type: "NYXIFY_SNAPBOARD_STATUS_ROWS",
          rows: statusRows,
        });
      }
      if (!rows.length) {
        return;
      }
      chrome.runtime.sendMessage({
        type: "NYXIFY_DETECTED_ROWS",
        rows: rows,
      });
    });
  }

  function queueScan() {
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(sendRows, ROW_SCAN_DEBOUNCE_MS);
  }

  function getStoredConfig() {
    return new Promise(function (resolve) {
      var now = Date.now();
      if (configCache && (now - configCacheAt) < 5000) {
        resolve(configCache);
        return;
      }
      chrome.storage.sync.get(CONFIG_KEY, function (result) {
        configCache = result && result[CONFIG_KEY] ? result[CONFIG_KEY] : {};
        configCacheAt = Date.now();
        resolve(configCache);
      });
    });
  }

  function getLocalApiConfig(config) {
    return {
      localApiUrl: String((config && config.localApiUrl) || "http://127.0.0.1:8866").trim(),
      localToken: String((config && config.localToken) || "").trim(),
    };
  }

  function extractRowId(rowKey) {
    var normalized = normalizeText(rowKey);
    if (normalized.toLowerCase().indexOf("snapboard:") === 0) {
      return normalizeText(normalized.slice("snapboard:".length));
    }
    return normalized;
  }

  function connectBridgePort() {
    try {
      bridgePort = chrome.runtime.connect({ name: "nyxify-snapboard-bridge" });
      bridgePort.onDisconnect.addListener(function () {
        bridgePort = null;
        window.setTimeout(connectBridgePort, 1500);
      });
    } catch (_error) {
      bridgePort = null;
      window.setTimeout(connectBridgePort, 1500);
    }
  }

  function buttonMatchesRotateIntent(button) {
    var text = normalizeText(button.innerText || button.textContent || "").toLowerCase();
    var title = normalizeText(button.getAttribute("title") || "").toLowerCase();
    var label = normalizeText(button.getAttribute("aria-label") || "").toLowerCase();
    var dataAction = normalizeText(button.getAttribute("data-action") || "").toLowerCase();
    var className = normalizeText(button.className || "").toLowerCase();
    var onclickText = normalizeText(button.getAttribute("onclick") || "").toLowerCase();
    var hint = [text, title, label, dataAction, className, onclickText].join(" ");
    return hint.indexOf("rotateproxy") >= 0
      || hint.indexOf("rotate proxy") >= 0
      || hint.indexOf("new proxy") >= 0
      || hint.indexOf("refresh proxy") >= 0
      || (hint.indexOf("proxy") >= 0 && (hint.indexOf("refresh") >= 0 || hint.indexOf("reload") >= 0 || hint.indexOf("renew") >= 0 || hint.indexOf("rotate") >= 0));
  }

  function clickElement(button) {
    if (!button) {
      return false;
    }
    try {
      if (typeof button.click === "function") {
        button.click();
        return true;
      }
    } catch (_error) {}
    try {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      return true;
    } catch (_error2) {}
    return false;
  }

  function isProviderOptionActive(button) {
    if (!button) {
      return false;
    }
    return button.classList.contains("active")
      || button.getAttribute("aria-pressed") === "true"
      || normalizeText(button.getAttribute("data-active")).toLowerCase() === "true";
  }

  function findAMProviderButton() {
    return document.querySelector('button.provider-option[data-provider="accountmanager"]')
      || document.querySelector('[data-provider="accountmanager"]')
      || document.querySelector('button.provider-option[data-provider="accountsmarket"]')
      || document.querySelector('[data-provider="accountsmarket"]')
      || document.querySelector('button.provider-option[data-provider="accsmarket"]')
      || document.querySelector('[data-provider="accsmarket"]')
      || document.querySelector('button.provider-option[data-provider="am"]')
      || document.querySelector('[data-provider="am"]')
      || toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = normalizeText(node.getAttribute("onclick") || "").toLowerCase();
        var text = normalizeText(node.innerText || node.textContent || "").toLowerCase();
        return onclickText.indexOf("setemailprovider('accountmanager')") >= 0
          || onclickText.indexOf('setemailprovider("accountmanager")') >= 0
          || onclickText.indexOf("setemailprovider('accountsmarket')") >= 0
          || onclickText.indexOf('setemailprovider("accountsmarket")') >= 0
          || onclickText.indexOf("setemailprovider('accsmarket')") >= 0
          || onclickText.indexOf('setemailprovider("accsmarket")') >= 0
          || onclickText.indexOf("setemailprovider('am')") >= 0
          || onclickText.indexOf('setemailprovider("am")') >= 0
          || text === "am";
      }) || null;
  }

  function lockProviderToAM() {
    var button = findAMProviderButton();
    if (!button) {
      return false;
    }
    if (isProviderOptionActive(button)) {
      return true;
    }
    return clickElement(button);
  }

  function findG5ProviderButton() {
    return document.querySelector('button.provider-option[data-provider="gmail500"]')
      || document.querySelector('[data-provider="gmail500"]')
      || toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = normalizeText(node.getAttribute("onclick") || "").toLowerCase();
        var text = normalizeText(node.innerText || node.textContent || "").toLowerCase();
        return onclickText.indexOf("setemailprovider('gmail500')") >= 0
          || onclickText.indexOf('setemailprovider("gmail500")') >= 0
          || text === "g5";
      }) || null;
  }

  function lockProviderToG5() {
    var button = findG5ProviderButton();
    if (!button) {
      return false;
    }
    if (isProviderOptionActive(button)) {
      return true;
    }
    return clickElement(button);
  }

  function findSPProviderButton() {
    return document.querySelector('button.provider-option[data-provider="smspool"]')
      || document.querySelector('[data-provider="smspool"]')
      || document.querySelector('button.provider-option[data-provider="sms_pool"]')
      || document.querySelector('[data-provider="sms_pool"]')
      || document.querySelector('button.provider-option[data-provider="sp"]')
      || document.querySelector('[data-provider="sp"]')
      || toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = normalizeText(node.getAttribute("onclick") || "").toLowerCase();
        var text = normalizeText(node.innerText || node.textContent || "").toLowerCase();
        return onclickText.indexOf("setphoneprovider('smspool')") >= 0
          || onclickText.indexOf('setphoneprovider("smspool")') >= 0
          || onclickText.indexOf("setphoneprovider('sms_pool')") >= 0
          || onclickText.indexOf('setphoneprovider("sms_pool")') >= 0
          || onclickText.indexOf("setphoneprovider('sp')") >= 0
          || onclickText.indexOf('setphoneprovider("sp")') >= 0
          || text === "sp";
      }) || null;
  }

  function lockProviderToSP() {
    var button = findSPProviderButton();
    if (!button) {
      return false;
    }
    if (isProviderOptionActive(button)) {
      return true;
    }
    return clickElement(button);
  }

  function findTVProviderButton() {
    return document.querySelector('button.provider-option[data-provider="textverified"]')
      || document.querySelector('[data-provider="textverified"]')
      || toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = normalizeText(node.getAttribute("onclick") || "").toLowerCase();
        var text = normalizeText(node.innerText || node.textContent || "").toLowerCase();
        return onclickText.indexOf("setphoneprovider('textverified')") >= 0
          || onclickText.indexOf('setphoneprovider("textverified")') >= 0
          || text === "tv";
      }) || null;
  }

  function lockProviderToTV() {
    var button = findTVProviderButton();
    if (!button) {
      return false;
    }
    if (isProviderOptionActive(button)) {
      return true;
    }
    return clickElement(button);
  }

  async function checkProviderLock() {
    var config = await getStoredConfig();
    if (config.lockG5) {
      lockProviderToG5();
    } else {
      lockProviderToAM();
    }
    if (config.lockTV) {
      lockProviderToTV();
    } else {
      lockProviderToSP();
    }
  }

  function scheduleProviderLock() {
    window.setTimeout(function () {
      checkProviderLock();
    }, 250);
  }

  function startProviderLockPoll() {
    if (providerLockTimer) {
      return;
    }
    providerLockTimer = window.setInterval(function () {
      checkProviderLock();
    }, PROVIDER_LOCK_POLL_MS);
    scheduleProviderLock();
  }

  function isLoginScreenVisible() {
    var screen = document.getElementById("loginScreen");
    if (!screen) {
      return false;
    }
    // SnapBoard toggles the overlay with an inline display style
    // ('flex' when logged out, 'none' when signed in); fall back to the
    // computed style if the inline one was never set.
    var display = normalizeText(screen.style && screen.style.display);
    if (!display) {
      try {
        display = window.getComputedStyle(screen).display;
      } catch (_error) {
        display = "";
      }
    }
    return display !== "" && display.toLowerCase() !== "none";
  }

  function findSignInButton() {
    var form = document.getElementById("loginForm");
    var scope = form || document;
    return scope.querySelector('button[type="submit"]')
      || toArray(scope.querySelectorAll("button")).find(function (node) {
        var text = normalizeText(node.innerText || node.textContent || "").toLowerCase();
        return text === "sign in" || text === "log in" || text === "login";
      }) || null;
  }

  function loginCredentialsPrefilled() {
    // Require BOTH fields before submitting so we never post an empty password.
    // Chrome's password manager often can't/ won't fill this form (the "name"
    // field isn't a recognized username), and even when it fills visually the
    // password value can be unreadable to JS — which is why the autofill-only
    // approach failed. fillLoginCredentialsIfNeeded() populates blanks from the
    // stored credentials first, so this gate then passes.
    var name = document.getElementById("loginName");
    var pass = document.getElementById("loginPassword");
    return !!(name && normalizeText(name.value) && pass && pass.value);
  }

  function getSnapboardLoginCredentials() {
    return new Promise(function (resolve) {
      try {
        chrome.storage.local.get(SNAPBOARD_LOGIN_KEY, function (result) {
          var stored = (result && result[SNAPBOARD_LOGIN_KEY]) || {};
          resolve({
            name: normalizeText(stored.name || ""),
            password: String(stored.password || ""),
          });
        });
      } catch (_error) {
        resolve({ name: "", password: "" });
      }
    });
  }

  // Fill the sign-in fields from the stored credentials, but only the blanks —
  // "uses it if not filled yet when signed out" — so anything Chrome did fill
  // (or a value the user typed) is respected.
  async function fillLoginCredentialsIfNeeded() {
    var nameField = document.getElementById("loginName");
    var passField = document.getElementById("loginPassword");
    if (!nameField || !passField) {
      return;
    }
    var needName = !normalizeText(nameField.value);
    var needPass = !passField.value;
    if (!needName && !needPass) {
      return;
    }
    var creds = await getSnapboardLoginCredentials();
    if (needName && creds.name) {
      setElementValue(nameField, creds.name);
    }
    if (needPass && creds.password) {
      setElementValue(passField, creds.password);
    }
  }

  function submitLoginForm(button) {
    // The login handler is a form 'submit' listener, so requestSubmit() (which
    // fires submit) is more reliable than a bare button.click(); fall back to a
    // click where requestSubmit is unavailable.
    var form = document.getElementById("loginForm");
    if (form && typeof form.requestSubmit === "function") {
      try {
        form.requestSubmit(button || undefined);
        return true;
      } catch (_error) {}
    }
    return clickElement(button);
  }

  async function attemptAutoLogin() {
    if (!isLoginScreenVisible()) {
      autoLoginAttempts = 0;
      return;
    }
    if (autoLoginAttempts >= AUTO_LOGIN_MAX_ATTEMPTS) {
      return;
    }
    var now = Date.now();
    if (now - autoLoginLastAttemptAt < AUTO_LOGIN_MIN_GAP_MS) {
      return;
    }
    await fillLoginCredentialsIfNeeded();
    if (!loginCredentialsPrefilled()) {
      return;
    }
    var button = findSignInButton();
    if (!button) {
      return;
    }
    autoLoginAttempts += 1;
    autoLoginLastAttemptAt = now;
    submitLoginForm(button);
  }

  function startAutoLoginPoll() {
    if (autoLoginTimer) {
      return;
    }
    autoLoginTimer = window.setInterval(function () { attemptAutoLogin(); }, AUTO_LOGIN_POLL_MS);
    window.setTimeout(function () { attemptAutoLogin(); }, 500);
  }

  // Drive + await a re-login on demand. A logged-out SnapBoard silently drops
  // its rows and stops handing out emails/numbers/OTPs, so a fetch that comes
  // back empty asks us (via the background bridge) to get the session back
  // before it retries. Types the stored credentials into any blank field, then
  // submits — bypassing attemptAutoLogin's attempt-cap/min-gap guards but still
  // only once both fields are populated so we never post an empty login.
  // Returns true once the login overlay is gone (or was never showing).
  async function ensureSnapboardLoggedIn(maxWaitMs) {
    if (!isLoginScreenVisible()) {
      return true;
    }
    autoLoginAttempts = 0;  // let the background-poll auto-login keep trying too
    var deadline = Date.now() + (maxWaitMs || 15000);
    while (isLoginScreenVisible() && Date.now() < deadline) {
      await fillLoginCredentialsIfNeeded();
      if (loginCredentialsPrefilled()) {
        var button = findSignInButton();
        if (button) {
          submitLoginForm(button);
        }
      }
      await sleep(1500);
    }
    return !isLoginScreenVisible();
  }

  function getCodeTextForRow(rowId, displayAttribute) {
    var selectors = [
      '.twofa-code-display[' + displayAttribute + '="' + rowId + '"] .twofa-code',
      '.twofa-code-display[' + displayAttribute + '="' + rowId + '"]',
      '[' + displayAttribute + '="' + rowId + '"] .twofa-code',
      '[' + displayAttribute + '="' + rowId + '"]',
    ];
    var i;
    for (i = 0; i < selectors.length; i += 1) {
      var node = document.querySelector(selectors[i]);
      if (!node) {
        continue;
      }
      var text = normalizeText(node.innerText || node.textContent || "");
      var match = text.match(/\b(\d{6})\b/);
      if (match) {
        return match[1];
      }
    }
    return "";
  }

  function getOtpTextForRow(rowId) {
    return getCodeTextForRow(rowId, "data-code-display");
  }

  function getSmsTextForRow(rowId) {
    return getCodeTextForRow(rowId, "data-sms-display");
  }

  function isVisibleElement(node) {
    var style;
    var rect;
    if (!node || node.nodeType !== 1) {
      return false;
    }
    try {
      style = window.getComputedStyle(node);
      if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) === 0) {
        return false;
      }
      rect = node.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    } catch (_error) {
      return false;
    }
  }

  function getOtpPopupCandidates() {
    var selectors = [
      ".toast",
      ".Toastify__toast",
      ".swal2-popup",
      ".alert",
      ".notification",
      ".notyf__toast",
      ".Vue-Toastification__toast",
      "[role='alert']",
      "[aria-live]",
      "[class*='toast' i]",
      "[class*='notif' i]",
      "[class*='alert' i]",
      "[class*='snackbar' i]",
    ];
    var seen = [];
    var candidates = [];

    selectors.forEach(function (selector) {
      var nodes;
      try {
        nodes = toArray(document.querySelectorAll(selector));
      } catch (_error) {
        nodes = [];
      }

      nodes.forEach(function (node) {
        var text;
        var match;
        if (seen.indexOf(node) >= 0 || !isVisibleElement(node)) {
          return;
        }
        seen.push(node);
        text = normalizeText(node.innerText || node.textContent || "");
        if (!/\bcode\b/i.test(text)) {
          return;
        }
        match = text.match(/\b(\d{6})\b/);
        if (match) {
          candidates.push({
            node: node,
            text: text,
            code: match[1],
          });
        }
      });
    });

    return candidates;
  }

  function captureOtpPopupSnapshot() {
    var snapshot = {
      nodes: [],
      texts: {},
    };

    getOtpPopupCandidates().forEach(function (candidate) {
      snapshot.nodes.push(candidate.node);
      snapshot.texts[candidate.text] = true;
    });

    return snapshot;
  }

  function getNewOtpPopupCode(snapshot) {
    var prior = snapshot || { nodes: [], texts: {} };
    var candidates = getOtpPopupCandidates();
    var i;
    var candidate;

    for (i = 0; i < candidates.length; i += 1) {
      candidate = candidates[i];
      if (prior.nodes.indexOf(candidate.node) < 0 || !prior.texts[candidate.text]) {
        return candidate.code;
      }
    }

    return "";
  }

  function clickCheckCode(rowId) {
    var button = document.querySelector('button.btn-check-code[data-check-code="' + rowId + '"]')
      || document.querySelector('button[data-check-code="' + rowId + '"]');
    if (!button) {
      button = toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = String(node.getAttribute("onclick") || "");
        return onclickText.indexOf("check2faCode") >= 0 && onclickText.indexOf(rowId) >= 0;
      }) || null;
    }
    if (!button) {
      return false;
    }
    button.click();
    return true;
  }

  function clickCheckSms(rowId) {
    var button = document.querySelector('button.btn-check-code[data-check-sms="' + rowId + '"]')
      || document.querySelector('button[data-check-sms="' + rowId + '"]');
    if (!button) {
      button = toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = String(node.getAttribute("onclick") || "");
        return onclickText.indexOf("checkSms") >= 0 && onclickText.indexOf(rowId) >= 0;
      }) || null;
    }
    if (!button) {
      return false;
    }
    button.click();
    return true;
  }

  function clickGetEmailButton(rowId) {
    var button = document.querySelector('button.btn-get-email[data-get-email="' + rowId + '"]')
      || document.querySelector('button[data-get-email="' + rowId + '"]');
    if (!button) {
      button = toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = String(node.getAttribute("onclick") || "");
        return onclickText.indexOf("get2faEmail") >= 0 && onclickText.indexOf(rowId) >= 0;
      }) || null;
    }
    if (!button) {
      return false;
    }
    return clickElement(button);
  }

  function clickGetPhoneButton(rowId) {
    var button = document.querySelector('button.btn-get-email[data-get-phone="' + rowId + '"]')
      || document.querySelector('button[data-get-phone="' + rowId + '"]');
    if (!button) {
      button = toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = String(node.getAttribute("onclick") || "");
        var title = normalizeText(node.getAttribute("title") || "").toLowerCase();
        return onclickText.indexOf("getPhone") >= 0 && onclickText.indexOf(rowId) >= 0
          || (title.indexOf("request phone") >= 0 && onclickText.indexOf(rowId) >= 0);
      }) || null;
    }
    if (!button) {
      return false;
    }
    return clickElement(button);
  }

  function findRedoEmailButton(rowId) {
    return document.querySelector('button.btn-redo-email[data-redo-email="' + rowId + '"]')
      || document.querySelector('button[data-redo-email="' + rowId + '"]')
      || toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = String(node.getAttribute("onclick") || "");
        var title = normalizeText(node.getAttribute("title") || "").toLowerCase();
        return onclickText.indexOf("redo2faEmail") >= 0 && onclickText.indexOf(rowId) >= 0
          || (title.indexOf("get new email") >= 0 && onclickText.indexOf(rowId) >= 0);
      }) || null;
  }

  function findRedoPhoneButton(rowId) {
    return document.querySelector('button.btn-redo-email[data-redo-phone="' + rowId + '"]')
      || document.querySelector('button[data-redo-phone="' + rowId + '"]')
      || toArray(document.querySelectorAll("button")).find(function (node) {
        var onclickText = String(node.getAttribute("onclick") || "");
        var title = normalizeText(node.getAttribute("title") || "").toLowerCase();
        return onclickText.indexOf("redoPhone") >= 0 && onclickText.indexOf(rowId) >= 0
          || (title.indexOf("get new number") >= 0 && onclickText.indexOf(rowId) >= 0);
      }) || null;
  }

  function clickRedoEmailButton(rowId) {
    return clickElement(findRedoEmailButton(rowId));
  }

  function clickRedoPhoneButton(rowId) {
    return clickElement(findRedoPhoneButton(rowId));
  }

  // A redo button on cooldown renders disabled with a "⏳ 45s" label; clicking
  // it does nothing. Read the remaining seconds so we can wait it out instead
  // of firing a silent no-op that looks like "the email/number never changed".
  function readRedoCooldownSeconds(button) {
    if (!button) {
      return 0;
    }
    var text = normalizeText(button.innerText || button.textContent || "");
    var match = text.match(/(\d+)\s*s/i);
    if (match) {
      var seconds = parseInt(match[1], 10);
      return isNaN(seconds) ? 0 : seconds;
    }
    // Disabled but no readable countdown — assume a full window so we still wait.
    return button.disabled ? 60 : 0;
  }

  function isRedoOnCooldown(button) {
    return !!button && (button.disabled || readRedoCooldownSeconds(button) > 0);
  }

  // Wait (bounded) for a redo button to leave its cooldown so a reorder click
  // actually lands. Re-locates the button each tick because SnapBoard re-renders
  // the row while the countdown ticks. Returns the ready button, or the latest
  // one found (possibly still on cooldown) once the cap elapses.
  async function waitForRedoReady(findButton, maxWaitMs) {
    var deadline = Date.now() + (maxWaitMs || REDO_COOLDOWN_MAX_WAIT_MS);
    var button = findButton();
    while (button && isRedoOnCooldown(button) && Date.now() < deadline) {
      await sleep(1000);
      button = findButton();
    }
    return button;
  }

  function waitForEmailForRow(rowId, timeoutMs, previousEmail) {
    return new Promise(function (resolve) {
      var startedAt = Date.now();
      var observer = null;
      var timer = null;
      var finished = false;
      var prior = normalizeComparableEmail(previousEmail);

      function cleanup(result) {
        if (finished) {
          return;
        }
        finished = true;
        if (observer) {
          observer.disconnect();
        }
        if (timer) {
          window.clearInterval(timer);
        }
        resolve(result || "");
      }

      function checkNow() {
        var email = readEmailFromRowId(rowId);
        if (email && normalizeComparableEmail(email) !== prior) {
          cleanup(email);
          return true;
        }
        if (email && !prior) {
          cleanup(email);
          return true;
        }
        if ((Date.now() - startedAt) >= timeoutMs) {
          cleanup("");
          return true;
        }
        return false;
      }

      if (checkNow()) {
        return;
      }

      var row = document.querySelector('tr[data-id="' + rowId + '"]');
      if (row && typeof MutationObserver !== "undefined") {
        observer = new MutationObserver(checkNow);
        observer.observe(row, {
          childList: true,
          subtree: true,
          characterData: true,
          attributes: true,
        });
      }

      timer = window.setInterval(checkNow, 350);
    });
  }

  function waitForPhoneForRow(rowId, timeoutMs, previousPhone) {
    return new Promise(function (resolve) {
      var startedAt = Date.now();
      var observer = null;
      var timer = null;
      var finished = false;
      var prior = normalizeText(previousPhone);

      function cleanup(result) {
        if (finished) {
          return;
        }
        finished = true;
        if (observer) {
          observer.disconnect();
        }
        if (timer) {
          window.clearInterval(timer);
        }
        resolve(result || "");
      }

      function checkNow() {
        var phone = readPhoneFromRowId(rowId);
        if (phone && phone !== prior) {
          cleanup(phone);
          return true;
        }
        if (phone && !prior) {
          cleanup(phone);
          return true;
        }
        if ((Date.now() - startedAt) >= timeoutMs) {
          cleanup("");
          return true;
        }
        return false;
      }

      if (checkNow()) {
        return;
      }

      var row = document.querySelector('tr[data-id="' + rowId + '"]');
      if (row && typeof MutationObserver !== "undefined") {
        observer = new MutationObserver(checkNow);
        observer.observe(row, {
          childList: true,
          subtree: true,
          characterData: true,
          attributes: true,
        });
      }

      timer = window.setInterval(checkNow, 350);
    });
  }

  function hasNoPendingOrderToast(kind) {
    var text = "";
    try {
      text = normalizeText(document.body ? (document.body.innerText || document.body.textContent || "") : "").toLowerCase();
    } catch (e) {
      text = "";
    }
    if (kind === "phone") {
      // "No pending phone order for this account. Request a number first."
      return text.indexOf("no pending phone order") >= 0
        || text.indexOf("no pending order") >= 0
        || text.indexOf("request a number first") >= 0
        || text.indexOf("get a number first") >= 0
        || text.indexOf("get number first") >= 0;
    }
    // "No pending email order for this account. Get email first."
    return text.indexOf("no pending email order") >= 0
      || text.indexOf("no pending order") >= 0
      || text.indexOf("get email first") >= 0;
  }

  async function requestEmailFetch(rowId, forceNew) {
    var currentEmail = readEmailFromRowId(rowId);
    if (currentEmail && !forceNew) {
      return { ok: true, email: currentEmail };
    }

    var clicked;
    if (forceNew) {
      // Reorder path: the redo button carries a ~60s cooldown. Wait it out so
      // the click actually orders a new email instead of no-opping while
      // disabled (the "email never changed → account failed" symptom).
      var redoEmail = await waitForRedoReady(function () { return findRedoEmailButton(rowId); });
      clicked = redoEmail ? clickElement(redoEmail) : false;
      if (!clicked) {
        // Fall back to Get Email so we still order one instead of failing.
        clicked = clickGetEmailButton(rowId);
      }
    } else {
      clicked = clickGetEmailButton(rowId);
      if (!clicked) {
        clicked = clickRedoEmailButton(rowId);
      }
    }
    if (!clicked) {
      return { ok: false, error: "No Get/Redo Email button found for row." };
    }

    var fetchedEmail = await waitForEmailForRow(rowId, EMAIL_FETCH_TIMEOUT_MS, currentEmail);

    // "No pending email order for this account. Get email first." — SnapBoard
    // needs an email ordered before it will hand one over. Click Get Email and
    // wait again before giving up so the Python side can keep proceeding.
    var noPendingEmailOrder = hasNoPendingOrderToast("email");
    if (!fetchedEmail && noPendingEmailOrder) {
      if (clickGetEmailButton(rowId)) {
        fetchedEmail = await waitForEmailForRow(rowId, EMAIL_FETCH_TIMEOUT_MS, currentEmail);
      }
      noPendingEmailOrder = hasNoPendingOrderToast("email");
    }

    if (!fetchedEmail) {
      return {
        ok: false,
        stale: noPendingEmailOrder,
        terminal: noPendingEmailOrder,
        no_pending_order: noPendingEmailOrder,
        error: forceNew
          ? "New email did not appear after clicking Redo Email."
          : (noPendingEmailOrder
            ? "No pending email order for this account. Get email first."
            : "Email did not appear after clicking Get Email."),
      };
    }

    queueScan();
    return { ok: true, email: fetchedEmail };
  }

  async function requestPhoneFetch(rowId, forceNew) {
    var currentPhone = readPhoneFromRowId(rowId);
    if (currentPhone && !forceNew) {
      return { ok: true, phone: currentPhone };
    }

    var clicked;
    if (forceNew) {
      // Reorder path: the redo button carries a ~60s cooldown. Wait it out so
      // the click actually orders a new number instead of no-opping while
      // disabled (the "phone never changed → account failed" symptom).
      var redoPhone = await waitForRedoReady(function () { return findRedoPhoneButton(rowId); });
      clicked = redoPhone ? clickElement(redoPhone) : false;
      if (!clicked) {
        // Fall back to Request Number so we still order one instead of failing.
        clicked = clickGetPhoneButton(rowId);
      }
    } else {
      clicked = clickGetPhoneButton(rowId);
      if (!clicked) {
        clicked = clickRedoPhoneButton(rowId);
      }
    }
    if (!clicked) {
      return { ok: false, error: "No Request/Redo Phone button found for row." };
    }

    var fetchedPhone = await waitForPhoneForRow(rowId, EMAIL_FETCH_TIMEOUT_MS, currentPhone);

    // "No pending phone order for this account. Request a number first." —
    // SnapBoard needs a number ordered before it hands one over. Mirror the
    // email path: click Request Number and wait again before giving up.
    var noPendingPhoneOrder = hasNoPendingOrderToast("phone");
    if (!fetchedPhone && noPendingPhoneOrder) {
      if (clickGetPhoneButton(rowId)) {
        fetchedPhone = await waitForPhoneForRow(rowId, EMAIL_FETCH_TIMEOUT_MS, currentPhone);
      }
      noPendingPhoneOrder = hasNoPendingOrderToast("phone");
    }

    if (!fetchedPhone) {
      return {
        ok: false,
        stale: noPendingPhoneOrder,
        terminal: noPendingPhoneOrder,
        no_pending_order: noPendingPhoneOrder,
        error: forceNew
          ? "New phone did not appear after clicking Redo Phone."
          : (noPendingPhoneOrder
            ? "No pending phone order for this account. Request a number first."
            : "Phone did not appear after clicking Request Number."),
      };
    }

    queueScan();
    return { ok: true, phone: fetchedPhone };
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, ms);
    });
  }

  function setElementValue(node, value) {
    if (!node) {
      return false;
    }
    try {
      node.focus();
      if ("value" in node) {
        var proto = node.tagName === "TEXTAREA"
          ? window.HTMLTextAreaElement.prototype
          : window.HTMLInputElement.prototype;
        var descriptor = Object.getOwnPropertyDescriptor(proto, "value");
        if (descriptor && descriptor.set) {
          descriptor.set.call(node, value);
        } else {
          node.value = value;
        }
      } else {
        node.textContent = value;
      }
      node.dispatchEvent(new Event("input", { bubbles: true }));
      node.dispatchEvent(new Event("change", { bubbles: true }));
      // Many SnapBoard cells persist on blur, not just change. Firing
      // both ensures the row updates without a manual refresh.
      try {
        node.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
      } catch (_blurError) {
        node.dispatchEvent(new Event("blur", { bubbles: true }));
      }
      try {
        node.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "Tab" }));
      } catch (_kbError) {}
      return true;
    } catch (_error) {
      return false;
    }
  }

  function buttonMatchesSaveIntent(button) {
    var text = normalizeText(button.innerText || button.textContent || "").toLowerCase();
    var title = normalizeText(button.getAttribute("title") || "").toLowerCase();
    var label = normalizeText(button.getAttribute("aria-label") || "").toLowerCase();
    var hint = [text, title, label].join(" ");
    return hint.indexOf("save") >= 0
      || hint.indexOf("update") >= 0
      || hint.indexOf("confirm") >= 0
      || hint.indexOf("done") >= 0
      || hint === "ok"
      || text === "✓";
  }

  function callPageUpdateField(rowId, field, value) {
    // Content scripts run in an isolated world and cannot invoke the page's
    // inline handlers (onchange="updateField(...)") or `window.updateField`
    // directly. Injecting a <script> tag runs in the page world, which does
    // have access to the SnapBoard app's functions and state.
    //
    // We also schedule two re-applications (~120ms and ~400ms later)
    // because SnapBoard re-renders the row from its in-memory state right
    // after updateField saves to the server, which can blank the visible
    // input until the next manual page refresh.
    try {
      var script = document.createElement("script");
      script.textContent =
        "(function(){try{" +
        "var rid=" + JSON.stringify(String(rowId)) + ";" +
        "var field=" + JSON.stringify(String(field)) + ";" +
        "var value=" + JSON.stringify(String(value)) + ";" +
        "function applyValue(){try{" +
          "var row=document.querySelector('tr[data-id=\"'+rid+'\"]');" +
          "var input=row?row.querySelector('input.input-'+field)" +
          "||row.querySelector('input[onchange*=\"'+field+'\"]'):null;" +
          "if(input&&input.value!==value){" +
            "var p=input.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;" +
            "var d=Object.getOwnPropertyDescriptor(p,'value');" +
            "if(d&&d.set){d.set.call(input,value);}else{input.value=value;}" +
            "input.dispatchEvent(new Event('input',{bubbles:true}));" +
            "input.dispatchEvent(new Event('change',{bubbles:true}));" +
            "try{input.dispatchEvent(new FocusEvent('blur',{bubbles:true}));}catch(_b){" +
            "input.dispatchEvent(new Event('blur',{bubbles:true}));}" +
          "}" +
        "}catch(_e){}}" +
        "applyValue();" +
        "if(typeof updateField==='function'){try{updateField(rid,field,value);}catch(_u){}}" +
        // Re-assert after SnapBoard's post-save re-render so the cell
        // shows the new value without requiring a refresh.
        "setTimeout(applyValue,120);" +
        "setTimeout(applyValue,400);" +
        "}catch(e){}})();";
      (document.head || document.documentElement).appendChild(script);
      script.remove();
      return true;
    } catch (_error) {
      return false;
    }
  }

  function findRowInput(rowId, selectors) {
    var row = document.querySelector('tr[data-id="' + rowId + '"]');
    if (!row) {
      return null;
    }
    for (var i = 0; i < selectors.length; i += 1) {
      var node = row.querySelector(selectors[i]);
      if (node) {
        return node;
      }
    }
    return null;
  }

  var USERNAME_INPUT_SELECTORS = [
    "input.cell-input.input-username",
    "input.input-username",
    "input[placeholder='username']",
    "input[onchange*=\"updateField\"][onchange*=\"username\"]",
  ];

  var ADSPOWER_INPUT_SELECTORS = [
    "input.cell-input.input-adspower",
    "input.input-adspower",
    "input[onchange*=\"updateField\"][onchange*=\"adspowerId\"]",
    "input[placeholder='ID']",
  ];

  var ADSPOWER_NAME_INPUT_SELECTORS = [
    "input.cell-input.input-adspowerName",
    "input.input-adspowerName",
    "input.cell-input.input-adspower-name",
    "input.input-adspower-name",
    "input[onchange*=\"updateField\"][onchange*=\"adspowerName\"]",
    "input[placeholder*='AdsPower name' i]",
  ];

  // Sync write so the bridge poll cycle (OTP, username, proxy, adspower)
  // stays snappy — a slow update here used to starve OTP auto-check.
  // callPageUpdateField schedules two re-applications at +120ms and +400ms
  // inside SnapBoard's own page world, which catches its post-save
  // re-render so the cell shows the new value without a refresh.
  function requestUsernameUpdate(rowId, username) {
    var input = findRowInput(rowId, USERNAME_INPUT_SELECTORS)
      || (function () {
        var row = document.querySelector('tr[data-id="' + rowId + '"]');
        return row ? (row.querySelector("input, textarea")) : null;
      })();
    if (!input) {
      return false;
    }

    setElementValue(input, username);
    callPageUpdateField(rowId, "username", username);

    return normalizeText(input.value || "") === normalizeText(username);
  }

  function requestAdspowerIdUpdate(rowId, adspowerId) {
    var input = findRowInput(rowId, ADSPOWER_INPUT_SELECTORS);
    if (!input) {
      return false;
    }

    setElementValue(input, adspowerId);
    callPageUpdateField(rowId, "adspowerId", adspowerId);

    return normalizeText(input.value || "") === normalizeText(adspowerId);
  }

  function requestAdspowerNameUpdate(rowId, adspowerName) {
    var input = findRowInput(rowId, ADSPOWER_NAME_INPUT_SELECTORS);
    if (!input) {
      return false;
    }

    setElementValue(input, adspowerName);
    callPageUpdateField(rowId, "adspowerName", adspowerName);

    return normalizeText(input.value || "") === normalizeText(adspowerName);
  }

  // Set a SnapBoard row's status cell (the <select class="status-select">).
  // Used to mark accounts as "Banned" when the Bitmoji bot hits a Snapchat
  // authorization error. SnapBoard attaches a change listener to the select
  // (there is no inline onchange), so dispatching a native change event after
  // setting the value persists the new status without a manual refresh.
  function setRowStatus(rowId, status) {
    var desired = normalizeText(status);
    if (!desired) {
      return false;
    }
    var row = document.querySelector('tr[data-id="' + rowId + '"]');
    if (!row) {
      return false;
    }
    var select = row.querySelector("select.cell-select.status-select")
      || row.querySelector("select.status-select")
      || toArray(row.querySelectorAll("select")).find(function (sel) {
        return toArray(sel.options || []).some(function (option) {
          return normalizeText(option.value) === desired;
        });
      });
    if (!select) {
      return false;
    }
    var hasOption = toArray(select.options || []).some(function (option) {
      return normalizeText(option.value) === desired;
    });
    if (!hasOption) {
      return false;
    }
    if (normalizeText(select.value) === desired) {
      return true;
    }
    select.value = desired;
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return normalizeText(select.value) === desired;
  }

  var proxyRotatePollInFlight = false;

  function clickRotateButton(rowId) {
    var row = document.querySelector('tr[data-id="' + rowId + '"]');
    if (!row) return false;
    var btn = row.querySelector(".btn-rotate")
      || row.querySelector('[data-action*="rotate" i]')
      || row.querySelector('[aria-label*="proxy" i]')
      || row.querySelector('[title*="proxy" i]')
      || row.querySelector('[title="Get new proxy"]')
      || toArray(row.querySelectorAll("button")).find(function (b) {
           return buttonMatchesRotateIntent(b);
         })
      || toArray(row.querySelectorAll("[role='button'], a, div")).find(function (b) {
           return buttonMatchesRotateIntent(b);
         }) || null;
    if (btn) { return clickElement(btn); }
    if (typeof window.rotateProxy === "function") { window.rotateProxy(rowId); return true; }
    return false;
  }

  function readProxyFromRow(rowId) {
    var row = document.querySelector('tr[data-id="' + rowId + '"]');
    if (!row) return "";
    var headerMap = getTableHeaderMap(row);
    return readValueFromAliases(row, headerMap, ["proxy", "proxy address", "ip address", "ip"]);
  }

  async function pollPendingProxyRotation() {
    if (proxyRotatePollInFlight) return;
    proxyRotatePollInFlight = true;
    try {
      var config = await getStoredConfig();
      var apiConfig = getLocalApiConfig(config);
      if (!apiConfig.localApiUrl) return;
      var headers = {};
      if (apiConfig.localToken) headers["X-Nyxify-Token"] = apiConfig.localToken;

      var response = await fetch(apiConfig.localApiUrl + "/proxy/rotate_pending", {
        method: "GET", headers: headers,
      });
      var payload = await response.json();
      if (!response.ok || !payload.ok || !payload.row_key) return;
      if (!payload.force && config.proxyBlockerEnabled === false && config.proxyCheckerEnabled === false) return;

      var rowKey = normalizeText(payload.row_key);
      var rowId = extractRowId(rowKey);
      if (!rowId) return;

      // Use the same robust multi-click rotate as the manual path: click the
      // rotate button up to maxClicks times and wait PROXY_ROTATE_WAIT_MS for the
      // proxy cell to actually change. The single-click / 16s wait this replaced
      // reported "did not change" when SnapBoard simply took longer than 16s to
      // swap the proxy, which read to the runner as a failed rotation.
      var maxClicks = parseInt(payload.max_clicks, 10);
      if (!(maxClicks >= 1)) maxClicks = 3;

      var result = await rotateProxyUntilChanged(rowId, PROXY_ROTATE_WAIT_MS, maxClicks);

      headers["Content-Type"] = "application/json";
      if (result && result.ok && result.proxy) {
        await fetch(apiConfig.localApiUrl + "/proxy/rotate_result", {
          method: "POST", headers: headers,
          body: JSON.stringify({ row_key: rowKey, proxy: result.proxy }),
        });
      } else {
        await fetch(apiConfig.localApiUrl + "/proxy/rotate_result", {
          method: "POST", headers: headers,
          body: JSON.stringify({ row_key: rowKey, error: (result && result.error) || "Proxy did not change after rotation" }),
        });
      }
    } catch (error) {
      return;
    } finally {
      proxyRotatePollInFlight = false;
    }
  }

  function readSnapboardRefreshAck() {
    try {
      var raw = window.sessionStorage.getItem(SNAPBOARD_REFRESH_ACK_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      return parsed && parsed.request_id ? parsed : null;
    } catch (_error) {
      return null;
    }
  }

  function writeSnapboardRefreshAck(request) {
    try {
      window.sessionStorage.setItem(SNAPBOARD_REFRESH_ACK_KEY, JSON.stringify({
        request_id: normalizeText(request && request.request_id),
        reason: normalizeText(request && request.reason),
        started_at: Date.now(),
      }));
    } catch (_error) {
    }
  }

  function clearSnapboardRefreshAck() {
    try {
      window.sessionStorage.removeItem(SNAPBOARD_REFRESH_ACK_KEY);
    } catch (_error) {
    }
  }

  async function postSnapboardRefreshResult(apiConfig, request, success, error) {
    var headers = { "Content-Type": "application/json" };
    if (apiConfig.localToken) {
      headers["X-Nyxify-Token"] = apiConfig.localToken;
    }
    var response = await fetch(apiConfig.localApiUrl + "/snapboard_refresh/result", {
      method: "POST",
      headers: headers,
      body: JSON.stringify({
        request_id: normalizeText(request && request.request_id),
        success: !!success,
        error: success ? "" : normalizeText(error),
      }),
    });
    var payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload && payload.error || "SnapBoard refresh result was not accepted.");
    }
  }

  async function completePendingSnapboardRefreshAck(apiConfig) {
    var ack = readSnapboardRefreshAck();
    if (!ack || !ack.request_id) {
      return false;
    }
    try {
      await postSnapboardRefreshResult(apiConfig, ack, true, "");
      clearSnapboardRefreshAck();
      return true;
    } catch (_error) {
      return false;
    }
  }

  async function pollPendingSnapboardRefresh() {
    if (snapboardRefreshPollInFlight) {
      return;
    }
    snapboardRefreshPollInFlight = true;

    try {
      var config = await getStoredConfig();
      var apiConfig = getLocalApiConfig(config);
      if (!apiConfig.localApiUrl) {
        return;
      }

      if (await completePendingSnapboardRefreshAck(apiConfig)) {
        return;
      }

      if (readSnapboardRefreshAck()) {
        return;
      }

      var headers = {};
      if (apiConfig.localToken) {
        headers["X-Nyxify-Token"] = apiConfig.localToken;
      }

      var response = await fetch(apiConfig.localApiUrl + "/snapboard_refresh/pending", {
        method: "GET",
        headers: headers,
      });
      var payload = await response.json();
      var request = payload && payload.request ? payload.request : null;
      if (!response.ok || !payload.ok || !request || !request.request_id) {
        return;
      }

      writeSnapboardRefreshAck(request);
      window.location.reload();
    } catch (_error) {
      return;
    } finally {
      snapboardRefreshPollInFlight = false;
    }
  }

  function waitForProxyChange(rowId, oldProxy, timeoutMs) {
    return new Promise(function (resolve) {
      var start = Date.now();
      var finished = false;
      var observer = null;
      var timer = null;

      function cleanup(result) {
        if (finished) {
          return;
        }
        finished = true;
        if (observer) {
          observer.disconnect();
        }
        if (timer) {
          window.clearInterval(timer);
        }
        resolve(result || "");
      }

      function checkNow() {
        var latest = readProxyFromRow(rowId);
        if (latest && latest !== oldProxy) {
          cleanup(latest);
          return true;
        }
        if ((Date.now() - start) >= timeoutMs) {
          cleanup("");
          return true;
        }
        return false;
      }

      if (checkNow()) {
        return;
      }

      var row = document.querySelector('tr[data-id="' + rowId + '"]');
      if (row && typeof MutationObserver !== "undefined") {
        observer = new MutationObserver(checkNow);
        observer.observe(row, {
          childList: true,
          subtree: true,
          characterData: true,
        });
      }

      timer = window.setInterval(checkNow, 350);
    });
  }

  function waitForOtpCode(rowId, timeoutMs, popupSnapshot) {
    return new Promise(function (resolve) {
      var startedAt = Date.now();
      var observer = null;
      var timer = null;
      var finished = false;

      function cleanup(result) {
        if (finished) {
          return;
        }
        finished = true;
        if (observer) {
          observer.disconnect();
        }
        if (timer) {
          window.clearInterval(timer);
        }
        resolve(result || "");
      }

      function checkNow() {
        var code = getOtpTextForRow(rowId) || getNewOtpPopupCode(popupSnapshot);
        if (code) {
          cleanup(code);
          return true;
        }
        if ((Date.now() - startedAt) >= timeoutMs) {
          cleanup("");
          return true;
        }
        return false;
      }

      if (checkNow()) {
        return;
      }

      var row = document.querySelector('tr[data-id="' + rowId + '"]');
      if (typeof MutationObserver !== "undefined") {
        observer = new MutationObserver(checkNow);
        if (row) {
          observer.observe(row, {
            childList: true,
            subtree: true,
            characterData: true,
          });
        }
        if (document.body) {
          observer.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true,
          });
        }
      }

      timer = window.setInterval(checkNow, 250);
    });
  }

  function waitForSmsCode(rowId, timeoutMs, popupSnapshot) {
    return new Promise(function (resolve) {
      var startedAt = Date.now();
      var observer = null;
      var timer = null;
      var finished = false;

      function cleanup(result) {
        if (finished) {
          return;
        }
        finished = true;
        if (observer) {
          observer.disconnect();
        }
        if (timer) {
          window.clearInterval(timer);
        }
        resolve(result || "");
      }

      function checkNow() {
        var code = getSmsTextForRow(rowId) || getNewOtpPopupCode(popupSnapshot);
        if (code) {
          cleanup(code);
          return true;
        }
        if ((Date.now() - startedAt) >= timeoutMs) {
          cleanup("");
          return true;
        }
        return false;
      }

      if (checkNow()) {
        return;
      }

      var row = document.querySelector('tr[data-id="' + rowId + '"]');
      if (typeof MutationObserver !== "undefined") {
        observer = new MutationObserver(checkNow);
        if (row) {
          observer.observe(row, {
            childList: true,
            subtree: true,
            characterData: true,
          });
        }
        if (document.body) {
          observer.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true,
          });
        }
      }

      timer = window.setInterval(checkNow, 250);
    });
  }

  async function clickCheckCodeUntilOtp(rowId, timeoutMs) {
    return clickAuthCodeUntilFound(rowId, timeoutMs, false);
  }

  async function clickCheckSmsUntilOtp(rowId, timeoutMs) {
    return clickAuthCodeUntilFound(rowId, timeoutMs, true);
  }

  async function clickAuthCodeUntilFound(rowId, timeoutMs, sms) {
    var startedAt = Date.now();
    var popupSnapshot = captureOtpPopupSnapshot();
    while ((Date.now() - startedAt) < timeoutMs) {
      var clicked = sms ? clickCheckSms(rowId) : clickCheckCode(rowId);
      if (clicked) {
        var latestCode = await (sms ? waitForSmsCode : waitForOtpCode)(
          rowId,
          Math.min(OTP_CLICK_RETRY_INTERVAL_MS, Math.max(500, timeoutMs - (Date.now() - startedAt))),
          popupSnapshot
        );
        if (latestCode) {
          return { ok: true, code: latestCode };
        }
        if (hasNoPendingOrderToast(sms ? "phone" : "email")) {
          return {
            ok: false,
            terminal: true,
            error: sms
              ? "No pending phone order for this account. Request a number first."
              : "No pending email order for this account. Get email first.",
          };
        }
        await sleep(300);
      } else {
        // Never proceed without having actually clicked the check button; fail
        // fast when it can't be found/clicked instead of burning the window.
        await sleep(500);
        if ((Date.now() - startedAt) >= OTP_CLICK_RETRY_INTERVAL_MS) {
          return {
            ok: false,
            error: sms
              ? "Check SMS button not found on SnapBoard row."
              : "Check Code button not found on SnapBoard row.",
          };
        }
      }
    }
    return {
      ok: false,
      error: sms ? "SMS code not found on SnapBoard row." : "OTP code not found on SnapBoard row.",
    };
  }

  async function rotateProxyUntilChanged(rowId, timeoutMs, maxClicks) {
    var oldProxy = readProxyFromRow(rowId);
    var attempt = 0;
    while (attempt < maxClicks) {
      attempt += 1;
      var clicked = clickRotateButton(rowId);
      if (!clicked) {
        if (attempt >= maxClicks) {
          return { ok: false, error: "No rotate button found for row." };
        }
        await sleep(400);
        continue;
      }
      var newProxy = await waitForProxyChange(rowId, oldProxy, timeoutMs);
      if (newProxy && newProxy !== oldProxy) {
        return { ok: true, proxy: newProxy };
      }
      await sleep(600);
    }
    return { ok: false, error: "Proxy did not change after rotation." };
  }

  async function pollPendingOtpRequest() {
    if (otpPollInFlight) {
      return;
    }
    otpPollInFlight = true;

    try {
      var config = await getStoredConfig();
      var apiConfig = getLocalApiConfig(config);
      if (!apiConfig.localApiUrl) {
        return;
      }

      var headers = {};
      if (apiConfig.localToken) {
        headers["X-Nyxify-Token"] = apiConfig.localToken;
      }

      var response = await fetch(apiConfig.localApiUrl + "/otp/pending", {
        method: "GET",
        headers: headers,
      });
      var payload = await response.json();
      if (!response.ok || !payload.ok || !payload.request) {
        return;
      }

      var rowKey = normalizeText(payload.request.row_key);
      var rowId = extractRowId(rowKey);
      if (!rowId) {
        return;
      }
      if (!normalizeComparableEmail(payload.request.email)) {
        headers["Content-Type"] = "application/json";
        await fetch(apiConfig.localApiUrl + "/otp/result", {
          method: "POST",
          headers: headers,
          body: JSON.stringify({
            row_key: rowKey,
            error: "Missing expected email for OTP check.",
          }),
        });
        return;
      }
      if (!rowMatchesExpectedEmail(rowId, payload.request.email)) {
        headers["Content-Type"] = "application/json";
        await fetch(apiConfig.localApiUrl + "/otp/result", {
          method: "POST",
          headers: headers,
          body: JSON.stringify({
            row_key: rowKey,
            error: "SnapBoard row email does not match pending OTP account.",
          }),
        });
        return;
      }

      var codeResult = await clickCheckCodeUntilOtp(rowId, OTP_FETCH_TIMEOUT_MS);
      if (!codeResult.ok || !codeResult.code) {
        headers["Content-Type"] = "application/json";
        await fetch(apiConfig.localApiUrl + "/otp/result", {
          method: "POST",
          headers: headers,
          body: JSON.stringify({
            row_key: rowKey,
            error: codeResult.error || "OTP code not found on SnapBoard row.",
          }),
        });
        return;
      }

      headers["Content-Type"] = "application/json";
      await fetch(apiConfig.localApiUrl + "/otp/result", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({
          row_key: rowKey,
          code: codeResult.code,
        }),
      });
    } catch (error) {
      return;
    } finally {
      otpPollInFlight = false;
    }
  }

  async function pollPendingUsernameUpdate() {
    if (usernameUpdatePollInFlight) {
      return;
    }
    usernameUpdatePollInFlight = true;

    try {
      var config = await getStoredConfig();
      var apiConfig = getLocalApiConfig(config);
      if (!apiConfig.localApiUrl) {
        return;
      }

      var headers = {};
      if (apiConfig.localToken) {
        headers["X-Nyxify-Token"] = apiConfig.localToken;
      }

      var response = await fetch(apiConfig.localApiUrl + "/username_update/pending", {
        method: "GET",
        headers: headers,
      });
      var payload = await response.json();
      if (!response.ok || !payload.ok || !payload.request) {
        return;
      }

      var rowKey = normalizeText(payload.request.row_key);
      var nextUsername = normalizeText(payload.request.username);
      var updated = requestUsernameUpdate(
        rowKey.replace(/^snapboard:/i, ""),
        nextUsername
      );

      headers["Content-Type"] = "application/json";
      await fetch(apiConfig.localApiUrl + "/username_update/result", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({
          row_key: rowKey,
          success: updated,
          error: updated ? "" : "SnapBoard username input was not updated",
        }),
      });
    } catch (_error) {
      return;
    } finally {
      usernameUpdatePollInFlight = false;
    }
  }

  async function pollPendingAdspowerUpdate() {
    if (adspowerUpdatePollInFlight) {
      return;
    }
    adspowerUpdatePollInFlight = true;

    try {
      var config = await getStoredConfig();
      var apiConfig = getLocalApiConfig(config);
      if (!apiConfig.localApiUrl) {
        return;
      }

      var headers = {};
      if (apiConfig.localToken) {
        headers["X-Nyxify-Token"] = apiConfig.localToken;
      }

      var response = await fetch(apiConfig.localApiUrl + "/adspower_update/pending", {
        method: "GET",
        headers: headers,
      });
      var payload = await response.json();
      if (!response.ok || !payload.ok || !payload.request) {
        return;
      }

      var rowKey = normalizeText(payload.request.row_key);
      var nextAdspowerId = normalizeText(payload.request.adspower_id);
      var updated = requestAdspowerIdUpdate(
        rowKey.replace(/^snapboard:/i, ""),
        nextAdspowerId
      );

      headers["Content-Type"] = "application/json";
      await fetch(apiConfig.localApiUrl + "/adspower_update/result", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({
          row_key: rowKey,
          success: updated,
          error: updated ? "" : "SnapBoard AdsPower id input was not updated",
        }),
      });
    } catch (_error) {
      return;
    } finally {
      adspowerUpdatePollInFlight = false;
    }
  }

  async function pollPendingAdspowerNameUpdate() {
    if (adspowerNameUpdatePollInFlight) {
      return;
    }
    adspowerNameUpdatePollInFlight = true;

    try {
      var config = await getStoredConfig();
      var apiConfig = getLocalApiConfig(config);
      if (!apiConfig.localApiUrl) {
        return;
      }

      var headers = {};
      if (apiConfig.localToken) {
        headers["X-Nyxify-Token"] = apiConfig.localToken;
      }

      var response = await fetch(apiConfig.localApiUrl + "/adspower_name_update/pending", {
        method: "GET",
        headers: headers,
      });
      var payload = await response.json();
      if (!response.ok || !payload.ok || !payload.request) {
        return;
      }

      var rowKey = normalizeText(payload.request.row_key);
      var nextName = normalizeText(payload.request.adspower_name);
      var updated = requestAdspowerNameUpdate(
        rowKey.replace(/^snapboard:/i, ""),
        nextName
      );

      headers["Content-Type"] = "application/json";
      await fetch(apiConfig.localApiUrl + "/adspower_name_update/result", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({
          row_key: rowKey,
          success: updated,
          error: updated ? "" : "SnapBoard AdsPower name input was not updated",
        }),
      });
    } catch (_error) {
      return;
    } finally {
      adspowerNameUpdatePollInFlight = false;
    }
  }

  async function pollPendingStatusUpdate() {
    if (statusUpdatePollInFlight) {
      return;
    }
    statusUpdatePollInFlight = true;

    try {
      var config = await getStoredConfig();
      var apiConfig = getLocalApiConfig(config);
      if (!apiConfig.localApiUrl) {
        return;
      }

      var headers = {};
      if (apiConfig.localToken) {
        headers["X-Nyxify-Token"] = apiConfig.localToken;
      }

      var response = await fetch(apiConfig.localApiUrl + "/status_update/pending", {
        method: "GET",
        headers: headers,
      });
      var payload = await response.json();
      if (!response.ok || !payload.ok || !payload.request) {
        return;
      }

      var rowKey = normalizeText(payload.request.row_key);
      var nextStatus = normalizeText(payload.request.status);
      var updated = setRowStatus(
        rowKey.replace(/^snapboard:/i, ""),
        nextStatus
      );

      headers["Content-Type"] = "application/json";
      await fetch(apiConfig.localApiUrl + "/status_update/result", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({
          row_key: rowKey,
          success: updated,
          error: updated ? "" : "SnapBoard status cell was not updated",
        }),
      });
    } catch (_error) {
      return;
    } finally {
      statusUpdatePollInFlight = false;
    }
  }

  function startStatusUpdatePoll() {
    if (statusUpdatePollTimer) {
      return;
    }
    statusUpdatePollTimer = window.setInterval(function () {
      pollPendingStatusUpdate();
    }, USERNAME_UPDATE_POLL_INTERVAL_MS);
  }

  function startUsernameUpdatePoll() {
    if (usernameUpdatePollTimer) {
      return;
    }
    usernameUpdatePollTimer = window.setInterval(function () {
      pollPendingUsernameUpdate();
    }, USERNAME_UPDATE_POLL_INTERVAL_MS);
  }

  function startAdspowerUpdatePoll() {
    if (adspowerUpdatePollTimer) {
      return;
    }
    adspowerUpdatePollTimer = window.setInterval(function () {
      pollPendingAdspowerUpdate();
    }, USERNAME_UPDATE_POLL_INTERVAL_MS);
  }

  function startAdspowerNameUpdatePoll() {
    if (adspowerNameUpdatePollTimer) {
      return;
    }
    adspowerNameUpdatePollTimer = window.setInterval(function () {
      pollPendingAdspowerNameUpdate();
    }, USERNAME_UPDATE_POLL_INTERVAL_MS);
  }

  function startSnapboardRefreshPoll() {
    if (snapboardRefreshPollTimer) {
      return;
    }
    snapboardRefreshPollTimer = window.setInterval(function () {
      pollPendingSnapboardRefresh();
    }, SNAPBOARD_REFRESH_POLL_INTERVAL_MS);
    window.setTimeout(function () {
      pollPendingSnapboardRefresh();
    }, 500);
  }

  function getReserveButton() {
    return document.getElementById("reserveBtn")
      || document.querySelector(".btn-reserve")
      || toArray(document.querySelectorAll("button")).find(function (b) {
           var onclick = normalizeText(b.getAttribute("onclick") || "").toLowerCase();
           return onclick.indexOf("reserveproxy") >= 0;
         }) || null;
  }

  function allRowsFilledNonePending() {
    var rows = toArray(document.querySelectorAll("tr[data-id]"));
    if (!rows.length) { return false; }
    var root = getRowRoot();
    var headerMap = getTableHeaderMap(root);
    for (var i = 0; i < rows.length; i++) {
      var adspowerId = normalizeText(readValueFromAliases(rows[i], headerMap, ["adspower", "adspower id", "profile id"]));
      if (!adspowerId) { return false; }
      var username = normalizeText(readValueFromAliases(rows[i], headerMap, ["username", "snap username", "snapchat username", "user", "snap user"]));
      if (!username) { return false; }
    }
    return true;
  }

  function reserveAutoFillClick() {
    return new Promise(function (resolve) {
      chrome.runtime.sendMessage({ type: "NYXIFY_AUTO_FILL_RESERVE_CLICK" }, function (response) {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message || "Auto-fill reservation failed." });
          return;
        }
        resolve(response || { ok: false, error: "Auto-fill reservation returned no response." });
      });
    });
  }

  async function checkAndAutoFill() {
    var config = await getStoredConfig();
    if (!config.autoFillRow) { return; }
    if (allRowsFilledNonePending()) {
      var btn = getReserveButton();
      if (!btn || btn.disabled) { return; }
      var reservation = await reserveAutoFillClick();
      if (reservation && reservation.ok && reservation.shouldClick) {
        clickElement(btn);
      }
    }
  }

  function startAutoFillPoll() {
    if (autoFillPollTimer) { return; }
    autoFillPollTimer = window.setInterval(function () { checkAndAutoFill(); }, AUTO_FILL_POLL_MS);
  }

  chrome.runtime.onMessage.addListener(function (message, _sender, sendResponse) {
    if (message && message.type === "NYXIFY_SCAN_BANNED_ROWS") {
      getRowLimit(function (rowLimit) {
        var requestedCount = parseInt(message.count, 10);
        var safeLimit = Number.isFinite(requestedCount) && requestedCount > 0
          ? Math.max(rowLimit, requestedCount)
          : 100000;
        var rows = extractSnapboardStatusRows(safeLimit);
        var banned = rows.filter(function (row) {
          return normalizeText(row.status).toLowerCase() === "banned";
        });
        sendResponse({ ok: true, rows: rows, banned: banned, count: banned.length });
      });
      return true;
    }

    if (!message || message.type !== "NYXIFY_SNAPBOARD_ACTION") {
      return undefined;
    }

    (async function () {
      // Session recovery is row-agnostic — handle it before the row-id check so
      // the background bridge can re-login a logged-out board on any empty fetch.
      if (message.action === "ensure_logged_in") {
        // Report whether the board was actually signed out so callers can tell a
        // real session problem apart from a fetch that's merely "not ready yet"
        // (e.g. an OTP that hasn't landed) — the latter must NOT trigger a
        // disruptive board reload for every other concurrent account.
        var wasLoggedOut = isLoginScreenVisible();
        var loggedIn = await ensureSnapboardLoggedIn(message.timeout_ms);
        sendResponse({ ok: loggedIn, logged_in: loggedIn, was_logged_out: wasLoggedOut });
        return;
      }

      var rowKey = normalizeText(message.row_key);
      var rowId = extractRowId(rowKey);
      if (!rowId) {
        sendResponse({ ok: false, error: "Missing SnapBoard row id." });
        return;
      }

      if (message.action === "otp") {
        if (!normalizeComparableEmail(message.email || message.expected_email)) {
          sendResponse({ ok: false, terminal: true, error: "Missing expected email for OTP check." });
          return;
        }
        if (!rowMatchesExpectedEmail(rowId, message.email || message.expected_email)) {
          sendResponse({ ok: false, error: "SnapBoard row email does not match pending OTP account." });
          return;
        }
        var codeResult = await clickCheckCodeUntilOtp(rowId, OTP_FETCH_TIMEOUT_MS);
        if (!codeResult.ok || !codeResult.code) {
          sendResponse({
            ok: false,
            terminal: !!codeResult.terminal,
            error: codeResult.error || "OTP code not found on SnapBoard row.",
          });
          return;
        }
        sendResponse({ ok: true, code: codeResult.code });
        return;
      }

      if (message.action === "email_fetch") {
        var emailResult = await requestEmailFetch(rowId, !!message.force_new);
        sendResponse(emailResult);
        return;
      }

      if (message.action === "phone_fetch") {
        var phoneResult = await requestPhoneFetch(rowId, !!message.force_new);
        sendResponse(phoneResult);
        return;
      }

      if (message.action === "sms") {
        if (!rowMatchesExpectedPhone(rowId, message.phone || message.expected_phone)) {
          sendResponse({ ok: false, error: "SnapBoard row phone does not match pending SMS account." });
          return;
        }
        var smsResult = await clickCheckSmsUntilOtp(rowId, OTP_FETCH_TIMEOUT_MS);
        if (!smsResult.ok || !smsResult.code) {
          sendResponse({
            ok: false,
            terminal: !!smsResult.terminal,
            error: smsResult.error || "SMS code not found on SnapBoard row.",
          });
          return;
        }
        sendResponse({ ok: true, code: smsResult.code });
        return;
      }

      if (message.action === "username_update") {
        var updated = requestUsernameUpdate(rowId, normalizeText(message.username));
        sendResponse({
          ok: updated,
          error: updated ? "" : "SnapBoard username input was not updated",
        });
        return;
      }

      if (message.action === "adspower_update") {
        var adsUpdated = requestAdspowerIdUpdate(rowId, normalizeText(message.adspower_id));
        sendResponse({
          ok: adsUpdated,
          error: adsUpdated ? "" : "SnapBoard AdsPower id input was not updated",
        });
        return;
      }

      if (message.action === "adspower_name_update") {
        var adsNameUpdated = requestAdspowerNameUpdate(rowId, normalizeText(message.adspower_name));
        sendResponse({
          ok: adsNameUpdated,
          error: adsNameUpdated ? "" : "SnapBoard AdsPower name input was not updated",
        });
        return;
      }

      if (message.action === "status_update") {
        var statusUpdated = setRowStatus(rowId, normalizeText(message.status));
        sendResponse({
          ok: statusUpdated,
          error: statusUpdated ? "" : "SnapBoard status cell was not updated",
        });
        return;
      }

      if (message.action === "proxy_rotate") {
        var requestedMaxClicks = parseInt(message.max_clicks, 10);
        var maxClicks = Number.isFinite(requestedMaxClicks) && requestedMaxClicks > 0
          ? requestedMaxClicks
          : PROXY_ROTATE_CLICK_ATTEMPTS;
        var proxyResult = await rotateProxyUntilChanged(rowId, PROXY_ROTATE_WAIT_MS, maxClicks);
        if (!proxyResult.ok) {
          sendResponse({ ok: false, error: proxyResult.error });
          return;
        }
        sendResponse({ ok: true, proxy: proxyResult.proxy });
        return;
      }

      sendResponse({ ok: false, error: "Unknown SnapBoard action." });
    })();

    return true;
  });

  document.addEventListener("input", queueScan, true);
  document.addEventListener("change", queueScan, true);
  document.addEventListener("click", queueScan, true);
  chrome.storage.onChanged.addListener(function (changes, areaName) {
    if (areaName !== "sync" || !changes[CONFIG_KEY]) {
      return;
    }
    configCache = changes[CONFIG_KEY].newValue || {};
    configCacheAt = Date.now();
    scheduleProviderLock();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", queueScan, { once: true });
  } else {
    queueScan();
  }
  connectBridgePort();

  var observer = new MutationObserver(queueScan);
  observer.observe(document.documentElement || document.body, {
    childList: true,
    subtree: true,
    characterData: true,
  });

  startAutoFillPoll();
  startUsernameUpdatePoll();
  startAdspowerUpdatePoll();
  startAdspowerNameUpdatePoll();
  startStatusUpdatePoll();
  startSnapboardRefreshPoll();
  startProviderLockPoll();
  startAutoLoginPoll();

})();
