#!/usr/bin/env python3
# loadgen.py — sustained mixed load for CivicMesh overnight power test
# stdlib only, no deps
import http.cookiejar
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---- CONFIG ----
BASE = "http://10.0.0.1"
CHANNELS = ["#civicmesh", "#testing"]
CLIENTS = 3
RUN_HOURS = 11
POST_EVERY_MIN = (2, 5)
GET_EVERY_SEC = (10, 30)
POST_ENABLED = False
TIMEOUT = 10
# ----------------

START = time.time()

def log(msg):
    elapsed = int(time.time() - START)
    print(f"{datetime.now().isoformat(timespec='seconds')} [+{elapsed:5d}s] {msg}", flush=True)

def make_opener():
    """Each client gets its own cookie jar so sessions are independent."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def do_get(opener, url):
    req = urllib.request.Request(url, method="GET")
    with opener.open(req, timeout=TIMEOUT) as r:
        body = r.read()
        return r.status, body

def do_post_json(opener, url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=TIMEOUT) as r:
        body = r.read()
        return r.status, body

def client(cid):
    opener = make_opener()

    # Prime session cookie by hitting /
    try:
        do_get(opener, f"{BASE}/")
    except Exception as e:
        log(f"client {cid} prime failed: {e}")
        return

    end = time.time() + RUN_HOURS * 3600
    gets = posts = errs = 0
    last_post = time.time() + random.uniform(30, 120)
    last_report = time.time()

    while time.time() < end:
        try:
            ch = random.choice(CHANNELS)
            qs = urllib.parse.urlencode({"channel": ch, "limit": 50})
            status, body = do_get(opener, f"{BASE}/api/messages?{qs}")
            gets += 1
            log(f"client {cid} GET {ch} → {status} ({len(body)}b)")
            if status >= 400:
                errs += 1
                log(f"client {cid} GET {ch} → {status}")

            if POST_ENABLED and time.time() - last_post > random.uniform(*POST_EVERY_MIN) * 60:
                try:
                    status, body = do_post_json(
                        opener,
                        f"{BASE}/api/post",
                        {
                            "channel": ch,
                            "content": f"loadtest c{cid} {int(time.time())}",
                            "name": f"loadtest-{cid}",
                        },
                    )
                    posts += 1
                    last_post = time.time()
                    if status >= 400:
                        errs += 1
                        log(f"client {cid} POST {ch} → {status} body={body[:100]!r}")
                except urllib.error.HTTPError as e:
                    posts += 1
                    last_post = time.time()
                    errs += 1
                    log(f"client {cid} POST {ch} → {e.code} body={e.read()[:100]!r}")

            if time.time() - last_report > 900:
                log(f"client {cid} running: gets={gets} posts={posts} errs={errs}")
                last_report = time.time()

        except urllib.error.HTTPError as e:
            errs += 1
            log(f"client {cid} HTTPError: {e.code} {e.reason}")
        except Exception as e:
            errs += 1
            log(f"client {cid} exception: {type(e).__name__}: {e}")

        time.sleep(random.uniform(*GET_EVERY_SEC))

    log(f"client {cid} done: gets={gets} posts={posts} errs={errs}")

def main():
    log(f"loadgen starting: {CLIENTS} clients × {RUN_HOURS}h against {BASE}")
    log(f"channels={CHANNELS} post_enabled={POST_ENABLED}")
    threads = [threading.Thread(target=client, args=(i,), daemon=False) for i in range(CLIENTS)]
    for t in threads:
        t.start()
        time.sleep(2)
    for t in threads:
        t.join()
    log(f"loadgen complete: ran {(time.time() - START)/3600:.2f}h")

if __name__ == "__main__":
    main()
