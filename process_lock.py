"""Cooperative startup lock for civicmesh-mesh (CIV-80).

Prevents two civicmesh-mesh processes from running against one radio
on the same host (the dev-tree + installed-systemd collision: a
developer runs `uv run civicmesh-mesh` from ~/code/CivicMesh without
first stopping the systemd service, or vice versa).

Mechanism: fcntl.flock(LOCK_EX | LOCK_NB) on a small file in
/run/lock/. The lock is on the open-file-description; the kernel
releases it on fd close (including SIGKILL), so there is no stale-lock
recovery path. The lock fd is held for the process lifetime by being
stashed at module scope in the caller.

Why /run/lock and not the device:
  - Opening /dev/ttyUSB* toggles DTR/RTS via the cp210x driver, which
    on the Heltec V3 is wired to the ESP32 reset/boot circuit (see
    recovery.py:_rts_pulse_action). Locking via a separate file leaves
    the radio untouched until meshcore opens it.
  - /run/lock is mode 1777 (sticky world-writable) on systemd by
    default, so any UID can create files there. The sticky bit means
    you can create, but not remove, other users' files.

Why O_RDONLY:
  - The installed service runs as `civicmesh:civicmesh` and the dev
    process typically as a regular user. The first process to create
    the file owns it (mode 0644 under default umask). A later process
    of a different UID can still open it O_RDONLY and flock — flock
    operates on the open-file-description, not the file's access
    mode. Trying O_RDWR would EACCES for the non-owner.

Cooperative scope: only catches processes that call into this module.
Non-CivicMesh tools (picocom, screen, cat /dev/ttyUSB0) still bypass.
That's the failure-mode-C3 residual; see
docs/radio-debugging/failure-modes.md.
"""

import errno
import fcntl
import os

LOCK_PATH = "/run/lock/civicmesh-mesh.lock"


def acquire_mesh_bot_lock() -> int:
    """Open and exclusive-lock the mesh_bot startup file.

    Returns the open fd on success. The caller must keep the fd alive
    for the lifetime of the process (typically by stashing it at
    module scope). The kernel releases the flock when the fd closes,
    including on SIGKILL, so explicit cleanup is unnecessary.

    Raises RuntimeError if another process already holds the lock, or
    if the file cannot be opened at all.
    """
    try:
        fd = os.open(LOCK_PATH, os.O_RDONLY | os.O_CREAT, 0o644)
    except OSError as e:
        raise RuntimeError(
            f"CIV-80: cannot open lock file {LOCK_PATH}: {e}. "
            "Expected /run/lock to exist and be world-writable (mode 1777). "
            "On systemd this is the default; verify with `ls -ld /run/lock`."
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
