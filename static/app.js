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
  maxNameChars: 10,
  expandedMeta: new Set(),
  postsRemaining: null,
  windowSec: 3600,
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
  refreshMessages(true);
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
  } catch (e) {
    setConnectionStatus(false);
    state.postsRemaining = null;
    updateCharCount();
    if (debug) {
      debug.hidden = true;
    }
  }
}

async function refreshMessages(scrollTop = false) {
  if (!state.activeChannel) return;
  $("postError").textContent = "";
  try {
    const url = `${API.messages}?channel=${encodeURIComponent(state.activeChannel)}&limit=80&offset=0`;
    const data = await fetchJSON(url, { method: "GET" });
    renderMessages((data.messages || []).slice().reverse());
    if (scrollTop) {
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

async function postMessage() {
  const name = $("name").value.trim();
  const content = $("content").value.trim();
  const channel = state.activeChannel;
  $("postError").textContent = "";
  if (!name) {
    $("postError").textContent = "Name is required to post.";
    return;
  }
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
    await refreshMessages(true);
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
    await refreshMessages();
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
  $("refreshBtn").onclick = () => refreshMessages(true);
  $("postBtn").onclick = () => postMessage();
  const channelToggle = $("channelToggle");
  if (channelToggle) {
    channelToggle.onclick = () => {
      const collapsed = document.body.classList.toggle("channels-collapsed");
      channelToggle.textContent = collapsed ? ">" : "v";
      channelToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
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
  state.activeChannel = state.channels[0] ? state.channels[0].name : null;
  if (state.activeChannel) {
    const ch = getChannel(state.activeChannel);
    const label = scopeLabel(ch ? ch.scope : "mesh");
    const icon = ch && ch.scope === "mesh"
      ? '<span class="channel__icon channel__icon--title" aria-hidden="true"></span>'
      : '<span class="channel__pin channel__pin--title" aria-hidden="true">📍</span>';
    $("channelTitle").innerHTML = `${icon}${label} · ${escapeHtml(state.activeChannel)}`;
    updatePostButton(ch);
  } else {
    $("channelTitle").textContent = "No channels configured";
    updatePostButton(null);
  }
  renderChannels();
  state.fingerprint = await computeFingerprint();
  await sendFingerprint(state.fingerprint);
  await refreshSession();
  await refreshMessages(true);

  // Poll
  state.polling = setInterval(() => {
    refreshSession();
    refreshMessages();
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
