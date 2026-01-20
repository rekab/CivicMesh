import argparse
import asyncio
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
    search_messages,
    update_vote,
)
from logger import setup_logging


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


async def _outbox_task(cfg, db_cfg: DBConfig, log, mesh_client):
    interval = cfg.limits.outbox_batch_interval_sec
    batch_size = cfg.limits.outbox_batch_size

    while True:
        try:
            pending = get_pending_outbox(db_cfg, limit=batch_size, log=log)
            if pending:
                log.info("outbox:pending=%d", len(pending))
                sent_ids: list[int] = []
                for item in pending:
                    # mesh_client API is placeholder; actual meshcore_py integration below
                    channel = item["channel"]
                    sender = item["sender"]
                    content = item["content"]
                    try:
                        log.debug("outbox:send id=%s channel=%s len=%d", item["id"], channel, len(content))
                        await mesh_client.send_channel_message(channel, content)
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
                        sid = item.get("session_id")
                        if sid:
                            try:
                                update_vote(
                                    db_cfg,
                                    message_id=mid,
                                    session_id=str(sid),
                                    vote_type=1,
                                    ts=_now_ts(),
                                    log=log,
                                )
                            except Exception as e:
                                log.error("outbox:auto_upvote_failed id=%s err=%s", item["id"], e, exc_info=True)
                        sent_ids.append(int(item["id"]))
                    except Exception as e:
                        log.error("outbox:send_failed id=%s err=%s", item["id"], e, exc_info=True)
                if sent_ids:
                    mark_outbox_sent(db_cfg, outbox_ids=sent_ids, log=log)
        except Exception as e:
            log.error("outbox:error %s", e, exc_info=True)

        await asyncio.sleep(interval)


def _parse_search(text: str) -> dict:
    # Advanced syntax:
    # search [#channel] [sender:name] keyword...
    parts = text.strip().split()
    if not parts or parts[0].lower() != "search":
        return {}
    channel = None
    sender = None
    keywords: list[str] = []
    for p in parts[1:]:
        if p.startswith("#") and channel is None:
            channel = p
        elif p.lower().startswith("sender:") and sender is None:
            sender = p.split(":", 1)[1]
        else:
            keywords.append(p)
    return {"channel": channel, "sender": sender, "q": " ".join(keywords).strip()}


async def main_async(config_path: str):
    cfg = load_config(config_path)
    log, sec = setup_logging("mesh_bot", cfg.logging)
    log.info("Civic Mesh mesh_bot starting")

    db_cfg = DBConfig(path=cfg.db_path)
    init_db(db_cfg, log=log)

    # Lazy import so web_server can run without meshcore_py installed.
    try:
        import meshcore_py  # type: ignore
    except Exception:
        meshcore_py = None  # type: ignore

    class _DummyMeshClient:
        async def connect(self):  # pragma: no cover
            raise RuntimeError("meshcore_py not installed")

        async def join_channel(self, _name: str):  # pragma: no cover
            return None

        async def send_channel_message(self, _channel: str, _content: str):  # pragma: no cover
            return None

        def on_channel_message(self, _cb):  # pragma: no cover
            return None

        def on_direct_message(self, _cb):  # pragma: no cover
            return None

        async def send_direct_message(self, _to: str, _content: str):  # pragma: no cover
            return None

    mesh_client = _DummyMeshClient()

    async def _connect_loop():
        nonlocal mesh_client
        backoff = 1
        while True:
            try:
                if meshcore_py is None:
                    raise RuntimeError("meshcore_py not available")

                # NOTE: meshcore_py API may differ; this is a best-effort integration scaffold.
                mesh_client = meshcore_py.Client(serial_port=cfg.radio.serial_port)
                await mesh_client.connect()
                log.info("mesh:connected port=%s", cfg.radio.serial_port)

                for ch in cfg.channels.names:
                    try:
                        await mesh_client.join_channel(ch)
                        log.info("mesh:joined channel=%s", ch)
                    except Exception as e:
                        log.error("mesh:join_failed channel=%s err=%s", ch, e, exc_info=True)

                # Handlers
                def _on_channel_message(msg):
                    try:
                        ts = _now_ts()
                        channel = getattr(msg, "channel", "") or getattr(msg, "room", "")
                        sender = getattr(msg, "sender", "") or getattr(msg, "from", "")
                        content = getattr(msg, "content", "") or getattr(msg, "text", "")
                        log.debug("mesh:rx channel=%s sender=%s len=%d", channel, sender, len(content))
                        insert_message(db_cfg, ts=ts, channel=channel, sender=sender, content=content, source="mesh", log=log)
                    except Exception as e:
                        log.error("mesh:rx_error %s", e, exc_info=True)

                async def _handle_dm(msg):
                    try:
                        text = getattr(msg, "content", "") or getattr(msg, "text", "")
                        sender = getattr(msg, "sender", "") or getattr(msg, "from", "")
                        log.debug("mesh:dm from=%s len=%d", sender, len(text))
                        q = _parse_search(text)
                        if not q:
                            await mesh_client.send_direct_message(sender, "Usage: search [#channel] [sender:name] keyword")
                            return
                        results = search_messages(
                            db_cfg,
                            query=q["q"],
                            channel=q.get("channel"),
                            sender=q.get("sender"),
                            limit=5,
                            log=log,
                        )
                        if not results:
                            await mesh_client.send_direct_message(sender, "No results.")
                            return
                        lines = []
                        for r in results:
                            lines.append(f'{r["channel"]} {r["sender"]} {r["ts"]}: {r["content"]}')
                        await mesh_client.send_direct_message(sender, "Results:\n" + "\n".join(lines))
                    except Exception as e:
                        log.error("mesh:dm_error %s", e, exc_info=True)

                mesh_client.on_channel_message(_on_channel_message)
                mesh_client.on_direct_message(lambda m: asyncio.create_task(_handle_dm(m)))

                backoff = 1
                return
            except Exception as e:
                log.error("mesh:connect_failed err=%s backoff=%ds", e, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    await _connect_loop()

    await asyncio.gather(
        _outbox_task(cfg, db_cfg, log, mesh_client),
        _retention_task(cfg, db_cfg, log),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    asyncio.run(main_async(args.config))


def main() -> None:
    """
    Console-script entrypoint (sync wrapper).
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    asyncio.run(main_async(args.config))
