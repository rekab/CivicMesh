"""Operator CLI for CivicMesh nodes (SSH-only).

Subcommands cover read-only inspection (stats, sessions list,
messages recent, outbox list), DB mutation (pin / unpin, outbox
cancel, cleanup), config validation, the apply pipeline, hub-docs
install/rollback, and the dev-to-prod `promote` flow.

The same binary runs in two modes: DEV (invoked via `uv run
civicmesh`, reads a checkout-local `config.toml`) and PROD (installed
to /usr/local/civicmesh, reads /usr/local/civicmesh/etc/config.toml).
Mode detection is by venv path; see `docs/civicmesh-tool.md` for the
bright-line rules and the full command reference.
"""

import argparse
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import power_monitor
from config import load_config

if TYPE_CHECKING:
    from config import AppConfig
from database import (
    ContactLookupError,
    DBConfig,
    _connect,
    cancel_outbox_message,
    clear_pending_outbox,
    delete_contact,
    get_node_identity,
    get_outbox_message,
    get_pending_outbox_filtered,
    get_recent_messages_filtered,
    get_recent_sessions,
    get_session_by_id,
    init_db,
    list_contacts,
    pin_message,
    resolve_contact_pubkey,
    set_contact_pinned,
    unpin_message,
)
from logger import setup_logging


PROD_TREE = Path("/usr/local/civicmesh")
PROD_VENV = Path("/usr/local/civicmesh/app/.venv")
EXIT_WRONG_MODE = 10

# Set in main() before dispatch; read by Phase-4 config handlers that need to
# default --config to a mode-determined path.
_MODE: str = ""
_PROJECT_ROOT: Path = PROD_TREE


