# External display API

Server-side contract for the optional Inkplate 6 e-paper bulletin display
that some CivicMesh hubs may have attached. Most hubs do not. The endpoint
is always registered but is gated by `external_display.enabled` in
`config.toml` so disabled hubs pay no cost beyond the routing check.

This document describes **v2** (`api_version = 2`), the current schema.
The Phase 0 placeholder v1 schema is preserved at the end of the document
for historical reference; field units pinned to v1 will receive a payload
that fails their `api_version` check (the server emits v2 regardless of
what a client expects) and must be reflashed.

## Endpoint

`GET /api/external-display/state`

Always registered. Accepts any Host header (the `path.startswith("/api/")`
check in `web_server.py` bypasses host validation for API routes).

## Responses

### Disabled (default)

When `external_display.enabled = false` (or the section is absent):

```
HTTP/1.0 404 Not Found
Content-Type: application/json; charset=utf-8

{"error": "not found"}
```

The 404 is explicit, not a fallthrough to the captive-portal redirect.
Firmware can probe this endpoint to discover whether a hub supports it.

### Enabled

When `external_display.enabled = true`:

```
HTTP/1.0 200 OK
Content-Type: application/json; charset=utf-8

{
  "api_version": 2,
  "server_time": 1747431138,
  "hub": {
    "site_name": "Greenwood Library",
    "callsign": "gwd"
  },
  "channels": [
    {
      "name": "#hub-board",
      "scope": "local",
      "messages": [
        {
          "id": 1234,
          "ts": 1747430800,
          "ts_str": "2025-05-16 14:46",
          "sender": "alice",
          "body": "Water in the parking lot"
        }
      ]
    },
    {
      "name": "#fremont",
      "scope": "mesh",
      "messages": []
    }
  ]
}
```

## v2 schema (api_version = 2)

| Field | Type | Meaning |
|---|---|---|
| `api_version` | int | `2` for this schema. See "Forward compatibility". |
| `server_time` | int | Unix epoch seconds, server clock, captured once at the start of request handling. Not NTP-synced; treat as opaque. On a hub with `[diagnostics] enabled = true`, this field can be deliberately skewed via `POST /api/_test/state` (`server_time_skew_seconds`) for UI testing — see `diagnostics/mesh-sim/README.md`. A "wildly wrong" `server_time` in a dev/staging environment is usually a forgotten skew override, not a clock drift. |
| `hub` | object | Hub identity from `[node]` config. |
| `hub.site_name` | string | Human-readable hub name (mirrors `cfg.node.site_name`). |
| `hub.callsign` | string | On-wire callsign (mirrors `cfg.node.callsign`; lowercased on config load). |
| `channels` | array | Stable order: all `[local].names` entries in config order (each with `scope: "local"`), then all `[channels].names` entries in config order (`scope: "mesh"`). Firmware tracks which channel is currently displayed via array index across refreshes; reordering or inserting on the server side will reshuffle the display. |
| `channels[].name` | string | Channel name verbatim from config, including the leading `#`. |
| `channels[].scope` | string | `"local"` or `"mesh"`. Derived from which config list the channel came from, not from message origin. |
| `channels[].messages` | array | Up to 15 messages per channel: pinned rows first (in `pin_order ASC NULLS LAST`, ties by `ts DESC`), then newest unpinned rows by `ts DESC`, hard-capped at 15 total. Empty channels return `[]`, never omitted. |
| `messages[].id` | int | `messages.id` PK from SQLite. Stable within a single hub. |
| `messages[].ts` | int | Unix epoch seconds. |
| `messages[].ts_str` | string | Pre-formatted `YYYY-MM-DD HH:MM` (24-hour) in the hub's configured `node.timezone` (default `America/Los_Angeles`). The renderer prints this verbatim — the Inkplate firmware does not compute time on its own. DST is handled by the Pi's zoneinfo database, so a fleet of hubs in different zones renders correct wall-clock without per-firmware tz config. Date+time (rather than just `HH:MM`) so messages near midnight, or multi-day-old activity surfaced by a quiet channel, don't read as ambiguously "today". `ts` is preserved alongside for future treatments (e.g. "X min ago") that need the raw epoch. |
| `messages[].sender` | string | Author display name, normalized (see below), capped at 64 chars. |
| `messages[].body` | string | Message body, normalized (see below), capped at 500 chars. |

The schema is intentionally silent on per-message pinning state, votes,
mesh-vs-portal origin, and outbox status. Pinning influences which messages
the server selects but is not surfaced to the client.

## Text normalization

The Inkplate's built-in font is the Adafruit GFX 5×7 ASCII bitmap, and the
bundled Free* GFX fonts don't include emoji or non-Latin scripts. The
server normalizes `sender` and `body` before serializing so the firmware
never has to render glyphs it doesn't carry:

1. **NFKD normalize, then ASCII-fold.** `unicodedata.normalize("NFKD", s)`
   followed by `.encode("ascii", errors="ignore").decode("ascii")`. Accented
   Latin chars are folded to base (`café` → `cafe`); emoji and non-Latin
   scripts are dropped entirely (`日本` → empty).
