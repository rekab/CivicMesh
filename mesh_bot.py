import argparse
import asyncio
import hashlib
import time
from typing import Optional

from config import load_config
from database import (
    DBConfig,
    cleanup_retention_bytes_per_channel,
    get_pending_outbox,
    init_db,
    insert_message,
    mark_outbox_sent,
    upsert_status,
)
from logger import setup_logging

EventType = None
MeshCore = None

DEFAULT_BAUDRATE = 115200


def _now_ts() -> int:
    return int(time.time())


async def _retention_task(cfg, db_cfg: DBConfig, log):
    while True:
        try:
            channels = list(dict.fromkeys(cfg.channels.names + cfg.local.names))
            for ch in channels:
                deleted = cleanup_retention_bytes_per_channel(
                    db_cfg, channel=ch, max_bytes=cfg.limits.retention_bytes_per_channel, log=log
                )
                if deleted:
                    log.info("retention:channel=%s deleted=%d", ch, deleted)
        except Exception as e:
            log.error("retention:error %s", e, exc_info=True)
        await asyncio.sleep(3600)


async def _heartbeat_task(cfg, db_cfg: DBConfig, log):
    while True:
        try:
            upsert_status(db_cfg, process="mesh_bot", radio_connected=True, log=log)
        except Exception as e:
            log.error("heartbeat:upsert_failed err=%s", e, exc_info=True)
        await asyncio.sleep(10)


async def _outbox_task(cfg, db_cfg: DBConfig, log, mesh_client, channel_name_to_idx: dict[str, int]):
    # Backoff levels map to a delay sequence [0, 2, 5, max_delay_sec].
    # The last entry is configurable so operators can cap the max pacing.
    # We advance one level after each send attempt to avoid draining large
    # backlogs too quickly on the mesh.
    max_delay_sec = cfg.limits.outbox_max_delay_sec
    delays = [0, 2, 5, max_delay_sec]
    idle_reset_sec = cfg.limits.outbox_idle_reset_sec
    last_send_time: Optional[float] = None
    backoff_level = 0

    while True:
        try:
            now = time.time()
            # If we've been idle long enough, reset the backoff so the next
            # message can go out immediately.
            if last_send_time is not None and now - last_send_time > idle_reset_sec:
                backoff_level = 0

            pending = get_pending_outbox(db_cfg, limit=1, log=log)
            # Nothing pending: avoid hot loop but don't alter backoff state.
            if not pending:
                await asyncio.sleep(1)
                continue

            delay = delays[backoff_level]
            # Enforce the current backoff delay since the last send attempt.
            if last_send_time is not None and now - last_send_time < delay:
                await asyncio.sleep(delay - (now - last_send_time))
                continue

            item = pending[0]
            channel = item["channel"]
            sender = item["sender"]
            content = item["content"]
            try:
                outbound = f"<{sender}@{cfg.hub.name}> {content}"
                log.debug("outbox:send id=%s channel=%s len=%d", item["id"], channel, len(content))
                channel_idx = channel_name_to_idx.get(channel)
                if channel_idx is None:
                    log.error("outbox:unknown_channel id=%s channel=%s", item["id"], channel)
                else:
                    result = await mesh_client.commands.send_chan_msg(channel_idx, outbound)
                    if result.type == EventType.ERROR:
                        log.error(
                            "outbox:send_failed id=%s channel=%s err=%s",
                            item["id"],
                            channel,
                            result.payload,
                        )
                    else:
                        mid = insert_message(
                            db_cfg,
                            ts=item["ts"],
                            channel=channel,
                            sender=sender,
                            content=content,
                            source="wifi",
                            session_id=item.get("session_id"),
                            fingerprint=item.get("fingerprint"),
                            log=log,
                        )
                        mark_outbox_sent(db_cfg, outbox_ids=[int(item["id"])], log=log)
            except Exception as e:
                log.error("outbox:send_failed id=%s err=%s", item["id"], e, exc_info=True)
            finally:
                # Advance backoff after each attempt to avoid flooding on large backlogs.
                last_send_time = time.time()
                backoff_level = min(backoff_level + 1, 3)
        except Exception as e:
            log.error("outbox:error %s", e, exc_info=True)