def _find_dev_project_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for pyproject.toml with [project].name == 'civicmesh'.

    Returns the project root, or None if no match is found before the
    filesystem root. `start` defaults to this file's directory (used by
    main() for dev-mode commands); promote passes the --from path.
    """
    base = (start or Path(__file__).resolve().parent).resolve()
    p = base
    while True:
        candidate = p / "pyproject.toml"
        if candidate.is_file():
            try:
                with candidate.open("rb") as f:
                    data = tomllib.load(f)
            except Exception:
                data = {}
            if data.get("project", {}).get("name") == "civicmesh":
                return p
        if p == p.parent:
            return None
        p = p.parent


def _refuse(msg: str) -> None:
    print(f"civicmesh: {msg}", file=sys.stderr)
    sys.exit(EXIT_WRONG_MODE)


def _check_refusals(*, mode: str, project_root: Path, args: argparse.Namespace) -> None:
    config_path = getattr(args, "config", None)
    virtual_env = os.environ.get("VIRTUAL_ENV")

    if mode == "dev":
        if config_path:
            resolved = Path(config_path).resolve()
            if resolved.is_relative_to(PROD_TREE):
                _refuse(
                    "this is the dev binary; use the prod binary at "
                    "/usr/local/bin/civicmesh for that path"
                )
        if virtual_env:
            expected = (project_root / ".venv").resolve()
            if Path(virtual_env).resolve() != expected:
                _refuse(
                    "VIRTUAL_ENV points elsewhere; either unset it or 'cd' "
                    "into the dev tree"
                )
    else:  # prod
        if config_path:
            resolved = Path(config_path).resolve()
            if not resolved.is_relative_to(PROD_TREE):
                _refuse(
                    "this is the prod binary; it operates only on "
                    "/usr/local/civicmesh/etc/config.toml"
                )
        if virtual_env:
            if Path(virtual_env).resolve() != PROD_VENV.resolve():
                _refuse(
                    "VIRTUAL_ENV points elsewhere; open a clean shell or unset"
                )


def _require_config(args: argparse.Namespace) -> str:
    if not getattr(args, "config", None):
        print(f"civicmesh: {args.cmd}: --config is required", file=sys.stderr)
        sys.exit(2)
    return args.config


def _load_runtime(args: argparse.Namespace):
    """Common per-subcommand setup: config, logger, DBConfig."""
    try:
        cfg = load_config(_require_config(args))
    except (ValueError, KeyError, OSError) as e:
        # Same shape as _cmd_config_show / _cmd_config_validate, no
        # subcommand name in the prefix because _load_runtime is shared
        # across many subcommands.
        print(f"civicmesh: {e}", file=sys.stderr)
        sys.exit(1)
    log, _ = setup_logging("civicmesh", cfg.logging)
    log.info("civicmesh:cmd=%s", args.cmd)
    db_cfg = DBConfig(path=cfg.db_path)
    return cfg, log, db_cfg


RECENT_ID_WIDTH = 5
RECENT_TS_WIDTH = 19
RECENT_CHANNEL_WIDTH = 12
RECENT_SENDER_WIDTH = 13

OUTBOX_ID_WIDTH = 5
OUTBOX_TS_WIDTH = 19
OUTBOX_CHANNEL_WIDTH = 12
OUTBOX_SENDER_WIDTH = 13
OUTBOX_CONTENT_WIDTH = 50

SESSION_LAST_WIDTH = 16
SESSION_NAME_WIDTH = 12
SESSION_LOCATION_WIDTH = 12
SESSION_MAC_WIDTH = 17
SESSION_POSTS_WIDTH = 5


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[: max_len - 3]}..."


def log_safe(s: str) -> str:
    return s.encode("unicode_escape", errors="backslashreplace").decode("ascii")


def _format_recent_messages(rows: list[dict[str, object]]) -> str:
    session_width = max(len("SESSION"), max((len(str(r.get("session_id", ""))) for r in rows), default=0))
    header = (
        f"{'ID':<{RECENT_ID_WIDTH}} "
        f"{'TS':<{RECENT_TS_WIDTH}} "
        f"{'CH':<{RECENT_CHANNEL_WIDTH}} "
        "SRC  "
        "ST   "
        "RT "
        f"{'SESSION':<{session_width}} "
        f"{'SENDER':<{RECENT_SENDER_WIDTH}} "
        "CONTENT"
    )
    lines = [header]
    for row in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row["ts"])))
        channel = str(row["channel"])
        source = str(row["source"])
        status = str(row.get("status") or "-")
        retry = row.get("retry_count")
        retry_str = str(retry) if retry is not None else "-"
        session_id = str(row.get("session_id") or "")
        sender = log_safe(repr(str(row["sender"])))
        content = log_safe(repr(str(row["content"])))
        lines.append(
            f"{row['id']:<{RECENT_ID_WIDTH}} "
            f"{ts:<{RECENT_TS_WIDTH}} "
            f"{channel:<{RECENT_CHANNEL_WIDTH}} "
            f"{source:<4} "
            f"{status:<4} "
            f"{retry_str:<2} "
            f"{session_id:<{session_width}} "
            f"{sender:<{RECENT_SENDER_WIDTH}} "
            f"{content}"
        )
    return "\n".join(lines)


def _format_outbox_messages(rows: list[dict[str, object]]) -> str:
    header = (
        f"{'ID':<{OUTBOX_ID_WIDTH}} "
        f"{'TS':<{OUTBOX_TS_WIDTH}} "
        f"{'CH':<{OUTBOX_CHANNEL_WIDTH}} "
        f"{'SENDER':<{OUTBOX_SENDER_WIDTH}} "
        "CONTENT"
    )
    lines = [header]
    for row in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row["ts"])))
        channel = _truncate(str(row["channel"]), OUTBOX_CHANNEL_WIDTH)
        sender = _truncate(log_safe(str(row["sender"])), OUTBOX_SENDER_WIDTH)
        content = _truncate(log_safe(str(row["content"])), OUTBOX_CONTENT_WIDTH)
        lines.append(
            f"{row['id']:<{OUTBOX_ID_WIDTH}} "
            f"{ts:<{OUTBOX_TS_WIDTH}} "
            f"{channel:<{OUTBOX_CHANNEL_WIDTH}} "
            f"{sender:<{OUTBOX_SENDER_WIDTH}} "
            f"{content}"
        )
    return "\n".join(lines)


def _format_sessions(rows: list[dict[str, object]]) -> str:
    session_width = max(len("SESSION"), max((len(str(r.get("session_id", ""))) for r in rows), default=0))
    header = (
        f"{'SESSION':<{session_width}} "
        f"{'LAST':<{SESSION_LAST_WIDTH}} "
        f"{'NAME':<{SESSION_NAME_WIDTH}} "
        f"{'LOC':<{SESSION_LOCATION_WIDTH}} "
        f"{'MAC':<{SESSION_MAC_WIDTH}} "
        f"{'POSTS':<{SESSION_POSTS_WIDTH}}"
    )
    lines = [header]
    for row in rows:
        last_post_ts = row.get("last_post_ts")
        if last_post_ts:
            last_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(last_post_ts)))
        else:
            last_ts = "-"
        session_id = str(row["session_id"])
        name = _truncate(str(row.get("name") or ""), SESSION_NAME_WIDTH)
        location = _truncate(str(row.get("location") or ""), SESSION_LOCATION_WIDTH)
        mac = _truncate(str(row.get("mac_address") or ""), SESSION_MAC_WIDTH)
        posts = str(row.get("post_count_hour") or 0)
        lines.append(
            f"{session_id:<{session_width}} "
            f"{last_ts:<{SESSION_LAST_WIDTH}} "
            f"{name:<{SESSION_NAME_WIDTH}} "
            f"{location:<{SESSION_LOCATION_WIDTH}} "
            f"{mac:<{SESSION_MAC_WIDTH}} "
            f"{posts:<{SESSION_POSTS_WIDTH}}"
        )
    return "\n".join(lines)


def _format_session_detail(row: dict[str, object]) -> str:
    name = str(row.get("name") or "")
    location = str(row.get("location") or "")
    mac = str(row.get("mac_address") or "")
    fingerprint = str(row.get("fingerprint") or "")
    posts = str(row.get("post_count_hour") or 0)
    return "\n".join(
        [
            f"name={name}",
            f"location={location}",
            f"mac={mac}",
            f"post_count_hour={posts}",
            f"fingerprint={fingerprint}",
        ]
    )


def _confirm_outbox_cancel(
    *,
    ts: int,
    sender: str,
    content: str,
    skip_confirmation: bool,
    input_fn: Callable[[str], str],
) -> bool:
    ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    safe_sender = log_safe(sender)
    safe_content = log_safe(content)
    print(f"[{ts_str}] <{safe_sender}> {safe_content}")
    if skip_confirmation:
        return True
    resp = input_fn("Cancel this outbox message? [y/N]: ").strip().lower()
    return resp in ("y", "yes")


def _handle_outbox_cancel(
    db_cfg: DBConfig,
    *,
    outbox_id: int,
    skip_confirmation: bool,
    input_fn: Callable[[str], str],
    log=None,
) -> bool:
    row = get_outbox_message(db_cfg, outbox_id=outbox_id, log=log)
    if not row:
        print("outbox id not found")
        return False
    if row.get("sent"):
        print("outbox message already sent")
        return False
    if not _confirm_outbox_cancel(
        ts=int(row["ts"]),
        sender=str(row["sender"]),
        content=str(row["content"]),
        skip_confirmation=skip_confirmation,
        input_fn=input_fn,
    ):
        print("canceled=0")
        return False
    canceled = cancel_outbox_message(db_cfg, outbox_id=outbox_id, log=log)
    print(f"canceled={1 if canceled else 0} id={outbox_id}")
    return canceled


def _confirm_outbox_clear(*, skip_confirmation: bool, input_fn: Callable[[str], str]) -> bool:
    if skip_confirmation:
        return True
    resp = input_fn("Cancel all pending outbox messages? [y/N]: ").strip().lower()
    return resp in ("y", "yes")


def _handle_outbox_clear(
    db_cfg: DBConfig,
    *,
    skip_confirmation: bool,
    input_fn: Callable[[str], str],
    log=None,
) -> int:
    if not _confirm_outbox_clear(skip_confirmation=skip_confirmation, input_fn=input_fn):
        print("cleared=0")
        return 0
    cleared = clear_pending_outbox(db_cfg, log=log)
    print(f"cleared={cleared}")
    return cleared


def _cmd_pin(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    pin_message(db_cfg, message_id=args.message_id, pin_order=args.order, log=log)
    print(f"Pinned message {args.message_id}")


def _cmd_unpin(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    unpin_message(db_cfg, message_id=args.message_id, log=log)
    print(f"Unpinned message {args.message_id}")


def _format_clock_last_correction(
    trigger,
    applied_boot_id,
    applied_at_monotonic,
    current_boot_id: str,
    mono_now: float,
) -> str:
    """Format the last_correction age for `civicmesh stats`.

    Boot identity (this-boot vs prior-boot) is decided by Linux
    `applied_boot_id == current_boot_id`. Earlier revisions of this
    function used `applied_at_monotonic > mono_now` for the same
    decision, but that test is only sufficient: a prior-boot row
    whose monotonic happened to be small (e.g. correction applied
    60s into that boot) silently passes once the current process
    has been up for >60s. Identity comparison has no such gap.

    The system_time_* columns are NOT used because they're in
    different reference frames per trigger ('consensus' /
    'external_step' write raw int(time.time()), 'admin' writes
    pre/post the date -s jump). Cross-trigger age via those columns
    would mix frames.

    `applied_at_monotonic` is still useful for the AGE computation
    once we know the row is from this boot — monotonic only advances
    within a boot, so `mono_now - applied_at_monotonic` is a true
    elapsed-time delta.

    Returns:
      "none"                — no row
      "<trigger>@prior_boot" — applied_boot_id != current_boot_id
                               (includes NULL applied_boot_id rows)
      "<trigger>@unknown"   — defensive: row from this boot but
                               monotonic missing
      "<trigger>@<N>s_ago"  — row from this boot, monotonic present
    """
    if trigger is None:
        return "none"
    if applied_boot_id != current_boot_id:
        return f"{trigger}@prior_boot"
    if applied_at_monotonic is None:
        return f"{trigger}@unknown"
    # Defensive: monotonic shouldn't run backwards within a boot, but
    # clamp the comparison just in case.
    if applied_at_monotonic > mono_now:
        return f"{trigger}@unknown"
    return f"{trigger}@{int(mono_now - applied_at_monotonic)}s_ago"


def _cmd_identity(args: argparse.Namespace) -> None:
    """Print the meshcore://contact/add URL for this hub on stdout.

    Headless escape hatch for the captive portal's QR card (CIV-14):
    pipe to `qrencode -t ANSIUTF8` for a terminal QR, or paste the URL
    into the MeshCore app's "Add by URL" entry.
    """
    import urllib.parse as _u

    _, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    row = get_node_identity(db_cfg, log=log)
    if row is None:
        print(
            "civicmesh: identity: mesh_bot has not connected yet "
            "(node_identity row is empty); start mesh_bot.",
            file=sys.stderr,
        )
        sys.exit(1)
    contact_url = (
        "meshcore://contact/add?"
        + _u.urlencode(
            {"name": row["name"], "public_key": row["public_key"], "type": "1"}
        )
    )
    print(contact_url)


def _cmd_stats(args: argparse.Namespace) -> None:
    import sqlite3
    import time as _time

    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sess = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        out = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='queued'").fetchone()[0]
        votes = conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
        # CIV-99: surface clock-correction state so operators see drift
        # / admin events without dropping into SQL. Three values:
        #   offset_seconds — current correction applied on every write
        #   last_correction — trigger + age of the most recent row
        #   vote_epoch — generation counter (bumped by admin / external_step)
        offset_row = conn.execute(
            "SELECT value FROM clock_state WHERE key='offset_seconds'"
        ).fetchone()
        offset = int(offset_row["value"]) if offset_row else 0
        ve_row = conn.execute(
            "SELECT value FROM clock_state WHERE key='vote_epoch'"
        ).fetchone()
        vote_epoch = int(ve_row["value"]) if ve_row else 0
        last_row = conn.execute(
            "SELECT trigger, applied_boot_id, applied_at_monotonic "
            "FROM clock_corrections ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    print(f"messages={msg} sessions={sess} outbox_pending={out} votes={votes}")
    if last_row is None:
        last_str = "none"
    else:
        from clock import get_boot_id
        last_str = _format_clock_last_correction(
            last_row["trigger"],
            last_row["applied_boot_id"],
            last_row["applied_at_monotonic"],
            get_boot_id(),
            _time.monotonic(),
        )
    print(
        f"clock offset_sec={offset} vote_epoch={vote_epoch} last_correction={last_str}"
    )


def _cmd_cleanup(args: argparse.Namespace) -> None:
    from database import cleanup_retention_bytes_per_channel

    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    chans = [args.channel] if args.channel else cfg.channels.names
    total = 0
    for ch in chans:
        d = cleanup_retention_bytes_per_channel(
            db_cfg, channel=ch, max_bytes=cfg.limits.retention_bytes_per_channel, log=log
        )
        total += d
    print(f"deleted={total}")


def _cmd_messages_recent(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    rows = get_recent_messages_filtered(
        db_cfg,
        channel=args.channel,
        source=args.source,
        session_id=args.session,
        limit=args.limit,
        log=log,
    )
    print(_format_recent_messages(rows))


def _cmd_outbox_list(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    rows = get_pending_outbox_filtered(
        db_cfg, channel=args.channel, limit=args.limit, log=log
    )
    print(_format_outbox_messages(rows))


def _cmd_outbox_cancel(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    _handle_outbox_cancel(
        db_cfg,
        outbox_id=args.outbox_id,
        skip_confirmation=args.skip_confirmation,
        input_fn=input,
        log=log,
    )


def _cmd_outbox_clear(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    _handle_outbox_clear(
        db_cfg,
        skip_confirmation=args.skip_confirmation,
        input_fn=input,
        log=log,
    )


def _cmd_sessions_list(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    rows = get_recent_sessions(db_cfg, limit=args.limit, log=log)
    print(_format_sessions(rows))


def _cmd_sessions_show(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    row = get_session_by_id(db_cfg, session_id=args.session_id, log=log)
    if not row:
        print("session not found")
        return
    print(_format_session_detail(row))


CONTACT_PUBKEY_PREFIX_WIDTH = 14
CONTACT_STATUS_WIDTH = 17
CONTACT_PINNED_WIDTH = 6
CONTACT_LAST_SEEN_WIDTH = 16
CONTACT_CREATED_WIDTH = 16


def _format_contacts(rows: list[dict[str, object]]) -> str:
    header = (
        f"{'PUBKEY':<{CONTACT_PUBKEY_PREFIX_WIDTH}} "
        f"{'STATUS':<{CONTACT_STATUS_WIDTH}} "
        f"{'PINNED':<{CONTACT_PINNED_WIDTH}} "
        f"{'LAST_SEEN':<{CONTACT_LAST_SEEN_WIDTH}} "
        f"{'CREATED':<{CONTACT_CREATED_WIDTH}}"
    )
    lines = [header]
    for row in rows:
        pubkey = str(row["pubkey"])[:12] + ".."
        status = str(row["status"])
        pinned = "yes" if row["pinned"] else "no"
        last_seen = row.get("last_seen")
        last_str = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(int(last_seen)))
            if last_seen
            else "-"
        )
        created_str = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(int(row["created_at"])),
        )
        lines.append(
            f"{pubkey:<{CONTACT_PUBKEY_PREFIX_WIDTH}} "
            f"{status:<{CONTACT_STATUS_WIDTH}} "
            f"{pinned:<{CONTACT_PINNED_WIDTH}} "
            f"{last_str:<{CONTACT_LAST_SEEN_WIDTH}} "
            f"{created_str:<{CONTACT_CREATED_WIDTH}}"
        )
    return "\n".join(lines)


def _cmd_contact_list(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    pinned_filter: bool | None = None
    if args.pinned:
        pinned_filter = True
    elif args.unpinned:
        pinned_filter = False
    rows = list_contacts(
        db_cfg, status=args.status, pinned=pinned_filter, log=log,
    )
    if not rows:
        print("no contacts")
        return
    print(_format_contacts(rows))


def _resolve_for_cli(db_cfg: DBConfig, query: str, log=None) -> str:
    """Wrap resolve_contact_pubkey: map the structured error to a
    stderr print + sys.exit so each contact handler is a one-liner.
    The dispatcher pattern matches _handle_outbox_cancel."""
    try:
        return resolve_contact_pubkey(db_cfg, query=query, log=log)
    except ContactLookupError as e:
        print(f"civicmesh: {e}", file=sys.stderr)
        sys.exit(e.exit_code)


def _cmd_contact_pin(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    pubkey = _resolve_for_cli(db_cfg, args.pubkey, log=log)
    rows = set_contact_pinned(db_cfg, pubkey=pubkey, pinned=True, log=log)
    print(f"pinned={rows} pubkey={pubkey[:12]}..")


def _cmd_contact_unpin(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    pubkey = _resolve_for_cli(db_cfg, args.pubkey, log=log)
    rows = set_contact_pinned(db_cfg, pubkey=pubkey, pinned=False, log=log)
    print(f"unpinned={rows} pubkey={pubkey[:12]}..")


def _confirm_contact_remove(
    *, pubkey: str, status: str, pinned: int,
    skip_confirmation: bool, input_fn: Callable[[str], str],
) -> bool:
    print(
        f"contact {pubkey[:12]}.. status={status} "
        f"pinned={'yes' if pinned else 'no'}"
    )
    if skip_confirmation:
        return True
    resp = input_fn("Remove this contact from the DB? [y/N]: ").strip().lower()
    return resp in ("y", "yes")


def _handle_contact_remove(
    db_cfg: DBConfig,
    *,
    pubkey: str,
    skip_confirmation: bool,
    input_fn: Callable[[str], str],
    log=None,
) -> int:
    # The contact row is what tells the DM handler "this user is
    # registered." Once we delete it, get_contact_by_pubkey_prefix
    # returns None for inbound DMs from that pubkey — same as if they
    # had never registered. The firmware contact (if still present)
    # becomes tier-3 disposable for the LRU helper and gets reclaimed
    # on the next ERR_CODE_TABLE_FULL.
    from database import get_contact_by_pubkey

    row = get_contact_by_pubkey(db_cfg, pubkey=pubkey, log=log)
    if row is None:
        print(f"removed=0 pubkey={pubkey[:12]}..")
        return 0
    if not _confirm_contact_remove(
        pubkey=pubkey,
        status=str(row["status"]),
        pinned=int(row["pinned"]),
        skip_confirmation=skip_confirmation,
        input_fn=input_fn,
    ):
        print("removed=0")
        return 0
    deleted = delete_contact(db_cfg, pubkey=pubkey, log=log)
    print(f"removed={deleted} pubkey={pubkey[:12]}..")
    return deleted


def _cmd_contact_remove(args: argparse.Namespace) -> None:
    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    pubkey = _resolve_for_cli(db_cfg, args.pubkey, log=log)
    _handle_contact_remove(
        db_cfg,
        pubkey=pubkey,
        skip_confirmation=args.skip_confirmation,
        input_fn=input,
        log=log,
    )


def _cmd_sessions_reset(args: argparse.Namespace) -> None:
    import sqlite3

    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    row = get_session_by_id(db_cfg, session_id=args.session_id, log=log)
    if not row:
        print("session not found")
        return
    conn = sqlite3.connect(cfg.db_path, timeout=5, isolation_level=None)
    try:
        conn.execute(
            "UPDATE sessions SET post_count_hour=0 WHERE session_id=?",
            (args.session_id,),
        )
    finally:
        conn.close()
    print(f"Reset post_count_hour for session {args.session_id}")


def _default_config_path() -> Path:
    if _MODE == "prod":
        return Path("/usr/local/civicmesh/etc/config.toml")
    return _PROJECT_ROOT / "config.toml"


def _default_db_path() -> Path:
    """Single source of truth for the SQLite DB location, mode-aware.

    Used by `civicmesh configure` to bake an absolute path into emitted
    config.toml files. config.py rejects non-absolute db_path values, so
    callers must resolve any returned path before writing it out.
    """
    if _MODE == "prod":
        return Path("/usr/local/civicmesh/var/civic_mesh.db")
    return _PROJECT_ROOT / "civic_mesh.db"


def _resolve_config_path(args: argparse.Namespace) -> Path:
    return Path(args.config).resolve() if args.config else _default_config_path()


def _strict_validation_errors(cfg: "AppConfig") -> list[str]:
    errors: list[str] = []
    if cfg.ap.channel not in (1, 6, 11):
        errors.append(
            f"ap.channel {cfg.ap.channel}: must be 1, 6, or 11 "
            "(the non-overlapping 2.4 GHz channels)"
        )
    return errors


def _cmd_configure(args: argparse.Namespace) -> None:
    if _MODE == "prod" and os.geteuid() == 0:
        _refuse(
            "configure: must run as the civicmesh user in prod\n"
            "   try: sudo -u civicmesh civicmesh configure"
        )
    from configure import run_configure

    sys.exit(run_configure(
        _resolve_config_path(args),
        _MODE,
        explicit_config=getattr(args, "config", None) is not None,
    ))


def _cmd_config_show(args: argparse.Namespace) -> None:
    import json

    import tomli_w

    from config import load_config, to_serializable_dict

    try:
        cfg = load_config(str(_resolve_config_path(args)))
    except (ValueError, KeyError, OSError) as e:
        print(f"civicmesh: config show: {e}", file=sys.stderr)
        sys.exit(1)
    data = to_serializable_dict(cfg)
    if args.format == "json":
        print(json.dumps(data, indent=2, default=str))
    else:
        print(tomli_w.dumps(data), end="")


def _cmd_config_validate(args: argparse.Namespace) -> None:
    from config import load_config

    try:
        cfg = load_config(str(_resolve_config_path(args)))
    except (ValueError, KeyError, OSError) as e:
        print(f"civicmesh: config validate: {e}", file=sys.stderr)
        sys.exit(1)
    errors = _strict_validation_errors(cfg)
    if errors:
        for msg in errors:
            print(f"civicmesh: config validate: {msg}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def _format_power_reading(r: "power_monitor.Reading") -> str:
    def _v(mv: int | None) -> str:
        return "—" if mv is None else f"{mv / 1000:.2f}"

    soc = "—" if r.soc is None else f"{r.soc:.1f}%"
    watt = "—" if r.power_w is None else f"{r.power_w:.1f}"
    return f"OK  SoC={soc} V={_v(r.voltage_mv)} A={_v(r.current_ma)} W={watt}"


def _cmd_power_test(args: argparse.Namespace) -> None:
    """Decode one BLE advert from the configured BMV and print it.

    Read-only diagnostic: builds the same BLESource the sampler uses, listens
    up to --duration seconds, prints SoC/V/A/W on the first decode (exit 0), or
    a no-data message (exit 1). Works regardless of [power_monitor].enabled —
    it's the on-node pre-enable bench check, reading the node's own config.
    Mirrors scripts/ble_smoke.py, but ships to prod (ble_smoke lives in
    scripts/, not in py-modules) and reads mac/key from config rather than argv.
    """
    import asyncio

    from config import load_config

    try:
        cfg = load_config(str(_resolve_config_path(args)))
    except (ValueError, KeyError, OSError) as e:
        print(f"civicmesh: power-test: {e}", file=sys.stderr)
        sys.exit(2)
    pm = cfg.power_monitor
    if not pm.mac or not pm.encryption_key:
        print(
            "civicmesh: power-test: [power_monitor] mac/encryption_key are empty "
            "in config; populate them (see docs/victron-ble-setup.md)",
            file=sys.stderr,
        )
        sys.exit(2)
    # A read-only diagnostic, run interactively (often `sudo -u civicmesh`)
    # from an arbitrary cwd — so it must NOT go through setup_logging, which
    # creates the runtime log dir. cfg.log_dir is relative (e.g. "var/logs")
    # and only resolves under the systemd unit's WorkingDirectory; from ~ it
    # would try to mkdir "var/" and EACCES. Log decode/scanner errors to
    # stderr instead, leaving stdout clean for the one result line. Mirrors
    # scripts/ble_smoke.py, which likewise uses a console logger.
    import logging

    log = logging.getLogger("civicmesh-power-test")
    if not log.handlers:
        _handler = logging.StreamHandler(sys.stderr)
        _handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(_handler)
    log.setLevel(logging.INFO)
    if not pm.enabled:
        print("note: [power_monitor].enabled=false (sampler is off); probing anyway.")
    print(f"listening for {pm.mac} for up to {args.duration:.0f}s (passive scan)…")
    try:
        decoded, reading = asyncio.run(
            power_monitor.probe(pm, log, duration_sec=args.duration)
        )
    except ImportError:
        print(
            "civicmesh: power-test: victron-ble not importable (base dependency — "
            "install is broken); run `uv sync` (on prod, re-run `civicmesh promote`)",
            file=sys.stderr,
        )
        sys.exit(2)
    if decoded and reading is not None:
        print(_format_power_reading(reading))
        sys.exit(0)
    print(
        f"civicmesh: power-test: no advert decoded from {pm.mac} within "
        f"{args.duration:.0f}s; check mac/key/adapter (rfkill, bluetooth.service). "
        "See docs/victron-ble-setup.md § Troubleshooting.",
        file=sys.stderr,
    )
    sys.exit(1)


def _migrate_logs_civ_104() -> None:
    """CIV-104: rewrite legacy [logging].log_dir = "logs" to "var/logs".

    Prior installs ran with the systemd unit's WorkingDirectory at
    /usr/local/civicmesh/app and a relative `log_dir = "logs"`, which
    landed runtime logs at app/logs/ next to source code. The unit's
    WorkingDirectory is now the tree root, so the same relative value
    would resolve to /usr/local/civicmesh/logs/ — a worse orphan.
    Rewrite the key here so existing nodes pick up var/logs/ on the
    next start.

    Files already in app/logs/ are intentionally orphaned rather than
    moved: rotation-capped (~150 MB max across services) and readable
    in place if anyone wants the pre-cutover lines. The banner below
    tells operators where to look.

    Idempotent: only rewrites the exact legacy literal "logs"; no-op
    on second run.
    """
    import re

    cfg_path = Path("/usr/local/civicmesh/etc/config.toml")
    if not cfg_path.is_file():
        return

    text = cfg_path.read_text()
    new_text = re.sub(
        r'^(\s*log_dir\s*=\s*)"logs"\s*$',
        r'\1"var/logs"',
        text,
        flags=re.MULTILINE,
    )
    if new_text == text:
        return

    cfg_path.write_text(new_text)
    print(
        'civicmesh: apply: migrated [logging].log_dir = "logs" -> "var/logs" '
        f"in {cfg_path} (CIV-104)."
    )
    print(
        "civicmesh: apply: previous logs at /usr/local/civicmesh/app/logs/ "
        "are orphaned (safe to read in place, safe to rm). New logs at "
        "/usr/local/civicmesh/var/logs/."
    )


def _print_cutover_banner(cfg: "AppConfig") -> None:
    print(f"""
