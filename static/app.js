const API = {
  channels: "/api/channels",
  messages: "/api/messages",
  post: "/api/post",
  vote: "/api/vote",
  session: "/api/session",
  status: "/api/status",
};

function $(id) {
  return document.getElementById(id);
}

/* ---- Helpers ---- */

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function getCookie(name) {
  const m = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
  return m ? decodeURIComponent(m[2]) : null;
}

function setCookie(name, value) {
  // Plain HTTP captive portal; no Secure flag. Keep it host-only.
  document.cookie = `${name}=${encodeURIComponent(value)}; Path=/; SameSite=Lax`;
}

function loadNameFromCookie() {
  const name = getCookie("civicmesh_name");
  if (name) $("name").value = name;
}

function storeNameToCookie() {
  const name = $("name").value.trim();
  if (name) {
    setCookie("civicmesh_name", name);
  }
}

async function sha1Hex(text) {
  if (!window.crypto || !window.crypto.subtle) return "";
  const data = new TextEncoder().encode(text);
  const hash = await window.crypto.subtle.digest("SHA-1", data);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function computeFingerprint() {
  const parts = [
    ["ua", navigator.userAgent],
    ["lang", navigator.language],
    ["platform", navigator.platform],
    ["vendor", navigator.vendor || ""],
    ["tz", Intl.DateTimeFormat().resolvedOptions().timeZone || ""],
    ["tzOffset", String(new Date().getTimezoneOffset())],
    ["screen", `${screen.width}x${screen.height}`],
    ["colorDepth", String(screen.colorDepth)],
    ["hw", String(navigator.hardwareConcurrency || "")],
    ["mem", String(navigator.deviceMemory || "")],
  ];
  const raw = parts.map(([k, v]) => `${k}=${v}`).join("|");
  return sha1Hex(raw);
}

async function sendFingerprint(fp) {
  if (!fp) return;
  try {
    await fetchJSON("/api/session/fingerprint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fingerprint: fp }),
    });
  } catch {
    // Best-effort only.
  }
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/* ---- Date / Time formatting ---- */

function fmtTime(ts) {
  try {
    const d = new Date(ts * 1000);
    let hours = d.getHours();
    const minutes = String(d.getMinutes()).padStart(2, "0");
    const ampm = hours >= 12 ? "pm" : "am";
    hours = hours % 12;
    if (hours === 0) hours = 12;
    return `${hours}:${minutes} ${ampm}`;
  } catch {
    return String(ts);
  }
}

function fmtFullTimestamp(ts) {
  try {
    const d = new Date(ts * 1000);
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    const month = months[d.getMonth()];
    const day = d.getDate();
    const year = d.getFullYear();
    return `${month} ${day}, ${year} ${fmtTime(ts)}`;
  } catch {
    return String(ts);
  }
}

function fmtDateLabel(ts) {
  try {
    const d = new Date(ts * 1000);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    if (msgDay.getTime() === today.getTime()) return "Today";
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    if (msgDay.getTime() === yesterday.getTime()) return "Yesterday";
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
  } catch {
    return "";
  }
}

function dateKey(ts) {
  try {
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  } catch {
    return "";
  }
}

function formatNextHour() {
  const now = new Date();
  const next = new Date(now);
  next.setMinutes(0, 0, 0);
  if (next <= now) {
    next.setHours(next.getHours() + 1);
  }
  let hours = next.getHours();
  const minutes = String(next.getMinutes()).padStart(2, "0");
  const ampm = hours >= 12 ? "pm" : "am";
  hours = hours % 12;
  if (hours === 0) hours = 12;
  return `${hours}:${minutes} ${ampm}`;
}

/* ---- State ---- */

let state = {
  channels: [],
  activeChannel: null,
  fingerprint: "",
  polling: null,
  maxChars: 100,
  maxNameChars: 12,
  namePattern: /^[A-Za-z0-9_-]+$/,
  expandedMeta: new Set(),
  postsRemaining: null,
  windowSec: 3600,
  radioStatus: "unknown",
  liveSize: 80,
  pageSize: 80,
  loadingOlder: false,
  hasMore: true,
  history: [],
  live: [],
  meshChannelNames: new Set(),
};

/* ---- View switching ---- */

function isDesktop() {
  return window.matchMedia("(min-width: 768px)").matches;
}

function showView(id) {
  if (isDesktop()) {
    // On desktop, channels sidebar is always visible.
    // Toggle between welcome and chat in the main area.
    $("viewChannels").classList.add("active");
    $("viewWelcome").classList.remove("active");
    $("viewChat").classList.remove("active");
    $(id).classList.add("active");
  } else {
    // On mobile, only one view at a time.
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $(id).classList.add("active");
  }
}

function showWelcome() {
  if (isDesktop()) {
    // On desktop, show welcome alongside channel sidebar
    $("viewChannels").classList.add("active");
    $("viewChat").classList.remove("active");
    $("viewWelcome").classList.add("active");
  } else {
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $("viewWelcome").classList.add("active");
  }
}

function showChannels() {
  localStorage.setItem("civicmesh_seen_welcome", "1");
  if (isDesktop()) {
    // On desktop, channels is always visible; show welcome or chat in main area
    $("viewChannels").classList.add("active");
    // If no active channel, show welcome; otherwise keep chat
    if (!state.activeChannel) {
      $("viewWelcome").classList.add("active");
      $("viewChat").classList.remove("active");
    }
  } else {
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $("viewChannels").classList.add("active");
  }
}

function showChat() {
  if (isDesktop()) {
    $("viewChannels").classList.add("active");
    $("viewWelcome").classList.remove("active");
    $("viewChat").classList.add("active");
  } else {
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $("viewChat").classList.add("active");
  }
}

/* ---- Channel rendering ---- */

function normalizeChannels(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.map(function(ch) {
    if (typeof ch === "string") {
      var scope = ch === "#local" ? "on-site" : "mesh";
      return { name: ch, scope: scope };
    }
    if (ch && typeof ch === "object") {
      return {
        name: String(ch.name || ""),
        scope: ch.scope === "on-site" ? "on-site" : "mesh",
      };
    }
    return { name: "", scope: "mesh" };
  }).filter(function(ch) { return ch.name; });
}

function scopeLabel(scope) {
  return scope === "on-site" ? "On-site" : "Mesh";
}

function scopeDescription(scope) {
  return scope === "on-site"
    ? "Messages stay at this hub"
    : "Messages relayed across the mesh network";
}

function getChannel(name) {
  return state.channels.find(function(c) { return c.name === name; }) || null;
}

function channelIconSvg(scope) {
  if (scope === "on-site") {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z"/><circle cx="12" cy="9" r="2.5"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="2.5"/><path d="M7.5 7.5a6.4 6.4 0 0 0 0 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/><path d="M16.5 7.5a6.4 6.4 0 0 1 0 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>';
}

function renderChannels() {
  var wrap = $("channelList");
  wrap.innerHTML = "";

  for (var i = 0; i < state.channels.length; i++) {
    var ch = state.channels[i];
    var card = document.createElement("div");
    card.className = "channel-card" + (ch.name === state.activeChannel ? " channel-card--active" : "");
    var iconClass = ch.scope === "mesh" ? "channel-card__icon--mesh" : "channel-card__icon--local";
    var badgeClass = ch.scope === "mesh" ? "channel-card__badge--mesh" : "channel-card__badge--local";
    card.innerHTML =
      '<div class="channel-card__icon ' + iconClass + '">' + channelIconSvg(ch.scope) + '</div>' +
      '<div class="channel-card__body">' +
        '<div class="channel-card__name">' + escapeHtml(ch.name) + '</div>' +
        '<div class="channel-card__desc">' + escapeHtml(scopeDescription(ch.scope)) + '</div>' +
      '</div>' +
      '<div class="channel-card__meta">' +
        '<div class="channel-card__badge ' + badgeClass + '">' + scopeLabel(ch.scope) + '</div>' +
      '</div>';
    card.setAttribute("data-channel", ch.name);
    card.addEventListener("click", (function(name) {
      return function() { setActiveChannel(name); };
    })(ch.name));
    wrap.appendChild(card);
  }
}

/* ---- Active channel ---- */

function setActiveChannel(name) {
  state.activeChannel = name;
  var ch = getChannel(name);
  var label = ch ? scopeLabel(ch.scope) : "Mesh";
  $("chatChannelName").textContent = ch ? ch.name : name;
  $("chatChannelScope").textContent = label;
  updateComposeLabels(ch);
  updatePostButton(ch);
  renderChannels();

  state.history = [];
  state.live = [];
  state.hasMore = true;
  state.loadingOlder = false;
  state.expandedMeta.clear();

  showChat();
  refreshLive(true);
  refreshRadioStatus();
}

function updateComposeLabels(ch) {
  var isLocal = ch && ch.scope === "on-site";
  $("composeTitle").textContent = ch ? "Post to " + ch.name : "Post";
  $("composeSubtitle").textContent = isLocal
    ? "Your message will stay at this hub"
    : "Your message will be relayed across the mesh network";
  $("postBtn").textContent = isLocal ? "Post On-site" : "Post to Mesh";
}

function updatePostButton(ch) {
  $("postBtn").disabled = !ch;
}

/* ---- Compose modal ---- */

function openCompose() {
  $("composeOverlay").classList.add("active");
  requestAnimationFrame(function() {
    $("composeModal").classList.add("active");
  });
  $("postError").textContent = "";
}

function closeCompose() {
  $("composeModal").classList.remove("active");
  $("composeOverlay").classList.remove("active");
}

/* ---- Message rendering ---- */

function sourceLabel(source) {
  if (source === "local") return "On-site";
  if (source === "mesh") return "Mesh";
  if (source === "wifi") return "WiFi";
  if (source === "pending") return "Queued";
  return String(source || "");
}

function renderMessages(msgs) {
  var wrap = $("messages");
  wrap.innerHTML = "";

  // Radio banner is now a separate element outside the scroll area

  var lastDateKey = "";
  var sid = getCookie("civicmesh_session") || "";

  for (var i = 0; i < msgs.length; i++) {
    var m = msgs[i];

    // Date separator
    var dk = dateKey(m.ts);
    if (dk && dk !== lastDateKey) {
      lastDateKey = dk;
      var sep = document.createElement("div");
      sep.className = "date-sep";
      sep.innerHTML = '<span class="date-sep__label">' + escapeHtml(fmtDateLabel(m.ts)) + '</span>';
      wrap.appendChild(sep);
    }

    var isOwn = m.session_id && m.session_id === sid;
    var isPending = Boolean(m.pending);
    var div = document.createElement("div");
    div.className = "msg-bubble" +
      (isOwn || isPending ? " msg-bubble--own" : "") +
      (isPending ? " msg-bubble--pending" : "");

    var metaId = "meta-" + String(m.id).replace(/[^a-zA-Z0-9_-]/g, "_");
    var isLast = (i === msgs.length - 1);
    var timeClass = "msg-bubble__time" + (isLast ? " msg-bubble__time--visible" : "");

    // Vote
    var uv = Number(m.upvotes || 0);
    var my = Number(m.user_vote || 0);
    var canVote = !m.session_id || m.session_id !== sid;
    var upActive = my === 1 ? " active-up" : "";
    var badge = uv > 0 ? " \u2605" + uv : "";
    var voteHtml = "";
    if (!isPending && canVote) {
      voteHtml = '<button class="btn--vote' + upActive + '" data-id="' + m.id + '" data-v="1" data-my="' + my + '">\u2605 Upvote' + badge + '</button>';
    } else if (uv > 0) {
      voteHtml = '<span>\u2605 ' + uv + '</span>';
    }

    var timeText = isPending ? "Queued for mesh" : fmtTime(m.ts);
    var senderText = isPending ? "You" : escapeHtml(m.sender || "unknown");

    var detailRows = "";
    if (isPending) {
      detailRows = '<div class="msg-bubble__detail-row"><span>Status</span><span>Queued \u2014 will send when radio connects</span></div>';
    } else {
      detailRows =
        '<div class="msg-bubble__detail-row"><span>Source</span><span>' + escapeHtml(sourceLabel(m.source)) + '</span></div>' +
        '<div class="msg-bubble__detail-row"><span>Time</span><span>' + escapeHtml(fmtFullTimestamp(m.ts)) + '</span></div>' +
        (voteHtml ? '<div style="margin-top:4px">' + voteHtml + '</div>' : '');
    }

    div.innerHTML =
      '<div class="msg-bubble__sender">' + senderText + '</div>' +
      '<div class="msg-bubble__text">' + escapeHtml(m.content) + '</div>' +
      '<div class="' + timeClass + '">' + timeText + '</div>' +
      '<div id="' + metaId + '" class="msg-bubble__detail">' + detailRows + '</div>';

    // Restore expanded state
    if (state.expandedMeta.has(metaId)) {
      var detail = div.querySelector("#" + metaId);
      if (detail) detail.classList.add("expanded");
      var timeEl = div.querySelector(".msg-bubble__time");
      if (timeEl) timeEl.classList.add("msg-bubble__time--visible");
    }

    // Tap to toggle detail
    div.addEventListener("click", (function(mid) {
      return function(e) {
        // Don't toggle if vote button was clicked
        if (e.target.closest(".btn--vote")) return;
        var detail = document.getElementById(mid);
        var timeEl = this.querySelector(".msg-bubble__time");
        if (!detail) return;
        detail.classList.toggle("expanded");
        if (detail.classList.contains("expanded")) {
          state.expandedMeta.add(mid);
          if (timeEl) timeEl.classList.add("msg-bubble__time--visible");
        } else {
          state.expandedMeta.delete(mid);
        }
      };
    })(metaId));

    wrap.appendChild(div);
  }

  // Wire up vote buttons
  wrap.querySelectorAll("button[data-id][data-v]").forEach(function(b) {
    b.addEventListener("click", async function(e) {
      e.stopPropagation();
      var id = Number(b.getAttribute("data-id"));
      var v = Number(b.getAttribute("data-v"));
      var current = Number(b.getAttribute("data-my") || "0");
      var next = current === v ? 0 : v;
      await vote(id, next);
    });
  });
}

function renderAllMessages() {
  renderMessages(state.history.concat(state.live));
}

/* ---- Radio status ---- */

function updateRadioBanner() {
  var banner = $("radioStatusBanner");
  if (!banner) return;
  var isMesh = state.meshChannelNames.has(state.activeChannel);
  banner.hidden = !(isMesh && state.radioStatus !== "online");
}

async function refreshRadioStatus() {
  try {
    var data = await fetchJSON(API.status, { method: "GET" });
    state.radioStatus = data.radio || "unknown";
  } catch {
    state.radioStatus = "unknown";
  }
  updateRadioBanner();
}

/* ---- Data fetching ---- */

async function refreshSession() {
  var debug = $("debugBanner");
  try {
    var data = await fetchJSON(API.session, { method: "GET" });
    setConnectionStatus(true);
    state.postsRemaining = data.posts_remaining;
    updateCharCount();
    if (debug) {
      debug.hidden = !data.debug_session;
    }
    if (typeof data.window_sec === "number" && data.window_sec > 0) {
      state.windowSec = data.window_sec;
    }
    if (typeof data.message_max_chars === "number" && data.message_max_chars > 0) {
      state.maxChars = data.message_max_chars;
      applyMaxChars();
    }
    if (typeof data.name_max_chars === "number" && data.name_max_chars > 0) {
      state.maxNameChars = data.name_max_chars;
      applyNameMax();
    }
    if (typeof data.name_pattern === "string" && data.name_pattern) {
      try {
        state.namePattern = new RegExp(data.name_pattern);
      } catch {
        // keep default
      }
    }
  } catch (e) {
    setConnectionStatus(false);
    state.postsRemaining = null;
    updateCharCount();
    if (debug) {
      debug.hidden = true;
    }
  }
}

async function fetchMessagesPage(offset, limit) {
  var url = API.messages + "?channel=" + encodeURIComponent(state.activeChannel) + "&limit=" + limit + "&offset=" + offset;
  var data = await fetchJSON(url, { method: "GET" });
  return data.messages || [];
}

async function refreshLive(scrollBottom) {
  if (!state.activeChannel) return;
  $("postError").textContent = "";
  var wrap = $("messages");
  var wasNearBottom = wrap
    ? wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 40
    : false;
  try {
    var rows = await fetchMessagesPage(0, state.liveSize);
    state.live = rows.slice().reverse();
    renderAllMessages();
    if (wrap && (scrollBottom || wasNearBottom)) {
      wrap.scrollTop = wrap.scrollHeight;
    }
  } catch (e) {
    $("postError").textContent = e.message || "Failed to load messages";
    if (e && e.message && String(e.message).includes("Failed to fetch")) {
      setConnectionStatus(false);
    }
  }
}

async function loadOlderMessages() {
  if (!state.activeChannel || state.loadingOlder || !state.hasMore) return;
  var wrap = $("messages");
  var prevHeight = wrap ? wrap.scrollHeight : 0;
  state.loadingOlder = true;
  try {
    var offset = state.liveSize + state.history.length;
    var rows = await fetchMessagesPage(offset, state.pageSize);
    var older = rows.slice().reverse();
    if (older.length === 0) {
      state.hasMore = false;
    } else {
      state.history = older.concat(state.history);
    }
    renderAllMessages();
    if (wrap) {
      var newHeight = wrap.scrollHeight;
      wrap.scrollTop = newHeight - prevHeight + wrap.scrollTop;
    }
  } catch (e) {
    $("postError").textContent = e.message || "Failed to load messages";
  } finally {
    state.loadingOlder = false;
  }
}

/* ---- Posting ---- */

async function postMessage() {
  var nameVal = $("name").value.trim();
  var content = $("content").value.trim();
  var channel = state.activeChannel;
  $("postError").textContent = "";
  if (!nameVal) {
    $("postError").textContent = "Name is required to post.";
    return;
  }
  validateNameLive();
  if ($("postError").textContent) return;
  if (!channel) {
    $("postError").textContent = "Pick a channel to post.";
    return;
  }
  if (!content) {
    $("postError").textContent = "Message is empty.";
    return;
  }
  if (content.length > state.maxChars) {
    $("postError").textContent = "Message is too long (" + content.length + "/" + state.maxChars + ").";
    return;
  }

  try {
    await fetchJSON(API.post, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: nameVal, channel: channel, content: content, fingerprint: state.fingerprint }),
    });
    storeNameToCookie();
    $("content").value = "";
    closeCompose();
    await refreshSession();
    await refreshLive(true);
  } catch (e) {
    if (e.status === 429 && e.data) {
      $("postError").textContent = "Rate limit exceeded. Posts remaining: " + (e.data.posts_remaining != null ? e.data.posts_remaining : 0);
    } else if (e.status === 403) {
      $("postError").textContent = "Session invalid (cookie/MAC validation failed). Reconnect to WiFi and refresh.";
    } else {
      $("postError").textContent = e.message || "Failed to post";
    }
    await refreshSession();
  }
}

