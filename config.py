from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
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


logger = logging.getLogger(__name__)


_IFACE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,14}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CALLSIGN_RE = re.compile(r"^[a-z0-9_-]{1,9}$")  # post-lowercase
_NON_OVERLAPPING_CHANNELS = frozenset({1, 6, 11})


@dataclass(frozen=True)
class NodeConfig:
    """Identity fields for this node.

    site_name: human-readable name of the physical hub/site this
        node serves; distinct from the on-wire `callsign`. Used
        for the portal masthead, captive-portal page titles, and
        the `session.location` stamp on new sessions.
    callsign: short on-wire identity, 1-9 chars, [A-Za-z0-9_-];
        lowercased on load. Set as the firmware SenderName so the
        MeshCore companion app uses it as the avatar header on
        every channel post.
    """
    site_name: str
    callsign: str


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
class NetworkConfig:
    ip: IPv4Address
    subnet_cidr: IPv4Network
    iface: str
    country_code: str
    dhcp_range_start: IPv4Address
    dhcp_range_end: IPv4Address
    dhcp_lease: str


@dataclass(frozen=True)
class ApConfig:
    ssid: str
    channel: int


@dataclass(frozen=True)
class WebConfig:
    port: int
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
    hub_docs_retention_count: int
    global_egress_per_hour: int
    outbox_max_depth: int


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
class ExternalDisplayConfig:
    # Default false so an absent [external_display] section = disabled,
    # which is the case for the majority of hubs (no physical e-paper
    # display attached). Phase 0 gates only the HTTP endpoint; Phase 1
    # will source real fields from server state, at which point the
    # "enabled = false ⇒ zero cost" guarantee depends on this default
    # surviving config-loader changes.
    enabled: bool = False


@dataclass(frozen=True)
class AppConfig:
    node: NodeConfig
    network: NetworkConfig
    ap: ApConfig
    radio: RadioConfig
    channels: ChannelsConfig
    local: LocalConfig
    web: WebConfig
    limits: LimitsConfig
    logging: LoggingConfig
    debug: DebugConfig
    recovery: RecoveryConfig
    external_display: ExternalDisplayConfig
    # Required, no default. The previous "civic_mesh.db" fallback resolved
    # against cwd and silently landed on whatever sat in the working
    # directory — exactly the trapdoor that let a misconfigured test
    # write fixture rows into the dev DB and broadcast them over LoRa.
    db_path: str


def _load_toml(path: str) -> dict[str, Any]:
    with open(path, "rb") as f:
        data = f.read()
    if tomllib is not None:
        return tomllib.loads(data.decode("utf-8"))
    if tomli is not None:  # pragma: no cover
        return tomli.loads(data.decode("utf-8"))
    raise RuntimeError("No TOML parser available (need Python 3.11+ or install tomli)")


def _validate_alias(raw: Any) -> str:
    host = str(raw).strip().lower()
    if "://" in host:
        raise ValueError(f"portal_aliases entry must not include a scheme (got {raw!r})")
    if "/" in host:
        raise ValueError(f"portal_aliases entry must not contain '/' (got {raw!r})")
    return host


def _validate_callsign(raw: Any) -> str:
    s = str(raw).strip().lower()
    if not _CALLSIGN_RE.match(s):
        raise ValueError(
            f"node.callsign {raw!r} must match ^[A-Za-z0-9_-]{{1,9}}$ "
            "(short on-wire identity; lowercased on load)"
        )
    return s


def _load_network(raw: dict[str, Any]) -> NetworkConfig:
    ip = IPv4Address(str(raw["ip"]))
    subnet_cidr = IPv4Network(str(raw["subnet_cidr"]), strict=False)
    iface = str(raw["iface"])
    if not _IFACE_RE.match(iface):
        raise ValueError(f"network.iface {iface!r} must match {_IFACE_RE.pattern}")
    country_code = str(raw["country_code"])
    if not _COUNTRY_RE.match(country_code):
        raise ValueError(
            f"network.country_code {country_code!r} must be ISO 3166-1 alpha-2 "
            "(two uppercase letters)"
        )
    return NetworkConfig(
        ip=ip,
        subnet_cidr=subnet_cidr,
        iface=iface,
        country_code=country_code,
        dhcp_range_start=IPv4Address(str(raw["dhcp_range_start"])),
        dhcp_range_end=IPv4Address(str(raw["dhcp_range_end"])),
        dhcp_lease=str(raw["dhcp_lease"]),
    )


