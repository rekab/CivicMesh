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

function fmtTime(ts) {
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return String(ts);
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
    div.className = "msg" + (m.pending ? " msg--pending" : "");

    const uv = Number(m.upvotes || 0);
    const dv = Number(m.downvotes || 0);
    const my = Number(m.user_vote || 0);
    const metaId = `meta-${String(m.id).replace(/[^a-zA-Z0-9_-]/g, "_")}`;

    const upActive = my === 1 ? " active-up" : "";
    const downActive = my === -1 ? " active-down" : "";

    const actions = m.pending
      ? `<div class="msg__actions"><span class="msg__meta">Queued for mesh broadcast</span></div>`
      : `
      <div class="msg__actions">
        <button class="btn btn--vote${upActive}" data-id="${m.id}" data-v="1" data-my="${my}">▲ ${uv}</button>
        <button class="btn btn--vote${downActive}" data-id="${m.id}" data-v="-1" data-my="${my}">▼ ${dv}</button>
      </div>
    `;

    div.innerHTML = `
      <div class="msg__top">
        <div>
          <span class="msg__who">${escapeHtml(m.sender || "unknown")}</span>
          <span class="msg__meta"> · ${escapeHtml(sourceLabel(m.source))} · ${fmtTime(m.ts)}</span>
          <button class="msg__meta-toggle" data-target="${metaId}" type="button">metadata</button>
        </div>
        <div class="msg__meta">${escapeHtml(m.channel)}</div>
      </div>
      <div class="msg__body">${escapeHtml(m.content)}</div>
      <div id="${metaId}" class="msg__meta-panel" hidden>
        <div><span class="msg__meta-key">session</span>${escapeHtml(m.session_id || "unknown")}</div>
        <div><span class="msg__meta-key">fingerprint</span>${escapeHtml(m.fingerprint || "unknown")}</div>
        <div><span class="msg__meta-key">source</span>${escapeHtml(sourceLabel(m.source))}</div>
        <div><span class="msg__meta-key">channel</span>${escapeHtml(m.channel)}</div>
        <div><span class="msg__meta-key">timestamp</span>${fmtTime(m.ts)}</div>
      </div>
      ${actions}
    `;

    wrap.appendChild(div);
  }

  wrap.querySelectorAll("button[data-id][data-v]").forEach((b) => {
    b.addEventListener("click", async () => {
      const id = Number(b.getAttribute("data-id"));
      const v = Number(b.getAttribute("data-v"));
      const current = Number(b.getAttribute("data-my") || "0");
      const next = current === v ? 0 : v;
      await vote(id, next);
    });
  });

  wrap.querySelectorAll("button.msg__meta-toggle").forEach((b) => {
    b.addEventListener("click", () => {
      const target = b.getAttribute("data-target");
      if (!target) return;
      const panel = document.getElementById(target);
      if (!panel) return;
      panel.hidden = !panel.hidden;
    });
  });
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
  const pill = $("sessionStatus");
  const rate = $("rateLimit");
  const debug = $("debugBanner");
  try {
    const data = await fetchJSON(API.session, { method: "GET" });
    pill.textContent = "Session: OK";
    pill.className = "pill pill--ok";
    rate.textContent = `Posts remaining this hour: ${data.posts_remaining}`;
    if (debug) {
      debug.hidden = !data.debug_session;
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
    pill.textContent = "Session: not validated";
    pill.className = "pill pill--warn";
    if (debug) {
      debug.hidden = true;
    }
    if (e && e.status === 429 && e.data && typeof e.data.posts_remaining !== "undefined") {
      rate.textContent = `Posts remaining this hour: ${e.data.posts_remaining}`;
    }
  }
}

async function refreshMessages(scrollTop = false) {
  if (!state.activeChannel) return;
  $("postError").textContent = "";
  try {
    const url = `${API.messages}?channel=${encodeURIComponent(state.activeChannel)}&limit=80&offset=0`;
    const data = await fetchJSON(url, { method: "GET" });
    renderMessages(data.messages || []);
    if (scrollTop) window.scrollTo({ top: 0, behavior: "instant" });
  } catch (e) {
    $("postError").textContent = e.message || "Failed to load messages";
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
    await refreshMessages();
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
  charCount.textContent = `${used}/${max}`;
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
