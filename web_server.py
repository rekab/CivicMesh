import argparse
import html as html_mod
import http.server
import json
import os
import pathlib
import posixpath
import re
import secrets
import shutil
import time
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Optional

PROD_TREE = pathlib.Path("/usr/local/civicmesh")


def _var_dir() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    if here.is_relative_to(PROD_TREE):
        return PROD_TREE / "var"
    return here.parent / "var"

from config import load_config
from database import (
    DBConfig,
    compute_stats,
    create_or_update_session,
    get_message,
    get_messages,
    get_status,
    get_session,
    get_user_vote,
    get_vote_counts,
    init_db,
    insert_message,
    insert_telemetry_event,
    posts_in_last_window,
    queue_outbox_and_message,
    reconcile_message_status,
    record_post_for_session,
    touch_session_last_seen,
    update_vote,
    update_session_fingerprint,
)
from logger import setup_logging


def _now_ts() -> int:
    return int(time.time())


def _record_telemetry_event(db_cfg, kind, detail=None):
    """Fire-and-forget telemetry event insert."""
    try:
        insert_telemetry_event(db_cfg, ts=_now_ts(), kind=kind, detail=detail)
    except Exception:
        pass


_stats_cache: dict = {"ts": 0.0, "data": None}

_FEEDBACK_WINDOW_S = 12 * 3600
_FEEDBACK_BUDGET_BYTES = 1_000_000


def _recent_feedback_bytes(path, log, window_s=_FEEDBACK_WINDOW_S):
    """Sum line lengths of feedback entries with ts within the last window_s seconds."""
    cutoff = time.time() - window_s
    total = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                try:
                    ts = datetime.fromisoformat(json.loads(line)["ts"]).timestamp()
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    log.warning("skipping unparseable feedback line: %s", e)
                    continue
                if ts >= cutoff:
                    total += len(line)
    except FileNotFoundError:
        return 0
    return total


def _feedback_ctx(db_path):
    """Collect lightweight server context for a feedback entry."""
    from telemetry import read_uptime_sec
    ctx = {}
    uptime = read_uptime_sec()
    if uptime is not None:
        ctx["uptime_s"] = uptime
    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE ts >= ?",
                (int(time.time()) - 86400,),
            ).fetchone()
            ctx["msg_24h"] = row[0] if row else 0
    except Exception:
        pass
    try:
        ctx["free_mb"] = shutil.disk_usage("/").free // (1024 * 1024)
    except Exception:
        pass
    return ctx


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, obj: Any) -> None:
    if status >= 400:
        try:
            _record_telemetry_event(
                handler.server.db_cfg, "http_error",
                {"status": status, "path": handler.path},
            )
        except Exception:
            pass
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: http.server.BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode("utf-8"))


