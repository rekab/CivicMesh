# mesh-sim

A bench tool that scribbles synthetic mesh activity directly into the
CivicMesh `messages` table so you can iterate on the captive portal UI
and the `/api/external-display/state` payload without standing up a real
radio.

**It is honest about what it is.** The tool bypasses `meshcore_py`, the
outbox, the radio, and every piece of ingest plumbing. Rows are
INSERTed straight into SQLite with `source='mesh'` (by default). That's
appropriate for UI iteration; it is **not** an integration-test harness.
For radio-layer behavior, see `diagnostics/radio/`.

The tool refuses to run unless `[diagnostics] enabled = true` in the
loaded config. That gate exists so a prod hub can't accidentally have
the injector active.

## Quick start

From the repo root, on a dev/staging hub:

```bash
# 1. Enable the diagnostics gate in your config.toml:
#    [diagnostics]
#    enabled = true

# 2. Run a starter scenario against your dev DB:
python diagnostics/mesh-sim/inject.py diagnostics/mesh-sim/scenarios/silent-drift.json

# 3. View the result:
#    - browser: http://<hub-ip>/    (captive portal)
#    - api:     http://<hub-ip>/api/external-display/state

# 4. Iterate. --replace-injected clears the previous batch and writes a fresh one
#    without touching any real-radio rows you may also have in the DB:
python diagnostics/mesh-sim/inject.py diagnostics/mesh-sim/scenarios/silent-drift.json --replace-injected

# 5. Hard reset (no real-radio rows to preserve):
python diagnostics/mesh-sim/inject.py --wipe-all --yes
```

`inject.py` writes the IDs of the rows it inserted to
`diagnostics/mesh-sim/.injected_ids.json` (gitignored). That's what
`--replace-injected` reads to know which rows are safe to delete.

## CLI

```
inject.py SCENARIO [--replace-injected | --wipe-all --yes]
                   [--anchor ISO8601] [--config PATH]
```

| Flag | Meaning |
|---|---|
| `SCENARIO` | Path to a scenario JSON file. Optional if you're invoking `--wipe-all` or `--replace-injected` alone. |
| `--replace-injected` | Before inserting the scenario, delete the rows recorded in `.injected_ids.json` (and their `votes`). Real-radio rows survive. |
| `--wipe-all` | `DELETE FROM messages; DELETE FROM votes;`. Requires `--yes` or refuses to run. Does NOT touch `outbox` (live radio state). |
| `--yes` | Confirms `--wipe-all`. Ignored otherwise. |
| `--anchor` | ISO8601 datetime used as the `t=0` reference for `ts_offset`. Default: wall-clock now (UTC). Naive datetimes are interpreted as local time. |
| `--config` | Path to `config.toml`. Default: `$CIVICMESH_CONFIG` or `./config.toml`. |

`--replace-injected` and `--wipe-all` are mutually exclusive. `--wipe-all`
may be combined with a scenario path: it wipes first, then inserts the
scenario fresh.

## Scenario JSON schema

```json
{
  "name": "grid-down-tuesday",
  "description": "Two-hour crisis window across configured channels.",
  "tags": ["crisis", "mutual-aid"],
  "messages": [
    {
      "channel": "#civicmesh",
      "sender": "alice",
      "body": "Power's out on 65th.",
      "ts_offset": "-1h30m",
      "source": "mesh",
      "pinned": false
    }
  ]
}
```

### Top level

| Field | Required | Type | Notes |
|---|---|---|---|
| `name` | yes | string | Non-empty. Surfaces in the injector's success line. |
| `messages` | yes | array | One scenario message per entry. |
| `description` | no | string | Free-form for humans. Not used by the tool. |
| `tags` | no | array of strings | Free-form for humans. Not used by the tool. |

Any other top-level field is a hard error naming the offending field.

### Per message