Configuration applied. The system is staged for AP mode.

On next boot:      AP mode — SSID "{cfg.ap.ssid}", portal at http://{cfg.network.ip}

To cut over, run:

    sudo reboot

If you are connected over WiFi, this SSH session will end on reboot
and will not reconnect — {cfg.network.iface} will be in AP mode, not
client mode. Reconnect by joining "{cfg.ap.ssid}" from your phone or
laptop.
""")


def _cmd_apply(args: argparse.Namespace) -> None:
    import subprocess as _sub

    from config import load_config
    # `apply` package imports moved below the early gates (mode,
    # root, CIV-99 timesyncd mask check) so failures in those gates
    # don't carry the cost of resolving the renderer/plan dependency
    # tree, and so the timesync-mask unit tests can drive _cmd_apply
    # without resolving `apply.*` at all.

    if _MODE == "dev" and not args.dry_run:
        print(
            "civicmesh: apply runs only in prod; "
            "use `apply --dry-run` to preview, or `promote` to deploy",
            file=sys.stderr,
        )
        sys.exit(10)

    if not args.dry_run and os.geteuid() != 0:
        print("civicmesh: apply requires root; re-run with sudo", file=sys.stderr)
        sys.exit(1)

    # Load config first — both the strict-validation block below AND
    # the CIV-99 timesyncd-mask check need cfg in hand. The mask check
    # is gated on cfg.clock.require_timesync_masked so dev / RTC-backed
    # machines can opt out (see docs/clock_consensus.md § "Dev / RTC
    # machines").
    try:
        cfg = load_config(str(_resolve_config_path(args)))
    except (ValueError, KeyError, OSError) as e:
        print(f"civicmesh: apply: {e}", file=sys.stderr)
        sys.exit(2)

    errors = _strict_validation_errors(cfg)
    if errors:
        for msg in errors:
            print(f"civicmesh: apply: {msg}", file=sys.stderr)
        sys.exit(3)

    # CIV-99: apply refuses to proceed unless systemd-timesyncd (and
    # chrony, if installed) are PERSISTENTLY MASKED — not
    # masked-runtime, not merely disabled. The clock-correction design
    # depends on "no other process touches the system clock"
    # (docs/clock_consensus.md). The accepted states are exactly:
    #
    #   "masked"            — persistent mask in /etc; survives reboot
    #   unit not installed   — no file, nothing to start
    #
    # Every other state is rejected, including:
    #
    #   "masked-runtime"    — mask lives under /run, vanishes on reboot;
    #                         `apply` stages the next boot, so accepting
    #                         this would let the machine come back with
    #                         NTP startable. The runtime mask is for
    #                         operator triage, not deployment.
    #   "disabled"          — does not autostart, but can still be
    #                         started manually or pulled in via
    #                         another unit's Requires=.
    #   "enabled" / "enabled-runtime" / "static" / "alias" / "indirect"
    #   / "generated" / "linked" / "linked-runtime"
    #                       — all permit start.
    #
    # Two opt-outs:
    #   - dry-run: operator may want to preview the plan before fixing
    #     the masking state.
    #   - cfg.clock.require_timesync_masked = false: dev / RTC /
    #     internet-connected machines that intentionally trust NTP.
    #     This ONLY skips the apply pre-flight check; the runtime
    #     offset-on-write model and the external-step detector are
    #     unchanged.
    if not args.dry_run and cfg.clock.require_timesync_masked:
        # Only "masked" is acceptable. masked-runtime is NOT — see the
        # block comment above.
        _MASKED_OK = ("masked",)
        for unit in ("systemd-timesyncd.service", "chrony.service"):
            try:
                result = _sub.run(
                    ["systemctl", "is-enabled", unit],
                    capture_output=True, text=True, check=False,
                )
            except FileNotFoundError:
                # systemctl missing — non-systemd host (CI / tests). Skip.
                break
            state = (result.stdout or "").strip().lower()
            if state in _MASKED_OK:
                continue
            stderr = (result.stderr or "").strip().lower()
            # "no such file or directory" / "no such unit" => unit isn't
            # installed on this host. Exit codes vary across systemd
            # versions (1 on older, 4 on newer); match on stderr text
            # for portability.
            if "no such" in stderr or "not-found" in state or "not found" in stderr:
                continue
            # Tailor the message to the actual failing state. The
            # masked-runtime case in particular is a tempting trap
            # (`systemctl mask --runtime` looks identical at a glance);
            # call it out by name so the operator sees the reboot risk.
            if state == "masked-runtime":
                why = (
                    "Runtime masks live under /run and disappear on the "
                    "next reboot. `apply` stages deployment for the next "
                    "boot, so accepting masked-runtime would let the node "
                    "come up with NTP startable."
                )
            elif state == "disabled":
                why = (
                    "Disabled units do not autostart, but can still be "
                    "started manually or pulled in by another unit's "
                    "Requires=."
                )
            else:
                why = (
                    "This state permits the unit to be started by "
                    "systemd or by an operator."
                )
            print(
                f"civicmesh: apply: {unit} is '{state or '<no state>'}' "
                f"(exit {result.returncode}). CIV-99 requires this unit "
                "to be PERSISTENTLY MASKED (not runtime-masked, not merely "
                f"disabled). {why} Run:\n"
                f"  sudo systemctl mask {unit}\n"
                "(no --runtime flag) and re-run `civicmesh apply`. "
                "If this is a dev / internet-connected / RTC-backed "
                "machine, set `[clock] require_timesync_masked = false` "
                "in your config to opt out. "
                "See docs/clock_consensus.md.",
                file=sys.stderr,
            )
            sys.exit(7)

    # Renderer/plan imports — moved here so the early gates above (mode,
    # root, CIV-99 timesyncd mask) don't pay the cost or risk of
    # resolving them.
    from apply import driver, restart, validate
    plan_obj = driver.plan(cfg)

    if args.dry_run:
        driver.print_plan(plan_obj, dry_run=True)
        sys.exit(0)

    val_errors = validate.validate_plan(plan_obj, cfg)
    if val_errors:
        for msg in val_errors:
            print(f"civicmesh: apply: {msg}", file=sys.stderr)
        sys.exit(6)

    try:
        driver.apply_plan(plan_obj)
    except OSError as e:
        print(f"civicmesh: apply: write failed: {e}", file=sys.stderr)
        sys.exit(4)

    print(f"wrote {len(plan_obj.changes)} file(s):")
    for change in plan_obj.changes:
        print(f"  {change.abs_path}")

    if args.no_restart:
        sys.exit(0)

    try:
        _sub.run(["systemctl", "daemon-reload"], check=True)
        # hostapd and dnsmasq are masked by their Debian package postinst
        # (since Buster, 2019): "don't auto-start until the operator has
        # rendered a config." apply is the operator-driven step that
        # renders that config — the immediately-preceding
        # driver.apply_plan() just wrote /etc/hostapd/hostapd.conf and
        # the dnsmasq drop-in — so satisfying the unmask half of the
        # package contract belongs here, not in bootstrap. systemctl
        # unmask is idempotent on already-unmasked units, so this is
        # safe on every apply re-run.
        _sub.run(
            ["systemctl", "unmask", "hostapd.service", "dnsmasq.service"],
            check=True,
        )
        # systemd-networkd is the unit that consumes the
        # /etc/systemd/network/20-<iface>-ap.network file the renderer
        # writes. Raspberry Pi OS ships with NetworkManager as the
        # default and networkd not enabled — without this, wlan0 never
        # gets the static AP IP at boot, dnsmasq fails with "unknown
        # interface wlan0", and no client gets DHCP.
        _sub.run(
            ["systemctl", "enable", "hostapd", "dnsmasq", "nftables",
             "rfkill-unblock-wifi", "systemd-networkd"],
            check=True,
        )
        _sub.run(["systemctl", "disable", "wpa_supplicant.service"], check=True)
        # Enable the app-tier units unconditionally (idempotent). bootstrap
        # lays the unit files down via promote, but only `apply` knows the
        # operator has reached the configure-then-apply step that means
        # "yes, run these on boot." Without this, `systemctl is-enabled
        # civicmesh-web civicmesh-mesh` returns `disabled` after a fresh
        # bootstrap + configure + apply — they only run today because the
        # restart below side-effect-starts them in the same boot.
        _sub.run(
            ["systemctl", "enable", "civicmesh-web", "civicmesh-mesh"],
            check=True,
        )
        # CIV-80: create the civicmesh-mesh startup lock file at
        # /run/lock/civicmesh-mesh.lock now (don't wait for reboot).
        # Idempotent — re-runs on subsequent applies just no-op.
        _sub.run(
            ["systemd-tmpfiles", "--create", "/etc/tmpfiles.d/civicmesh.conf"],
            check=True,
        )
        # CIV-104: rewrite legacy log_dir before the auto-restart fires.
        # Must run after driver.apply_plan (which wrote the new systemd
        # unit) and before restart.derive_actions, so the restarting
        # services see the rewritten config on their first boot under
        # the new WorkingDirectory.
        _migrate_logs_civ_104()
        actions = restart.derive_actions(c.abs_path for c in plan_obj.changes)
        if actions:
            restart.run_actions(actions)
    except _sub.CalledProcessError as e:
        print(f"civicmesh: apply: service staging failed: {e}", file=sys.stderr)
        sys.exit(5)

    _print_cutover_banner(cfg)
    sys.exit(0)


def _hub_docs_var_dir() -> Path:
    """Hub-docs <var> root, derived from mode (no config field)."""
    if _MODE == "prod":
        return PROD_TREE / "var"
    return _PROJECT_ROOT / "var"


def _load_retention(args: argparse.Namespace) -> int:
    """Resolve hub_docs_retention_count from config.

    Matrix:
      --config absent, default config absent  -> default 3 (fresh-node grace)
      --config absent, default config exists  -> use field (or raise on broken)
      --config present, file loads cleanly    -> use field
      --config present, file broken / missing -> raise HubDocsError(exit 1)
    """
    from hub_docs import HubDocsError, _HUB_DOCS_RETENTION_DEFAULT

    explicit = getattr(args, "config", None) is not None
    path = _resolve_config_path(args)
    if not path.exists():
        if explicit:
            raise HubDocsError(
                f"--config path does not exist: {path}", exit_code=1
            )
        return _HUB_DOCS_RETENTION_DEFAULT
    try:
        from config import load_config
        cfg = load_config(str(path))
    except (ValueError, KeyError, OSError, tomllib.TOMLDecodeError) as e:
        raise HubDocsError(
            f"cannot load config at {path}: {e}", exit_code=1
        )
    return cfg.limits.hub_docs_retention_count


def _cmd_install_hub_docs(args: argparse.Namespace) -> None:
    if _MODE == "prod" and os.geteuid() == 0:
        _refuse(
            "install-hub-docs: must run as the civicmesh user in prod\n"
            "   try: sudo -u civicmesh civicmesh install-hub-docs ..."
        )
    from hub_docs import HubDocsError, install_hub_docs

    var_dir = _hub_docs_var_dir()
    try:
        retention = _load_retention(args)
        result = install_hub_docs(
            Path(args.zip_path),
            var_dir=var_dir,
            retention=retention,
            dry_run=args.dry_run,
        )
    except HubDocsError as e:
        print(f"civicmesh install-hub-docs: {e}", file=sys.stderr)
        sys.exit(e.exit_code)

    if args.dry_run:
        print(
            f"dry_run release_id={result['release_id']} "
            f"docs={result['docs']}"
        )
    else:
        prev = result["previous"] if result["previous"] else "none"
        print(
            f"installed release_id={result['release_id']} "
            f"previous={prev} pruned={len(result['pruned'])}"
        )


def _cmd_rollback_hub_docs(args: argparse.Namespace) -> None:
    if _MODE == "prod" and os.geteuid() == 0:
        _refuse(
            "rollback-hub-docs: must run as the civicmesh user in prod\n"
            "   try: sudo -u civicmesh civicmesh rollback-hub-docs ..."
        )
    from hub_docs import HubDocsError, rollback_hub_docs

    var_dir = _hub_docs_var_dir()
    try:
        result = rollback_hub_docs(var_dir=var_dir, to_id=args.to_id)
    except HubDocsError as e:
        print(f"civicmesh rollback-hub-docs: {e}", file=sys.stderr)
        sys.exit(e.exit_code)

    line = (
        f"rolled_back release_id={result['release_id']} "
        f"previous={result['previous']}"
    )
    if result.get("noop"):
        line += " noop=true"
    print(line)


def _cmd_promote(args: argparse.Namespace) -> None:
    from promote import run_promote

    src_dir = Path(args.src_dir).resolve()
    sys.exit(run_promote(
        src_dir, mode=_MODE, dry_run=args.dry_run, restart=args.restart,
    ))


def _cmd_set_clock(args: argparse.Namespace) -> None:
    """CIV-99 admin command: promote the corrected display time into the OS.

    Reads `clock_state.offset_seconds`, sets the system clock to
    `int(time.time()) + offset`, saves to `fake-hwclock`, then commits
    `offset_seconds=0`, bumps `vote_epoch`, clears per-session clock
    report fields, and appends an `'admin'` audit row. All DB writes
    run under one `BEGIN EXCLUSIVE` so concurrent inserts from
    web_server / mesh_bot serialize correctly via busy_timeout
    (see database._connect).

    `fake-hwclock save` failure does NOT roll back the DB. The system
    clock is already correct; rolling back the DB would leave the
    running node double-corrected (wall_now = jumped_clock + old_offset)
    until reboot — far worse than the only remaining risk after a
    fake-hwclock failure (correction lost on reboot, observable via
    `source_summary.fake_hwclock_save_failed=true` and a CRITICAL log).
    Exits non-zero so the operator notices and re-runs after fixing
    fake-hwclock.

    Refuses to run unless invoked as root. SSH-only by design — there
    is no sudoers rule and no setuid helper.
    """
    import subprocess as _sub
    import json as _json
    from logger import setup_logging as _setup_logging
    if os.geteuid() != 0:
        print(
            "civicmesh: set-clock requires root (run via SSH as root or with sudo)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        cfg = load_config(str(_resolve_config_path(args)))
    except (ValueError, KeyError, OSError) as e:
        print(f"civicmesh: set-clock: {e}", file=sys.stderr)
        sys.exit(2)

    db_cfg = DBConfig(path=cfg.db_path)
    log, _sec = _setup_logging("civicmesh-set-clock", cfg.logging)

    conn = _connect(db_cfg)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        try:
            row = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()
            current_offset = int(row["value"]) if row else 0
            original_system_time = int(time.time())
            target = original_system_time + current_offset
            mono = time.monotonic()
        except:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        # Step 4: date -s @<target>. Atomic w.r.t. fake-hwclock failure —
        # if this fails the system clock is unchanged and we ROLLBACK
        # leaving the DB in its prior state.
        log.info(
            "set-clock:date_attempt original=%d offset=%d target=%d",
            original_system_time, current_offset, target,
        )
        try:
            _sub.run(["date", "-s", f"@{target}"], check=True, capture_output=True)
        except _sub.CalledProcessError as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            stderr = (e.stderr or b"").decode("utf-8", "replace").strip()
            log.error("set-clock:date_failed err=%s stderr=%s", e, stderr)
            print(
                f"civicmesh: set-clock: `date -s` failed: {stderr or e}; "
                "system clock and database left unchanged.",
                file=sys.stderr,
            )
            conn.close()
            sys.exit(3)

        # Step 5: fake-hwclock save. Failure here does NOT roll back the
        # DB. See docstring for the rationale.
        fake_hwclock_save_failed = False
        fake_hwclock_stderr = ""
        try:
            _sub.run(["fake-hwclock", "save"], check=True, capture_output=True)
        except _sub.CalledProcessError as e:
            fake_hwclock_save_failed = True
            fake_hwclock_stderr = (e.stderr or b"").decode("utf-8", "replace").strip()
            log.error(
                "set-clock:fake_hwclock_save_failed err=%s stderr=%s",
                e, fake_hwclock_stderr,
            )
        except FileNotFoundError as e:
            fake_hwclock_save_failed = True
            fake_hwclock_stderr = f"fake-hwclock not on PATH: {e}"
            log.error("set-clock:fake_hwclock_not_found err=%s", e)

        # Steps 6-9: persist offset=0, bump vote_epoch, clear session
        # clock fields, append audit row. Whether fake-hwclock succeeded
        # or not — see docstring.
        try:
            conn.execute(
                "UPDATE clock_state SET value='0' WHERE key='offset_seconds'"
            )
            conn.execute(
                "UPDATE clock_state SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) "
                "WHERE key='vote_epoch'"
            )
            conn.execute(
                """
                UPDATE sessions SET
                  clock_offset_vote_sec=NULL,
                  clock_reported_system_ts=NULL,
                  clock_report_mono=NULL,
                  clock_report_boot_id=NULL,
                  clock_vote_epoch=NULL
                """
            )
            source_summary = _json.dumps({
                "target_epoch": target,
                "original_system_time": original_system_time,
                "offset_before_sec": current_offset,
                "fake_hwclock_save_failed": fake_hwclock_save_failed,
                "fake_hwclock_stderr": fake_hwclock_stderr,
            }, sort_keys=True, separators=(",", ":"))
            # Stamp the current Linux boot ID on the audit row so it can
            # be boot-scoped later (mainly for `civicmesh stats`'
            # prior-boot detection; first_correction_done only consults
            # 'consensus' rows). The admin command runs on Linux because
            # set-clock requires `date -s` + `fake-hwclock save`.
            from clock import get_boot_id
            conn.execute(
                """
                INSERT INTO clock_corrections
                  (applied_at_monotonic, applied_boot_id,
                   system_time_before, system_time_after,
                   offset_before_sec, offset_after_sec,
                   trigger, voter_count, median_offset_vote_sec, source_summary)
                VALUES (?, ?, ?, ?, ?, 0, 'admin', NULL, NULL, ?)
                """,
                (mono, get_boot_id(),
                 original_system_time, target,
                 current_offset, source_summary),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if fake_hwclock_save_failed:
        # Live node is now correctly timed (offset=0 against the freshly-
        # jumped system clock), but the correction will be LOST on next
        # reboot because fake-hwclock retains the pre-jump file. Operator
        # must investigate and re-run `civicmesh set-clock` after fixing.
        log.critical(
            "set-clock:fake_hwclock_save_failed wall_runtime=correct "
            "reboot_risk=correction_will_be_lost stderr=%r",
            fake_hwclock_stderr,
        )
        print(
            "civicmesh: set-clock: system clock corrected and DB committed, "
            f"BUT `fake-hwclock save` failed ({fake_hwclock_stderr or 'unknown error'}). "
            "Runtime timestamps are correct, but the correction will be lost on next "
            "reboot. Investigate fake-hwclock (permissions / disk space / package "
            "install) and re-run `civicmesh set-clock`.",
            file=sys.stderr,
        )
        sys.exit(4)

    log.info("set-clock:ok offset_now=0 target=%d", target)
    print(f"set-clock: ok (system clock set to epoch {target}; offset_seconds=0)")
    sys.exit(0)


def _stub(name: str, phase: str) -> Callable[[argparse.Namespace], None]:
    def handler(args: argparse.Namespace) -> None:
        print(f"civicmesh: {name}: not implemented; arrives in Phase {phase} (CIV-56)", file=sys.stderr)
        sys.exit(1)
    return handler


def main():
    binary = Path(sys.argv[0]).resolve()
    mode = "prod" if str(binary).startswith(str(PROD_TREE) + "/") else "dev"
    project_root = PROD_TREE if mode == "prod" else _find_dev_project_root()
    if mode == "dev" and project_root is None:
        print(
            "civicmesh: could not locate dev project root "
            "(no pyproject.toml with [project].name = 'civicmesh' "
            f"found from {Path.cwd()} upward)",
            file=sys.stderr,
        )
        sys.exit(1)
    global _MODE, _PROJECT_ROOT
    _MODE = mode
    _PROJECT_ROOT = project_root

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=False, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pin = sub.add_parser("pin")
    p_pin.add_argument("message_id", type=int)
    p_pin.add_argument("--order", type=int, default=None)

    p_unpin = sub.add_parser("unpin")
    p_unpin.add_argument("message_id", type=int)

    sub.add_parser("stats")
    # CIV-14 onboarding: print the meshcore://contact/add URL so an
    # operator can scan it (or pipe to `qrencode`) and add the hub as
    # a companion contact in the MeshCore phone app.
    sub.add_parser("identity")

    p_cleanup = sub.add_parser("cleanup")
    p_cleanup.add_argument("--channel", default=None)

    p_messages = sub.add_parser("messages")
    sub_messages = p_messages.add_subparsers(dest="messages_cmd", required=True)

    p_recent = sub_messages.add_parser("recent")
    p_recent.add_argument("--channel", default=None)
    p_recent.add_argument("--source", choices=["mesh", "wifi"], default=None)
    p_recent.add_argument("--session", default=None)
    p_recent.add_argument("--limit", type=int, default=20)

    p_outbox = sub.add_parser("outbox")
    sub_outbox = p_outbox.add_subparsers(dest="outbox_cmd", required=True)

    p_outbox_list = sub_outbox.add_parser("list")
    p_outbox_list.add_argument("--channel", default=None)
    p_outbox_list.add_argument("--limit", type=int, default=20)

    p_outbox_cancel = sub_outbox.add_parser("cancel")
    p_outbox_cancel.add_argument("outbox_id", type=int)
    p_outbox_cancel.add_argument("--skip_confirmation", action="store_true")

    p_outbox_clear = sub_outbox.add_parser("clear")
    p_outbox_clear.add_argument("--skip_confirmation", action="store_true")

    p_sessions = sub.add_parser("sessions")
    sub_sessions = p_sessions.add_subparsers(dest="sessions_cmd", required=True)

    p_sessions_list = sub_sessions.add_parser("list")
    p_sessions_list.add_argument("--limit", type=int, default=20)

    p_sessions_show = sub_sessions.add_parser("show")
    p_sessions_show.add_argument("session_id")

    p_sessions_reset = sub_sessions.add_parser("reset")
    p_sessions_reset.add_argument("session_id")

    # CIV-14 admin contact management. pubkey arguments accept either
    # a full 64-hex pubkey or a 12-63 char prefix (12 is what
    # CONTACT_MSG_RECV logs carry, so operators reading mesh_bot logs
    # can paste those directly).
    p_contact = sub.add_parser("contact")
    sub_contact = p_contact.add_subparsers(dest="contact_cmd", required=True)

    p_contact_list = sub_contact.add_parser("list")
    p_contact_list.add_argument(
        "--status",
        choices=["pending", "added", "evicted", "error_table_full", "error_other"],
        default=None,
    )
    pinned_group = p_contact_list.add_mutually_exclusive_group()
    pinned_group.add_argument("--pinned", action="store_true")
    pinned_group.add_argument("--unpinned", action="store_true")

    p_contact_pin = sub_contact.add_parser("pin")
    p_contact_pin.add_argument("pubkey")

    p_contact_unpin = sub_contact.add_parser("unpin")
    p_contact_unpin.add_argument("pubkey")

    p_contact_remove = sub_contact.add_parser("remove")
    p_contact_remove.add_argument("pubkey")
    p_contact_remove.add_argument("--skip_confirmation", action="store_true")

    sub.add_parser("configure")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--no-restart", action="store_true")
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--from", dest="src_dir", default=".")
    p_promote.add_argument("--dry-run", action="store_true")
    # --restart is opt-in: promote ships code, restart is an operational
    # decision the operator owns (a schema-breaking change should not
    # trigger an automatic restart that crash-loops the units).
    p_promote.add_argument("--restart", action="store_true")

    p_install_hd = sub.add_parser("install-hub-docs")
    p_install_hd.add_argument("zip_path")
    p_install_hd.add_argument("--dry-run", action="store_true")

    p_rollback_hd = sub.add_parser("rollback-hub-docs")
    p_rollback_hd.add_argument("--to", dest="to_id", default=None)

    p_config = sub.add_parser("config")
    sub_config = p_config.add_subparsers(dest="config_cmd", required=True)
    p_config_show = sub_config.add_parser("show")
    p_config_show.add_argument("--format", choices=["toml", "json"], default="toml")
    sub_config.add_parser("validate")

    # CIV-99: promote corrected display time into the OS clock. Runs as
    # root over SSH; refuses unless geteuid()==0. See _cmd_set_clock.
    sub.add_parser("set-clock")

    # Read-only BLE bench check: decode one advert from the configured BMV and
    # print SoC/V/A/W. On-node equivalent of scripts/ble_smoke.py.
    p_power_test = sub.add_parser("power-test")
    p_power_test.add_argument("--duration", type=float, default=30.0)

    args = ap.parse_args()

    _check_refusals(mode=mode, project_root=project_root, args=args)

    if args.cmd == "messages" and args.messages_cmd == "recent":
        return _cmd_messages_recent(args)
    if args.cmd == "outbox":
        return {
            "list": _cmd_outbox_list,
            "cancel": _cmd_outbox_cancel,
            "clear": _cmd_outbox_clear,
        }[args.outbox_cmd](args)
    if args.cmd == "sessions":
        return {
            "list": _cmd_sessions_list,
            "show": _cmd_sessions_show,
            "reset": _cmd_sessions_reset,
        }[args.sessions_cmd](args)
    if args.cmd == "contact":
        return {
            "list": _cmd_contact_list,
            "pin": _cmd_contact_pin,
            "unpin": _cmd_contact_unpin,
            "remove": _cmd_contact_remove,
        }[args.contact_cmd](args)
    if args.cmd == "config":
        return {
            "show": _cmd_config_show,
            "validate": _cmd_config_validate,
        }[args.config_cmd](args)

    return {
        "pin": _cmd_pin,
        "unpin": _cmd_unpin,
        "stats": _cmd_stats,
        "identity": _cmd_identity,
        "cleanup": _cmd_cleanup,
        "configure": _cmd_configure,
        "apply": _cmd_apply,
        "promote": _cmd_promote,
        "install-hub-docs": _cmd_install_hub_docs,
        "rollback-hub-docs": _cmd_rollback_hub_docs,
        "set-clock": _cmd_set_clock,
        "power-test": _cmd_power_test,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
