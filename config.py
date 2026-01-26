from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from logger import LoggingConfig

try:
    import tomllib  # py311+
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore

try:
    import tomli  # type: ignore
except Exception:  # pragma: no cover
    tomli = None


@dataclass(frozen=True)
class HubConfig:
    name: str
    location: str


@dataclass(frozen=True)
class RadioConfig:
    serial_port: str
    freq_mhz: float
    bw_khz: float
    sf: int
    cr: int


@dataclass(frozen=True)
class ChannelsConfig:
    names: list[str]


@dataclass(frozen=True)
class LocalConfig:
    names: list[str]


@dataclass(frozen=True)
class WifiConfig:
    ssid: str


@dataclass(frozen=True)
class WebConfig:
    port: int


@dataclass(frozen=True)
class LimitsConfig:
    posts_per_hour: int
    message_max_chars: int
    name_max_chars: int
    outbox_batch_interval_sec: int
    outbox_batch_size: int
    retention_bytes_per_channel: int


@dataclass(frozen=True)
class DebugConfig:
    allow_eth0: bool


@dataclass(frozen=True)
class AppConfig:
    hub: HubConfig
    radio: RadioConfig
    channels: ChannelsConfig
    local: LocalConfig
    wifi: WifiConfig
    web: WebConfig
    limits: LimitsConfig
    logging: LoggingConfig
    debug: DebugConfig
    db_path: str = "civic_mesh.db"


def _load_toml(path: str) -> dict[str, Any]:
    with open(path, "rb") as f:
        data = f.read()
    if tomllib is not None:
        return tomllib.loads(data.decode("utf-8"))
    if tomli is not None:  # pragma: no cover
        return tomli.loads(data.decode("utf-8"))
    raise RuntimeError("No TOML parser available (need Python 3.11+ or install tomli)")


def load_config(path: str) -> AppConfig:
    raw = _load_toml(path)

    hub = raw.get("hub", {})
    radio = raw.get("radio", {})
    channels = raw.get("channels", {})
    local = raw.get("local", {})
    wifi = raw.get("wifi", {})
    web = raw.get("web", {})
    limits = raw.get("limits", {})
    logging_raw = raw.get("logging", {})
    debug_raw = raw.get("debug", {})

    db_path = raw.get("db_path") or raw.get("db", {}).get("path") or "civic_mesh.db"
    db_path = os.path.expanduser(db_path)

    local_names = local.get("names")
    if local_names is None:
        local_names = ["#local"]

    return AppConfig(
        hub=HubConfig(
            name=str(hub.get("name", "Civic Mesh Hub")),
            location=str(hub.get("location", "")),
        ),
        radio=RadioConfig(
            serial_port=str(radio.get("serial_port", "/dev/ttyUSB0")),
            freq_mhz=float(radio.get("freq_mhz", 910.525)),
            bw_khz=float(radio.get("bw_khz", 62.5)),
            sf=int(radio.get("sf", 7)),
            cr=int(radio.get("cr", 5)),
        ),
        channels=ChannelsConfig(names=[str(x) for x in (channels.get("names") or [])]),
        local=LocalConfig(names=[str(x) for x in local_names]),
        wifi=WifiConfig(ssid=str(wifi.get("ssid", "CivicMesh-Hub"))),
        web=WebConfig(port=int(web.get("port", 80))),
        limits=LimitsConfig(
            posts_per_hour=int(limits.get("posts_per_hour", 10)),
            message_max_chars=int(limits.get("message_max_chars", 200)),
            name_max_chars=int(limits.get("name_max_chars", 10)),
            outbox_batch_interval_sec=int(limits.get("outbox_batch_interval_sec", 30)),
            outbox_batch_size=int(limits.get("outbox_batch_size", 5)),
            retention_bytes_per_channel=int(limits.get("retention_bytes_per_channel", 10 * 1024 * 1024 * 1024)),
        ),
        logging=LoggingConfig(
            log_dir=str(logging_raw.get("log_dir", "logs")),
            log_level=str(logging_raw.get("log_level", "INFO")),
            enable_security_log=bool(logging_raw.get("enable_security_log", True)),
        ),
        debug=DebugConfig(
            allow_eth0=bool(debug_raw.get("allow_eth0", False)),
        ),
        db_path=db_path,
    )