| Field | Required | Type | Notes |
|---|---|---|---|
| `channel` | yes | string | Including the leading `#`. Channels not in `[channels].names` or `[local].names` warn on stderr (once each) but the message is still inserted. |
| `sender` | yes | string | Capped at `limits.name_max_chars` (default 12). |
| `body` | yes | string | Capped at `limits.message_max_chars` (default 100). |
| `ts_offset` | yes | string | Duration relative to `--anchor`. Grammar below. |
| `source` | no | string | One of `"mesh"` (default), `"wifi"`, `"local"`. Anything else is a hard error. `"wifi"` warns once because the injector creates no outbox row, so the UI will treat the message as posted-but-unsent forever. |
| `pinned` | no | bool | Default `false`. When true, the inserted row gets `pinned=1` and the next-available `pin_order` for that channel. |

Any other per-message field is a hard error naming the offending field.

### Duration grammar (`ts_offset`)

Optional sign, then one or more `<integer><unit>` segments in **strict
descending unit order** (`h` then `m` then `s`), no whitespace. Bare `0`
and `0s` (with or without sign) are accepted as zero. Negative is the
past, positive is the future, zero is the anchor.

| Accept | Reject |
|---|---|
| `-1h` | `1.5h` (no fractional) |
| `-90m` | `1h 30m` (no whitespace) |
| `-1h30m` | `30` (bare int other than zero) |
| `+5m` | `-1m30h` (wrong unit order) |
| `0s` | `-h` (number required before unit) |
| `0` | `""` (empty) |
| `-0` | `1d` (no day/week units) |

Day and week aren't supported on purpose. If you need them, your scenario
is probably the wrong shape; consider splitting it.

## How `--replace-injected` knows what's yours

Every successful insert records its `messages.id` in
`diagnostics/mesh-sim/.injected_ids.json`. `--replace-injected` reads
that file, deletes those exact rows (and any `votes` referencing them),
clears the file, then proceeds with the new scenario. Real radio
traffic that landed in the DB via `mesh_bot` is not in the sidecar and
is not touched.

A `--wipe-all` clears the sidecar at the same time, so a subsequent
`--replace-injected` does not try to DELETE rows that no longer exist.

If you ever need to start fresh by hand (e.g. the sidecar has gone out
of sync with the DB), just delete the file:

```bash
rm diagnostics/mesh-sim/.injected_ids.json
```

## Authoring new scenarios with Claude Code

The tool is dumb on purpose: it parses and inserts. All creative work
happens at authorship time, in a separate Claude Code session. Use the
prompt below verbatim — the recurring-cast and uneven-distribution
rules are load-bearing and will produce a flat, unconvincing channel
feed if dropped.