def _parse_cookies(cookie_header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not cookie_header:
        return out
    parts = cookie_header.split(";")
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def get_link_from_ip(client_ip: str) -> tuple[Optional[str], Optional[str]]:
    """
    Fast + low power: read kernel ARP cache from /proc/net/arp.
    """
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        # header: IP address  HW type  Flags  HW address  Mask  Device
        for line in lines[1:]:
            cols = line.split()
            if len(cols) >= 4 and cols[0] == client_ip:
                mac = cols[3].lower()
                if mac and mac != "00:00:00:00:00:00":
                    dev = cols[5] if len(cols) > 5 else None
                    return mac, dev
    except FileNotFoundError:
        return None, None
    except Exception:
        return None, None
    return None, None


class CivicMeshHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "CivicMeshWeb/0.1"
    _set_cookie: Optional[str] = None

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        cookie = getattr(self, "_set_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._set_cookie = None
        attachment = getattr(self, "_force_attachment", None)
        if attachment:
            safe = urllib.parse.quote(attachment, safe="")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{safe}"',
            )
            self._force_attachment = None
        super().end_headers()

    def translate_path(self, path: str) -> str:
        bare = urllib.parse.urlparse(path).path
        if bare.startswith("/var/"):
            var_dir = getattr(self.server, "var_dir", None)
            if var_dir is None:
                return super().translate_path(path)
            rel = path[len("/var/"):]
            orig = self.directory
            self.directory = str(var_dir)
            try:
                return super().translate_path("/" + rel)
            finally:
                self.directory = orig
        return super().translate_path(path)

    def _discovered_slugs(self) -> set[str]:
        var_dir = getattr(self.server, "var_dir", None)
        log = self.server.log
        if var_dir is None or not var_dir.exists():
            return set()
        out: set[str] = set()
        for entry in var_dir.iterdir():
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if not resolved.is_dir():
                continue
            idx = entry / "index.json"
            if not idx.is_file():
                continue
            try:
                with idx.open("rb") as f:
                    obj = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                log.warning(
                    "var:unparseable_index slug=%s err=%s",
                    entry.name, e,
                )
                continue
            if not isinstance(obj, dict):
                log.warning(
                    "var:non_object_index slug=%s", entry.name,
                )
                continue
            out.add(entry.name)
        return out

    def _send_var_index_json(self) -> None:
        slugs = sorted(self._discovered_slugs())
        body = json.dumps(
            {"libraries": slugs}, ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # Redirect stdlib logging to our logger (set on server object)
        log = getattr(self.server, "log", None)
        if log:
            log.debug("http:%s %s", self.address_string(), fmt % args)

    def _client_ip(self) -> str:
        # In captive portal mode, we don't trust proxy headers.
        return self.client_address[0]

    def _get_session_id(self) -> Optional[str]:
        cookies = _parse_cookies(self.headers.get("Cookie", ""))
        return cookies.get("civicmesh_session")

    def _ensure_session_cookie(self) -> str:
        sid = self._get_session_id()
        if sid:
            return sid
        sid = secrets.token_urlsafe(24)
        self._set_cookie = f"civicmesh_session={sid}; Path=/; SameSite=Lax"
        return sid

    def _channel_entries(self) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for name in self.server.cfg.local.names:
            if name in seen:
                continue
            entries.append({"name": name, "scope": "on-site"})
            seen.add(name)
        for name in self.server.cfg.channels.names:
            if name in seen:
                continue
            entries.append({"name": name, "scope": "mesh"})
            seen.add(name)
        return entries

    def _all_channel_names(self) -> list[str]:
        return [c["name"] for c in self._channel_entries()]

    def _require_session(self):
        log = self.server.log
        sec = getattr(self.server, "sec", None)
        ip = self._client_ip()
        mac, dev = get_link_from_ip(ip)
        mac = mac or ""
        debug_eth0 = bool(self.server.cfg.debug.allow_eth0 and dev == "eth0")
        if mac:
            log.debug("mac:lookup ip=%s mac=%s", ip, mac)
        else:
            log.warning("mac:not_found ip=%s", ip)
        sid = self._get_session_id()
        if not sid:
            if sec:
                sec.error("CookieValidationFailed", ip=ip, mac=mac, msg="missing cookie", path=self.path, ua=self.headers.get("User-Agent", ""))
            log.warning("session:missing_cookie ip=%s mac=%s path=%s ua=%s", ip, mac, self.path, self.headers.get("User-Agent", ""))
            return None, ip, mac
        sess = get_session(self.server.db_cfg, session_id=sid, log=log)
        if not sess:
            if debug_eth0:
                create_or_update_session(
                    self.server.db_cfg,
                    session_id=sid,
                    name="debug",
                    location="eth0",
                    mac_address=mac or None,
                    log=log,
                )
                sess = get_session(self.server.db_cfg, session_id=sid, log=log)
            if not sess:
                if sec:
                    sec.error("CookieValidationFailed", ip=ip, mac=mac, msg="unknown session", session_id=sid, path=self.path, ua=self.headers.get("User-Agent", ""))
                log.warning("session:unknown_session ip=%s mac=%s sid=%s path=%s ua=%s", ip, mac, sid, self.path, self.headers.get("User-Agent", ""))
                return None, ip, mac
        stored_mac = (sess.get("mac_address") or "").lower()
        if stored_mac and mac and stored_mac != mac:
            if not debug_eth0:
                # MAC mismatch: the session cookie is valid but the device's MAC has
                # changed. This happens routinely on modern Android, which rotates
                # its per-SSID randomized MAC on reconnection. Since sessions carry
                # no secrets or privilege (just a display name), we log the event
                # for audit purposes but accept the session and update the stored MAC.
                # Abuse prevention (spam) is handled by rate limiting, not MAC pinning.
                if sec:
                    sec.error(
                        "MacMismatch",
                        ip=ip,
                        mac=mac,
                        msg="MAC mismatch",
                        session_id=sid,
                        stored_mac=stored_mac,
                        current_mac=mac,
                        path=self.path,
                        ua=self.headers.get("User-Agent", ""),
                    )
                _record_telemetry_event(self.server.db_cfg, "mac_mismatch", {"ip": ip, "session_id": sid})
                log.warning("session:mac_mismatch ip=%s mac=%s stored_mac=%s sid=%s path=%s ua=%s", ip, mac, stored_mac, sid, self.path, self.headers.get("User-Agent", ""))
                try:
                    create_or_update_session(
                        self.server.db_cfg,
                        session_id=sid,
                        name=sess.get("name") or "",
                        location=sess.get("location") or self.server.cfg.node.site_name,
                        mac_address=mac,
                        fingerprint=sess.get("fingerprint"),
                        log=log,
                    )
                    log.info("session:mac_updated ip=%s old_mac=%s new_mac=%s sid=%s", ip, stored_mac, mac, sid)
                except Exception as e:
                    log.error("session:mac_update_failed sid=%s err=%s", sid, e, exc_info=True)
        return sess, ip, mac

    def do_GET(self):
        log = self.server.log
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        host = (self.headers.get("Host", "") or "").split(":", 1)[0].lower()
        portal_host = str(self.server.cfg.network.ip)
        portal_url = f"http://{portal_host}"
        accepted_hosts = {portal_host, *self.server.cfg.web.portal_aliases}

        # Captive portal probe handling — always captive.
        #
        # Phones probe known URLs after associating to test for internet. We
        # always 302 these to the trampoline, regardless of any in-app state.
        # The OS sees a permanent walled garden and never tries to promote our
        # SSID to "validated" — which would otherwise cause it to revalidate
        # against the (nonexistent) internet, fail, and fall back to cellular.
        # See docs/captive-portal-precedent.md §2.
        if path in (
            "/generate_204",
            "/gen_204",
            "/hotspot-detect.html",
            "/library/test/success.html",
            "/connecttest.txt",
            "/ncsi.txt",
        ):
            ip = self._client_ip()
            log.info(
                "probe:hit path=%s ip=%s host=%s ua=%r",
                path, ip, host, self.headers.get("User-Agent", ""),
            )
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"{portal_url}/welcome")
            self.end_headers()
            return

        if path == "/welcome":
            node_name = self.server.cfg.node.site_name
            url = f"{portal_url}/"
            portal_aliases = self.server.cfg.web.portal_aliases
            alias_hint = ""
            if portal_aliases:
                alias_hint = f'<p class="hint">Bookmark this page: <strong>{html_mod.escape(portal_aliases[0])}</strong></p>'
            html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{node_name}</title>
    <style>
      :root {{
        --bg: #f8f6f1;
        --text: #1f2328;
        --muted: #4b5563;
        --accent: #0f4c81;
        --border: #d0d7de;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
        background: var(--bg);
        color: var(--text);
        padding: 20px;
      }}
      .wrap {{
        max-width: 640px;
        margin: 0 auto;
      }}
      h1 {{
        font-size: 26px;
        margin: 6px 0 12px;
      }}
      p {{
        margin: 8px 0;
        color: var(--muted);
        font-size: 16px;
        line-height: 1.4;
      }}
      .url {{
        margin: 16px 0 10px;
        padding: 14px 16px;
        border: 2px solid var(--accent);
        border-radius: 10px;
        font-size: 20px;
        font-weight: 700;
        color: var(--accent);
        text-align: center;
        background: #fff;
      }}
      .btn {{
        display: inline-block;
        margin-top: 8px;
        padding: 12px 16px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: #fff;
        font-size: 16px;
        font-weight: 600;
        color: var(--text);
      }}
      .hint {{
        margin-top: 12px;
        font-size: 14px;
        color: var(--muted);
      }}
      noscript .js-only {{ display: none; }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <h1>{node_name}</h1>
      <p>This is a neighborhood mesh radio chat board. No internet connection is needed.</p>
      <div class="url">{url}</div>
      <noscript>
        <p class="hint">Open your browser and go to the address above.</p>
      </noscript>
      <div class="js-only">
        <button id="copyBtn" class="btn" type="button">Copy link</button>
        <span id="copyStatus" class="hint" style="margin-left:8px;"></span>
        <a class="btn" href="/portal-accept" style="margin-left:8px; text-decoration:none;">Continue</a>
      </div>
      <p class="hint">For the full experience, open <strong>{portal_host}</strong> in Safari or Chrome.</p>
      {alias_hint}
    </div>
    <script>
      (function() {{
        var url = "{url}";
        var btn = document.getElementById("copyBtn");
        var status = document.getElementById("copyStatus");
        if (!btn) return;
        btn.addEventListener("click", function() {{
          function ok() {{ if (status) status.textContent = "Copied."; }}
          function fail() {{ if (status) status.textContent = "Copy failed."; }}
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(url).then(ok).catch(function() {{
              fallbackCopy();
            }});
            return;
          }}
          function fallbackCopy() {{
            try {{
              var ta = document.createElement("textarea");
              ta.value = url;
              ta.setAttribute("readonly", "");
              ta.style.position = "absolute";
              ta.style.left = "-9999px";
              document.body.appendChild(ta);
              ta.select();
              var success = document.execCommand("copy");
              document.body.removeChild(ta);
              success ? ok() : fail();
            }} catch (e) {{
              fail();
            }}
          }}
          fallbackCopy();
        }});
      }})();
    </script>
  </body>
