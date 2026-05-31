// CIV-14 onboarding: render the meshcore://contact/add QR inside the
// Node Stats sheet. The QR library is vendored under /vendor/ and is
// lazy-loaded on the first stats-sheet open so the captive portal's
// cold landing doesn't pay the ~50KB cost for a card most walk-up users
// never see. /api/identity returns 503 until mesh_bot has connected at
// least once — in that case we leave the card hidden.

(function () {
  "use strict";

  var QR_LIB_URL = "/vendor/qrcode-generator.js";
  var IDENTITY_URL = "/api/identity";
  // typeNumber=0 = auto-size for payload; 'M' = ~15% error correction,
  // the standard default. Our payload is the contact_url, ~120-160
  // ASCII chars after urlencoding — fits comfortably in a small version.
  var QR_TYPE_NUMBER = 0;
  var QR_ERROR_CORRECTION = "M";
  // cellSize=4 yields a ~120px QR for the small version typical of our
  // payload; phones scan this size from ~6 inches without zoom.
  var QR_CELL_SIZE = 4;
  var QR_MARGIN = 2;

  var loaded = false;       // identity card has been populated once
  var loading = false;      // request or lib-load is in flight
  var libPromise = null;    // memoized lib loader

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

  function formatPubkey(hex) {
    // Group every 8 chars for legibility — 64 hex chars wrap awkwardly
    // otherwise. Pure presentation; the underlying value is unchanged.
    if (!hex) return "—";
    return hex.match(/.{1,8}/g).join(" ");
  }

  function loadIdentity() {
    if (loaded || loading) return;
    loading = true;
    var card = document.getElementById("identityCard");
    var qrHost = document.getElementById("identityQr");
    var nameEl = document.getElementById("identityName");
    var pubEl = document.getElementById("identityPubkey");
    if (!card || !qrHost || !nameEl || !pubEl) {
      loading = false;
      return;
    }

    fetch(IDENTITY_URL, { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.status === 503) {
          // mesh_bot has not connected yet — keep the card hidden;
          // a future stats-sheet open will retry.
          loading = false;
          return null;
        }
        if (!resp.ok) {
          throw new Error("identity fetch: HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (data) {
        if (!data) return;
        return loadQrLib().then(function () {
          renderQr(qrHost, data.contact_url);
          nameEl.textContent = data.name || "—";
          pubEl.textContent = formatPubkey(data.public_key);
          card.hidden = false;
          loaded = true;
          loading = false;
        });
      })
      .catch(function (err) {
        // Silent on the UI — the card stays hidden. A console line
        // makes the failure visible to operators viewing devtools.
        if (window.console && console.warn) console.warn("identity:", err);
        loading = false;
      });
  }

  function attach() {
    var btn = document.getElementById("statusBtn");
    var btnD = document.getElementById("statusBtnDesktop");
    // Same loader on both stats-sheet triggers. app.js's openStatsSheet
    // also runs on these clicks; both listeners fire and DOM mutations
    // are independent so order doesn't matter.
    if (btn) btn.addEventListener("click", loadIdentity);
    if (btnD) btnD.addEventListener("click", loadIdentity);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attach);
  } else {
    attach();
  }
})();
