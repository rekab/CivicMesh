# External display API

Server-side contract for the optional Inkplate 6 e-paper bulletin display
that some CivicMesh hubs may have attached. Most hubs do not. The endpoint
is always registered, but is gated by `external_display.enabled` in
`config.toml` so disabled hubs pay no cost beyond the routing check.

This document describes **v0** (`api_version = 1`), the Phase 0 stub.
Field contents are hardcoded placeholders this phase. Phase 1 will source
`hub` from `[node]` config and `messages` from the database without
changing the schema.

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

## v0 schema

| Field | Type | Meaning |
|---|---|---|
| `api_version` | int | Currently `1`. See "Forward compatibility" below. |
| `hub` | object | Hub identity. |
| `hub.site_name` | string | Human-readable hub name (mirrors `[node].site_name`). |
| `hub.callsign` | string | On-wire callsign (mirrors `[node].callsign`). |
| `messages` | array | Recent messages to display. Ordering is server-controlled — firmware renders in array order, top to bottom. |
| `messages[].id` | int | Server-assigned message identifier. Stable within a single hub. |
| `messages[].channel` | string | Channel name including the leading `#`. |
| `messages[].sender` | string | Display name of the author. |
| `messages[].body` | string | Message text. |

## Forward compatibility

The schema follows two rules so a firmware build pinned to `api_version: 1`
can keep running across server upgrades:

1. **New fields are additive.** The server may add fields to existing
   objects without bumping `api_version`. Firmware MUST ignore unknown
   fields rather than rejecting the response.
2. **Breaking changes bump `api_version`.** Removing a field, renaming a
   field, or changing a field's type counts as breaking. Firmware MUST
   compare the received `api_version` to its pinned value and refuse to
   render (e.g., display an "unsupported hub version" screen) if the
   server version is higher than expected.

The server emits exactly one `api_version` per response. There is no
content negotiation — firmware does not request a specific version.

## Phase 0 → Phase 1

| Phase 0 (now) | Phase 1 (planned) |
|---|---|
| `hub` is hardcoded to `{site_name: "Placeholder Hub", callsign: "stub"}` | `hub` is read from `cfg.node` |
| `messages` is a hardcoded two-entry list | `messages` is sourced from the message table, filtered to channels in `cfg.channels.names`, limited to the most recent N |
| No timestamp field | A `server_time` (or equivalent) field may be added if firmware needs clock sync; additive, no `api_version` bump |

The Phase 0 stub exists so firmware development can begin against a
stable contract before server-side data plumbing is ready.