def _validate_ssid(raw: Any) -> str:
    ssid = str(raw)
    if not 1 <= len(ssid) <= 32:
        raise ValueError(f"ap.ssid must be 1-32 chars (got {len(ssid)})")
    return ssid


def _load_ap(raw: dict[str, Any]) -> ApConfig:
    ssid = _validate_ssid(raw["ssid"])
    channel = int(raw["channel"])
    if channel < 1 or channel > 11:
        raise ValueError(
            f"ap.channel {channel} is outside the 2.4 GHz FCC range (1-11)"
        )
    if channel not in _NON_OVERLAPPING_CHANNELS:
        # Prefix with "config:" so this parse-time advisory is visually
        # distinct from apply/configure/validate's "civicmesh: <verb>: ..."
        # errors when both fire on the same run (operator scanning stderr
        # shouldn't miss the strict-validator failure that follows).
        logger.warning(
            "config: ap.channel %d is not a non-overlapping channel; "
            "1, 6, and 11 are recommended for 2.4 GHz",
            channel,
        )
    return ApConfig(ssid=ssid, channel=channel)


def _validate_hub_docs_retention_count(value: int) -> int:
    if value < 1:
        raise ValueError(
            f"limits.hub_docs_retention_count must be >= 1, got {value}"
        )
    return value


def _validate_global_egress_per_hour(value: int) -> int:
    # Zero would deadlock the outbox loop (no token ever granted); negative is
    # nonsense. The radio's physical ceiling is ~360/hr at the PNW LoRa profile,
    # so reject obvious operator typos that would silently disable mesh egress.
    if value < 1:
        raise ValueError(
            f"limits.global_egress_per_hour must be >= 1, got {value}"
        )
    return value


def _validate_outbox_max_depth(value: int) -> int:
    # Zero would 429 every post; negative is nonsense.
    if value < 1:
        raise ValueError(
            f"limits.outbox_max_depth must be >= 1, got {value}"
        )
    return value


def _validate_network_consistency(network: NetworkConfig) -> None:
    if network.ip not in network.subnet_cidr:
        raise ValueError(
            f"network.ip {network.ip} is not inside network.subnet_cidr {network.subnet_cidr}"
        )
    if network.dhcp_range_start not in network.subnet_cidr:
        raise ValueError(
            f"network.dhcp_range_start {network.dhcp_range_start} is not inside "
            f"network.subnet_cidr {network.subnet_cidr}"
        )
    if network.dhcp_range_end not in network.subnet_cidr:
        raise ValueError(
            f"network.dhcp_range_end {network.dhcp_range_end} is not inside "
            f"network.subnet_cidr {network.subnet_cidr}"
        )
    if network.dhcp_range_start > network.dhcp_range_end:
        raise ValueError(
            f"network.dhcp_range_start {network.dhcp_range_start} is greater than "
            f"network.dhcp_range_end {network.dhcp_range_end}"
        )
    if network.dhcp_range_start <= network.ip <= network.dhcp_range_end:
        raise ValueError(
            f"network.ip {network.ip} falls inside the DHCP range "
            f"[{network.dhcp_range_start}, {network.dhcp_range_end}]; "
            "the AP IP must be reserved"
        )


