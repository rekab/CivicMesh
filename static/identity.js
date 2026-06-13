// CIV-14: captive-portal contact-add flow. Walk-up users paste their
// MeshCore pubkey (or share-identity URL) to register with this node;
// the node's own identity QR appears AFTER successful registration so
// they can scan it back into the MeshCore app to add the node as a
// contact.
//
// State lives entirely client-side. The cookie `civicmesh_my_pubkey`
// holds the last valid pubkey the user has typed so the form is
// pre-filled across captive-portal sessions. The server never reads
// that cookie; it only sees the explicit form submission.
//
// QR gating: the QR shows only when we have *backend-confirmed*
// registration for the current input value. Typing a valid-looking
// pubkey is NOT enough — that lets a confused passer-by scan a QR for
// a registration that doesn't actually exist. Editing the input after
// registration hides the QR until the new pubkey is registered.
//
// State machine:
//   1. On first Node Stats open: fetch /api/identity for the node's QR
//      data (server-trusted; the client cannot mint a valid contact URL).
//      Restore the cookie value into the input. Fetch
//      /api/contacts/<pk>/status so the "✓ Registered" indicator and
//      the QR survive revisits when the backend already knows us.
//   2. On input change: hide the QR if the input no longer matches the
//      registered pubkey. Update the cookie when the value becomes
//      valid. Clear any stale status indicator.
//   3. On Register click: POST /api/contacts. On 200 with status='added'
//      (or after polling /api/contacts/<pk>/status every 1.5s for up to
//      10s flips to 'added'), reveal the QR. On error, show the message.

