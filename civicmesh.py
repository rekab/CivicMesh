import argparse
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from config import load_config

if TYPE_CHECKING:
    from config import AppConfig
from database import (
    DBConfig,
    cancel_outbox_message,
    clear_pending_outbox,
    get_outbox_message,
    get_pending_outbox_filtered,
    get_recent_messages_filtered,
    get_recent_sessions,
    get_session_by_id,
    init_db,
    pin_message,
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
    cfg = load_config(_require_config(args))
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


def _cmd_stats(args: argparse.Namespace) -> None:
    import sqlite3

    cfg, log, db_cfg = _load_runtime(args)
    init_db(db_cfg, log=log)
    conn = sqlite3.connect(cfg.db_path)
    try:
        msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sess = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        out = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='queued'").fetchone()[0]
        votes = conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
    finally:
        conn.close()
    print(f"messages={msg} sessions={sess} outbox_pending={out} votes={votes}")


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

    sys.exit(run_configure(_resolve_config_path(args), _MODE))


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


def _print_cutover_banner(cfg: "AppConfig") -> None:
    print(f"""
Configuration applied. The system is staged for AP mode.

Currently running: WiFi client mode (this SSH session is fine).
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

    from apply import driver, restart, validate
    from config import load_config

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
        _sub.run(
            ["systemctl", "enable", "hostapd", "dnsmasq", "nftables",
             "rfkill-unblock-wifi"],
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
    sys.exit(run_promote(src_dir, mode=_MODE, dry_run=args.dry_run))


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

    sub.add_parser("configure")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--no-restart", action="store_true")
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--from", dest="src_dir", default=".")
    p_promote.add_argument("--dry-run", action="store_true")

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
    if args.cmd == "config":
        return {
            "show": _cmd_config_show,
            "validate": _cmd_config_validate,
        }[args.config_cmd](args)

    return {
        "pin": _cmd_pin,
        "unpin": _cmd_unpin,
        "stats": _cmd_stats,
        "cleanup": _cmd_cleanup,
        "configure": _cmd_configure,
        "apply": _cmd_apply,
        "promote": _cmd_promote,
        "install-hub-docs": _cmd_install_hub_docs,
        "rollback-hub-docs": _cmd_rollback_hub_docs,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