> I want a new mesh-sim scenario at `diagnostics/mesh-sim/scenarios/<NAME>.json`.
>
> Theme: `<THEME>` (e.g. "afternoon storm, water main break", "school
> lockout drill", "mutual aid for a wildfire evacuation").
>
> Duration: about `<DURATION>` (e.g. "90 minutes", "3 hours"). Use
> `ts_offset` values relative to anchor=now; the most recent message
> should be at or close to `0`.
>
> Length: about `<COUNT>` messages. About `<N_PEOPLE>` distinct senders.
>
> Rules — these are not negotiable:
>
> 1. **Recurring cast.** The same `<N_PEOPLE>` names recur. Don't invent
>    a new sender per message. A real channel has a handful of regulars.
> 2. **Uneven distribution.** One sender is chatty (~25-35% of
>    messages). One or two are quiet (1-2 messages). The rest fall in
>    the middle. A real channel is never uniform.
> 3. **Channels.** Use only `#civicmesh`, `#testing`, and `#local`
>    unless the user's config.toml has others. Most traffic on
>    `#civicmesh`; occasional cross-channel test relay on `#testing`.
> 4. **Character limits.** Sender names: ≤12 chars, `[A-Za-z0-9_-]`.
>    Bodies: ≤100 chars. The injector will hard-error on overlong values.
> 5. **ASCII only.** No emoji, no smart quotes, no accents. Bodies must
>    survive `str.encode("ascii", errors="strict")`.
> 6. **Realistic chronology.** Newer messages reply to older ones; the
>    same person doesn't post twice in 30 seconds; later messages
>    sometimes reference earlier ones ("on it", "thanks luz", "update on
>    the cat").
> 7. **One pinned message** if the scenario benefits from one
>    (announcements, dispatch notices). Otherwise none. Set `"pinned":
>    true` on that one entry.
>
> Output the scenario JSON only — no commentary, no markdown fence.
> Match the shape of `diagnostics/mesh-sim/scenarios/silent-drift.json`.

If you write the prompt and the result is flat ("everyone posts once,
all evenly spaced"), the cast/distribution rules got dropped. Reissue
the prompt with those rules quoted explicitly.

## `/api/_test/state` — in-memory server-state overrides

Companion to the injector. The injector covers UI state derivable
from message *content*; this endpoint covers UI state derivable from
server *health*: radio status, recovery state, last-heard timestamp,
server clock. Override the fields and exercise the captive portal's
status indicators (and the external-display payload's `server_time`)
without running mesh_bot or unplugging the radio.

Gated by the same `[diagnostics] enabled = true` flag this tool
uses. Override store is in-memory on the running web server; gone
on restart, never persisted.

| Method | Behavior |
|---|---|
| `GET /api/_test/state` | Returns the current override dict (`{}` if empty). |
| `POST /api/_test/state` | JSON body: a dict of `{field: value}`. Merges into the store. A `null` value clears that one field. Unknown fields → 400 naming the field, no partial application. Type mismatch → 400. |
| `DELETE /api/_test/state` | Clears all overrides, returns 204. |

All three return 404 with `{"error": "not found"}` when
`[diagnostics]` is disabled.

### Allowlist

| Field | Type | Effect |
|---|---|---|
| `radio_status` | str — one of `"online"`, `"offline"`, `"recovering"`, `"needs_human"` | Replaces `radio_status` in the `/api/status` response. Wins over any value the derivation block would compute, including overrides flowing through `last_seen_ts` and `recovery_state` below. |
| `recovery_state` | str or `null` | Replaces `recovery_state` in `/api/status`. Also flows into the derivation block, so e.g. setting it to `"recovering"` (without overriding `radio_status` directly) flips `radio_status` to `"recovering"`. No enum constraint — the operator picks the value they want to test. |
| `last_seen_ts` | int (epoch seconds) or `null` | Replaces `last_seen_ts` in `/api/status` normal branch; `age_sec` re-derives from it. **No-op when the status row is missing** — the empty-branch response shape doesn't expose `last_seen_ts`, so the override has nowhere to land. To test "mesh_bot is stale," ensure a status row exists (mesh_bot wrote one once) and override `last_seen_ts` to something old. |
| `server_time_skew_seconds` | int (any sign) | Additive on wall-clock at the two response entry points: `/api/status` (where it shifts both `age_sec` and `outbox_queue_depth`'s window) and `/api/external-display/state` (where it shifts `server_time`). Internal time use — telemetry inserts, rate-limit windows, DB row timestamps — is unaffected. |

### Examples

```bash
# Flip the radio indicator to "offline" without unplugging anything:
curl -X POST http://civicmesh/api/_test/state \
     -d '{"radio_status":"offline"}'

# Shift server_time forward by an hour for the external-display payload:
curl -X POST http://civicmesh/api/_test/state \
     -d '{"server_time_skew_seconds":3600}'

# Inspect the current overrides:
curl http://civicmesh/api/_test/state

# Clear one field (others remain):
curl -X POST http://civicmesh/api/_test/state \
     -d '{"radio_status":null}'

# Clear every override at once:
curl -X DELETE http://civicmesh/api/_test/state
```

The override store is on the running web server process. Restart
`civicmesh-web` and every override resets to absent — same lifetime
as any other in-memory state.

## Pointer

The sibling tool `diagnostics/radio/` exercises the actual radio layer
end-to-end. mesh-sim deliberately bypasses it. If you're trying to
reproduce a `send_chan_msg` ERROR, characterize echo timings, or
validate that two physical nodes are actually talking, use radio/, not
mesh-sim.
