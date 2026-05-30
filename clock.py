"""Wall-clock correction primitives: offset, vote epoch, boot id, and the
pure consensus math.

CivicMesh hubs have no RTC and run without internet at deploy sites; the
system clock starts stale every cold boot. We never set the OS clock from
bot code (see docs/clock_consensus.md for the rationale). Instead we track
an integer offset_seconds in `clock_state` and apply it on every
timestamped DB write:  ts = int(time.time()) + offset_seconds.

This module holds:

- `get_offset`, `get_vote_epoch`, `wall_now`: thin readers around the
  `clock_state` KV table. Stamping helpers in database.py read these
  WITHIN their own BEGIN IMMEDIATE so the value is consistent with the
  raw `time.time()` they pair with — see the comments on
  `_wall_writer_txn` in database.py for the actor-vs-actor race this
  defends against.

- `get_boot_id`: reads `/proc/sys/kernel/random/boot_id` once per
  process and caches it. The boot id gates report eligibility across
  OS reboots via pure equality. Monotonic-time comparisons cannot do
  this safely — a prior-boot report captured at monotonic=5s passes
  any "> current_monotonic" test taken at monotonic=10s.

- `sanity_check_wall`: shared absolute-epoch sanity bound, called by
  /api/clock at ingest and by the consensus task at evaluation.

- `evaluate_consensus`: pure-function consensus math. MAC dedupe,
  quorum, median, sanity bound, forward-only on first correction,
  bounded bidirectional nudge after. Unit-tested without touching the
  DB (see tests/test_clock.py).

VOTE SHAPE — this naming exists to prevent a real bug:
`clock_offset_vote_sec` (the column the SPA writes via /api/clock) is
the ABSOLUTE raw-system offset endorsed by the client:
    clock_offset_vote_sec = client_epoch - int(time.time())
NOT a residual against corrected wall. The consensus formula is:
    candidate_offset = median(votes)
NOT `current_offset + median(votes)` — that would double-apply the
offset on every tick after the first acceptance.
"""

import json
import sys
import time
from dataclasses import dataclass
from statistics import median
from typing import NamedTuple, Optional

from database import DBConfig, _connect


def ensure_linux_platform() -> None:
    """Hard-fail at process startup if we're not on Linux.

    CivicMesh's clock-correction design (CIV-99) reads
    `/proc/sys/kernel/random/boot_id` for the cross-boot eligibility
    gate. That file is Linux-specific — macOS, BSD, Windows, and
    container runtimes without a /proc/sys mount do not provide it,
    and no equivalent UUID-per-boot exists in the stdlib.

    Failing here, loudly, beats letting the bot run and then explode
    on the first /api/clock report or the first consensus tick with
    `FileNotFoundError: /proc/sys/kernel/random/boot_id`. Dev users
    on a Mac get a clear, actionable error at the entry point;
    production on Linux Pi is unaffected.

    Tests can stub this by patching `clock.ensure_linux_platform`.
    """
    if sys.platform != "linux":
        raise RuntimeError(
            "CivicMesh requires Linux (sys.platform == 'linux'). "
            f"Detected sys.platform={sys.platform!r}. The clock-consensus "
            "design (CIV-99) depends on /proc/sys/kernel/random/boot_id "
            "for cross-boot eligibility; that file is Linux-only. "
            "Production runs on Raspberry Pi OS; dev runs on a Linux VM "
            "or container. See docs/clock_consensus.md and AGENTS.md."
        )


# ---------------------------------------------------------------------------
# clock_state readers
# ---------------------------------------------------------------------------

def _read_int_kv(conn, key: str, default: int = 0) -> int:
    import sqlite3 as _sqlite3
    try:
        row = conn.execute(
            "SELECT value FROM clock_state WHERE key=?", (key,),
        ).fetchone()
    except _sqlite3.OperationalError:
        # clock_state hasn't been initialized yet (very-early-boot init_db
        # race, or a test fixture that opens a connection before init_db).
        # Safe default: act as if no correction has happened.
        return default
    if row is None:
        return default
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return default


def get_offset(cfg: DBConfig, *, conn=None) -> int:
    """Read clock_state.offset_seconds.

    When `conn` is provided, reads on the caller's transaction. This is
    the only legitimate path for a write helper that needs to stamp
    `ts = raw + offset`: both the offset read and the INSERT must run
    under the same BEGIN IMMEDIATE so an admin command's BEGIN EXCLUSIVE
    can't slip between them.

    When `conn` is None, opens a private connection — for read-only
    callers (response payloads, retention cutoffs, telemetry display).
    """
    if conn is not None:
        return _read_int_kv(conn, "offset_seconds", 0)
    private = _connect(cfg)
    try:
        return _read_int_kv(private, "offset_seconds", 0)
    finally:
        private.close()


