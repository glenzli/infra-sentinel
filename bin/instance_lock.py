#!/usr/bin/env python3
"""Small cross-platform single-instance lock for the local Infra Agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


def acquire_process_lock(path: Path) -> BinaryIO | None:
    """Acquire a non-blocking process lock and retain it through the handle.

    The lock file contains only the current process id.  Callers must keep the
    returned handle open for as long as they own the process instance.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    handle.seek(1)
    handle.truncate()
    handle.write(f"{os.getpid()}\n".encode("ascii"))
    handle.flush()
    return handle
