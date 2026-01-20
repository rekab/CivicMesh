import argparse
import http.server
import json
import os
import secrets
import time
import urllib.parse
from http import HTTPStatus
from typing import Any, Optional

from config import load_config
from database import (
    DBConfig,
    create_or_update_session,
    get_message,
    get_messages,
    get_session,
    get_user_vote,
    get_vote_counts,
    init_db,
    insert_message,
    get_pending_outbox_for_channel,
    posts_in_last_window,
    queue_outbox,
    record_post_for_session,
    update_vote,
    update_session_fingerprint,
)
from logger import setup_logging


def _now_ts() -> int:
    return int(time.time())


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, obj: Any) -> None:
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

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

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
                return None, ip, mac
        stored_mac = (sess.get("mac_address") or "").lower()
        if stored_mac and mac and stored_mac != mac:
            if not debug_eth0:
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
                return None, ip, mac
        return sess, ip, mac

    def do_GET(self):
        log = self.server.log
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

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
            if channel in self.server.cfg.channels.names:
                pending = get_pending_outbox_for_channel(
                    self.server.db_cfg,
                    channel=channel,
                    limit=10,
                    log=log,
                )
                if pending:
                    pending_msgs = []
                    for p in pending:
                        pending_msgs.append(
                            {
                                "id": f"pending-{p['id']}",
                                "ts": int(p["ts"]),
                                "channel": p["channel"],
                                "sender": p["sender"],
                                "content": p["content"],
                                "source": "pending",
                                "session_id": p.get("session_id"),
                                "fingerprint": p.get("fingerprint"),
                                "upvotes": 0,
                                "downvotes": 0,
                                "user_vote": 0,
                                "pending": True,
                            }
                        )
                    rows = pending_msgs + rows
            _json(self, 200, {"messages": rows})
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
                },
            )
            return

        # Captive portal: send everything else to index.html (unless it's a static file)
        if path == "/" or path.startswith("/static/") or path.endswith(".js") or path.endswith(".css") or path.endswith(".svg"):
            return super().do_GET()

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
                _json(self, 403, {"error": "session invalid"})
                return

            data = _read_json(self)
            channel = str(data.get("channel", ""))
            content = str(data.get("content", ""))
            name = str(data.get("name", sess.get("name") or ""))
            fingerprint = str(data.get("fingerprint", "") or "").strip().lower()
            if not channel or channel not in self._all_channel_names():
                _json(self, 400, {"error": "invalid channel"})
                return
            if not content:
                _json(self, 400, {"error": "empty message"})
                return
            if len(name) > self.server.cfg.limits.name_max_chars:
                _json(self, 400, {"error": "name too long"})
                return
            if len(content) > self.server.cfg.limits.message_max_chars:
                _json(self, 400, {"error": "message too long"})
                return

            sid = sess["session_id"]
            count = posts_in_last_window(self.server.db_cfg, session_id=sid, window_sec=3600, now_ts=_now_ts(), log=log)
            limit = self.server.cfg.limits.posts_per_hour
            if count >= limit:
                if sec:
                    sec.error("RateLimitExceeded", ip=ip, mac=mac, msg="rate limit exceeded", session_id=sid, count=count, limit=limit, path=path)
                remaining = 0
                _json(self, 429, {"error": "rate limit exceeded", "posts_remaining": remaining, "limit": limit, "window_sec": 3600})
                return

            # Update session info (name/location/mac)
            create_or_update_session(
                self.server.db_cfg,
                session_id=sid,
                name=name,
                location=self.server.cfg.wifi.ssid,
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

            oid = queue_outbox(
                self.server.db_cfg,
                ts=_now_ts(),
                channel=channel,
                sender=name,
                content=content,
                session_id=sid,
                fingerprint=fingerprint or None,
                log=log,
            )
            record_post_for_session(self.server.db_cfg, session_id=sid, now_ts=_now_ts(), log=log)
            log.info("post:queued id=%d channel=%s session=%s len=%d", oid, channel, sid, len(content))
            _json(self, 200, {"ok": True, "outbox_id": oid})
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

        _json(self, 404, {"error": "not found"})


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    log, sec = setup_logging("web_server", cfg.logging)
    log.info("Civic Mesh web_server starting")

    db_cfg = DBConfig(path=cfg.db_path)
    init_db(db_cfg, log=log)

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    os.makedirs(static_dir, exist_ok=True)

    class _Server(http.server.ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.cfg = cfg
            self.db_cfg = db_cfg
            self.log = log
            self.sec = sec

    def _handler(*a, **kw):
        return CivicMeshHandler(*a, directory=static_dir, **kw)

    srv = _Server(("", cfg.web.port), _handler)

    # Create session cookie on first visit to "/" via JS, but we also allow creating it here by a special endpoint if needed.
    log.info("http:listening port=%d", cfg.web.port)
    srv.serve_forever()


if __name__ == "__main__":
    run()