async function vote(messageId, voteType) {
  try {
    await fetchJSON(API.vote, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId, vote_type: voteType }),
    });
    await refreshLive();
  } catch (e) {
    $("postError").textContent = e.status === 403 ? "Vote blocked (session invalid)." : "Vote failed.";
    await refreshSession();
  }
}

/* ---- UI helpers ---- */

function updateCharCount() {
  var content = $("content");
  var charCount = $("charCount");
  if (!content || !charCount) return;
  var used = content.value.length;
  var max = state.maxChars;
  var ratio = max > 0 ? used / max : 0;
  var remaining = typeof state.postsRemaining === "number" ? state.postsRemaining : "\u2026";
  var until = formatNextHour();
  charCount.textContent = used + "/" + max + " \u00b7 " + remaining + " msgs left until " + until;
  charCount.classList.remove("compose-field__hint--warn", "compose-field__hint--bad");
  if (ratio >= 1) {
    charCount.classList.add("compose-field__hint--bad");
  } else if (ratio >= 0.9) {
    charCount.classList.add("compose-field__hint--warn");
  }
}

function applyMaxChars() {
  var content = $("content");
  if (content) {
    content.setAttribute("maxlength", String(state.maxChars));
  }
  updateCharCount();
}

function applyNameMax() {
  var name = $("name");
  if (name) {
    name.setAttribute("maxlength", String(state.maxNameChars));
  }
}

