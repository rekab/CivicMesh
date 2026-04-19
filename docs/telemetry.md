# System Telemetry

How system health data is collected, stored, and served. Covers the
sampling loop, database tables, health thresholds, event emission, and
the `/api/stats` response shape.

## Overview

CivicMesh collects Pi-side telemetry (CPU, memory, disk, network,
throttle state) every 60 seconds and stores it in SQLite alongside
operational events (rate limits, MAC mismatches, HTTP errors). The data
is served to the UI via the `system` key in `/api/stats` and displayed
in the Node Stats dialog's "System health" card.

## Architecture

```
telemetry.py          database.py              web_server.py
─────────────         ──────────────           ──────────────
sample_once() ──────► telemetry_samples        /api/stats
  (every 60s)         telemetry_events ◄────── compute_stats()
  runs in                                        └─► _build_system_telemetry()
  mesh_bot.py                                         (single connection,
  via asyncio                                          6 queries)
  .gather()
                      telemetry_events ◄────── _record_telemetry_event()
                        (mac_mismatch,           (fire-and-forget from
                         rate_limit,              request handlers)
                         http_error)
```

## Data sources

All reads are stdlib file reads except `vcgencmd`, which is the only
subprocess call.

| Metric | Source | Frequency | Notes |
|--------|--------|-----------|-------|
| Uptime | `/proc/uptime` | 60s | First field, seconds as float |
| Load average | `/proc/loadavg` | 60s | 1-min average (first field) |
| CPU temp | `/sys/class/thermal/thermal_zone0/temp` | 60s | Millidegrees ÷ 1000 |
| Memory | `/proc/meminfo` | 60s | `MemTotal` and `MemAvailable` in kB |
| Disk | `os.statvfs("/")` | 60s | `f_bavail * f_frsize` for free bytes |
| Network | `/proc/net/dev` | 60s | Cumulative bytes, summed across non-lo interfaces |
| Throttled | `vcgencmd get_throttled` | 300s | Bitmask; `None` on non-Pi hardware |

Every reader returns `None` on missing files (non-Pi dev hardware),
which is written as SQL `NULL`. The sampler never errors on a missing
source.

## Database tables

### `telemetry_samples`

One row per sample (every 60s). Primary key is the unix timestamp.

```sql
CREATE TABLE IF NOT EXISTS telemetry_samples (
    ts INTEGER PRIMARY KEY,
    uptime_s INTEGER,
    load_1m REAL,
    cpu_temp_c REAL,
    mem_available_kb INTEGER,
    mem_total_kb INTEGER,
    disk_free_kb INTEGER,
    disk_total_kb INTEGER,
    net_rx_bytes INTEGER,         -- cumulative since boot
    net_tx_bytes INTEGER,         -- cumulative since boot
    outbox_depth INTEGER,
    outbox_oldest_age_s INTEGER,
    throttled_bitmask INTEGER     -- nullable, sampled every 5 min
);
```

All columns except `ts` are nullable. On non-Pi hardware, most will be
`NULL`.

### `telemetry_events`

Sparse event log. Autoincrement primary key — no deduplication, every
event gets its own row.

```sql
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- see Event kinds below
    detail TEXT                    -- JSON blob, nullable
);
```

Indexed on `(ts)` and `(kind, ts)`.

### Event kinds

| Kind | Emitted by | Detail JSON |
|------|-----------|-------------|
| `throttle_change` | `telemetry.py` (on bitmask change) | `{"old": "0x1", "new": "0x5", "changed_bits": ["Currently throttled (+)"], "active_now": ["Under-voltage detected", "Currently throttled"]}` |
| `rate_limit` | `web_server.py` (rate limit hit) | `{"session_id": "..."}` |
| `mac_mismatch` | `web_server.py` (MAC rotation) | `{"ip": "...", "session_id": "..."}` |
| `http_error` | `web_server.py` (status >= 400) | `{"status": 429, "path": "/api/post"}` |

Throttle change detection emits on **any** bitmask value change (not
just 0↔nonzero), so transitions like `0x1 → 0x5` are captured. The
`changed_bits` array labels each flipped bit with `(+)` for newly set
and `(-)` for newly cleared.

## Throttle bitmask

The `vcgencmd get_throttled` bitmask encodes current state (bits 0–3)
and boot-history (bits 16–19):

| Bit | Label |
|-----|-------|
| 0 | Under-voltage detected |
| 1 | Arm frequency capped |
| 2 | Currently throttled |
| 3 | Soft temperature limit active |
| 16 | Under-voltage has occurred (since boot) |
| 17 | Arm frequency capping has occurred |
| 18 | Throttling has occurred |
| 19 | Soft temperature limit has occurred |

## Sampling lifecycle

1. `mesh_bot.py` starts `telemetry.telemetry_loop()` as a 4th task in
   `asyncio.gather`, alongside outbox sender, retention, and heartbeat.