async def main_async(config_path: str, *, meshcore_debug: bool = False):
    cfg = load_config(config_path)
    log, sec = setup_logging("mesh_bot", cfg.logging)
    log.info("Civic Mesh mesh_bot starting")

    db_cfg = DBConfig(path=cfg.db_path)
    init_db(db_cfg, log=log)

    global EventType
    global MeshCore

    # Lazy import so web_server can run without meshcore installed.
    try:
        from meshcore import EventType, MeshCore  # type: ignore
    except Exception:
        EventType = None  # type: ignore
        MeshCore = None  # type: ignore

    mesh_client = None
    channel_name_to_idx = {name: idx for idx, name in enumerate(cfg.channels.names)}

    async def _connect_loop():
        nonlocal mesh_client
        backoff = 1
        while True:
            try:
                if MeshCore is None or EventType is None:
                    raise RuntimeError("meshcore not available")

                mesh_client = await MeshCore.create_serial(
                    cfg.radio.serial_port,
                    DEFAULT_BAUDRATE,
                    debug=meshcore_debug,
                )
                log.info("mesh:connected port=%s baudrate=%d", cfg.radio.serial_port, DEFAULT_BAUDRATE)

                resp = await mesh_client.commands.set_radio(
                    cfg.radio.freq_mhz,
                    cfg.radio.bw_khz,
                    cfg.radio.sf,
                    cfg.radio.cr,
                )
                log.info(
                    "mesh:set_radio event=%s type=%s payload=%s",
                    resp,
                    getattr(resp, "type", None),
                    getattr(resp, "payload", None),
                )
                if resp.type == EventType.ERROR:
                    log.error("mesh:set_radio_failed err=%s", resp.payload)

                await mesh_client.commands.send_appstart()
                radio_info = getattr(mesh_client, "self_info", {}) or {}
                log.info(
                    "mesh:radio_after freq=%s bw=%s sf=%s cr=%s",
                    radio_info.get("radio_freq"),
                    radio_info.get("radio_bw"),
                    radio_info.get("radio_sf"),
                    radio_info.get("radio_cr"),
                )

                for idx, name in enumerate(cfg.channels.names):
                    secret_hex = hashlib.sha256(name.encode()).hexdigest()[:32]
                    secret_bytes = bytes.fromhex(secret_hex)
                    result = await mesh_client.commands.set_channel(idx, name, secret_bytes)
                    if result.type == EventType.ERROR:
                        log.error("mesh:set_channel_failed channel=%s err=%s", name, result.payload)
                    else:
                        log.info("mesh:channel_set idx=%d name=%s", idx, name)

                await mesh_client.start_auto_message_fetching()
                log.info("mesh:auto_fetch_started")
                upsert_status(db_cfg, process="mesh_bot", radio_connected=True, log=log)

                # Handlers
                def _on_channel_message(event):
                    try:
                        msg = event.payload
                        channel_idx = msg.get("channel_idx")
                        if isinstance(channel_idx, int) and 0 <= channel_idx < len(cfg.channels.names):
                            channel = cfg.channels.names[channel_idx]
                        else:
                            channel = f"#channel-{channel_idx}"
                        sender = msg.get("sender", "") or msg.get("pubkey_prefix", "")
                        content = msg.get("text") or msg.get("content", "")
                        if not sender and isinstance(content, str) and ": " in content:
                            # Fallback for payloads that embed "Name: message" without sender metadata.
                            name, rest = content.split(": ", 1)
                            if name:
                                sender = name
                                content = rest
                        log.debug("mesh:rx channel=%s sender=%s len=%d", channel, sender, len(content))
                        insert_message(db_cfg, ts=_now_ts(), channel=channel, sender=sender, content=content, source="mesh", log=log)
                    except Exception as e:
                        log.error("mesh:rx_error %s", e, exc_info=True)

                mesh_client.subscribe(EventType.CHANNEL_MSG_RECV, _on_channel_message)

                backoff = 1
                return
            except Exception as e:
                try:
                    upsert_status(db_cfg, process="mesh_bot", radio_connected=False, log=log)
                except Exception as se:
                    log.error("heartbeat:down_failed err=%s", se, exc_info=True)
                log.error("mesh:connect_failed err=%s backoff=%ds", e, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    await _connect_loop()

    await asyncio.gather(
        _outbox_task(cfg, db_cfg, log, mesh_client, channel_name_to_idx),
        _retention_task(cfg, db_cfg, log),
        _heartbeat_task(cfg, db_cfg, log),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--meshcore-debug", action="store_true", help="Enable meshcore library debug logging")
    args = ap.parse_args()
    asyncio.run(main_async(args.config, meshcore_debug=args.meshcore_debug))


def main() -> None:
    """
    Console-script entrypoint (sync wrapper).
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--meshcore-debug", action="store_true", help="Enable meshcore library debug logging")
    args = ap.parse_args()
    asyncio.run(main_async(args.config, meshcore_debug=args.meshcore_debug))