function setConnectionStatus(connected) {
  var dot = $("statusDot");
  var label = $("sessionStatus");
  if (!dot || !label) return;
  if (connected) {
    label.textContent = "Connected";
    dot.className = "topbar__dot topbar__dot--ok";
  } else {
    label.textContent = "Offline";
    dot.className = "topbar__dot topbar__dot--bad";
  }
}

function validateNameLive() {
  var nameEl = $("name");
  var errorEl = $("postError");
  if (!nameEl || !errorEl) return;
  var name = nameEl.value.trim();
  if (!name) {
    if (errorEl.textContent.includes("Name")) errorEl.textContent = "";
    return;
  }
  if (name.length > state.maxNameChars) {
    errorEl.textContent = "Name is too long (" + name.length + "/" + state.maxNameChars + ").";
    return;
  }
  if (!state.namePattern.test(name)) {
    errorEl.textContent = "Name can only use A-Z, a-z, 0-9, - or _.";
    return;
  }
  if (errorEl.textContent.includes("Name")) {
    errorEl.textContent = "";
  }
}

/* ---- Init ---- */

async function init() {
  // Load saved name
  loadNameFromCookie();
  $("name").addEventListener("change", storeNameToCookie);
  $("name").addEventListener("blur", storeNameToCookie);
  $("name").addEventListener("input", validateNameLive);

  // View navigation
  $("welcomeStartBtn").addEventListener("click", showChannels);
  $("aboutLink").addEventListener("click", showWelcome);
  $("chatBackBtn").addEventListener("click", showChannels);

  // Compose modal
  $("fabBtn").addEventListener("click", openCompose);
  $("composeCancelBtn").addEventListener("click", closeCompose);
  $("composeOverlay").addEventListener("click", closeCompose);
  $("postBtn").addEventListener("click", postMessage);

  // Char count
  var content = $("content");
  if (content) {
    content.addEventListener("input", updateCharCount);
  }
  applyMaxChars();
  applyNameMax();

  // Fetch channels
  var data = await fetchJSON(API.channels, { method: "GET" });
  state.channels = normalizeChannels(data.channel_details || data.channels || []);
  state.meshChannelNames = new Set(state.channels.filter(function(c) { return c.scope === "mesh"; }).map(function(c) { return c.name; }));
  renderChannels();

  // Session + fingerprint
  state.fingerprint = await computeFingerprint();
  await sendFingerprint(state.fingerprint);
  await refreshSession();

  // Decide initial view: show welcome on first visit, channels on return
  var seenWelcome = localStorage.getItem("civicmesh_seen_welcome");
  if (seenWelcome) {
    showChannels();
  } else {
    showWelcome();
  }

  // Re-layout on resize (e.g. rotating tablet)
  window.addEventListener("resize", function() {
    if (isDesktop()) {
      $("viewChannels").classList.add("active");
      if (!state.activeChannel && !$("viewWelcome").classList.contains("active")) {
        $("viewWelcome").classList.add("active");
      }
    }
  });

  // Infinite scroll (scroll to top loads older)
  var wrap = $("messages");
  if (wrap) {
    wrap.addEventListener("scroll", function() {
      if (wrap.scrollTop <= 40) {
        loadOlderMessages();
      }
    });
  }

  // Polling
  state.polling = setInterval(function() {
    refreshSession();
    refreshLive();
  }, 8000);
  setInterval(function() {
    refreshRadioStatus();
  }, 15000);
}

window.addEventListener("load", init);
