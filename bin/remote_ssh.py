"""One-shot SSH transport shared by read-only remote observers."""

from __future__ import annotations

import re
import subprocess
from typing import Sequence


HOST_ALIAS_RE = re.compile(r"[A-Za-z0-9._-]+\Z")


def run_read_only_script(
    ssh_host: str,
    script: str,
    arguments: Sequence[str] = (),
    *,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    """Run a static shell script through a hardened, non-persistent SSH call.

    Callers must validate every positional argument for their own protocol
    before invoking this transport. The SSH host is restricted to a local
    ``~/.ssh/config`` alias so configuration cannot inject SSH options.
    """
    if not HOST_ALIAS_RE.fullmatch(ssh_host):
        raise ValueError("ssh_host 必须是 ssh config 中的主机别名")
    command = [
        "/usr/bin/ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-o", "ControlMaster=no",
        "-o", "ControlPersist=no",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        ssh_host,
        "/bin/sh", "-s", "--", *arguments,
    ]
    try:
        return subprocess.run(
            command,
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"SSH 无法完成：{exc}") from exc