def get_vote_epoch(cfg: DBConfig, *, conn=None) -> int:
    if conn is not None:
        return _read_int_kv(conn, "vote_epoch", 0)
    private = _connect(cfg)
    try:
        return _read_int_kv(private, "vote_epoch", 0)
    finally:
        private.close()


def wall_now(cfg: DBConfig, *, conn=None) -> int:
    """Corrected wall time as an integer epoch.

    Equals `int(time.time()) + offset_seconds`. NOT to be called inside
    the centralized DB write helpers — those must read raw time and the
    offset directly under their own write transaction. `wall_now` is for
    response payloads, retention cutoffs, cookie-age math, and similar
    read-only callers where consistency with an open write txn does not
    matter.
    """
    return int(time.time()) + get_offset(cfg, conn=conn)


# ---------------------------------------------------------------------------
# Linux boot ID (cross-boot identity gate)
# ---------------------------------------------------------------------------

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_cached_boot_id: Optional[str] = None


def get_boot_id() -> str:
    """Return the current Linux boot ID (cached for the process lifetime).

    Reads `/proc/sys/kernel/random/boot_id`, a UUID generated at OS boot
    and stable for the boot's lifetime. Stamped onto each
    `sessions.clock_report_boot_id` at /api/clock capture; the consensus
    task filters eligibility by equality. Boot-id equality is what
    excludes prior-boot reports across reboots — see module docstring
    for why monotonic comparisons can't do this.
    """
    global _cached_boot_id
    if _cached_boot_id is not None:
        return _cached_boot_id
    with open(_BOOT_ID_PATH, "r") as f:
        _cached_boot_id = f.read().strip()
    return _cached_boot_id


def _reset_boot_id_cache_for_tests() -> None:
    """Test-only: clear the process-wide boot-id cache between fixtures."""
    global _cached_boot_id
    _cached_boot_id = None


_PROD_TREE_PREFIX = "/usr/local/civicmesh/"


def warn_if_prod_opt_out_of_timesync_mask(log, *, caller_file: Optional[str] = None) -> None:
    """Log CRITICAL if running from the prod tree with the dev opt-out set.

    The `[clock] require_timesync_masked = false` opt-out is intended for
    dev / RTC-backed / internet-connected machines that intentionally
    trust NTP. Shipping that flag to a production disaster node (e.g.
    by copying a dev config.toml into prod) would silently disable the
    `civicmesh apply` mask gate — operator never sees the safety
    refusal that's supposed to catch unmasked timesyncd.

    Heuristic: production deployments live under /usr/local/civicmesh/.
    If `web_server.py` or `mesh_bot.py` is running from that tree AND
    the opt-out is set, log loudly at every process start.

    `caller_file` lets tests inject a fake path. Production callers
    pass None (the default), which inspects the calling frame's
    __file__. clock.py is in both dev and prod trees, so we can't
    use clock.__file__ for the check — we want the entry point's.
    """
    if caller_file is None:
        import inspect
        caller_frame = inspect.stack()[1]
        caller_file = caller_frame.filename
    if not caller_file.startswith(_PROD_TREE_PREFIX):
        return
    log.critical(
        "CIV-99: running from the prod tree (%s) with "
        "[clock] require_timesync_masked = false. This opt-out is meant "
        "for dev / RTC / internet-connected hosts that trust NTP. On a "
        "production disaster node it disables the `civicmesh apply` "
        "mask gate that's supposed to refuse unmasked timesyncd. If "
        "this is a deliberate hybrid deployment, fine — otherwise flip "
        "require_timesync_masked back to true in config.toml.",
        caller_file,
    )


# ---------------------------------------------------------------------------
# Sanity bound on absolute corrected wall time
# ---------------------------------------------------------------------------

def sanity_check_wall(
    t: int, floor_epoch: int, ceiling_epoch: int,
) -> Optional[str]:
    """Return None if `t` is within [floor, ceiling], else an error string.

    Both bounds are ABSOLUTE epochs (not raw-relative). The very scenario
    this feature targets is a stale raw clock — a ceiling computed as
    "raw_now + 5y" would reject correct phone reports and break the
    feature in its main use case.
    """
    if t < floor_epoch:
        return (
            f"wall time {t} below sanity_floor_epoch {floor_epoch}"
        )
    if t > ceiling_epoch:
        return (
            f"wall time {t} above sanity_ceiling_epoch {ceiling_epoch}"
        )
    return None


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------