2. Every 60s, `sample_once()` runs in the default thread executor (via
   `run_in_executor`) so SQLite writes don't block the event loop.
3. Every 5th tick (300s), `read_throttled()` is also called; otherwise
   `throttled_bitmask` is `NULL` for that sample.
4. Throttle state-change detection compares the new bitmask to the
   previous reading (module-level `_last_throttled_bitmask`). On the
   first sample after process start, no event is emitted (no prior
   state to diff against).

## Retention

Pruning runs hourly in `_retention_task` (mesh_bot.py):

- `telemetry_samples`: rows older than **7 days** are deleted
- `telemetry_events`: rows older than **30 days** are deleted

## `/api/stats` response shape

The `system` key is added to the existing stats response by
`_build_system_telemetry()`, which opens a single SQLite connection
and runs 6 queries:

```json
{
  "now_ts": 1713560000,
  "wifi_sessions": { "...existing..." },
  "messages_seen": { "...existing..." },
  "messages_sent": { "...existing..." },
  "direct_repeaters": { "...existing..." },
  "system": {
    "now_ts": 1713560000,
    "uptime_s": 308241,
    "cpu": {
      "load_1m": 0.37,
      "temp_c": 52.4,
      "throttled_now": false,
      "load_1h_series": { "sample_sec": 60, "values": [0.35, 0.41, ...] },
      "temp_1h_series": { "sample_sec": 60, "values": [51.2, 52.0, ...] }
    },
    "mem": {
      "available_mb": 2180,
      "total_mb": 3840,
      "available_1h_series": { "sample_sec": 60, "values": [2200, 2180, ...] }
    },
    "disk": {
      "free_mb": 18420,
      "total_mb": 28000,
      "series_24h": { "sample_sec": 3600, "values": [18500, 18480, ...] }
    },
    "net": {
      "rx_now_Bps": 42300,
      "tx_now_Bps": 15100,
      "rx_24h": { "sample_sec": 3600, "values": [38000, 41000, ...] },
      "tx_24h": { "sample_sec": 3600, "values": [12000, 14000, ...] }
    },
    "outbox": {
      "depth_now": 0,
      "oldest_age_s": null
    },
    "throttle_events_24h": [],
    "events_24h": {
      "rate_limit": 0,
      "mac_mismatch": 0,
      "http_errors": {}
    }
  }
}
```

### Series fields

- `load_1h_series`, `temp_1h_series`, `available_1h_series`: up to 60
  values at 60s intervals from the last hour. Values may contain `null`
  entries (sparse data after boot, failed reads).
- `series_24h` (disk): hourly averages over the last 24 hours (up to 24
  values).
- `rx_24h`, `tx_24h` (net): hourly byte-rate in bytes/sec over the last
  24 hours. Each value is the intra-bucket delta (last cumulative bytes
  minus first cumulative bytes within that hour, divided by elapsed
  seconds). Clamped to 0 on counter reset (reboot). For the current
  partial-hour bucket, if only 1 sample exists (not enough for an
  intra-bucket delta), the value falls back to `rx_now_Bps`/`tx_now_Bps`
  so the chart isn't blank during the first hour after boot.
- `rx_now_Bps`, `tx_now_Bps`: instantaneous rate from the two most
  recent samples.

### Null semantics

- `throttled_now`: `true` if bits 0–3 are nonzero, `false` if zero,
  `null` if no throttle reading is available (non-Pi or no sample yet).
- `outbox.oldest_age_s`: `null` when the queue is empty.
- Series values may contain `null` for samples where the reader returned
  `None`.
- When no telemetry samples exist yet (first 60s after boot), the
  `system` key is still present but all scalar fields are `null` and
  series are empty arrays.

## UI health thresholds

The Node Stats "System health" card applies these thresholds to color
metric cards and compute the overall status chip:

| Metric | Warn | Critical |
|--------|------|----------|
| CPU temp | > 70°C | > 80°C |
| CPU load (1m) | > 2.0 | > 3.5 |
| Memory free | < 100 MB | < 50 MB |
| Disk free | < 10% | < 5% |
| Outbox depth | > 5 | — |
| Outbox age | > 60s | — |
| Rate limit (24h) | > 10 | — |
| HTTP 5xx (24h) | > 0 | — |
| Throttle events (24h) | any | — |

Metrics with `null` values get status `n/a` and render at reduced
opacity. `n/a` does not worsen the overall chip status — it means
"unknown", not "bad".

## Event emission in web_server.py

Events are emitted via `_record_telemetry_event()`, a fire-and-forget
wrapper that silently swallows exceptions so telemetry never breaks
request handling.

- **`mac_mismatch`**: emitted in `_require_session()` when a session's
  stored MAC doesn't match the current request MAC.
- **`rate_limit`**: emitted in `do_POST` `/api/post` when the per-hour
  post limit is exceeded.
- **`http_error`**: emitted in `_json()` for any response with status
  >= 400. Detail includes the status code and request path.
