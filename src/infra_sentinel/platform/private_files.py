"""Atomic private-file publication with native permissions on each host."""

from __future__ import annotations

import os
from pathlib import Path
import uuid


def private_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    created = False
    try:
        if os.name == "nt":
            from .windows_security import open_private_file
            stream = open_private_file(temporary)
        else:
            stream = os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        created = True
        with stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
