"""Cooperative startup lock for civicmesh-mesh (CIV-80).

Prevents two civicmesh-mesh processes from running against one radio
on the same host (the dev-tree + installed-systemd collision: a
developer runs `uv run civicmesh-mesh` from ~/code/CivicMesh without
first stopping the systemd service, or vice versa).

Mechanism: fcntl.flock(LOCK_EX | LOCK_NB) on a small file
pre-created by /etc/tmpfiles.d/civicmesh.conf with group=dialout
mode 0664. The lock is on the open-file-description; the kernel
releases it on fd close (including SIGKILL), so there is no
stale-lock recovery path. The lock fd is held for the process
lifetime by being stashed at module scope in the caller.

Why this file, not the device:
  - Opening /dev/ttyUSB* toggles DTR/RTS via the cp210x driver,
    which on the Heltec V3 is wired to the ESP32 reset/boot circuit
    (see recovery.py:_rts_pulse_action). Locking via a separate file
    leaves the radio untouched until meshcore opens it.

Why /run/lock and not /tmp:
  - /tmp on Debian Bookworm / current Raspberry Pi OS is governed by
    fs.protected_regular=2, which forbids cross-user opens of regular
    files in sticky world-writable directories. Both directions break:
    a file created by the dev user can't be opened by the service
    user, and vice versa. /tmp is fundamentally unusable for this.
  - /run/lock on the same distros is root:lock 0775, not 1777, so a
    service running as civicmesh cannot directly create files there.
    The fix is to pre-create the lock file via systemd-tmpfiles with
    group=dialout, so both the service (in dialout) and any dev user
    (in dialout to talk to the radio) can open it. apply ships the
    tmpfiles.d config and runs `systemd-tmpfiles --create` so the
    file exists immediately; subsequent boots repopulate it via
    systemd-tmpfiles-setup.service.

Why O_RDWR:
  - The file is mode 0664 (rw-rw-r--), so opening O_RDWR succeeds
    only for owners of the file's group. The file's group is
    `dialout`, so by design only dialout members (the users
    authorized to talk to the radio) can acquire the lock. A user
    outside dialout couldn't run mesh_bot anyway.

Cooperative scope: only catches processes that call into this
module. Non-CivicMesh tools (picocom, screen, cat /dev/ttyUSB0)
still bypass — they don't open this file at all. That's the
failure-mode-C3 residual; see docs/radio-debugging/failure-modes.md.

Dev fallback: when LOCK_PATH does not exist (e.g., a developer
running tests on a non-Pi machine that never ran `civicmesh apply`),
the acquire path tries `O_CREAT` as a best-effort fallback. That
works where /run/lock is the legacy 1777 (still common outside
Bookworm-derived distros). Where /run/lock is the modern 0775
root:lock, the fallback EACCESes and the error message points at
`civicmesh apply`.
"""

import errno
import fcntl
import os

LOCK_PATH = "/run/lock/civicmesh-mesh.lock"


def acquire_mesh_bot_lock() -> int:
    """Open the pre-created lock file and acquire flock.

    The lock file is created by /etc/tmpfiles.d/civicmesh.conf with
    group=dialout mode 0664; the caller's UID must be in `dialout`
    (which it must be anyway to use the serial port).

    Returns the open fd on success. The caller must keep the fd
    alive for the lifetime of the process; the kernel releases the
    flock on fd close, including SIGKILL.

    Raises RuntimeError if another process already holds the lock,
    or if the file cannot be opened (with a message pointing at
    `civicmesh apply`).
    """
    try:
        fd = os.open(LOCK_PATH, os.O_RDWR)
    except FileNotFoundError:
        # Dev fallback: create the file ourselves. Works where
        # /run/lock is legacy 1777; fails on Bookworm-style 0775.
        try:
            fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o664)
        except OSError as e:
            raise RuntimeError(
                f"CIV-80: {LOCK_PATH} does not exist and cannot be "
                f"created: {e}. On Debian Bookworm / Raspberry Pi OS, "
                f"/run/lock is root:lock 0775, so the file must be "
                f"pre-created by /etc/tmpfiles.d/civicmesh.conf. Run "
                f"`sudo civicmesh apply` to install it, or manually: "
                f"`sudo install -m 0664 -o root -g dialout /dev/null "
                f"{LOCK_PATH}`."
            ) from e
    except PermissionError as e:
        raise RuntimeError(
            f"CIV-80: cannot open {LOCK_PATH}: {e}. The file should "
            f"be mode 0664 group=dialout, and the current user must "
            f"be in `dialout`. Verify with `ls -l {LOCK_PATH}` and "
            f"`id`. If wrong, re-run `sudo civicmesh apply`."
        ) from e

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise RuntimeError(
                f"CIV-80: another civicmesh-mesh is already running "
                f"(lock held on {LOCK_PATH}). "
                "If you started a dev mesh_bot alongside the installed "
                "service, stop the service first: "
                "`sudo systemctl stop civicmesh-mesh`. "
                f"To see the holding pid: `lsof {LOCK_PATH}`."
            ) from e
        raise RuntimeError(
            f"CIV-80: failed to flock {LOCK_PATH}: {e}"
        ) from e

    return fd