(function () {
  "use strict";

  var QR_LIB_URL = "/vendor/qrcode-generator.js";
  var IDENTITY_URL = "/api/identity";
  var CONTACTS_URL = "/api/contacts";
  var COOKIE_NAME = "civicmesh_my_pubkey";
  var COOKIE_MAX_AGE = 60 * 60 * 24 * 30;  // 30 days

  // typeNumber=0 = auto-size for payload; 'M' = ~15% error correction.
  var QR_TYPE_NUMBER = 0;
  var QR_ERROR_CORRECTION = "M";
  var QR_CELL_SIZE = 4;
  var QR_MARGIN = 2;

  var POLL_INTERVAL_MS = 1500;
  var POLL_MAX_MS = 10000;

  var initialized = false;
  var libPromise = null;
  var identityData = null;
  var qrRenderedFor = null;  // contact_url last rendered, to avoid rerender
  var registeredPubkey = null;  // pubkey the backend has confirmed as 'added'
  var copyHandlerAttached = false;  // wire the copy button's click only once
  var copyResetTimer = null;  // pending "Copied!" -> "Tap to copy" revert

  function loadQrLib() {
    if (window.qrcode) return Promise.resolve(window.qrcode);
    if (libPromise) return libPromise;
    libPromise = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = QR_LIB_URL;
      s.async = true;
      s.onload = function () {
        if (window.qrcode) resolve(window.qrcode);
        else reject(new Error("qrcode-generator did not expose window.qrcode"));
      };
      s.onerror = function () { reject(new Error("failed to load " + QR_LIB_URL)); };
      document.head.appendChild(s);
    });
    return libPromise;
  }

  function renderQr(container, text) {
    var qr = window.qrcode(QR_TYPE_NUMBER, QR_ERROR_CORRECTION);
    qr.addData(text);
    qr.make();
    container.innerHTML = qr.createSvgTag(QR_CELL_SIZE, QR_MARGIN);
  }

  // iPadOS 13+ reports as "Macintosh"; the touch check catches it. iOS Safari
  // has no working programmatic copy over plain HTTP, so we treat it specially
  // (see onCopyClick): navigator.clipboard needs a secure context, and
  // execCommand("copy") returns true while copying nothing.
  var IS_IOS = /ipad|iphone|ipod/i.test(navigator.userAgent) ||
    (/macintosh/i.test(navigator.userAgent) && navigator.maxTouchPoints > 1);

  // Copy `text` to the clipboard, returning a Promise. Used on non-iOS only:
  //   1. navigator.clipboard.writeText — secure contexts only. The portal is
  //      plain HTTP today so this is normally absent; kept first so we
  //      transparently use it if ever served over HTTPS.
  //   2. Legacy execCommand("copy") via an off-screen textarea — what Android
  //      Chrome and desktop browsers actually use over HTTP.
  //   3. Reject — caller falls back to selecting the URI for a manual copy.
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "0";
      ta.style.left = "0";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      var ok = false;
      try {
        ta.select();
        ok = document.execCommand("copy");
      } catch (e) {
        ok = false;
      } finally {
        document.body.removeChild(ta);
      }
      if (ok) resolve();
      else reject(new Error("copy command failed"));
    });
  }

  // Select the full visible URI so the user can invoke the native Copy. On
  // iOS this is the whole story; elsewhere it's the manual-copy fallback.
  function selectUri() {
    var uriEl = document.getElementById("identityCopyUri");
    if (!uriEl) return;
    var range = document.createRange();
    range.selectNodeContents(uriEl);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function formatPubkey(hex) {
    // Return the full hex string unbroken so operators can copy/paste it.
    // .identity__pubkey has `word-break: break-all` so it wraps cleanly
    // on narrow phones without inserting unselectable whitespace.
    return hex || "—";
  }

  // Accept either a 64-char hex string or a meshcore://contact/add?...
  // URL carrying public_key= in the query string. Returns lowercase hex
  // or null if no valid pubkey extractable.
  function parsePubkey(raw) {
    if (!raw) return null;
    var s = raw.trim().toLowerCase();
    if (/^[0-9a-f]{64}$/.test(s)) return s;
    if (s.indexOf("meshcore://") === 0) {
      var qIdx = s.indexOf("?");
      if (qIdx < 0) return null;
      var parts = s.substring(qIdx + 1).split("&");
      for (var i = 0; i < parts.length; i++) {
        var kv = parts[i].split("=");
        if (kv[0] === "public_key" && kv[1]) {
          var pk;
          try { pk = decodeURIComponent(kv[1]).toLowerCase(); }
          catch (e) { return null; }
          if (/^[0-9a-f]{64}$/.test(pk)) return pk;
        }
      }
    }
    return null;
  }

  function getCookie(name) {
    var nameEQ = name + "=";
    var parts = document.cookie.split(";");
    for (var i = 0; i < parts.length; i++) {
      var c = parts[i].replace(/^\s+/, "");
      if (c.indexOf(nameEQ) === 0) {
        try { return decodeURIComponent(c.substring(nameEQ.length)); }
        catch (e) { return null; }
      }
    }
    return null;
  }

  function setCookie(name, value) {
    document.cookie = name + "=" + encodeURIComponent(value) +
      "; Max-Age=" + COOKIE_MAX_AGE +
      "; Path=/; SameSite=Lax";
  }

  function setStatus(text, kind) {
    var el = document.getElementById("contactStatus");
    if (!el) return;
    el.textContent = text || "";
    el.className = "contact-status" + (kind ? " contact-status--" + kind : "");
    el.hidden = !text;
  }

  function hideQr() {
    var wrap = document.getElementById("identityQrWrap");
    if (wrap) wrap.hidden = true;
  }

  function setCopyCta(text, copied) {
    var btn = document.getElementById("identityCopy");
    var cta = document.getElementById("identityCopyCta");
    if (cta) cta.textContent = text;
    if (btn) {
      if (copied) btn.classList.add("is-copied");
      else btn.classList.remove("is-copied");
    }
  }

  function onCopyClick() {
    if (!identityData) return;
    if (copyResetTimer) { clearTimeout(copyResetTimer); copyResetTimer = null; }
    // iOS: no honest programmatic copy over HTTP. Select the URI and let the
    // user tap the native Copy that pops over the selection. Don't claim
    // "Copied!" — execCommand would lie and the clipboard would stay empty.
    if (IS_IOS) {
      selectUri();
      setCopyCta("Selected — tap Copy", false);
      return;
    }
    copyText(identityData.contact_url).then(function () {
      setCopyCta("Copied!", true);
      copyResetTimer = setTimeout(function () {
        setCopyCta("Tap to copy", false);
        copyResetTimer = null;
      }, 1800);
    }).catch(function () {
      // execCommand failed too: select the URI so the user can copy manually.
      selectUri();
      setCopyCta("Press & hold to copy", false);
    });
  }

  // Show the node's contact URI in the copy button and wire its click once.
  // Independent of the QR library so copy works even if the QR fails to load.
  function setupCopyButton() {
    var wrap = document.getElementById("identityCopy");
    var uriEl = document.getElementById("identityCopyUri");
    var cta = document.getElementById("identityCopyCta");
    if (!wrap || !uriEl || !cta || !identityData) return;
    uriEl.textContent = identityData.contact_url || "—";
    setCopyCta("Tap to copy", false);
    wrap.hidden = false;
    if (!copyHandlerAttached) {
      cta.addEventListener("click", onCopyClick);
      copyHandlerAttached = true;
    }
  }

  // Reveal the QR for the current input value. Caller is responsible for
  // ensuring registration has been backend-confirmed for that pubkey.
  function revealQr() {
    var wrap = document.getElementById("identityQrWrap");
    var qrHost = document.getElementById("identityQr");
    var nameEl = document.getElementById("identityName");
    var pubEl = document.getElementById("identityPubkey");
    if (!wrap || !qrHost || !nameEl || !pubEl || !identityData) return;
    wrap.hidden = false;
    setupCopyButton();
    if (qrRenderedFor === identityData.contact_url) return;
    loadQrLib().then(function () {
      renderQr(qrHost, identityData.contact_url);
      nameEl.textContent = identityData.name || "—";
      pubEl.textContent = formatPubkey(identityData.public_key);
      qrRenderedFor = identityData.contact_url;
    }).catch(function (err) {
      if (window.console && console.warn) console.warn("identity:", err);
    });
  }

  function applyStatusPayload(data, pubkeyForStatus) {
    if (!data) return;
    if (data.status === "added") {
      setStatus("✓ Registered", "ok");
      if (pubkeyForStatus) {
        registeredPubkey = pubkeyForStatus;
        revealQr();
      }
    } else if (data.status === "error_table_full") {
      setStatus(
        "⚠ Couldn't register: contact table is full. Tell the operator.",
        "err"
      );
    } else if (data.status === "error_other") {
      setStatus(
        "⚠ Couldn't register: " + (data.error_detail || "firmware error"),
        "err"
      );
    } else if (data.status === "pending") {
      setStatus("Registering…", "info");
    }
  }

  function pollStatus(pubkey, deadline) {
    if (Date.now() > deadline) {
      setStatus(
        "Still registering — refresh in a minute to confirm.",
        "info"
      );
      return;
    }
    fetch(CONTACTS_URL + "/" + pubkey + "/status", { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.status === 404) {
          setTimeout(function () { pollStatus(pubkey, deadline); }, POLL_INTERVAL_MS);
          return null;
        }
        if (!resp.ok) throw new Error("status: HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data) return;
        if (data.status === "pending") {
          setTimeout(function () { pollStatus(pubkey, deadline); }, POLL_INTERVAL_MS);
        } else {
          applyStatusPayload(data, pubkey);
        }
      })
      .catch(function (err) {
        setStatus("Couldn't check status: " + err.message, "err");
      });
  }

  function onSubmit(ev) {
    ev.preventDefault();
    var input = document.getElementById("contactPubkey");
    var submitBtn = document.getElementById("contactSubmit");
    if (!input || !submitBtn) return;
    var pk = parsePubkey(input.value);
    if (!pk) {
      setStatus("Pubkey must be 64 hex chars or a meshcore:// URL.", "err");
      return;
    }
    submitBtn.disabled = true;
    setStatus("Registering…", "info");
    fetch(CONTACTS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ pubkey: pk }),
    })
      .then(function (resp) {
        return resp.json().then(function (body) {
          return { status: resp.status, body: body };
        });
      })
      .then(function (r) {
        submitBtn.disabled = false;
        if (r.status === 200) {
          setCookie(COOKIE_NAME, pk);
          if (r.body && r.body.status === "added") {
            setStatus("✓ Registered", "ok");
            registeredPubkey = pk;
            revealQr();
          } else {
            pollStatus(pk, Date.now() + POLL_MAX_MS);
          }
        } else {
          var msg = (r.body && r.body.error) || ("HTTP " + r.status);
          setStatus("⚠ " + msg, "err");
        }
      })
      .catch(function (err) {
        submitBtn.disabled = false;
        setStatus("Couldn't reach the server: " + err.message, "err");
      });
  }

  function fetchIdentity() {
    return fetch(IDENTITY_URL, { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.status === 503) return null;
        if (!resp.ok) throw new Error("identity fetch: HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        identityData = data;
      })
      .catch(function (err) {
        if (window.console && console.warn) console.warn("identity:", err);
      });
  }

  function checkExistingStatus() {
    var input = document.getElementById("contactPubkey");
    if (!input) return;
    var pk = parsePubkey(input.value);
    if (!pk) return;
    fetch(CONTACTS_URL + "/" + pk + "/status", { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.status === 404) return null;
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (data) {
        if (data && data.status === "pending") {
          // A pending row from a previous visit: catch up to it.
          pollStatus(pk, Date.now() + POLL_MAX_MS);
        } else {
          applyStatusPayload(data, pk);
        }
      })
      .catch(function () { /* silent — no status is fine */ });
  }

  function init() {
    if (initialized) return;
    initialized = true;
    var card = document.getElementById("identityCard");
    var form = document.getElementById("contactForm");
    var input = document.getElementById("contactPubkey");
    if (!card || !form || !input) {
      initialized = false;
      return;
    }
    var saved = getCookie(COOKIE_NAME);
    if (saved) input.value = saved;
    form.addEventListener("submit", onSubmit);
    input.addEventListener("input", function () {
      setStatus("");
      var pk = parsePubkey(input.value);
      if (pk) setCookie(COOKIE_NAME, pk);
      // Hide the QR if the input no longer matches the registered pubkey
      // (handles "I just want to register a different phone").
      if (pk !== registeredPubkey) hideQr();
    });
    card.hidden = false;
    fetchIdentity().then(function () {
      // Don't reveal QR on load just because the input looks valid;
      // checkExistingStatus is the one that reveals on backend-confirmed
      // 'added'. Returning users see the QR; first-time visitors don't.
      checkExistingStatus();
    });
  }

  function attach() {
    var btn = document.getElementById("statusBtn");
    var btnD = document.getElementById("statusBtnDesktop");
    if (btn) btn.addEventListener("click", init);
    if (btnD) btnD.addEventListener("click", init);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attach);
  } else {
    attach();
  }
})();
