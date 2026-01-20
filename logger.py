import logging
import logging.handlers
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional


_LOG_INJECTION_RE = re.compile(r"[\r\n\t]")


def _sanitize_for_log(value: object, max_len: int = 500) -> str:
    """
    Prevent log injection and overly-large log entries. Never log message bodies
    from untrusted clients in security events; log lengths or hashes instead.
    """
    if value is None:
        return ""
    s = str(value)
    s = _LOG_INJECTION_RE.sub(" ", s)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


class _SanitizingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Sanitize the message after %-formatting is applied by base class
        msg = super().format(record)
        return _sanitize_for_log(msg, max_len=2000)


class _RateLimitedSecurityLogger:
    """
    Very small in-process rate limiter to avoid log flooding in hostile envs.
    Keyed by (event_type, ip, mac). Allows N events per window.
    """

    def __init__(self, logger: logging.Logger, limit: int = 20, window_sec: int = 60):
        self._logger = logger
        self._limit = limit
        self._window = window_sec
        self._buckets: dict[tuple[str, str, str], tuple[float, int]] = {}

    def error(self, event_type: str, ip: str = "", mac: str = "", msg: str = "", **fields: object) -> None:
        now = time.time()
        key = (event_type, ip or "", mac or "")
        start, count = self._buckets.get(key, (now, 0))
        if now - start > self._window:
            start, count = now, 0
        if count >= self._limit:
            self._buckets[key] = (start, count + 1)
            return
        self._buckets[key] = (start, count + 1)

        safe_fields = {k: _sanitize_for_log(v) for k, v in fields.items()}
        self._logger.error("SECURITY:%s %s %s", _sanitize_for_log(event_type), _sanitize_for_log(msg), safe_fields)


@dataclass(frozen=True)
class LoggingConfig:
    log_dir: str = "logs"
    log_level: str = "INFO"
    enable_security_log: bool = True
    rotate_max_bytes: int = 10 * 1024 * 1024
    rotate_backup_count: int = 5


def setup_logging(service_name: str, cfg: LoggingConfig) -> tuple[logging.Logger, Optional[_RateLimitedSecurityLogger]]:
    os.makedirs(cfg.log_dir, exist_ok=True)

    level = getattr(logging, cfg.log_level.upper(), logging.INFO)

    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    logger.propagate = False

    # Avoid duplicate handlers on reload
    if logger.handlers:
        return logger, None

    fmt = _SanitizingFormatter(
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s] [%(funcName)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # Console (systemd/journalctl)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # Service file log (DEBUG+)
    fh = logging.handlers.RotatingFileHandler(
        filename=os.path.join(cfg.log_dir, f"{service_name}.log"),
        maxBytes=cfg.rotate_max_bytes,
        backupCount=cfg.rotate_backup_count,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sec = None
    if cfg.enable_security_log:
        sec_logger = logging.getLogger("security")
        sec_logger.setLevel(logging.ERROR)
        sec_logger.propagate = False

        sfh = logging.FileHandler(os.path.join(cfg.log_dir, "security.log"), encoding="utf-8")
        sfh.setLevel(logging.ERROR)
        sfh.setFormatter(fmt)
        sec_logger.addHandler(sfh)

        sec = _RateLimitedSecurityLogger(sec_logger)

    return logger, sec