def to_serializable_dict(cfg: AppConfig) -> dict[str, Any]:
    """Inverse of load_config: emit a TOML/JSON-friendly dict.

    Used by `civicmesh config show` and the configure round-trip path.
    Drops nothing the schema knows; loses any unknown TOML keys that
    were in the source file (load_config silently ignores those).
    """
    return {
        "node": {"site_name": cfg.node.site_name, "callsign": cfg.node.callsign},
        "network": {
            "ip": str(cfg.network.ip),
            "subnet_cidr": str(cfg.network.subnet_cidr),
            "iface": cfg.network.iface,
            "country_code": cfg.network.country_code,
            "dhcp_range_start": str(cfg.network.dhcp_range_start),
            "dhcp_range_end": str(cfg.network.dhcp_range_end),
            "dhcp_lease": cfg.network.dhcp_lease,
        },
        "ap": {"ssid": cfg.ap.ssid, "channel": cfg.ap.channel},
        "radio": {
            "serial_port": cfg.radio.serial_port,
            "freq_mhz": cfg.radio.freq_mhz,
            "bw_khz": cfg.radio.bw_khz,
            "sf": cfg.radio.sf,
            "cr": cfg.radio.cr,
        },
        "channels": {"names": list(cfg.channels.names)},
        "local": {"names": list(cfg.local.names)},
        "web": {
            "port": cfg.web.port,
            "portal_aliases": list(cfg.web.portal_aliases),
        },
        "limits": {
            "posts_per_hour": cfg.limits.posts_per_hour,
            "message_max_chars": cfg.limits.message_max_chars,
            "name_max_chars": cfg.limits.name_max_chars,
            "name_pattern": cfg.limits.name_pattern,
            "outbox_max_retries": cfg.limits.outbox_max_retries,
            "outbox_max_delay_sec": cfg.limits.outbox_max_delay_sec,
            "outbox_idle_reset_sec": cfg.limits.outbox_idle_reset_sec,
            "outbox_echo_wait_sec": cfg.limits.outbox_echo_wait_sec,
            "retention_bytes_per_channel": cfg.limits.retention_bytes_per_channel,
            "hub_docs_retention_count": cfg.limits.hub_docs_retention_count,
            "global_egress_per_hour": cfg.limits.global_egress_per_hour,
            "outbox_max_depth": cfg.limits.outbox_max_depth,
        },
        "logging": {
            "log_dir": cfg.logging.log_dir,
            "log_level": cfg.logging.log_level,
            "enable_security_log": cfg.logging.enable_security_log,
        },
        "debug": {"allow_eth0": cfg.debug.allow_eth0},
        "external_display": {"enabled": cfg.external_display.enabled},
        "recovery": {
            "liveness_interval_sec": cfg.recovery.liveness_interval_sec,
            "liveness_timeout_sec": cfg.recovery.liveness_timeout_sec,
            "liveness_consecutive_threshold": cfg.recovery.liveness_consecutive_threshold,
            "outbox_consecutive_threshold": cfg.recovery.outbox_consecutive_threshold,
            "verify_timeout_sec": cfg.recovery.verify_timeout_sec,
            "post_rts_settle_sec": cfg.recovery.post_rts_settle_sec,
            "rts_pulse_width_sec": cfg.recovery.rts_pulse_width_sec,
            "flapping_window_sec": cfg.recovery.flapping_window_sec,
            "flapping_max_recoveries": cfg.recovery.flapping_max_recoveries,
            "backoff_base_sec": cfg.recovery.backoff_base_sec,
            "backoff_cap_sec": cfg.recovery.backoff_cap_sec,
        },
        "db_path": cfg.db_path,
    }


