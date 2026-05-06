const API = {
  channels: "/api/channels",
  messages: "/api/messages",
  post: "/api/post",
  vote: "/api/vote",
  session: "/api/session",
  status: "/api/status",
  stats: "/api/stats",
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
  namePattern: /^[^:@]+$/,
  expandedMeta: new Set(),
  postsRemaining: null,
  windowSec: 3600,
  radioStatus: "unknown",
  recoveryState: null,
  liveSize: 80,
  pageSize: 80,
  loadingOlder: false,
  hasMore: true,
  history: [],
  live: [],
  meshChannelNames: new Set(),
  libraries: {},
  activeLibrary: null,
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
    $("viewLibrary").classList.remove("active");
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
    $("viewLibrary").classList.remove("active");
    $("viewWelcome").classList.add("active");
  } else {
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $("viewWelcome").classList.add("active");
  }
}

function showChannels() {
  localStorage.setItem("civicmesh_seen_welcome", "1");
  state.activeLibrary = null;
  if (isDesktop()) {
    // On desktop, channels is always visible; show welcome or chat in main area
    $("viewChannels").classList.add("active");
    $("viewLibrary").classList.remove("active");
    // If no active channel, show welcome; otherwise keep chat
    if (!state.activeChannel) {
      $("viewWelcome").classList.add("active");
      $("viewChat").classList.remove("active");
    }
  } else {
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $("viewChannels").classList.add("active");
  }
  renderChannels();
}

function showChat() {
  if (isDesktop()) {
    $("viewChannels").classList.add("active");
    $("viewWelcome").classList.remove("active");
    $("viewLibrary").classList.remove("active");
    $("viewChat").classList.add("active");
  } else {
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $("viewChat").classList.add("active");
  }
}