2. **Collapse whitespace runs.** Any run of `\s` (tab, newline, CR, VT, FF,
   space, multiple spaces) becomes a single space. Critical: this happens
   *before* control-char stripping so an embedded newline becomes a word
   boundary instead of being deleted. `"Greenwood\nLibrary"` →
   `"Greenwood Library"`.
3. **Strip remaining control chars.** Anything in `0x00-0x1F` plus `0x7F`
   (DEL). After step 2 the only whitespace left is `0x20` (space), so this
   strip cannot accidentally remove word boundaries.
4. **Strip leading/trailing whitespace.**
5. **Cap length** at 64 (sender) or 500 (body). Hard truncate — no
   ellipsis; the firmware's text-layout code adds its own if needed.

Raw text is preserved unchanged in the messages table; normalization is
only applied at this API boundary. The English-Latin-script restriction
is a known tradeoff for the current Seattle deployment.

## Forward compatibility

Two rules keep firmware pinned to a given `api_version` viable across
non-breaking server upgrades:

1. **New fields are additive.** The server may add fields to existing
   objects without bumping `api_version`. Firmware MUST ignore unknown
   fields rather than rejecting the response.
2. **Breaking changes bump `api_version`.** Removing a field, renaming
   one, changing a type, or reshuffling the top-level shape counts as
   breaking. Firmware MUST compare the received `api_version` to its
   pinned value and refuse to render (e.g. show an "unsupported hub
   version" screen) if the server's version is higher than expected.

The server emits exactly one `api_version` per response. There is no
content negotiation — firmware does not request a specific version, and
the server has no backward-compat path for older versions.

## Companion endpoints

Chat content lives here. Operator-coded chrome — the §5 nerd strip
and radio liveness — comes from two other server routes the firmware
also reads:

- `GET /api/stats` — pre-bucketed metrics (uptime, CPU, memory, outbox,
  session counts, message-activity histograms). Cached 20 s
  server-side.
- `GET /api/status` — captive-portal radio + mesh_bot liveness
  (`radio_status`, `age_sec`).

Neither has a written wire-format spec; both are de facto API by the
existing UI consumers. See `inkplate/README.md` "Server endpoints
consumed" for the firmware's polling cadence on each, and
`inkplate/render/src/stats.cpp` / `status.cpp` for the exact field
subset the renderer parses.

## Renderer

The reference consumer of this payload is the C++ render library at
`inkplate/render/`. It compiles against Adafruit_GFX for both the
ESP32 (Inkplate library) and a host PNG driver at `inkplate/host/`
that's used for layout iteration without flashing. See
`inkplate/README.md` for the iteration loop and
`inkplate/render/NOTES.md` for the dep-rule the renderer holds itself
to (Adafruit_GFX + ArduinoJson only — no Arduino runtime).

The 15 fixtures under `inkplate/fixtures/` exercise every documented
field of this payload at every documented edge case (empty channels,
five-message limit, long-body truncation, position indicator across
multiple channels, plus an envelope-side `cached_payload` carrying a
nested v2 payload). They protect renderer stability — the wire-format
contract itself is tested by `tests/test_external_display_api.py`,
which exercises `build_state()` directly. If a future server change
adds a new payload field, the fixtures stay valid (the renderer
ignores unknown fields per the additive-fields rule above); only a
breaking schema bump would require fixture updates.

### Schema history

| Version | Shipped | Breaking change(s) from prior |
|---|---|---|
| 1 | Phase 0 — hardcoded placeholder | (initial) |
| 2 | Phase 1 — real data | Flat `messages[]` → nested `channels[].messages[]`; added `scope`, `server_time`; messages now carry `ts` (epoch int) and drop the `channel` field (channel is on the parent); `hub` now sourced from `cfg.node` instead of placeholders. |
| 2 (additive) | Inkplate bulletin rework | `messages[].ts_str` added; per-channel cap raised from 5 to 15. Both additive per the forward-compat rule above — older firmware reading this payload ignores `ts_str` and gracefully consumes whatever subset of the 15 it has room for. |

---

## Historical: Phase 0 (api_version 1)

> v1 was a hardcoded placeholder shipped in Phase 0 to establish the contract
> before real data was wired. Current servers emit only v2; field units
> pinned to v1 must be reflashed.

### v1 example payload

```
HTTP/1.0 200 OK
Content-Type: application/json; charset=utf-8

{
  "api_version": 1,
  "hub": {
    "site_name": "Placeholder Hub",
    "callsign": "stub"
  },
  "messages": [
    {
      "id": 1,
      "channel": "#civicmesh",
      "sender": "alice",
      "body": "Hardcoded sample message — Phase 0 stub."
    },
    ...
  ]
}
```

### v1 schema

| Field | Type | Meaning |
|---|---|---|
| `api_version` | int | Always `1` in this schema. |
| `hub` | object | Hub identity (hardcoded placeholder in Phase 0). |
| `hub.site_name` | string | Human-readable hub name. |
| `hub.callsign` | string | On-wire callsign. |
| `messages` | array | Flat list of recent messages across all channels. |
| `messages[].id` | int | Server-assigned message identifier. |
| `messages[].channel` | string | Channel name including the leading `#`. |
| `messages[].sender` | string | Display name of the author. |
| `messages[].body` | string | Message text (un-normalized). |

The disabled-state 404 contract, the endpoint path, and the forward-compat
rules carried forward unchanged into v2.
