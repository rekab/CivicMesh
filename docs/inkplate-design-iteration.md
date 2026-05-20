# Iterating on Inkplate UI design without flashing

The Inkplate e-paper bulletin display has two simulators so the
design loop is **host-side only** — no Inkplate, no radio, no
flashing. Edit a screen, render a PNG, eyeball, repeat. Cycle
time is seconds.

The two pieces:

1. **Conversation simulator** — writes synthetic mesh activity to
   the SQLite `messages` table so the captive portal and the
   external-display payload have realistic content to render.
2. **UI render simulator** — compiles the same C++ render source
   that runs on the ESP32 against a host-side framebuffer and
   writes a 1-bit 800×600 PNG.

They compose end-to-end via a small helper that fetches the live
payload and pipes it through the renderer.

## Conversation simulator — `diagnostics/mesh-sim/`

Bypasses `meshcore_py`, the outbox, and the radio entirely. Writes
JSON scenarios straight into the `messages` table. Each scenario has
a recurring cast of senders with uneven posting distributions —
deliberately uneven, since a real channel is never uniform. Two
committed scenarios are starting points: `silent-drift.json`
(week-3 blackout, silent dirigible drones) and `phantom-jam.json`
(mystery jam jars appearing on porches).

```bash
# Enable diagnostics gate first (in config.toml):
#   [diagnostics]
#   enabled = true
# Then inject a scenario:
python diagnostics/mesh-sim/inject.py \
    diagnostics/mesh-sim/scenarios/silent-drift.json

# Reset cleanly between iterations:
python diagnostics/mesh-sim/inject.py --wipe-all --yes
```

Authoring new scenarios: the README has a Claude-Code-ready prompt
that produces recurring-cast / realistic-distribution channel content
on demand. See [`diagnostics/mesh-sim/README.md`](../diagnostics/mesh-sim/README.md)
§ "Authoring new scenarios with Claude Code".

**Companion endpoint** `/api/_test/state` overrides server-health
fields (`radio_status`, `recovery_state`, `last_seen_ts`,
`server_time_skew_seconds`) in memory on the running web server.
Same gate. Use to exercise the captive portal's status indicators
and the external-display footer's relative-time strings without
unplugging anything.

```bash
# Flip the radio indicator to "offline":
curl -X POST http://civicmesh/api/_test/state -d '{"radio_status":"offline"}'

# Shift server_time forward by an hour for the external-display payload:
curl -X POST http://civicmesh/api/_test/state -d '{"server_time_skew_seconds":3600}'

# Clear everything:
curl -X DELETE http://civicmesh/api/_test/state
```

Gone on server restart; never persisted.

## UI render simulator — `inkplate/host/host_render`

The C++ render library at `inkplate/render/` is Adafruit_GFX-only and
depends on nothing Arduino-specific. It compiles for two targets from
the same source:

- **ESP32** (the Inkplate panel itself) — only used in
  `firmware/bulletin/`, not relevant to the design loop.
- **Host** — `inkplate/host/` builds it against a `GFXcanvas1`
  framebuffer with vendored Adafruit_GFX, lodepng, and ArduinoJson.
  Produces `host_render`, a stdin-to-stdout PNG driver.

```bash
cd inkplate/host && make            # one-time build

# Render a fixture
./host_render < ../fixtures/normal_bulletin__pinned_first/in.json > /tmp/f.png

# View — `xdg-open` on Linux, `open` on macOS, or from the dev Pi:
#   scp civicmesh:/tmp/f.png ~/Downloads/  (run from desktop)
```

15 fixtures live under `inkplate/fixtures/`, each a combined
envelope+payload `in.json` paired with a committed `expected.png`
golden. They cover the four screen templates and every documented
edge case:

- Normal bulletin (pinned first / empty channel / five messages /
  long body truncation / channel-cycle position indicator)
- Stale (radio_state down)
- Low battery / critical battery (full-screen takeover)
- Five failure-shell variants (AP unreachable with-and-without cached
  payload, DNS failure, Pi unreachable, HTTP error, bad JSON)
- API version mismatch

To iterate on a layout: edit a screen in `inkplate/render/src/screens/`,
rebuild (`make`), re-render a fixture, eyeball. Diff against the golden
with `inkplate/tools/regen_fixtures.sh --check`. Once happy, regenerate
goldens with `--write` and commit the visual delta.

## End-to-end loop (composes both simulators)

```bash
# 1. Inject a scenario into the DB
python diagnostics/mesh-sim/inject.py diagnostics/mesh-sim/scenarios/silent-drift.json

# 2. (Optional) override server-health state
curl -X POST http://civicmesh/api/_test/state -d '{"radio_status":"recovering"}'

# 3. Fetch the live payload and render through host_render
inkplate/tools/render-live.sh /tmp/live.png
# Default hub is HUB=http://civicmesh; override with
#   HUB=http://localhost:8080 inkplate/tools/render-live.sh /tmp/live.png

# 4. View the PNG (xdg-open / open / scp from Pi)
```

No Inkplate, no radio, no flashing — just the dev hub plus two
simulators driving it.

## Where to dig deeper

- [`inkplate/README.md`](../inkplate/README.md) — full renderer /
  host / tools / fixtures guide; Prerequisites; fallback ladder for
  arduino-cli library discovery (only relevant if you do want to
  flash the bulletin firmware).
- [`inkplate/render/NOTES.md`](../inkplate/render/NOTES.md) —
  Adafruit_GFX-only dep rule, the baseline-anchor convention for
  custom GFX fonts, the three library footguns the renderer
  sidesteps.
- [`diagnostics/mesh-sim/README.md`](../diagnostics/mesh-sim/README.md)
  — scenario JSON schema, the scenario-authoring prompt, sidecar
  file (`.injected_ids.json`) semantics, full `/api/_test/state`
  allowlist and examples.
- [`docs/external-display-api.md`](external-display-api.md) —
  wire-format contract that the renderer consumes (v2 schema,
  text-normalization rules, forward-compatibility rules).