def load_config(path: str) -> AppConfig:
    raw = _load_toml(path)

    flags: list[str] = []
    if "portal_host" in raw.get("web", {}):
        flags.append(
            "  - `web.portal_host` is no longer supported; the portal host is now derived from `network.ip`. (CIV-60)"
        )
    if raw.get("network") is None:
        flags.append("  - Required section `[network]` is missing. (CIV-60)")
    if raw.get("ap") is None:
        flags.append("  - Required section `[ap]` is missing. (CIV-60)")

    # CIV-11: [node] split into site_name + callsign. Legacy `name`
    # auto-aliases to site_name with a WARN further down, but `callsign`
    # has no value migration — semantics differ (short on-wire identity,
    # not a human label) and the operator must supply a fresh value.
    # Surface this as a schema-changed flag pointing at `civicmesh
    # configure` so legacy configs don't silently default callsign to "".
    _node_raw = raw.get("node") or raw.get("hub") or {}
    if "name" in _node_raw and "callsign" not in _node_raw:
        flags.append(
            "  - `[node]` has legacy `name` but no `callsign`; callsign "
            "is the new short on-wire identity (no value migration from "
            "`name` or `location`). Run `civicmesh configure` to add it. "
            "(CIV-11)"
        )

    if flags:
        raise ValueError(
            "config.toml schema has changed.\n"
            + "\n".join(flags)
            + "\nSee docs/civicmesh-tool.md § CONFIGURATION FILE."
        )

    network = _load_network(raw["network"])
    _validate_network_consistency(network)
    ap = _load_ap(raw["ap"])

    node = raw.get("node") or raw.get("hub") or {}  # [hub] accepted as fallback
    radio = raw.get("radio", {})
    channels = raw.get("channels", {})
    local = raw.get("local", {})
    web = raw.get("web", {})
    limits = raw.get("limits", {})
    logging_raw = raw.get("logging", {})
    debug_raw = raw.get("debug", {})
    recovery_raw = raw.get("recovery", {})
    external_display_raw = raw.get("external_display", {})

    db_path = raw.get("db_path") or raw.get("db", {}).get("path")
    if not db_path:
        raise ValueError(
            f"config: db_path is required (set top-level "
            f"`db_path = \"...\"` in {path})"
        )
    db_path = os.path.expanduser(db_path)
    # Required absolute so cwd never enters DB resolution. A relative
    # path on a service that runs with WorkingDirectory=/usr/local/civicmesh/app
    # would silently land in the wrong tree; tests with a relative path
    # would land on the dev DB. Same root cause both ways.
    if not os.path.isabs(db_path):
        raise ValueError(
            f"config: db_path must be absolute (got {db_path!r} in {path}). "
            f"Use an absolute path or one prefixed with ~/ which expanduser will resolve."
        )

    local_names = local.get("names")
    if local_names is None:
        local_names = ["#local"]

    if "location" in node:
        raise ValueError(
            "node.location is no longer supported. The geographic-coords "
            "ticket lands separately. Use node.site_name (long, human, "
            "for masthead and session.location stamping) and node.callsign "
            "(short on-wire identity, <=9 chars [A-Za-z0-9_-])."
        )

    raw_name = node.get("name")
    raw_site = node.get("site_name")
    if raw_name is not None and raw_site is not None:
        if str(raw_name).strip() != str(raw_site).strip():
            raise ValueError(
                "node.name and node.site_name disagree "
                f"(name={raw_name!r}, site_name={raw_site!r}). "
                "Remove node.name; node.site_name is the new field."
            )
    elif raw_name is not None and raw_site is None:
        logger.warning(
            "config: node.name is deprecated; rename to node.site_name. "
            "Accepting %r as site_name for now.", raw_name,
        )
        raw_site = raw_name

    if raw_site is None or not str(raw_site).strip():
        raise ValueError("node.site_name is required (non-empty string)")
    site_name = str(raw_site).strip()

    if "callsign" not in node:
        raise ValueError("node.callsign is required (short on-wire identity)")
    callsign = _validate_callsign(node["callsign"])

    return AppConfig(
        node=NodeConfig(
            site_name=site_name,
            callsign=callsign,
        ),
        network=network,
        ap=ap,
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
            portal_aliases=tuple(
                _validate_alias(a)
                for a in web.get("portal_aliases", [])
                if str(a).strip()
            ),
        ),
        limits=LimitsConfig(
            posts_per_hour=int(limits.get("posts_per_hour", 10)),
            message_max_chars=int(limits.get("message_max_chars", 200)),
            name_max_chars=int(limits.get("name_max_chars", 12)),
            # Allow any characters except : (receive-side parser delimiter)
            # and @ (outbound sender@node delimiter).
            name_pattern=str(limits.get("name_pattern", r"^[^:@]+$")),
            outbox_max_retries=int(limits.get("outbox_max_retries", 3)),
            outbox_max_delay_sec=int(limits.get("outbox_max_delay_sec", 10)),
            outbox_idle_reset_sec=int(limits.get("outbox_idle_reset_sec", 60)),
            outbox_echo_wait_sec=int(limits.get("outbox_echo_wait_sec", 8)),
            retention_bytes_per_channel=int(limits.get("retention_bytes_per_channel", 10 * 1024 * 1024 * 1024)),
            hub_docs_retention_count=_validate_hub_docs_retention_count(
                int(limits.get("hub_docs_retention_count", 3))
            ),
            # Sized below radio capacity (~360/hr at PNW LoRa profile) so the
            # relay does not dominate a shared channel. Pairs with outbox_max_depth.
            global_egress_per_hour=_validate_global_egress_per_hour(
                int(limits.get("global_egress_per_hour", 200))
            ),
            # Drains in ~18 minutes at the policy bucket rate above; long enough
            # for normal radio reconnect, short enough that sustained outage
            # cannot store hours of work for a recovery flood.
            outbox_max_depth=_validate_outbox_max_depth(
                int(limits.get("outbox_max_depth", 60))
            ),
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
        external_display=ExternalDisplayConfig(
            enabled=bool(external_display_raw.get("enabled", False)),
        ),
        db_path=db_path,
    )