class Report(NamedTuple):
    """One per-session clock report eligible for consensus.

    `offset_vote_sec` is the absolute raw-system offset endorsed by the
    client (client_epoch - int(time.time()) at capture). Stored as-is in
    sessions.clock_offset_vote_sec.
    """
    session_id: str
    mac_address: Optional[str]
    offset_vote_sec: int


@dataclass(frozen=True)
class ConsensusConfig:
    """The subset of [clock] config the pure math needs.

    Kept separate from the full ClockConfig so tests don't have to
    construct a TOML-shaped object; mesh_bot adapts cfg.clock to this at
    the call site.
    """
    quorum_min_cookies: int
    sanity_floor_epoch: int
    sanity_ceiling_epoch: int
    max_nudge_sec: int


@dataclass(frozen=True)
class ConsensusDecision:
    new_offset: int
    nudge: int
    voter_count: int
    median_offset_vote_sec: int
    accept_reason: str  # 'first_correction_forward' | 'nudge_within_cap'
    source_summary: dict


def _dedupe_by_mac(reports: list[Report]) -> list[Report]:
    """Collapse reports sharing a non-null MAC to a single vote (last wins).

    Callers should pass `reports` in most-recent-last order so the kept
    entry per MAC is the freshest. Sessions with NULL/empty MAC are kept
    as distinct votes (we don't know they're the same device).
    """
    by_mac: dict[str, Report] = {}
    no_mac: list[Report] = []
    for r in reports:
        mac = (r.mac_address or "").strip()
        if mac:
            by_mac[mac] = r
        else:
            no_mac.append(r)
    out: list[Report] = list(by_mac.values())
    out.extend(no_mac)
    return out


def evaluate_consensus(
    reports: list[Report],
    *,
    current_offset: int,
    raw_now: int,
    first_correction_done: bool,
    cfg: ConsensusConfig,
) -> Optional[ConsensusDecision]:
    """Pure consensus math.

    Math (after MAC dedupe):
        candidate_offset = median(votes)              # absolute offset
        nudge            = candidate_offset - current_offset

    Acceptance:
        first_correction_done = False  → forward-only on corrected wall
            (candidate_offset >= current_offset). No magnitude cap. The
            big-jump case the issue exists for.
        first_correction_done = True   → abs(nudge) <= max_nudge_sec,
            sign-agnostic (Pi-Zero RC drift typically forward; bidir
            nudges track it).

    Sanity bound: reject if raw_now + candidate_offset is outside
    [sanity_floor_epoch, sanity_ceiling_epoch] (both absolute).

    Returns a ConsensusDecision even when nudge == 0; the consensus task
    is responsible for no-op suppression (don't write an audit row or
    telemetry event when nothing changed). Keeping this distinction at
    the caller means the pure math stays decisional, not side-effecting.

    Returns None on quorum, sanity, or acceptance-rule failure.
    """
    deduped = _dedupe_by_mac(reports)
    if len({r.session_id for r in deduped}) < cfg.quorum_min_cookies:
        return None

    votes = [int(r.offset_vote_sec) for r in deduped]
    candidate_offset = int(round(median(votes)))
    nudge = candidate_offset - int(current_offset)

    corrected_wall = int(raw_now) + candidate_offset
    if sanity_check_wall(
        corrected_wall, cfg.sanity_floor_epoch, cfg.sanity_ceiling_epoch,
    ) is not None:
        return None

    if not first_correction_done:
        if candidate_offset < int(current_offset):
            return None
        accept_reason = "first_correction_forward"
    else:
        if abs(nudge) > cfg.max_nudge_sec:
            return None
        accept_reason = "nudge_within_cap"

    voter_count = len({r.session_id for r in deduped})
    source_summary = {
        "cookies": sorted({r.session_id for r in deduped}),
        "macs": sorted(
            {(r.mac_address or "") for r in deduped if r.mac_address}
        ),
        "votes": sorted(votes),
        "candidate_offset_sec": candidate_offset,
        "nudge_sec": nudge,
    }
    return ConsensusDecision(
        new_offset=candidate_offset,
        nudge=nudge,
        voter_count=voter_count,
        median_offset_vote_sec=candidate_offset,
        accept_reason=accept_reason,
        source_summary=source_summary,
    )


def summary_to_json(s: dict) -> str:
    """JSON-encode a source_summary dict for storage."""
    return json.dumps(s, separators=(",", ":"), sort_keys=True)
