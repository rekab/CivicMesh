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
class WebConfig:
    port: int
    portal_host: str
    portal_aliases: tuple[str, ...]


@dataclass(frozen=True)
class LimitsConfig:
    posts_per_hour: int
    message_max_chars: int
    name_max_chars: int
    name_pattern: str
    outbox_max_retries: int
    outbox_max_delay_sec: int
    outbox_idle_reset_sec: int
    outbox_echo_wait_sec: int
    retention_bytes_per_channel: int


@dataclass(frozen=True)
class DebugConfig:
    allow_eth0: bool


@dataclass(frozen=True)
class RecoveryConfig:
    liveness_interval_sec: float = 30.0
    liveness_timeout_sec: float = 5.0
    liveness_consecutive_threshold: int = 3
    outbox_consecutive_threshold: int = 3
    verify_timeout_sec: float = 5.0
    post_rts_settle_sec: float = 5.0
    rts_pulse_width_sec: float = 0.1
    flapping_window_sec: int = 3600
    flapping_max_recoveries: int = 6
    backoff_base_sec: float = 60.0
    backoff_cap_sec: float = 3600.0


@dataclass(frozen=True)
class AppConfig:
    hub: HubConfig
    radio: RadioConfig
    channels: ChannelsConfig
    local: LocalConfig
    web: WebConfig
    limits: LimitsConfig
    logging: LoggingConfig
    debug: DebugConfig
    recovery: RecoveryConfig
    db_path: str = "civic_mesh.db"


def _load_toml(path: str) -> dict[str, Any]:
    with open(path, "rb") as f:
        data = f.read()
    if tomllib is not None:
        return tomllib.loads(data.decode("utf-8"))
    if tomli is not None:  # pragma: no cover
        return tomli.loads(data.decode("utf-8"))
    raise RuntimeError("No TOML parser available (need Python 3.11+ or install tomli)")


def _validate_portal_host(raw: Any) -> str:
    host = str(raw).strip().lower()
    if "://" in host:
        raise ValueError(f"portal_host must not include a scheme (got {raw!r})")
    if "/" in host:
        raise ValueError(f"portal_host must not contain '/' (got {raw!r})")
    return host


def load_config(path: str) -> AppConfig:
    raw = _load_toml(path)

    hub = raw.get("hub", {})
    radio = raw.get("radio", {})
    channels = raw.get("channels", {})
    local = raw.get("local", {})
    web = raw.get("web", {})
    limits = raw.get("limits", {})
    logging_raw = raw.get("logging", {})
    debug_raw = raw.get("debug", {})
    recovery_raw = raw.get("recovery", {})

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
        web=WebConfig(
            port=int(web.get("port", 80)),
            portal_host=_validate_portal_host(web.get("portal_host", "10.0.0.1")),
            portal_aliases=tuple(
                _validate_portal_host(a)
                for a in web.get("portal_aliases", [])
                if str(a).strip()
            ),
        ),
        limits=LimitsConfig(
            posts_per_hour=int(limits.get("posts_per_hour", 10)),
            message_max_chars=int(limits.get("message_max_chars", 200)),
            name_max_chars=int(limits.get("name_max_chars", 12)),
            name_pattern=str(limits.get("name_pattern", r"^[A-Za-z0-9_-]+$")),
            outbox_max_retries=int(limits.get("outbox_max_retries", 3)),
            outbox_max_delay_sec=int(limits.get("outbox_max_delay_sec", 10)),
            outbox_idle_reset_sec=int(limits.get("outbox_idle_reset_sec", 60)),
            outbox_echo_wait_sec=int(limits.get("outbox_echo_wait_sec", 8)),
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
        recovery=RecoveryConfig(
            liveness_interval_sec=float(recovery_raw.get("liveness_interval_sec", 30.0)),
            liveness_timeout_sec=float(recovery_raw.get("liveness_timeout_sec", 5.0)),
            liveness_consecutive_threshold=int(recovery_raw.get("liveness_consecutive_threshold", 3)),
            outbox_consecutive_threshold=int(recovery_raw.get("outbox_consecutive_threshold", 3)),
            verify_timeout_sec=float(recovery_raw.get("verify_timeout_sec", 5.0)),
            post_rts_settle_sec=float(recovery_raw.get("post_rts_settle_sec", 5.0)),
            rts_pulse_width_sec=float(recovery_raw.get("rts_pulse_width_sec", 0.1)),
            flapping_window_sec=int(recovery_raw.get("flapping_window_sec", 3600)),
            flapping_max_recoveries=int(recovery_raw.get("flapping_max_recoveries", 6)),
            backoff_base_sec=float(recovery_raw.get("backoff_base_sec", 60.0)),
            backoff_cap_sec=float(recovery_raw.get("backoff_cap_sec", 3600.0)),
        ),
        db_path=db_path,
    )
