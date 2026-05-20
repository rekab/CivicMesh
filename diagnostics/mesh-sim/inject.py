#!/usr/bin/env python3
"""mesh-sim: inject synthetic mesh activity into the CivicMesh messages
table for UI iteration on the captive portal and external-display payload.

Bypasses meshcore_py, the outbox, and the radio layer entirely — rows are
INSERTed straight into `messages`. Honest about what it is: a UI-iteration
tool, not an integration-test harness. Use diagnostics/radio/ for radio.

Gated by `[diagnostics] enabled = true` in config.toml. Refuses to run
otherwise as a belt-and-suspenders guard against accidental prod use.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the repo root importable when running this script directly
# (`python diagnostics/mesh-sim/inject.py ...`). The package layout puts
# config.py and database.py at the repo root; the harness in
# diagnostics/radio/ does the same.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import AppConfig, load_config  # noqa: E402
from database import DBConfig, init_db, insert_message  # noqa: E402


# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------

# Strict grammar: optional sign, then h/m/s segments in descending unit
# order with no whitespace. Each segment is optional but at least one
# must be present — handled below the regex match, since the regex itself
# happily matches the empty string.
_DURATION_RE = re.compile(r"^([+-]?)(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class BadDuration(ValueError):
    pass


def parse_duration(s: str) -> int:
    """Parse a ts_offset string into a signed integer of seconds.

    Accepts: -1h, -90m, -1h30m, +5m, 0s, 0 (bare zero), -0, +0, -0s, etc.
    Rejects: 1.5h, '1h 30m', '30', '-1m30h', '-h', '', None.
    """
    if not isinstance(s, str):
        raise BadDuration(f"ts_offset must be a string, got {type(s).__name__}: {s!r}")
    # Bare-zero special case: "0", "+0", "-0" are accepted as zero seconds.
    # Without this, the regex matches them but the no-segment check below
    # would reject them.
    if s in ("0", "+0", "-0"):
        return 0
    m = _DURATION_RE.match(s)
    if not m:
        raise BadDuration(
            f"ts_offset {s!r} is not a valid duration. "
            "Format: optional [+/-] then segments in order [Nh][Nm][Ns] "
            "with no whitespace (e.g. -1h30m, +5m, 0s)."
        )
    sign, h, mins, sec = m.groups()
    if h is None and mins is None and sec is None:
        # Sign-only or empty — regex matched but there was nothing to parse.
        raise BadDuration(
            f"ts_offset {s!r} has no h/m/s segment. "
            "Use 0 or 0s for zero offset."
        )
    total = (int(h or 0) * 3600) + (int(mins or 0) * 60) + int(sec or 0)
    return -total if sign == "-" else total


# ---------------------------------------------------------------------------
# Scenario JSON schema
# ---------------------------------------------------------------------------

_SCENARIO_TOP_REQUIRED = {"name", "messages"}
_SCENARIO_TOP_OPTIONAL = {"description", "tags"}
_SCENARIO_TOP_ALL = _SCENARIO_TOP_REQUIRED | _SCENARIO_TOP_OPTIONAL

_MESSAGE_REQUIRED = {"channel", "sender", "body", "ts_offset"}
_MESSAGE_OPTIONAL = {"source", "pinned"}
_MESSAGE_ALL = _MESSAGE_REQUIRED | _MESSAGE_OPTIONAL

# Same set production accepts via insert_message; "local" is a real
# historical source value (see the status-column comment in database.py).
# "wifi" is allowed but warned about — its production semantics require an
# outbox row, which the injector deliberately does not create, so a row
# inserted as source=wifi will look perpetually queued to the UI.
_SOURCE_ALLOWLIST = frozenset({"mesh", "wifi", "local"})


class ScenarioError(ValueError):
    pass


def _reject_unknown(d: dict, allowed: set, where: str) -> None:
    bad = set(d) - allowed
    if bad:
        raise ScenarioError(
            f"{where}: unknown field(s) {sorted(bad)!r}. "
            f"Allowed: {sorted(allowed)!r}."
        )


def validate_scenario(doc: Any, cfg: AppConfig) -> list[dict]:
    """Strict validation. Returns the per-message dicts ready for insertion.

    `cfg` is needed for length caps (name_max_chars, message_max_chars)
    and for the channel warning. Channel warnings are emitted here as a
    side effect rather than queued — keeps the call site simple.
    """
    if not isinstance(doc, dict):
        raise ScenarioError(f"scenario root: expected object, got {type(doc).__name__}")

    _reject_unknown(doc, _SCENARIO_TOP_ALL, "scenario")
    missing = _SCENARIO_TOP_REQUIRED - set(doc)
    if missing:
        raise ScenarioError(f"scenario: missing required field(s) {sorted(missing)!r}")
    if not isinstance(doc["name"], str) or not doc["name"].strip():
        raise ScenarioError("scenario.name: must be a non-empty string")
    if not isinstance(doc["messages"], list):
        raise ScenarioError("scenario.messages: must be a list")

    known_channels = set(cfg.channels.names) | set(cfg.local.names)
    warned_channels: set[str] = set()
    out: list[dict] = []

    name_cap = cfg.limits.name_max_chars
    body_cap = cfg.limits.message_max_chars

    for i, msg in enumerate(doc["messages"]):
        prefix = f"scenario.messages[{i}]"
        if not isinstance(msg, dict):
            raise ScenarioError(f"{prefix}: expected object, got {type(msg).__name__}")
        _reject_unknown(msg, _MESSAGE_ALL, prefix)
        missing = _MESSAGE_REQUIRED - set(msg)
        if missing:
            raise ScenarioError(
                f"{prefix}: missing required field(s) {sorted(missing)!r}"
            )

        channel = msg["channel"]
        sender = msg["sender"]
        body = msg["body"]
        ts_offset_raw = msg["ts_offset"]
        source = msg.get("source", "mesh")
        pinned = msg.get("pinned", False)

        if not isinstance(channel, str) or not channel.strip():
            raise ScenarioError(f"{prefix}.channel: must be a non-empty string")
        if not isinstance(sender, str) or not sender.strip():
            raise ScenarioError(f"{prefix}.sender: must be a non-empty string")
        if not isinstance(body, str) or not body.strip():
            raise ScenarioError(f"{prefix}.body: must be a non-empty string")
        if not isinstance(pinned, bool):
            raise ScenarioError(
                f"{prefix}.pinned: must be a boolean, got {type(pinned).__name__}"
            )
        if not isinstance(source, str):
            raise ScenarioError(
                f"{prefix}.source: must be a string, got {type(source).__name__}"
            )
        if source not in _SOURCE_ALLOWLIST:
            raise ScenarioError(
                f"{prefix}.source: {source!r} is not in allowlist "
                f"{sorted(_SOURCE_ALLOWLIST)!r}"
            )

        if len(sender) > name_cap:
            raise ScenarioError(
                f"{prefix}.sender: {len(sender)} chars exceeds limits.name_max_chars={name_cap} "
                f"(value: {sender!r})"
            )
        if len(body) > body_cap:
            raise ScenarioError(
                f"{prefix}.body: {len(body)} chars exceeds limits.message_max_chars={body_cap}"
            )

        try:
            ts_offset_sec = parse_duration(ts_offset_raw)
        except BadDuration as e:
            raise ScenarioError(f"{prefix}.ts_offset: {e}") from None

        if channel not in known_channels and channel not in warned_channels:
            # Warn once per unknown channel name — repeating per-message would
            # bury the output for a 40-message scenario.
            print(
                f"mesh-sim: warning: channel {channel!r} is not in "
                f"[channels].names or [local].names; inserting anyway.",
                file=sys.stderr,
            )
            warned_channels.add(channel)

        if source == "wifi":
            # Once per scenario; same reasoning as the channel warning.
            if "wifi" not in warned_channels:  # reuse set as flag dedupe
                print(
                    f"mesh-sim: warning: {prefix}.source='wifi' bypasses the outbox; "
                    "the resulting row will not have an outbox link and the UI "
                    "will treat it as a posted-but-unsent message forever.",
                    file=sys.stderr,
                )
                warned_channels.add("wifi")  # not a channel, harmless sentinel

        out.append({
            "channel": channel,
            "sender": sender,
            "body": body,
            "source": source,
            "pinned": pinned,
            "ts_offset_sec": ts_offset_sec,
        })

    return out


# ---------------------------------------------------------------------------
# Anchor parsing
# ---------------------------------------------------------------------------


def parse_anchor(s: str | None) -> int:
    """Resolve --anchor to an epoch-seconds int.

    None → wall-clock now (UTC). A string → ISO8601 parsed by
    datetime.fromisoformat (which accepts the "Z" suffix on Python 3.11+).
    Naive datetimes are interpreted as local time, matching the operator's
    intuition when they type `--anchor 2026-05-16T14:00:00`.
    """
    if s is None:
        return int(datetime.now(timezone.utc).timestamp())
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(
            f"--anchor {s!r} is not a valid ISO8601 datetime: {e}"
        ) from None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # assume local
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Sidecar (.injected_ids.json) — tracks rows we own so --replace-injected
# can clean up without disturbing real-radio traffic.
# ---------------------------------------------------------------------------

_SIDECAR_NAME = ".injected_ids.json"


def _sidecar_path() -> Path:
    return Path(__file__).resolve().parent / _SIDECAR_NAME


def _load_sidecar() -> list[int]:
    p = _sidecar_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"mesh-sim: failed to read sidecar {p}: {e}. "
            "Delete the file by hand if it's corrupted."
        ) from None
    if not isinstance(data, list) or not all(isinstance(x, int) for x in data):
        raise RuntimeError(
            f"mesh-sim: sidecar {p} is malformed (expected a JSON list of ints)."
        )
    return data


def _write_sidecar(ids: list[int]) -> None:
    p = _sidecar_path()
    p.write_text(json.dumps(ids))


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------


def _connect_writable(db_path: str) -> sqlite3.Connection:
    """Open a connection with the same settings the app uses.

    autocommit + row_factory matches database._connect. We don't import
    that helper because it's private and we don't need the retry decorator
    here — this script runs interactively and a lock error is a useful
    failure mode rather than something to silently retry through.
    """
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _check_wal_mode(conn: sqlite3.Connection) -> None:
    # One PRAGMA query. Catches the case where someone hand-created the DB
    # file with sqlite3 CLI and forgot WAL mode — readers (mesh_bot,
    # web_server) would then block on every injector write, which would
    # look like the app hanging at random.
    row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = (row[0] if row else "").lower()
    if mode != "wal":
        raise RuntimeError(
            f"mesh-sim: database journal_mode is {mode!r}, expected 'wal'. "
            "Run civicmesh-web once or call database.init_db to fix."
        )


def _next_pin_order(conn: sqlite3.Connection, channel: str) -> int:
    """Return the next pin_order for this channel: existing max + 1, or 1."""
    row = conn.execute(
        "SELECT COALESCE(MAX(pin_order), 0) AS m FROM messages "
        "WHERE channel=? AND pinned=1",
        (channel,),
    ).fetchone()
    return int(row["m"]) + 1


def do_wipe_all(db_cfg: DBConfig) -> tuple[int, int]:
    """TRUNCATE-equivalent: delete every row from messages and votes.

    Does NOT touch outbox. Outbox rows reflect in-flight or historical
    radio state that the operator might care about; clearing them would
    erase real send-attempt history. If you want a true reset, drop the
    DB file.
    """
    conn = _connect_writable(db_cfg.path)
    try:
        _check_wal_mode(conn)
        conn.execute("BEGIN")
        cur_v = conn.execute("DELETE FROM votes")
        cur_m = conn.execute("DELETE FROM messages")
        conn.execute("COMMIT")
        return cur_m.rowcount or 0, cur_v.rowcount or 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def do_replace_injected(db_cfg: DBConfig) -> tuple[int, int]:
    """Delete the rows recorded in the sidecar, then clear the sidecar.

    Returns (messages_deleted, votes_deleted). Rows not in the sidecar
    (i.e. real-radio traffic, or rows from before mesh-sim existed) are
    left alone — that's the entire point of having a sidecar in the first
    place rather than DELETEing by source.
    """
    ids = _load_sidecar()
    if not ids:
        return 0, 0
    conn = _connect_writable(db_cfg.path)
    try:
        _check_wal_mode(conn)
        placeholders = ",".join("?" * len(ids))
        conn.execute("BEGIN")
        cur_v = conn.execute(
            f"DELETE FROM votes WHERE message_id IN ({placeholders})",
            ids,
        )
        cur_m = conn.execute(
            f"DELETE FROM messages WHERE id IN ({placeholders})",
            ids,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    _write_sidecar([])
    return cur_m.rowcount or 0, cur_v.rowcount or 0


def do_inject(db_cfg: DBConfig, anchor_epoch: int, validated: list[dict]) -> list[int]:
    """Insert rows and return the new ids. Appends to the sidecar."""
    # Pre-flight the WAL check on a one-shot connection so we fail early
    # if the DB is misconfigured, before we start inserting.
    conn = _connect_writable(db_cfg.path)
    try:
        _check_wal_mode(conn)
    finally:
        conn.close()

    new_ids: list[int] = []
    # insert_message opens its own connection per call; that's wasteful at
    # scale but matches how the production code uses it and keeps the
    # injector's behavior identical to a real ingest path.
    for m in validated:
        ts = anchor_epoch + m["ts_offset_sec"]
        msg_id = insert_message(
            db_cfg,
            ts=ts,
            channel=m["channel"],
            sender=m["sender"],
            content=m["body"],
            source=m["source"],
        )
        if m["pinned"]:
            # insert_message doesn't expose pinned/pin_order, so do a
            # follow-up UPDATE. Picks the next available pin_order for
            # that channel so multiple pinned messages stack predictably
            # in the order they appear in the scenario.
            pin_conn = _connect_writable(db_cfg.path)
            try:
                order = _next_pin_order(pin_conn, m["channel"])
                pin_conn.execute(
                    "UPDATE messages SET pinned=1, pin_order=? WHERE id=?",
                    (order, msg_id),
                )
            finally:
                pin_conn.close()
        new_ids.append(msg_id)

    existing = _load_sidecar()
    _write_sidecar(existing + new_ids)
    return new_ids


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-sim inject",
        description=(
            "Inject synthetic mesh activity into the CivicMesh messages "
            "table for UI iteration. Bypasses the radio + outbox path. "
            "Refuses to run unless [diagnostics] enabled = true."
        ),
    )
    p.add_argument(
        "scenario",
        nargs="?",
        help="Path to a scenario JSON file. Required unless --wipe-all is used alone.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.toml (default: CIVICMESH_CONFIG env or ./config.toml).",
    )
    p.add_argument(
        "--anchor",
        default=None,
        help="ISO8601 datetime used as the t=0 reference for ts_offset. "
             "Default: wall-clock now (UTC).",
    )
    # Lets the host-side iteration harness (inkplate/tools/contact-sheet.py)
    # point the injector at a scratch DB without editing config.toml.
    # Bypasses cfg.db_path entirely; sidecar tracking still applies and is
    # the operator's responsibility — using --replace-injected against a
    # different DB than the one the sidecar was recorded against will
    # silently fail to find anything to delete.
    p.add_argument(
        "--db",
        default=None,
        help="Override cfg.db_path with this SQLite path. For the host-side "
             "iteration harness; leaves config.toml alone.",
    )
    mx = p.add_mutually_exclusive_group()
    mx.add_argument(
        "--replace-injected",
        action="store_true",
        help="Delete rows recorded in .injected_ids.json before appending. "
             "Leaves real-radio rows untouched.",
    )
    mx.add_argument(
        "--wipe-all",
        action="store_true",
        help="Delete every row from messages and votes. Requires --yes.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm --wipe-all. Has no effect without --wipe-all.",
    )
    return p


def _resolve_config_path(arg_value: str | None) -> str:
    if arg_value:
        return arg_value
    env = os.environ.get("CIVICMESH_CONFIG")
    if env:
        return env
    return "config.toml"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(_resolve_config_path(args.config))
    except (ValueError, KeyError, OSError) as e:
        print(f"mesh-sim: failed to load config: {e}", file=sys.stderr)
        return 2

    if not cfg.diagnostics.enabled:
        print(
            "mesh-sim: refusing to run — [diagnostics] enabled is false. "
            "Set `enabled = true` under [diagnostics] in your config.toml "
            "ONLY on a dev/staging hub.",
            file=sys.stderr,
        )
        return 2

    db_cfg = DBConfig(path=args.db or cfg.db_path)
    # Ensure schema exists; harmless if already initialized. The WAL-mode
    # PRAGMA check happens inside each do_* function so a misconfigured DB
    # fails before we touch any rows.
    init_db(db_cfg)

    if args.wipe_all:
        if not args.yes:
            print(
                "mesh-sim: --wipe-all deletes every row from messages and votes. "
                "Re-run with --yes to confirm.",
                file=sys.stderr,
            )
            return 2
        print(
            "===========================================================\n"
            "  mesh-sim: --wipe-all\n"
            "  deleting every row from `messages` and `votes`\n"
            "  (outbox is left alone)\n"
            "===========================================================",
            file=sys.stderr,
        )
        m, v = do_wipe_all(db_cfg)
        print(f"mesh-sim: wiped {m} messages and {v} votes.", file=sys.stderr)
        # Sidecar is now stale; clear it so a later --replace-injected
        # doesn't try to DELETE ids that no longer exist.
        _write_sidecar([])
        # --wipe-all + a scenario means wipe THEN inject; fall through.
        if args.scenario is None:
            return 0

    if args.replace_injected:
        m, v = do_replace_injected(db_cfg)
        print(
            f"mesh-sim: --replace-injected removed {m} messages and {v} votes "
            "previously inserted by this tool.",
            file=sys.stderr,
        )

    if args.scenario is None:
        # --wipe-all alone already handled above; --replace-injected alone
        # is also legal (cleanup without a fresh append).
        return 0

    try:
        anchor = parse_anchor(args.anchor)
    except ValueError as e:
        print(f"mesh-sim: {e}", file=sys.stderr)
        return 2

    scenario_path = Path(args.scenario)
    try:
        doc = json.loads(scenario_path.read_text())
    except FileNotFoundError:
        print(f"mesh-sim: scenario not found: {scenario_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(
            f"mesh-sim: {scenario_path}: invalid JSON: {e}",
            file=sys.stderr,
        )
        return 2

    try:
        validated = validate_scenario(doc, cfg)
    except ScenarioError as e:
        print(f"mesh-sim: {scenario_path}: {e}", file=sys.stderr)
        return 2

    new_ids = do_inject(db_cfg, anchor, validated)
    name = doc.get("name", "<unnamed>")
    print(
        f"mesh-sim: inserted {len(new_ids)} messages from scenario {name!r} "
        f"(anchor={datetime.fromtimestamp(anchor, tz=timezone.utc).isoformat()}). "
        f"Tracked ids in {_SIDECAR_NAME}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