</html>"""
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/portal-accept":
            ip = self._client_ip()
            log.info("portal:accepted ip=%s", ip)
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"{portal_url}/")
            self.end_headers()
            return

        if path == "/feedback":
            node_name = self.server.cfg.node.site_name
            html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Feedback — {html_mod.escape(node_name)}</title>
    <style>
      :root {{
        --bg: #f8f6f1;
        --text: #1f2328;
        --muted: #4b5563;
        --accent: #0f4c81;
        --border: #d0d7de;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
        background: var(--bg);
        color: var(--text);
        padding: 20px;
      }}
      .wrap {{ max-width: 640px; margin: 0 auto; }}
      h1 {{ font-size: 22px; margin: 6px 0 12px; }}
      p {{ margin: 8px 0; color: var(--muted); font-size: 15px; line-height: 1.5; }}
      textarea {{
        width: 100%;
        min-height: 140px;
        padding: 12px;
        border: 1.5px solid var(--border);
        border-radius: 8px;
        font-family: inherit;
        font-size: 15px;
        resize: vertical;
      }}
      textarea:focus {{
        outline: none;
        border-color: var(--accent);
        box-shadow: 0 0 0 3px rgba(15, 76, 129, 0.1);
      }}
      .btn {{
        display: inline-block;
        margin-top: 10px;
        padding: 12px 24px;
        border-radius: 10px;
        border: none;
        background: var(--accent);
        color: #fff;
        font-size: 16px;
        font-weight: 600;
        cursor: pointer;
      }}
      .back {{ margin-top: 16px; font-size: 14px; }}
      .back a {{ color: var(--accent); }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <h1>Send Feedback</h1>
      <p>Tell the developers what's working, what's broken, or what's confusing.
         This goes straight to the team and not posted to the mesh.</p>
      <form method="post" action="/feedback">
        <textarea name="text" maxlength="2000" placeholder="Your feedback&hellip;" required></textarea>
        <br>
        <button type="submit" class="btn">Submit</button>
      </form>
      <p class="back"><a href="/">&larr; Back to CivicMesh</a></p>
    </div>
  </body>
</html>"""
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/feedback/thanks":
            node_name = self.server.cfg.node.site_name
            html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Thanks — {html_mod.escape(node_name)}</title>
    <style>
      :root {{ --bg: #f8f6f1; --text: #1f2328; --accent: #0f4c81; --muted: #4b5563; }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
        background: var(--bg);
        color: var(--text);
        padding: 20px;
      }}
      .wrap {{ max-width: 640px; margin: 0 auto; }}
      h1 {{ font-size: 22px; margin: 6px 0 12px; }}
      p {{ margin: 8px 0; color: var(--muted); font-size: 15px; line-height: 1.5; }}
      a {{ color: var(--accent); }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <h1>Thanks!</h1>
      <p>Your feedback has been recorded.</p>
      <p><a href="/">&larr; Back to CivicMesh</a></p>
    </div>
  </body>
</html>"""
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # API routes are always served regardless of Host.
        if path.startswith("/api/"):
            _sid = self._get_session_id()
            if _sid:
                touch_session_last_seen(self.server.db_cfg, _sid, _now_ts())
        else:
            client_ip = self._client_ip()
            _mac, dev = get_link_from_ip(client_ip)
            allow_any_host = bool(self.server.cfg.debug.allow_eth0 and dev == "eth0")
            if path == "/":
                if host not in accepted_hosts and not allow_any_host:
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", f"{portal_url}/")
                    self.end_headers()
                    return
                sid = self._ensure_session_cookie()
                if not get_session(self.server.db_cfg, session_id=sid, log=log):
                    ip = client_ip
                    mac, _ = get_link_from_ip(ip)
                    create_or_update_session(
                        self.server.db_cfg,
                        session_id=sid,
                        name="",
                        location=self.server.cfg.node.site_name,
                        mac_address=mac,
                        log=log,
                    )
            if path == "/var/index.json":
                if host not in accepted_hosts and not allow_any_host:
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", f"{portal_url}/")
                    self.end_headers()
                    return
                self._send_var_index_json()
                return
            if path.startswith("/var/"):
                if host not in accepted_hosts and not allow_any_host:
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", f"{portal_url}/")
                    self.end_headers()
                    return
                segs = path.split("/", 3)  # ['', 'var', '<slug>', ...]
                slug = segs[2] if len(segs) >= 3 else ""
                if slug not in self._discovered_slugs():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                fs_path = self.translate_path(path)
                if os.path.isdir(fs_path):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if path.lower().endswith(".pdf"):
                    self._force_attachment = posixpath.basename(path)
                return super().do_GET()
            if path == "/" or path.startswith("/static/") or path.endswith(".js") or path.endswith(".css") or path.endswith(".svg"):
                if host not in accepted_hosts and not allow_any_host:
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", f"{portal_url}/")
                    self.end_headers()
                    return
                return super().do_GET()
            if host not in accepted_hosts and not allow_any_host:
                log.info(
                    "catchall:wrong_host path=%s ip=%s host=%s ua=%r",
                    path, client_ip, host, self.headers.get("User-Agent", ""),
                )
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", f"{portal_url}/")
                self.end_headers()
                return
            log.info(
                "catchall:redirect path=%s ip=%s host=%s ua=%r",
                path, client_ip, host, self.headers.get("User-Agent", ""),
            )
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/api/channels":
            _json(
                self,
                200,
                {
                    "channels": self._all_channel_names(),
                    "channel_details": self._channel_entries(),
                },
            )
            return

        if path == "/api/messages":
            qs = urllib.parse.parse_qs(parsed.query)
            channel = (qs.get("channel") or [""])[0]
            limit = int((qs.get("limit") or ["50"])[0])
            offset = int((qs.get("offset") or ["0"])[0])
            if not channel:
                _json(self, 400, {"error": "channel required"})
                return
            rows = get_messages(self.server.db_cfg, channel=channel, limit=limit, offset=offset, include_pinned=True, log=log)

            # Add user's vote if cookie present
            sid = self._get_session_id()
            if sid:
                for r in rows:
                    r["user_vote"] = get_user_vote(self.server.db_cfg, message_id=int(r["id"]), session_id=sid, log=log)
            _json(self, 200, {"messages": rows})
            return

        if path == "/api/status":
            row = get_status(self.server.db_cfg, process="mesh_bot", log=log)
            node_name = self.server.cfg.node.site_name
            now = _now_ts()

            if not row or not row.get("last_seen_ts"):
                _json(self, 200, {
                    "radio_status": "needs_human",
                    "recovery_state": None,
                    "radio": "offline",  # deprecated — use radio_status
                    "mesh_bot_seen": False,
                    "node_name": node_name,
                    "hub_name": node_name,  # deprecated — use node_name
                })
                return

            age = now - int(row["last_seen_ts"])
            state = row.get("state")
            connected = bool(row.get("radio_connected"))

            if age > 30:
                radio_status = "needs_human"
            elif state == "healthy" and connected:
                radio_status = "online"
            elif state in ("recovering", "disconnected"):
                radio_status = "recovering"
            elif state == "needs_human":
                radio_status = "needs_human"
            else:
                radio_status = "offline"

            _json(
                self,
                200,
                {
                    "radio_status": radio_status,
                    "recovery_state": state,
                    "radio": "online" if radio_status == "online" else "offline",  # deprecated — use radio_status
                    "mesh_bot_seen": True,
                    "last_seen_ts": int(row["last_seen_ts"]),
                    "age_sec": int(age),
                    "node_name": node_name,
                    "hub_name": node_name,  # deprecated — use node_name
                },
            )
            return

        if path == "/api/stats":
            now = time.time()
            if _stats_cache["data"] is not None and now - _stats_cache["ts"] < 20:
                _json(self, 200, _stats_cache["data"])
                return
            data = compute_stats(self.server.db_cfg, _now_ts(), log=log)
            _stats_cache["ts"] = now
            _stats_cache["data"] = data
            _json(self, 200, data)
            return

        if path == "/api/votes":
            qs = urllib.parse.parse_qs(parsed.query)
            mid = int((qs.get("message_id") or ["0"])[0])
            up, down = get_vote_counts(self.server.db_cfg, message_id=mid, log=log)
            sid = self._get_session_id()
            uv = get_user_vote(self.server.db_cfg, message_id=mid, session_id=sid, log=log) if sid else 0
            _json(self, 200, {"message_id": mid, "upvotes": up, "downvotes": down, "user_vote": uv})
            return

        if path == "/api/session":
            sess, ip, mac = self._require_session()
            if not sess:
                _json(self, 401, {"error": "no valid session", "mac_valid": False})
                return
            sid = sess["session_id"]
            # Web-server rate limit: governs ingest from WiFi clients into the DB/outbox.
            # Mesh transmit rate limiting/backoff is handled separately in mesh_bot.py.
            count = posts_in_last_window(self.server.db_cfg, session_id=sid, window_sec=3600, now_ts=_now_ts(), log=log)
            limit = self.server.cfg.limits.posts_per_hour
            remaining = max(0, limit - count)
            debug_mode = bool(self.server.cfg.debug.allow_eth0 and get_link_from_ip(ip)[1] == "eth0")
            _json(
                self,
                200,
                {
                    "posts_remaining": remaining,
                    "limit": limit,
                    "window_sec": 3600,
                    "mac_valid": True,
                    "debug_session": debug_mode,
                    "session_id": sid,
                    "fingerprint": sess.get("fingerprint") or "",
                    "message_max_chars": self.server.cfg.limits.message_max_chars,
                    "name_max_chars": self.server.cfg.limits.name_max_chars,
                    "name_pattern": self.server.cfg.limits.name_pattern,
                },
            )
            return

        # Captive portal: send everything else to index.html (302, not 301)
        log.info(
            "api:unknown path=%s ip=%s ua=%r",
            path, self._client_ip(), self.headers.get("User-Agent", ""),
        )
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/")
        self.end_headers()

    def do_POST(self):
        log = self.server.log
        sec = getattr(self.server, "sec", None)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/post":
            sess, ip, mac = self._require_session()
            if not sess:
                log.warning("post:session_invalid ip=%s mac=%s path=%s ua=%s", ip, mac, path, self.headers.get("User-Agent", ""))
                _json(self, 403, {"error": "session invalid"})
                return

            sid = sess["session_id"]
            data = _read_json(self)
            channel = str(data.get("channel", ""))
            content = str(data.get("content", ""))
            name = str(data.get("name", sess.get("name") or "")).strip()
            fingerprint = str(data.get("fingerprint", "") or "").strip().lower()
            if not channel or channel not in self._all_channel_names():
                _json(self, 400, {"error": "invalid channel"})
                return
            if not content:
                _json(self, 400, {"error": "empty message"})
                return
            if len(name) > self.server.cfg.limits.name_max_chars:
                if sec:
                    sec.error("InvalidName", ip=ip, mac=mac, msg="name too long", session_id=sid, name=name)
                _json(self, 400, {"error": "name too long"})
                return
            if not re.match(self.server.cfg.limits.name_pattern, name):
                if sec:
                    sec.error("InvalidName", ip=ip, mac=mac, msg="name invalid characters", session_id=sid, name=name)
                _json(self, 400, {"error": "Pick any character but : and @. That's the ^[^:@]+$!"})
                return
            if len(content) > self.server.cfg.limits.message_max_chars:
                _json(self, 400, {"error": "message too long"})
                return
            # Web-server rate limit: throttles client posts before they hit the DB/outbox.
            # Mesh-side send throttling/backoff (for radio transmission) is enforced in mesh_bot.py.
            count = posts_in_last_window(self.server.db_cfg, session_id=sid, window_sec=3600, now_ts=_now_ts(), log=log)
            limit = self.server.cfg.limits.posts_per_hour
            if count >= limit:
                if sec:
                    sec.error("RateLimitExceeded", ip=ip, mac=mac, msg="rate limit exceeded", session_id=sid, count=count, limit=limit, path=path)
                _record_telemetry_event(self.server.db_cfg, "rate_limit", {"session_id": sid})
                remaining = 0
                _json(self, 429, {"error": "rate limit exceeded", "posts_remaining": remaining, "limit": limit, "window_sec": 3600})
                return

            # Update session info (name/location/mac)
            create_or_update_session(
                self.server.db_cfg,
                session_id=sid,
                name=name,
                location=self.server.cfg.node.site_name,
                mac_address=mac or sess.get("mac_address"),
                fingerprint=fingerprint or None,
                log=log,
            )

            if channel in self.server.cfg.local.names:
                mid = insert_message(
                    self.server.db_cfg,
                    ts=_now_ts(),
                    channel=channel,
                    sender=name,
                    content=content,
                    source="local",
                    session_id=sid,
                    fingerprint=fingerprint or None,
                    log=log,
                )
                record_post_for_session(self.server.db_cfg, session_id=sid, now_ts=_now_ts(), log=log)
                log.info("post:local id=%d channel=%s session=%s len=%d", mid, channel, sid, len(content))
                _json(self, 200, {"ok": True, "message_id": mid, "local": True})
                return

            now = _now_ts()
            oid, mid = queue_outbox_and_message(
                self.server.db_cfg,
                ts=now,
                channel=channel,
                sender=name,
                content=content,
                session_id=sid,
                fingerprint=fingerprint or None,
                log=log,
            )
            record_post_for_session(self.server.db_cfg, session_id=sid, now_ts=now, log=log)
            log.info("post:queued outbox_id=%d message_id=%d channel=%s session=%s len=%d", oid, mid, channel, sid, len(content))
            _json(self, 200, {"ok": True, "outbox_id": oid, "message_id": mid})
            return

        if path == "/api/vote":
            sess, ip, mac = self._require_session()
            if not sess:
                _json(self, 403, {"error": "session invalid"})
                return
            data = _read_json(self)
            mid = int(data.get("message_id", 0))
            vt = int(data.get("vote_type", 0))
            sid = sess["session_id"]
            try:
                msg = get_message(self.server.db_cfg, message_id=mid, log=log)
                if msg and msg.get("session_id") == sid and vt != 0:
                    _json(self, 400, {"error": "cannot vote on your own post"})
                    return
                update_vote(self.server.db_cfg, message_id=mid, session_id=sid, vote_type=vt, ts=_now_ts(), log=log)
                up, down = get_vote_counts(self.server.db_cfg, message_id=mid, log=log)
                uv = get_user_vote(self.server.db_cfg, message_id=mid, session_id=sid, log=log)
                _json(self, 200, {"ok": True, "message_id": mid, "upvotes": up, "downvotes": down, "user_vote": uv})
            except Exception as e:
                if sec:
                    sec.error("VoteFailed", ip=ip, mac=mac, msg="vote failed", session_id=sid, message_id=mid, err=str(e))
                _json(self, 400, {"error": "vote failed"})
            return

        if path == "/api/session/fingerprint":
            sess, ip, mac = self._require_session()
            if not sess:
                _json(self, 403, {"error": "session invalid"})
                return
            data = _read_json(self)
            fp = str(data.get("fingerprint", "")).strip().lower()
            if not fp or len(fp) != 40 or any(c not in "0123456789abcdef" for c in fp):
                _json(self, 400, {"error": "invalid fingerprint"})
                return
            try:
                update_session_fingerprint(self.server.db_cfg, session_id=sess["session_id"], fingerprint=fp, log=log)
                _json(self, 200, {"ok": True})
            except Exception as e:
                if sec:
                    sec.error("FingerprintUpdateFailed", ip=ip, mac=mac, msg="fingerprint update failed", session_id=sess["session_id"], err=str(e))
                _json(self, 400, {"error": "fingerprint update failed"})
            return

        if path == "/feedback":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
            text = form.get("text", [""])[0]

            def _feedback_error(status, title, message):
                node_name = self.server.cfg.node.site_name
                h = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(title)} — {html_mod.escape(node_name)}</title>
<style>body{{margin:0;font-family:system-ui,sans-serif;background:#f8f6f1;color:#1f2328;padding:20px}}
.wrap{{max-width:640px;margin:0 auto}}h1{{font-size:22px}}p{{color:#4b5563;font-size:15px;line-height:1.5}}
a{{color:#0f4c81}}</style></head><body><div class="wrap">
<h1>{html_mod.escape(title)}</h1><p>{html_mod.escape(message)}</p>
<p><a href="/feedback">&larr; Try again</a></p></div></body></html>"""
                body = h.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            if not text.strip():
                _feedback_error(400, "Empty feedback", "Please enter some feedback.")
                return

            if len(text.encode("utf-8")) > 2048:
                _feedback_error(413, "Too long", "Feedback must be under 2048 bytes. Please shorten your message.")
                return

            fb_path = self.server.feedback_path
            recent = _recent_feedback_bytes(fb_path, log)
            if recent > _FEEDBACK_BUDGET_BYTES:
                log.warning("feedback:circuit_breaker recent_bytes=%d path=%s", recent, fb_path)
                _feedback_error(503, "Temporarily unavailable", "Feedback temporarily unavailable \u2014 please try again later.")
                return

            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ip": self._client_ip(),
                "location": self.server.cfg.node.site_name,
                "text": text.strip(),
                "ctx": _feedback_ctx(self.server.db_cfg.path),
            }
            try:
                with open(fb_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    f.flush()
                log.info("feedback:saved ip=%s len=%d", self._client_ip(), len(text))
            except Exception as e:
                log.error("feedback:write_failed path=%s err=%s", fb_path, e)
                _feedback_error(500, "Error", "Could not save feedback. Please try again.")
                return

            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/feedback/thanks")
            self.end_headers()
            return

        _json(self, 404, {"error": "not found"})


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument(
        "--log-level",
        default=None,
        help="Override config log level (DEBUG, INFO, WARNING, ERROR).",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    log, sec = setup_logging("web_server", cfg.logging, level_override=args.log_level)
    log.info("Civic Mesh web_server starting")

    db_cfg = DBConfig(path=cfg.db_path)
    init_db(db_cfg, log=log)
    reconcile_message_status(db_cfg, log=log)

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    os.makedirs(static_dir, exist_ok=True)

    class _Server(http.server.ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.cfg = cfg
            self.db_cfg = db_cfg
            self.log = log
            self.sec = sec
            self.var_dir = _var_dir()
            # Feedback file lives alongside the database
            db_dir = os.path.dirname(os.path.abspath(cfg.db_path)) or "."
            self.feedback_path = os.path.join(db_dir, "feedback.jsonl")

    def _handler(*a, **kw):
        return CivicMeshHandler(*a, directory=static_dir, **kw)

    srv = _Server(("", cfg.web.port), _handler)

    # Create session cookie on first visit to "/" via JS, but we also allow creating it here by a special endpoint if needed.
    log.info("http:listening port=%d", cfg.web.port)
    srv.serve_forever()


if __name__ == "__main__":
    run()
