const API = {
  channels: "/api/channels",
  messages: "/api/messages",
  post: "/api/post",
  vote: "/api/vote",
  session: "/api/session",
};

function $(id) {
  return document.getElementById(id);
}

function fmtShortTimestamp(ts) {
  try {
    const d = new Date(ts * 1000);
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    let hours = d.getHours();
    const minutes = String(d.getMinutes()).padStart(2, "0");
    const ampm = hours >= 12 ? "pm" : "am";
    hours = hours % 12;
    if (hours === 0) hours = 12;
    return `[${year}-${month}-${day} ${hours}:${minutes} ${ampm}]`;
  } catch {
    return `[${String(ts)}]`;
  }
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

function ensureSession() {
  if (!getCookie("civicmesh_session")) {
    // Generate client-side; server validates via DB + MAC
    const sid = crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + String(Date.now());
    setCookie("civicmesh_session", sid);
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
  liveSize: 80,
  pageSize: 80,
  loadingOlder: false,
  hasMore: true,
  history: [],
  live: [],
};

function normalizeChannels(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.map((ch) => {
    if (typeof ch === "string") {
      const scope = ch === "#local" ? "on-site" : "mesh";
      return { name: ch, scope };
    }
    if (ch && typeof ch === "object") {
      return {
        name: String(ch.name || ""),
        scope: ch.scope === "on-site" ? "on-site" : "mesh",
      };
    }
    return { name: "", scope: "mesh" };
  }).filter((ch) => ch.name);
}

function scopeLabel(scope) {
  return scope === "on-site" ? "On-site" : "Mesh";
}

function getChannel(name) {
  return state.channels.find((c) => c.name === name) || null;
}

function setActiveChannel(name) {
  state.activeChannel = name;
  const ch = getChannel(name);
  const label = ch ? scopeLabel(ch.scope) : "Mesh";
  const icon = ch && ch.scope === "mesh"
    ? '<span class="channel__icon channel__icon--title" aria-hidden="true"></span>'
    : '<span class="channel__pin channel__pin--title" aria-hidden="true">📍</span>';
  $("channelTitle").innerHTML = ch
    ? `${icon}${label} · ${escapeHtml(name)}`
    : `${label} · ${escapeHtml(name)}`;
  updatePostButton(ch);
  renderChannels();
  const helpToggle = $("helpToggle");
  if (helpToggle) helpToggle.classList.remove("menu-entry--active");
  const welcome = $("welcomeBox");
  if (welcome) welcome.hidden = true;
  const chatPanel = document.querySelector(".panel--main");
  if (chatPanel) chatPanel.hidden = false;
  const docsPanel = document.querySelector(".panel--docs");
  if (docsPanel) docsPanel.hidden = true;
  const composer = document.querySelector(".composer");
  if (composer) composer.hidden = false;
  document.body.classList.add("channels-collapsed");
  const toggle = $("channelToggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", "false");
  }
  state.history = [];
  state.live = [];
  state.hasMore = true;
  state.loadingOlder = false;
  refreshLive(true);
}

function updatePostButton(ch) {
  const btn = $("postBtn");
  const label = ch && ch.scope === "on-site" ? "Post On-site" : "Post to Mesh";
  btn.textContent = label;
  btn.disabled = !ch;
  const hint = $("channelHint");
  if (hint) hint.hidden = Boolean(ch);
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

function renderChannels() {
  const wrap = $("channels");
  wrap.innerHTML = "";

  for (const ch of state.channels) {
    const btn = document.createElement("div");
    btn.className = "channel" + (ch.name === state.activeChannel ? " channel--active" : "");
    const listIcon = ch.scope === "mesh"
      ? '<span class="channel__icon" aria-hidden="true"></span>'
      : '<span class="channel__pin" aria-hidden="true">📍</span>';
    const metaIcon = ch.scope === "mesh"
      ? '<span class="channel__icon" aria-hidden="true"></span>'
      : '<span class="channel__pin" aria-hidden="true">📍</span>';
    btn.innerHTML = `<div class="channel__name">${listIcon}${ch.name}</div><div class="channel__meta">${metaIcon}${scopeLabel(ch.scope)}</div>`;
    btn.onclick = () => {
      setActiveChannel(ch.name);
    };
    wrap.appendChild(btn);
  }
}

function renderMessages(msgs) {
  const wrap = $("messages");
  wrap.innerHTML = "";
  for (const m of msgs) {
    const div = document.createElement("div");
    div.className = "msg msg--compact" + (m.pending ? " msg--pending" : "");

    const uv = Number(m.upvotes || 0);
    const my = Number(m.user_vote || 0);
    const metaId = `meta-${String(m.id).replace(/[^a-zA-Z0-9_-]/g, "_")}`;
    const sid = getCookie("civicmesh_session") || "";
    const canVote = !m.session_id || m.session_id !== sid;

    const upActive = my === 1 ? " active-up" : "";

    const badge = uv > 0 ? `<span class="msg__badge">★${uv}</span>` : "";
    let actions = "";
    if (m.pending) {
      actions = `<span class="msg__meta">Queued for mesh broadcast</span>`;
    } else if (canVote) {
      actions = `<button class="btn btn--vote${upActive}" data-id="${m.id}" data-v="1" data-my="${my}" hidden>★</button>`;
    }

    div.innerHTML = `
      <div class="msg__line">
        <span class="msg__time">${fmtShortTimestamp(m.ts)}</span>
        ${sourceIcon(m.source)}
        <span class="msg__who">&lt;${escapeHtml(m.sender || "unknown")}&gt;</span>
        <span class="msg__body-inline">${escapeHtml(m.content)}</span>
        ${badge}
        ${actions}
      </div>
      <div id="${metaId}" class="msg__meta-panel" hidden>
        <div><span class="msg__meta-key">source</span>${escapeHtml(sourceLabel(m.source))}</div>
        <div><span class="msg__meta-key">user ID</span><span class="msg__meta-value">${escapeHtml(m.fingerprint || "unknown")}</span></div>
      </div>
    `;

    if (state.expandedMeta.has(metaId)) {
      const panel = div.querySelector(`#${metaId}`);
      if (panel) panel.hidden = false;
      const actionsEl = div.querySelector(".btn.btn--vote");
      if (actionsEl) actionsEl.hidden = false;
    }
    div.addEventListener("click", () => {
      const panel = div.querySelector(`#${metaId}`);
      if (!panel) return;
      panel.hidden = !panel.hidden;
      if (panel.hidden) {
        state.expandedMeta.delete(metaId);
        const actionsEl = div.querySelector(".btn.btn--vote");
        if (actionsEl) actionsEl.hidden = true;
      } else {
        state.expandedMeta.add(metaId);
        const actionsEl = div.querySelector(".btn.btn--vote");
        if (actionsEl) actionsEl.hidden = false;
      }
    });
    wrap.appendChild(div);
  }

  wrap.querySelectorAll("button[data-id][data-v]").forEach((b) => {
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = Number(b.getAttribute("data-id"));
      const v = Number(b.getAttribute("data-v"));
      const current = Number(b.getAttribute("data-my") || "0");
      const next = current === v ? 0 : v;
      await vote(id, next);
    });
  });

  // No inline metadata toggle buttons; whole message toggles metadata panel.
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function sourceLabel(source) {
  if (source === "local") return "On-site";
  if (source === "mesh") return "Mesh";
  if (source === "wifi") return "WiFi";
  if (source === "pending") return "Queued";
  return String(source || "");
}

function sourceIcon(source) {
  if (source === "mesh") return '<span class="msg__icon msg__icon--mesh" aria-hidden="true"></span>';
  if (source === "local" || source === "wifi") return '<span class="msg__icon msg__icon--local" aria-hidden="true">📍</span>';
  return "";
}

async function refreshSession() {
  const debug = $("debugBanner");
  try {
    const data = await fetchJSON(API.session, { method: "GET" });
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
  const url = `${API.messages}?channel=${encodeURIComponent(state.activeChannel)}&limit=${limit}&offset=${offset}`;
  const data = await fetchJSON(url, { method: "GET" });
  return data.messages || [];
}

function renderAllMessages() {
  renderMessages(state.history.concat(state.live));
}

async function refreshLive(scrollBottom = false) {
  if (!state.activeChannel) return;
  $("postError").textContent = "";
  try {
    const rows = await fetchMessagesPage(0, state.liveSize);
    state.live = rows.slice().reverse();
    renderAllMessages();
    if (scrollBottom) {
      const wrap = $("messages");
      if (wrap) wrap.scrollTop = wrap.scrollHeight;
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
  const wrap = $("messages");
  const prevHeight = wrap ? wrap.scrollHeight : 0;
  state.loadingOlder = true;
  try {
    const offset = state.liveSize + state.history.length;
    const rows = await fetchMessagesPage(offset, state.pageSize);
    const older = rows.slice().reverse();
    if (older.length === 0) {
      state.hasMore = false;
    } else {
      state.history = older.concat(state.history);
    }
    renderAllMessages();
    if (wrap) {
      const newHeight = wrap.scrollHeight;
      wrap.scrollTop = newHeight - prevHeight + wrap.scrollTop;
    }
  } catch (e) {
    $("postError").textContent = e.message || "Failed to load messages";
  } finally {
    state.loadingOlder = false;
  }
}

async function postMessage() {
  const name = $("name").value.trim();
  const content = $("content").value.trim();
  const channel = state.activeChannel;
  $("postError").textContent = "";
  if (!name) {
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
    $("postError").textContent = `Message is too long (${content.length}/${state.maxChars}).`;
    return;
  }

  try {
    await fetchJSON(API.post, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, channel, content, fingerprint: state.fingerprint }),
    });
    storeNameToCookie();
    $("content").value = "";
    await refreshSession();
    await refreshLive(true);
  } catch (e) {
    if (e.status === 429 && e.data) {
      $("postError").textContent = `Rate limit exceeded. Posts remaining: ${e.data.posts_remaining ?? 0}`;
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

async function init() {
  ensureSession();
  loadNameFromCookie();
  $("name").addEventListener("change", storeNameToCookie);
  $("name").addEventListener("blur", storeNameToCookie);
  $("name").addEventListener("input", validateNameLive);
  $("refreshBtn").onclick = () => refreshLive(true);
  $("postBtn").onclick = () => postMessage();
  const channelToggle = $("channelToggle");
  if (channelToggle) {
    channelToggle.onclick = () => {
      const collapsed = document.body.classList.toggle("channels-collapsed");
      channelToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    };
  }
  const helpToggle = $("helpToggle");
  const helpPanel = document.querySelector(".panel--docs");
  const helpClose = $("helpClose");
  if (helpToggle && helpPanel) {
    helpToggle.onclick = () => {
      helpPanel.hidden = false;
      const chatPanel = document.querySelector(".panel--main");
      if (chatPanel) chatPanel.hidden = true;
      const composer = document.querySelector(".composer");
      if (composer) composer.hidden = true;
      const welcome = $("welcomeBox");
      if (welcome) welcome.hidden = true;
      state.activeChannel = null;
      state.expandedMeta.clear();
      updatePostButton(null);
      renderChannels();
      helpToggle.classList.add("menu-entry--active");
    };
  }
  if (helpClose && helpPanel) {
    helpClose.onclick = () => {
      helpPanel.hidden = true;
      if (state.activeChannel) {
        const chatPanel = document.querySelector(".panel--main");
        if (chatPanel) chatPanel.hidden = false;
        const composer = document.querySelector(".composer");
        if (composer) composer.hidden = false;
      }
      helpToggle.classList.remove("menu-entry--active");
    };
  }
  const content = $("content");
  if (content) {
    content.addEventListener("input", updateCharCount);
  }
  applyMaxChars();
  applyNameMax();

  const data = await fetchJSON(API.channels, { method: "GET" });
  state.channels = normalizeChannels(data.channel_details || data.channels || []);
  state.activeChannel = null;
  $("channelTitle").textContent = "";
  updatePostButton(null);
  state.history = [];
  state.live = [];
  state.hasMore = true;
  state.loadingOlder = false;
  const chatPanel = document.querySelector(".panel--main");
  if (chatPanel) chatPanel.hidden = true;
  const docsPanel = document.querySelector(".panel--docs");
  if (docsPanel) docsPanel.hidden = true;
  const composer = document.querySelector(".composer");
  if (composer) composer.hidden = true;
  renderChannels();
  state.fingerprint = await computeFingerprint();
  await sendFingerprint(state.fingerprint);
  await refreshSession();
  await refreshLive(true);

  const wrap = $("messages");
  if (wrap) {
    wrap.addEventListener("scroll", () => {
      if (wrap.scrollTop <= 40) {
        loadOlderMessages();
      }
    });
  }

  // Poll
  state.polling = setInterval(() => {
    refreshSession();
    refreshLive();
  }, 8000);
}

window.addEventListener("load", init);
function updateCharCount() {
  const content = $("content");
  const charCount = $("charCount");
  if (!content || !charCount) return;
  const used = content.value.length;
  const max = state.maxChars;
  const ratio = max > 0 ? used / max : 0;
  const remaining = typeof state.postsRemaining === "number" ? state.postsRemaining : "…";
  const until = formatNextHour();
  charCount.textContent = `${used}/${max} · ${remaining} msgs left until ${until}`;
  charCount.classList.remove("hint--warn", "hint--bad");
  if (ratio >= 1) {
    charCount.classList.add("hint--bad");
  } else if (ratio >= 0.9) {
    charCount.classList.add("hint--warn");
  }
}

function applyMaxChars() {
  const content = $("content");
  if (content) {
    content.setAttribute("maxlength", String(state.maxChars));
  }
  updateCharCount();
}

function applyNameMax() {
  const name = $("name");
  if (name) {
    name.setAttribute("maxlength", String(state.maxNameChars));
  }
}

function setConnectionStatus(connected) {
  const pill = $("sessionStatus");
  if (!pill) return;
  if (connected) {
    pill.textContent = "Connected";
    pill.className = "pill pill--ok";
  } else {
    pill.textContent = "NOT connected";
    pill.className = "pill pill--bad";
  }
}

function validateNameLive() {
  const nameEl = $("name");
  const errorEl = $("postError");
  if (!nameEl || !errorEl) return;
  const name = nameEl.value.trim();
  if (!name) {
    if (errorEl.textContent.includes("Name")) errorEl.textContent = "";
    return;
  }
  if (name.length > state.maxNameChars) {
    errorEl.textContent = `Name is too long (${name.length}/${state.maxNameChars}).`;
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