function showLibrary() {
  if (isDesktop()) {
    $("viewChannels").classList.add("active");
    $("viewWelcome").classList.remove("active");
    $("viewChat").classList.remove("active");
    $("viewLibrary").classList.add("active");
  } else {
    document.querySelectorAll(".view").forEach(function(v) { v.classList.remove("active"); });
    $("viewLibrary").classList.add("active");
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
    ? "Stays on site"
    : "Reaches the wider network";
}

function getChannel(name) {
  return state.channels.find(function(c) { return c.name === name; }) || null;
}

function channelIconSvg(scope) {
  if (scope === "on-site") {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z"/><circle cx="12" cy="9" r="2.5"/></svg>';
  }
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="2em" height="2em" aria-label="CivicMesh"><path d="M 13.17 18.83 A 4 4 0 0 1 13.17 13.17" stroke="#2e8b57" stroke-width="1.8" fill="none" stroke-linecap="round"/><path d="M 10.34 21.66 A 8 8 0 0 1 10.34 10.34" stroke="#2e8b57" stroke-width="1.8" fill="none" stroke-linecap="round"/><path d="M 7.51 24.49 A 12 12 0 0 1 7.51 7.51" stroke="#2e8b57" stroke-width="1.8" fill="none" stroke-linecap="round"/><line x1="16" y1="16" x2="20" y2="12" stroke="#1e4f8a" stroke-width="0.7"/><line x1="16" y1="16" x2="21" y2="20" stroke="#1e4f8a" stroke-width="0.7"/><line x1="20" y1="12" x2="21" y2="20" stroke="#1e4f8a" stroke-width="0.7"/><line x1="20" y1="12" x2="25" y2="9" stroke="#1e4f8a" stroke-width="0.7"/><line x1="20" y1="12" x2="27" y2="16" stroke="#1e4f8a" stroke-width="0.7"/><line x1="21" y1="20" x2="27" y2="16" stroke="#1e4f8a" stroke-width="0.7"/><line x1="21" y1="20" x2="24" y2="23" stroke="#1e4f8a" stroke-width="0.7"/><line x1="25" y1="9" x2="27" y2="16" stroke="#1e4f8a" stroke-width="0.7"/><line x1="27" y1="16" x2="24" y2="23" stroke="#1e4f8a" stroke-width="0.7"/><circle cx="16" cy="16" r="2.2" fill="#1e4f8a"/><circle cx="20" cy="12" r="1.7" fill="#1e4f8a"/><circle cx="21" cy="20" r="1.7" fill="#1e4f8a"/><circle cx="25" cy="9" r="1.7" fill="#1e4f8a"/><circle cx="27" cy="16" r="1.7" fill="#1e4f8a"/><circle cx="24" cy="23" r="1.7" fill="#1e4f8a"/></svg>';
}

function renderChannels() {
  var wrap = $("channelList");
  wrap.innerHTML = "";

  var locals = state.channels.filter(function(c){ return c.scope !== "mesh"; });
  var meshes = state.channels.filter(function(c){ return c.scope === "mesh"; });

  function appendGroup(title, list) {
    if (!list.length) return;
    var header = document.createElement("div");
    header.className = "channel-group__header";
    header.innerHTML = '<div class="channel-group__title">' + escapeHtml(title) + '</div>';
    wrap.appendChild(header);

    for (var i = 0; i < list.length; i++) {
      var ch = list[i];
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

  appendGroup("Channels on this hotspot", locals);
  appendGroup("MeshCore channels", meshes);
  appendLibraryGroup(wrap);
}

/* ---- Reference library rendering ---- */

function humanize(slug) {
  return String(slug || "")
    .split(/[-_]/)
    .filter(function(s) { return s.length > 0; })
    .map(function(s) { return s.charAt(0).toUpperCase() + s.slice(1); })
    .join(" ");
}

function fmtBytesShort(n) {
  if (typeof n !== "number" || isNaN(n)) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

function fmtBuiltAtDate(s) {
  if (!s) return "—";
  var m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[1] + "-" + m[2] + "-" + m[3] : String(s);
}

function libraryDocCount(lib) {
  if (!lib || !Array.isArray(lib.categories)) return 0;
  var n = 0;
  for (var i = 0; i < lib.categories.length; i++) {
    var cat = lib.categories[i];
    if (Array.isArray(cat.docs)) n += cat.docs.length;
  }
  return n;
}

function libraryIconSvg() {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h9l4 4v14H6z"/><polyline points="14 3 14 8 19 8"/><line x1="9" y1="13" x2="16" y2="13"/><line x1="9" y1="17" x2="16" y2="17"/></svg>';
}

function appendLibraryGroup(wrap) {
  var slugs = Object.keys(state.libraries).sort();
  if (!slugs.length) return;
  var header = document.createElement("div");
  header.className = "channel-group__header channel-group__header--library";
  header.innerHTML = '<div class="channel-group__title">Reference</div>';
  wrap.appendChild(header);
  for (var i = 0; i < slugs.length; i++) {
    var slug = slugs[i];
    var lib = state.libraries[slug];
    var displayTitle = lib.title || humanize(slug);
    var docCount = libraryDocCount(lib);
    var card = document.createElement("div");
    card.className = "channel-card channel-card--library" +
      (slug === state.activeLibrary ? " channel-card--active" : "");
    card.innerHTML =
      '<div class="channel-card__icon channel-card__icon--library">' + libraryIconSvg() + '</div>' +
      '<div class="channel-card__body">' +
        '<div class="channel-card__name">' + escapeHtml(displayTitle) + '</div>' +
        '<div class="channel-card__desc">' + docCount + ' documents · read &amp; download</div>' +
      '</div>' +
      '<div class="channel-card__meta">' +
        '<div class="channel-card__badge channel-card__badge--library">FILES</div>' +
      '</div>';
    card.setAttribute("data-library", slug);
    card.addEventListener("click", (function(s) {
      return function() { setActiveLibrary(s); };
    })(slug));
    wrap.appendChild(card);
  }
}

function setActiveLibrary(slug) {
  state.activeLibrary = slug;
  state.activeChannel = null;
  renderChannels();
  renderLibrary(slug);
  showLibrary();
}

function renderLibrary(slug) {
  var lib = state.libraries[slug];
  if (!lib) return;
  var displayTitle = lib.title || humanize(slug);
  $("libTitle").textContent = displayTitle;

  var prov = $("libProvenance");
  prov.innerHTML = "";
  if (lib.source_label) {
    var srcLine = document.createElement("div");
    srcLine.className = "lib__provenance-meta";
    srcLine.textContent = "Mirrored from " + lib.source_label;
    prov.appendChild(srcLine);
  }
  if (lib.note) {
    var noteEl = document.createElement("div");
    noteEl.className = "lib__provenance-note";
    noteEl.textContent = lib.note;
    prov.appendChild(noteEl);
  }
  var sumLine = document.createElement("div");
  sumLine.className = "lib__provenance-meta lib__provenance-meta--summary";
  sumLine.textContent = libraryDocCount(lib) + " documents · last sync " + fmtBuiltAtDate(lib.built_at);
  prov.appendChild(sumLine);

  var list = $("libList");
  list.innerHTML = "";
  var cats = Array.isArray(lib.categories) ? lib.categories : [];
  for (var ci = 0; ci < cats.length; ci++) {
    var cat = cats[ci];
    var section = document.createElement("section");
    section.className = "lib__category";
    var ch = document.createElement("h3");
    ch.className = "lib__category-title";
    ch.textContent = cat.name || "";
    section.appendChild(ch);
    var docs = Array.isArray(cat.docs) ? cat.docs : [];
    for (var di = 0; di < docs.length; di++) {
      section.appendChild(buildLibDocRow(slug, cat.name || "", docs[di]));
    }
    list.appendChild(section);
  }
}

function buildLibDocRow(slug, categoryName, doc) {
  var row = document.createElement("button");
  row.type = "button";
  row.className = "lib-doc";
  var langChip = "";
  if (doc.lang && doc.lang !== "en") {
    langChip = '<span class="lib-doc__lang-chip">' + escapeHtml(String(doc.lang).toUpperCase()) + '</span>';
  }
  var meta = [];
  if (doc.published) meta.push("published " + escapeHtml(doc.published));
  if (typeof doc.size_bytes === "number") meta.push(fmtBytesShort(doc.size_bytes));
  row.innerHTML =
    '<div class="lib-doc__icon" aria-hidden="true"><span class="lib-doc__icon-label">PDF</span></div>' +
    '<div class="lib-doc__body">' +
      '<div class="lib-doc__title">' + escapeHtml(doc.title || doc.filename || "") + langChip + '</div>' +
      '<div class="lib-doc__meta">' + meta.join('<span class="lib-doc__meta-sep"> · </span>') + '</div>' +
    '</div>';
  row.addEventListener("click", function() {
    openLibDetail(slug, categoryName, doc);
  });
  return row;
}

function openLibDetail(slug, categoryName, doc) {
  var lib = state.libraries[slug];
  $("libDetailTitle").textContent = doc.title || doc.filename || "";
  $("libDetailSub").textContent = (lib && lib.source_label)
    ? "Source: " + lib.source_label
    : "";

  var rows = $("libDetailRows");
  rows.innerHTML = "";
  function addRow(label, value) {
    if (value === null || value === undefined || value === "") return;
    var dt = document.createElement("dt");
    dt.className = "lib-detail__row-lbl";
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.className = "lib-detail__row-val";
    dd.textContent = value;
    rows.appendChild(dt);
    rows.appendChild(dd);
  }

  addRow("Category", categoryName);
  addRow("Language", doc.lang || "");
  if (doc.published) addRow("Published", doc.published);
  addRow("Last reviewed", doc.last_reviewed || "");
  if (typeof doc.size_bytes === "number") addRow("Size", fmtBytesShort(doc.size_bytes));
  if (lib && lib.source_label) addRow("Source", lib.source_label);

  var dl = $("libDetailDownload");
  dl.href = "/var/" + encodeURIComponent(slug) + "/" + encodeURIComponent(doc.filename || "");
  if (doc.filename) {
    dl.setAttribute("download", doc.filename);
  } else {
    dl.removeAttribute("download");
  }

  $("libDetailOverlay").classList.add("active");
  requestAnimationFrame(function() {
    $("libDetailSheet").classList.add("active");
  });
}

function closeLibDetail() {
  $("libDetailSheet").classList.remove("active");
  $("libDetailOverlay").classList.remove("active");
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
    ? "Your message will stay at this node"
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
  return String(source || "");
}

function renderMessages(msgs) {
  var wrap = $("messageInner") || $("messages");
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
    var isPending = m.source === "wifi" && m.status === "queued";
    var isFailed = m.source === "wifi" && m.status === "failed";
    var div = document.createElement("div");
    div.className = "msg-bubble" +
      (isOwn ? " msg-bubble--own" : "") +
      (isPending ? " msg-bubble--pending" : "") +
      (isFailed ? " msg-bubble--failed" : "");

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
    if (!isPending && !isFailed && canVote) {
      voteHtml = '<button class="btn--vote' + upActive + '" data-id="' + m.id + '" data-v="1" data-my="' + my + '">\u2605 Upvote' + badge + '</button>';
    } else if (uv > 0) {
      voteHtml = '<span>\u2605 ' + uv + '</span>';
    }

    var timeText = isPending ? "Queued for mesh" : isFailed ? "Failed to send" : fmtTime(m.ts);
    var senderText = isOwn && (isPending || isFailed) ? "You" : escapeHtml(m.sender || "unknown");

    var detailRows = "";
    if (isPending) {
      detailRows = '<div class="msg-bubble__detail-row"><span>Status</span><span>Queued \u2014 will send when radio connects</span></div>';
    } else if (isFailed) {
      detailRows = '<div class="msg-bubble__detail-row"><span>Status</span><span>Failed to send \u2014 radio may have been offline</span></div>' +
        (typeof m.retry_count === "number" ? '<div class="msg-bubble__detail-row"><span>Retries</span><span>' + m.retry_count + '/3</span></div>' : '');
    } else {
      detailRows =
        '<div class="msg-bubble__detail-row"><span>Source</span><span>' + escapeHtml(sourceLabel(m.source)) + '</span></div>' +
        '<div class="msg-bubble__detail-row"><span>Time</span><span>' + escapeHtml(fmtFullTimestamp(m.ts)) + '</span></div>' +
        (voteHtml ? '<div style="margin-top:4px">' + voteHtml + '</div>' : '') +
        (m.source === "wifi" && state.meshChannelNames.has(state.activeChannel) && typeof m.heard_count === "number" && m.heard_count > 0
          ? '<div class="msg-bubble__detail-row"><span>Heard</span><span>' + m.heard_count + ' repeats</span></div>'
          : '');
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
  if (!isMesh || state.radioStatus === "online") {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  if (state.radioStatus === "recovering") {
    banner.textContent = "Radio reconnecting \u2014 messages will queue and send when it\u2019s back.";
  } else if (state.radioStatus === "needs_human") {
    banner.textContent = "Radio needs attention \u2014 messages will queue and send once it\u2019s working.";
  } else {
    banner.textContent = "Radio offline \u2014 messages will queue but won\u2019t transmit yet.";
  }
}

// Radio status is the sole driver of the header indicator. When the
// browser can't reach the Pi (fetch throws), the indicator goes stale
// for up to ~15s until this function runs again. This is acceptable —
// adding a separate connectivity indicator would conflate two signals.
async function refreshRadioStatus() {
  try {
    var data = await fetchJSON(API.status, { method: "GET" });
    state.radioStatus = data.radio_status || "offline";
    state.recoveryState = data.recovery_state || null;
    state.nodeName = data.node_name || data.hub_name || state.nodeName;
  } catch {
    state.radioStatus = "offline";
    state.recoveryState = null;
  }
  setRadioStatus(state.radioStatus);
  updateRadioBanner();
}

/* ---- Data fetching ---- */

async function refreshSession() {
  var debug = $("debugBanner");
  try {
    var data = await fetchJSON(API.session, { method: "GET" });
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

function setRadioStatus(status) {
  var pairs = [
    { dot: $("topbarRadioDot"), line: $("topbarRadioLine") },
    { dot: $("desktopRadioDot"), line: $("desktopRadioLine") },
  ];
  var label, dotClass, lineClass, alertGlyph;
  switch (status) {
    case "online":
      label = "Mesh Radio";
      dotClass = "topbar__mini-dot topbar__mini-dot--ok";
      lineClass = "topbar__status-line";
      alertGlyph = "";
      break;
    case "recovering":
      label = "Mesh Radio reconnecting";
      dotClass = "topbar__mini-dot topbar__mini-dot--warn";
      lineClass = "topbar__status-line";
      alertGlyph = "";
      break;
    case "offline":
      label = "Mesh Radio offline";
      dotClass = "topbar__mini-dot topbar__mini-dot--alert";
      lineClass = "topbar__status-line topbar__status-line--muted";
      alertGlyph = "";
      break;
    case "needs_human":
      label = "Mesh Radio needs attention";
      dotClass = "topbar__mini-dot topbar__mini-dot--alert";
      lineClass = "topbar__status-line topbar__status-line--muted";
      alertGlyph = '<svg class="topbar__warn-glyph" viewBox="0 0 12 12" aria-hidden="true">'
                 + '<path d="M6 1.5 L11 10.5 L1 10.5 Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'
                 + '<line x1="6" y1="5" x2="6" y2="7.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>'
                 + '<circle cx="6" cy="9" r="0.6" fill="currentColor"/>'
                 + '</svg>';
      break;
    default:
      label = "Mesh Radio offline";
      dotClass = "topbar__mini-dot topbar__mini-dot--alert";
      lineClass = "topbar__status-line topbar__status-line--muted";
      alertGlyph = "";
  }
  pairs.forEach(function(p) {
    if (!p.dot || !p.line) return;
    p.dot.className = dotClass;
    p.dot.innerHTML = "";
    p.line.className = lineClass;
    p.line.innerHTML = "";
    p.line.appendChild(p.dot);
    if (alertGlyph) p.line.insertAdjacentHTML("beforeend", alertGlyph);
    p.line.insertAdjacentText("beforeend", label);
  });
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
    errorEl.textContent = "Pick any character but : and @. That\u2019s the ^[^:@]+$!";
    return;
  }
  if (errorEl.textContent.includes("Name")) {
    errorEl.textContent = "";
  }
}

/* ---- Node Stats Overlay ---- */

var statsState = {
  open: false,
  range: "hour",
  data: null,
  liveTimer: null,
};

function fmtStatNum(n) {
  if (n == null) return "\u2014";
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k";
  return String(n);
}

function fmtUptime(s) {
  if (s == null) return "\u2014";
  var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}

function fmtMb(mb) {
  if (mb == null) return "\u2014";
  if (mb >= 1024) return (mb / 1024).toFixed(1) + " GB";
  return mb + " MB";
}

function fmtBps(bps) {
  if (bps == null) return "\u2014";
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " MB/s";
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + " KB/s";
  return bps + " B/s";
}

function statsRangeMeta(r) {
  switch (r) {
    case "5min": return { label: "5 minutes", bucket: "30 s", axisStart: "\u22125m", axisEnd: "now" };
    case "hour": return { label: "1 hour",    bucket: "5 min", axisStart: "\u22121h", axisEnd: "now" };
    case "day":  return { label: "24 hours",  bucket: "1 h",  axisStart: "\u221224h", axisEnd: "now" };
    case "week": return { label: "7 days",    bucket: "6 h",  axisStart: "\u22127d", axisEnd: "now" };
  }
  return { label: "", bucket: "", axisStart: "", axisEnd: "now" };
}

function tickTile(el, newVal) {
  if (!el) return;
  var nv = fmtStatNum(newVal);
  if (el.textContent !== nv) {
    el.textContent = nv;
    el.classList.remove("tick"); void el.offsetWidth; el.classList.add("tick");
  }
}

/* ---- Diagnostic mini sparklines (area+line SVGs) ---- */

function renderDiagSpark(hostId, values, opts) {
  var host = $(hostId);
  if (!host) return;
  if (!values || !values.length) return;
  var filtered = [];
  for (var i = 0; i < values.length; i++) {
    if (values[i] != null) filtered.push({ i: i, v: values[i] });
  }
  if (filtered.length < 1) return;
  // Single point: duplicate it so we draw a flat line
  if (filtered.length === 1) filtered.push({ i: Math.max(filtered[0].i + 1, values.length - 1), v: filtered[0].v });
  opts = opts || {};
  var color = opts.color || "#1e4f8a";
  var min = 0;
  var max = filtered[0].v;
  for (var j = 1; j < filtered.length; j++) {
    if (filtered[j].v > max) max = filtered[j].v;
  }
  if (max <= min) max = min + 1;
  var w = host.clientWidth || 200, h = host.clientHeight || 22;
  var pts = [];
  for (var k = 0; k < filtered.length; k++) {
    var x = (filtered[k].i / Math.max(values.length - 1, 1)) * w;
    var y = h - ((filtered[k].v - min) / (max - min)) * (h - 4) - 2;
    pts.push(x.toFixed(1) + "," + y.toFixed(1));
  }
  var area = "M0," + h + " L" + pts.join(" L") + " L" + w + "," + h + " Z";
  var line = "M" + pts.join(" L");
  host.innerHTML =
    '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" width="100%" height="100%">' +
    '<path d="' + area + '" fill="' + color + '" fill-opacity="0.14"/>' +
    '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.4" stroke-linejoin="round"/>' +
    '</svg>';
}

function renderDiagNetSpark(hostId, rx, tx) {
  var host = $(hostId);
  if (!host || !rx || !tx) return;
  var rxF = [], txF = [];
  for (var i = 0; i < rx.length; i++) {
    if (rx[i] != null) rxF.push({ i: i, v: rx[i] });
  }
  for (var j = 0; j < tx.length; j++) {
    if (tx[j] != null) txF.push({ i: j, v: tx[j] });
  }
  if (rxF.length < 1 && txF.length < 1) return;
  // Single point: duplicate for flat line
  if (rxF.length === 1) rxF.push({ i: Math.max(rxF[0].i + 1, rx.length - 1), v: rxF[0].v });
  if (txF.length === 1) txF.push({ i: Math.max(txF[0].i + 1, tx.length - 1), v: txF[0].v });
  var allVals = rxF.map(function(p) { return p.v; }).concat(txF.map(function(p) { return p.v; }));
  var max = Math.max.apply(null, allVals.concat([1]));
  var len = Math.max(rx.length, tx.length);
  var w = host.clientWidth || 380, h = host.clientHeight || 54;

  function toPath(points) {
    var pts = [];
    for (var k = 0; k < points.length; k++) {
      var x = (points[k].i / Math.max(len - 1, 1)) * w;
      var y = h - (points[k].v / max) * (h - 4) - 2;
      pts.push(x.toFixed(1) + "," + y.toFixed(1));
    }
    return { line: "M" + pts.join(" L"), area: "M0," + h + " L" + pts.join(" L") + " L" + w + "," + h + " Z" };
  }
  var rxP = rxF.length >= 2 ? toPath(rxF) : null;
  var txP = txF.length >= 2 ? toPath(txF) : null;
  var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" width="100%" height="100%">';
  if (rxP) {
    svg += '<path d="' + rxP.area + '" fill="#1e4f8a" fill-opacity="0.14"/>';
    svg += '<path d="' + rxP.line + '" fill="none" stroke="#1e4f8a" stroke-width="1.4" stroke-linejoin="round"/>';
  }
  if (txP) {
    svg += '<path d="' + txP.line + '" fill="none" stroke="#e06713" stroke-width="1.4" stroke-linejoin="round" stroke-dasharray="2.5,2"/>';
  }
  svg += '</svg>';
  host.innerHTML = svg;
}

/* ---- Health thresholds ---- */

function healthOf(metric, value) {
  if (value == null || (typeof value === "number" && isNaN(value))) return "n/a";
  switch (metric) {
    case "cpu_temp":     return value > 80 ? "crit" : value > 70 ? "warn" : "ok";
    case "cpu_load":     return value > 3.5 ? "crit" : value > 2.0 ? "warn" : "ok";
    case "mem":          return value < 50 ? "crit" : value < 100 ? "warn" : "ok";
    case "disk_pct":     return value < 5 ? "crit" : value < 10 ? "warn" : "ok";
    case "outbox_depth": return value > 5 ? "warn" : "ok";
    case "outbox_age":   return value > 60 ? "warn" : "ok";
    case "rate_limit":   return value > 10 ? "warn" : "ok";
    case "http_5xx":     return value > 0 ? "warn" : "ok";
  }
  return "ok";
}

function worstHealth(a, b) {
  var order = { "n/a": 0, ok: 1, warn: 2, crit: 3 };
  return (order[a] || 0) >= (order[b] || 0) ? a : b;
}

function sparkColor(health) {
  if (health === "crit") return "#a4201d";
  if (health === "warn") return "#b5390a";
  return "#1e4f8a";
}

/* ---- Render system health card ---- */

function renderSysHealth(sys) {
  if (!sys || !sys.cpu) return;

  // Compute health per metric
  var h = {
    load: healthOf("cpu_load", sys.cpu.load_1m),
    temp: healthOf("cpu_temp", sys.cpu.temp_c),
    mem:  healthOf("mem", sys.mem && sys.mem.available_mb),
    disk: healthOf("disk_pct", sys.disk ? (sys.disk.free_mb / sys.disk.total_mb) * 100 : null),
    outboxDepth: healthOf("outbox_depth", sys.outbox && sys.outbox.depth_now),
    outboxAge:   healthOf("outbox_age",   sys.outbox && sys.outbox.oldest_age_s),
    rate:  healthOf("rate_limit", sys.events_24h && sys.events_24h.rate_limit),
    http5: healthOf("http_5xx", sys.events_24h && sys.events_24h.http_errors && sys.events_24h.http_errors["500"]),
  };
  var throttleEvents = sys.throttle_events_24h || [];
  h.throttle = throttleEvents.length > 0 ? "warn" : "ok";

  // Overall status chip
  var worst = "ok";
  var keys = Object.keys(h);
  for (var i = 0; i < keys.length; i++) worst = worstHealth(worst, h[keys[i]]);

  var chip = $("sysStatusChip");
  var chipLabel = $("sysStatusLabel");
  if (chip) {
    chip.dataset.status = worst;
    var warnCount = 0, critCount = 0;
    for (var j = 0; j < keys.length; j++) {
      if (h[keys[j]] === "warn") warnCount++;
      if (h[keys[j]] === "crit") critCount++;
    }
    var text = "Healthy";
    if (critCount) text = critCount + " critical" + (critCount > 1 ? "s" : "");
    else if (warnCount) text = warnCount + " warning" + (warnCount > 1 ? "s" : "");
    if (chipLabel) chipLabel.textContent = text;
  }

  // Build health tip content
  var tipEl = $("sysHealthTip");
  if (tipEl) {
    var checks = [
      { key: "load", label: "CPU load", val: sys.cpu.load_1m, fmt: function(v) { return v != null ? v.toFixed(2) : "n/a"; } },
      { key: "temp", label: "CPU temp", val: sys.cpu.temp_c, fmt: function(v) { return v != null ? v.toFixed(1) + "\u00b0C" : "n/a"; } },
      { key: "mem",  label: "Memory free", val: sys.mem && sys.mem.available_mb, fmt: function(v) { return v != null ? fmtMb(v) : "n/a"; } },
      { key: "disk", label: "Disk free", val: sys.disk ? (sys.disk.free_mb / sys.disk.total_mb) * 100 : null, fmt: function(v) { return v != null ? v.toFixed(0) + "%" : "n/a"; } },
      { key: "outboxDepth", label: "Outbox depth", val: sys.outbox && sys.outbox.depth_now, fmt: function(v) { return v != null ? String(v) : "n/a"; } },
    ];
    var lines = [];
    var okCount = 0;
    for (var ci = 0; ci < checks.length; ci++) {
      var c = checks[ci];
      var st = h[c.key];
      if (st === "warn" || st === "crit") {
        lines.push('<div class="syshealth__tip-line"><span class="syshealth__tip-dot syshealth__tip-dot--' + st + '"></span>' + c.label + ' ' + c.fmt(c.val) + '</div>');
      } else if (st === "ok") {
        okCount++;
      }
    }
    if (!lines.length) {
      tipEl.innerHTML = '<div class="syshealth__tip-line"><span class="syshealth__tip-dot syshealth__tip-dot--ok"></span>All ' + okCount + ' checks passing</div>';
    } else {
      tipEl.innerHTML = lines.join("");
    }
  }

  // Metric cards
  function applyMetric(rootId, valId, sparkId, value, health, fmt, series, color) {
    var root = $(rootId);
    var valEl = $(valId);
    if (root) root.dataset.status = health;
    if (valEl) valEl.textContent = fmt(value);
    if (series) renderDiagSpark(sparkId, series, { color: color });
  }

  applyMetric("mCpuLoad", "mCpuLoadVal", "mCpuLoadSpark",
    sys.cpu.load_1m, h.load,
    function(v) { return v == null ? "\u2014" : v.toFixed(2); },
    sys.cpu.load_1h_series && sys.cpu.load_1h_series.values, sparkColor(h.load));

  applyMetric("mCpuTemp", "mCpuTempVal", "mCpuTempSpark",
    sys.cpu.temp_c, h.temp,
    function(v) { return v == null ? "\u2014" : v.toFixed(1) + "\u00b0C"; },
    sys.cpu.temp_1h_series && sys.cpu.temp_1h_series.values, sparkColor(h.temp));

  applyMetric("mMem", "mMemVal", "mMemSpark",
    sys.mem.available_mb, h.mem, fmtMb,
    sys.mem.available_1h_series && sys.mem.available_1h_series.values, sparkColor(h.mem));
  var mMemSub = $("mMemSub");
  if (mMemSub) mMemSub.textContent = "of " + fmtMb(sys.mem.total_mb);

  var diskPct = sys.disk ? (sys.disk.free_mb / sys.disk.total_mb) * 100 : null;
  applyMetric("mDisk", "mDiskVal", "mDiskSpark",
    sys.disk.free_mb, h.disk, fmtMb,
    sys.disk.series_24h && sys.disk.series_24h.values, sparkColor(h.disk));
  var mDiskSub = $("mDiskSub");
  if (mDiskSub && diskPct != null) mDiskSub.textContent = diskPct.toFixed(0) + "% of " + fmtMb(sys.disk.total_mb);

  // Network
  var sysNetIn = $("sysNetIn");
  var sysNetOut = $("sysNetOut");
  if (sysNetIn) sysNetIn.textContent = fmtBps(sys.net.rx_now_Bps);
  if (sysNetOut) sysNetOut.textContent = fmtBps(sys.net.tx_now_Bps);
  renderDiagNetSpark("sysNetSpark",
    (sys.net.rx_24h && sys.net.rx_24h.values) || [],
    (sys.net.tx_24h && sys.net.tx_24h.values) || []);

  // Outbox
  var outboxRow = $("sysOutboxRow");
  var outboxVal = $("sysOutboxVal");
  var depth = sys.outbox.depth_now, age = sys.outbox.oldest_age_s;
  var outboxHealth = worstHealth(h.outboxDepth, h.outboxAge);
  if (outboxRow) outboxRow.dataset.status = outboxHealth;
  if (outboxVal) {
    if (depth === 0) {
      outboxVal.textContent = "drained";
      outboxVal.className = "sys-row__value sys-row__muted";
    } else {
      outboxVal.textContent = depth + " queued \u00b7 oldest " + age + "s";
      outboxVal.className = "sys-row__value";
    }
  }

  // Throttle events
  var tCount = $("sysThrottleCount");
  var tList = $("sysThrottleList");
  var tRow = $("sysThrottleRow");
  if (tRow) tRow.dataset.status = throttleEvents.length ? "warn" : "ok";
  var throttledNow = sys.cpu.throttled_now;
  if (throttledNow === null) {
    if (tCount) { tCount.textContent = "not available on this host"; tCount.className = "sys-row__value sys-row__value--muted"; }
    if (tList) tList.hidden = true;
  } else if (!throttleEvents.length) {
    if (tCount) { tCount.textContent = "none in last 24h"; tCount.className = "sys-row__value sys-row__value--muted"; }
    if (tList) tList.hidden = true;
  } else {
    if (tCount) { tCount.textContent = throttleEvents.length + " event" + (throttleEvents.length > 1 ? "s" : ""); tCount.className = "sys-row__value sys-row__value--warn"; }
    if (tList) {
      tList.hidden = false;
      tList.innerHTML = "";
      for (var ti = 0; ti < throttleEvents.length; ti++) {
        var ev = throttleEvents[ti];
        var dt = new Date(ev.ts * 1000);
        var hh = dt.getHours(), mm = String(dt.getMinutes()).padStart(2, "0");
        var ap = hh >= 12 ? "pm" : "am"; hh = hh % 12 || 12;

        var li = document.createElement("li");
        // Determine kind from changed_bits: any (+) means something turned on
        var hasOn = (ev.changed_bits || []).some(function(b) { return b.indexOf("(+)") >= 0; });
        var hasOff = (ev.changed_bits || []).some(function(b) { return b.indexOf("(-)") >= 0; });
        li.className = "diag-events__item" + (hasOn ? " diag-events__item--on" : (hasOff ? " diag-events__item--off" : ""));

        var timeSpan = document.createElement("span");
        timeSpan.className = "diag-events__time";
        timeSpan.textContent = hh + ":" + mm + " " + ap;
        li.appendChild(timeSpan);

        var labelSpan = document.createElement("span");
        labelSpan.className = "diag-events__label";
        var label = (ev.changed_bits || []).join(", ");
        if (!label && ev.active_now) label = (ev.active_now || []).join(", ") || "Cleared";
        labelSpan.textContent = label || "State changed";
        li.appendChild(labelSpan);

        tList.appendChild(li);
      }
    }
  }

  // Footer counters
  var evts = sys.events_24h || { rate_limit: 0, mac_mismatch: 0, http_errors: {} };

  var rateItem = $("sysRateItem");
  if (rateItem) rateItem.dataset.status = evts.rate_limit === 0 ? "zero" : (h.rate === "ok" ? "nonzero" : "warn");
  var rateNum = $("sysRateNum");
  if (rateNum) rateNum.textContent = evts.rate_limit;

  var macItem = $("sysMacItem");
  if (macItem) macItem.dataset.status = evts.mac_mismatch === 0 ? "zero" : "warn";
  var macNum = $("sysMacNum");
  if (macNum) macNum.textContent = evts.mac_mismatch;

  var httpPills = $("sysHttpPills");
  var httpItem = $("sysHttpItem");
  if (httpPills) {
    var entries = Object.keys(evts.http_errors || {}).filter(function(k) { return evts.http_errors[k] > 0; });
    if (!entries.length) {
      httpPills.innerHTML = "";
      var none = document.createElement("span");
      none.className = "sys-footer__none";
      none.textContent = "none";
      httpPills.appendChild(none);
      if (httpItem) httpItem.dataset.status = "zero";
    } else {
      httpPills.innerHTML = "";
      var has5xx = false;
      for (var pi = 0; pi < entries.length; pi++) {
        var code = entries[pi];
        var pill = document.createElement("span");
        pill.className = "http-pill http-pill--" + code[0] + "xx";
        pill.textContent = code + " ";
        var b = document.createElement("b");
        b.textContent = evts.http_errors[code];
        pill.appendChild(b);
        httpPills.appendChild(pill);
        if (code[0] === "5") has5xx = true;
      }
      if (httpItem) httpItem.dataset.status = has5xx ? "warn" : "nonzero";
    }
  }
}

function renderRadioStatusRow() {
  var row = $("sysRadioRow");
  var val = $("sysRadioVal");
  if (!row || !val) return;
  var status = state.radioStatus || "offline";
  var rec = state.recoveryState;
  var label, statusAttr;
  switch (status) {
    case "online":      label = "Online";       statusAttr = "ok";   break;
    case "recovering":  label = "Reconnecting"; statusAttr = "warn"; break;
    case "needs_human": label = "Needs attention (auto-retry continues)"; statusAttr = "warn"; break;
    case "offline":     label = "Offline";      statusAttr = "warn"; break;
    default:            label = "Unknown";      statusAttr = "warn";
  }
  if (rec && rec !== status && rec !== "healthy") {
    label += " (" + rec + ")";
  }
  var restarts24h = (statsState.data
                     && statsState.data.radio_restarts
                     && statsState.data.radio_restarts.day) || 0;
  if (restarts24h > 0) {
    label += " \u00b7 " + restarts24h + " restart" + (restarts24h === 1 ? "" : "s") + " (24h)";
  }
  row.dataset.status = statusAttr;
  val.textContent = label;
}

function renderStats() {
  var d = statsState.data; if (!d) return;

  tickTile($("wifiNow"),  d.wifi_sessions.now);
  tickTile($("wifiDay"),  d.wifi_sessions.day);
  tickTile($("wifiWeek"), d.wifi_sessions.week);

  tickTile($("sentHour"), d.messages_sent.hour);
  tickTile($("sentDay"),  d.messages_sent.day);
  tickTile($("sentWeek"), d.messages_sent.week);

  tickTile($("failedHour"), d.messages_failed && d.messages_failed.hour);
  tickTile($("failedDay"),  d.messages_failed && d.messages_failed.day);
  tickTile($("failedWeek"), d.messages_failed && d.messages_failed.week);
  var fh = $("failedHour"); if (fh) fh.classList.toggle("stat-tile__big--warn", ((d.messages_failed && d.messages_failed.hour) || 0) > 0);
  var fd = $("failedDay");  if (fd) fd.classList.toggle("stat-tile__big--warn", ((d.messages_failed && d.messages_failed.day) || 0) > 0);

  tickTile($("repHour"),  d.direct_repeaters.hour);
  tickTile($("repDay"),   d.direct_repeaters.day);
  tickTile($("repWeek"),  d.direct_repeaters.week);

  renderSparkline();
  renderStatsRaw();

  // Header info
  var nameEl = $("statsNodeName");
  if (nameEl && state.nodeName) nameEl.textContent = state.nodeName;

  var sys = d.system || {};

  var uptimeEl = $("statsUptime");
  if (uptimeEl) {
    var radioLabels = { online: "radio online", recovering: "radio reconnecting", offline: "radio offline", needs_human: "radio needs attention" };
    var radioLabel = radioLabels[state.radioStatus] || "radio " + (state.radioStatus || "unknown");
    uptimeEl.textContent = (sys.cpu ? "Healthy" : "Collecting\u2026") + " \u00b7 " + radioLabel;
  }

  var uv = $("statsUptimeVal");
  if (uv) uv.textContent = fmtUptime(sys.uptime_s);

  renderRadioStatusRow();
  renderSysHealth(sys);

  var updated = $("statsUpdated");
  if (updated) {
    var dt = new Date(d.now_ts * 1000);
    var hh = dt.getHours(), mm = String(dt.getMinutes()).padStart(2, "0"),
        ss = String(dt.getSeconds()).padStart(2, "0");
    var ap = hh >= 12 ? "pm" : "am"; hh = hh % 12 || 12;
    updated.textContent = "updated " + hh + ":" + mm + ":" + ss + " " + ap;
  }
}

function renderSparkline() {
  var d = statsState.data; if (!d) return;
  var meta = statsRangeMeta(statsState.range);
  var seen = d.messages_seen[statsState.range];
  if (!seen) return;
  var values = seen.bars;
  var max = Math.max.apply(null, values.concat([1]));
  var total = values.reduce(function(a, b) { return a + b; }, 0);
  var peak = max;

  var scale = $("sparkScale");
  if (scale) scale.innerHTML = "<span>" + max + "</span><span>" + Math.round(max / 2) + "</span><span>0</span>";

  var axis = $("sparkAxis");
  if (axis) axis.innerHTML = "<span>" + meta.axisStart + "</span><span>" + meta.axisEnd + "</span>";

  var host = $("sparkBars");
  if (!host) return;
  host.innerHTML = "";

  values.forEach(function(v) {
    var bar = document.createElement("div");
    bar.className = "sparkline__bar" + (v === peak && v > 0 ? " sparkline__bar--peak" : (v > max * 0.65 ? " sparkline__bar--hot" : ""));
    bar.style.height = Math.max((v / max) * 100, v === 0 ? 0 : 3) + "%";
    if (v === 0) bar.setAttribute("data-zero", "1");
    var tip = document.createElement("div");
    tip.className = "sparkline__bar-tip";
    tip.textContent = v + " pkt";
    bar.appendChild(tip);
    host.appendChild(bar);
  });

  var sparkTotal = $("sparkTotal");
  var sparkPeak = $("sparkPeak");
  var sparkBucket = $("sparkBucket");
  if (sparkTotal) sparkTotal.textContent = fmtStatNum(total);
  if (sparkPeak) sparkPeak.textContent = fmtStatNum(peak);
  if (sparkBucket) sparkBucket.textContent = meta.bucket;
}

function renderStatsRaw() {
  var pre = $("statsRawCode");
  if (!pre || pre.hidden) return;
  var json = JSON.stringify(statsState.data, null, 2);
  var colored = json
    .replace(/("(?:\\.|[^"\\])*")(\s*:)/g, '<span class="k">$1</span>$2')
    .replace(/:\s*(-?\d+\.?\d*)/g, ': <span class="n">$1</span>')
    .replace(/:\s*("(?:\\.|[^"\\])*")/g, ': <span class="s">$1</span>')
    .replace(/([{}\[\],])/g, '<span class="p">$1</span>');
  pre.innerHTML = colored;
}

async function refreshStats() {
  try {
    statsState.data = await fetchJSON(API.stats, { method: "GET" });
  } catch (e) {
    // keep stale data on error
  }
  renderStats();
}

function openStatsSheet() {
  if (statsState.open) return;
  statsState.open = true;
  refreshStats();
  var sheet = $("statsSheet");
  var overlay = $("statsOverlay");
  overlay.classList.add("active");
  sheet.classList.add("active");
  if (window.matchMedia("(min-width: 768px)").matches) {
    sheet.style.transform = "translate(-50%, -50%) scale(1)";
    sheet.style.opacity = "1";
  }
  var btn = $("statusBtn"); if (btn) btn.setAttribute("aria-expanded", "true");
  var btnD = $("statusBtnDesktop"); if (btnD) btnD.setAttribute("aria-expanded", "true");
  // Auto-refresh every 20s while open
  statsState.liveTimer = setInterval(refreshStats, 20000);
}

function closeStatsSheet() {
  if (!statsState.open) return;
  statsState.open = false;
  var sheet = $("statsSheet");
  $("statsOverlay").classList.remove("active");
  sheet.classList.remove("active");
  sheet.style.transform = "";
  sheet.style.opacity = "";
  var btn = $("statusBtn"); if (btn) btn.setAttribute("aria-expanded", "false");
  var btnD = $("statusBtnDesktop"); if (btnD) btnD.setAttribute("aria-expanded", "false");
  if (statsState.liveTimer) { clearInterval(statsState.liveTimer); statsState.liveTimer = null; }
}

function setStatsRange(r) {
  statsState.range = r;
  document.querySelectorAll("#rangeTabs .stats-range__btn").forEach(function(b) {
    b.classList.toggle("stats-range__btn--active", b.dataset.range === r);
  });
  renderSparkline();
}

function toggleStatsRaw() {
  var btn = $("statsRawToggle");
  var pre = $("statsRawCode");
  var open = pre.hidden;
  pre.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) renderStatsRaw();
}

function initStats() {
  var btn = $("statusBtn"); if (btn) btn.addEventListener("click", openStatsSheet);
  var btnD = $("statusBtnDesktop"); if (btnD) btnD.addEventListener("click", openStatsSheet);
  $("statsCloseBtn").addEventListener("click", closeStatsSheet);
  $("statsOverlay").addEventListener("click", closeStatsSheet);
  document.addEventListener("keydown", function(e) { if (e.key === "Escape") closeStatsSheet(); });
  document.querySelectorAll("#rangeTabs .stats-range__btn").forEach(function(b) {
    b.addEventListener("click", function() { setStatsRange(b.dataset.range); });
  });
  $("statsRawToggle").addEventListener("click", toggleStatsRaw);
  var healthChip = $("sysStatusChip");
  if (healthChip) healthChip.addEventListener("click", function() {
    var tip = $("sysHealthTip");
    if (!tip) return;
    var show = tip.hidden;
    tip.hidden = !show;
    healthChip.setAttribute("aria-expanded", String(show));
  });
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
  $("libBackBtn").addEventListener("click", showChannels);
  $("libDetailCloseBtn").addEventListener("click", closeLibDetail);
  $("libDetailOverlay").addEventListener("click", closeLibDetail);

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

  // Stats overlay
  initStats();

  // Fetch channels
  var data = await fetchJSON(API.channels, { method: "GET" });
  state.channels = normalizeChannels(data.channel_details || data.channels || []);
  state.meshChannelNames = new Set(state.channels.filter(function(c) { return c.scope === "mesh"; }).map(function(c) { return c.name; }));

  // Discover installed reference libraries (CIV-92). Quiet on absence.
  try {
    var libIndex = await fetchJSON("/var/index.json", { method: "GET" });
    var slugs = Array.isArray(libIndex.libraries) ? libIndex.libraries : [];
    if (slugs.length) {
      var pairs = await Promise.all(slugs.map(async function(slug) {
        try {
          var idx = await fetchJSON(
            "/var/" + encodeURIComponent(slug) + "/index.json",
            { method: "GET" }
          );
          return [slug, idx];
        } catch (e) {
          return [slug, null];
        }
      }));
      pairs.forEach(function(pair) {
        if (pair[1] && typeof pair[1] === "object") {
          state.libraries[pair[0]] = pair[1];
        }
      });
    }
  } catch (e) {
    // /var/index.json unavailable; render no Reference section.
  }

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

  // Re-layout on resize (e.g. rotating tablet, resizing window)
  window.addEventListener("resize", function() {
    if (isDesktop()) {
      // Desktop: channels sidebar always visible alongside current content
      $("viewChannels").classList.add("active");
      if (!state.activeChannel && !$("viewChat").classList.contains("active")) {
        $("viewWelcome").classList.add("active");
      }
    } else {
      // Mobile: only one view at a time
      if (state.activeChannel && $("viewChat").classList.contains("active")) {
        $("viewChannels").classList.remove("active");
        $("viewWelcome").classList.remove("active");
      } else if ($("viewChannels").classList.contains("active")) {
        $("viewChat").classList.remove("active");
        $("viewWelcome").classList.remove("active");
      } else {
        $("viewChannels").classList.remove("active");
        $("viewChat").classList.remove("active");
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
