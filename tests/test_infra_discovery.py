from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_discovery import (  # noqa: E402
    DiscoveryError,
    LeaseExpired,
    discovery_paths,
    read_registration,
    resolve_unix_socket,
    runtime_root,
    validate_runtime_paths,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def manifest(*, protocol: str = "pcp.runtime.observer", expires: int = 45) -> dict[str, object]:
    return {
        "schema": "infra.discovery.registration",
        "schema_version": "20260810.1",
        "service": {"kind": "pcp", "instance_id": "local", "generation": "gen-1"},
        "lease": {
            "renewed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(seconds=expires)).isoformat(),
        },
        "offers": [{
            "protocol": protocol,
            "protocol_versions": ["20260810.1", "v2"],
            "binding": "infra.local.unix-socket",
            "endpoint": "sockets/pcp-gen-1.sock",
        }],
    }


def write_manifest(root: Path, value: dict[str, object]) -> Path:
    path = root / "registrations" / "pcp--local.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


class InfraDiscoveryTests(unittest.TestCase):
    def test_platform_roots_and_absolute_override(self) -> None:
        self.assertEqual(
            runtime_root(platform="darwin", environment={}, darwin_user_temp_dir=Path("/private/var/user")),
            Path("/private/var/user/infra-protocol"),
        )
        self.assertEqual(
            runtime_root(platform="linux", environment={"XDG_RUNTIME_DIR": "/run/user/501"}),
            Path("/run/user/501/infra-protocol"),
        )
        self.assertEqual(
            runtime_root(platform="darwin", environment={"INFRA_PROTOCOL_RUNTIME_DIR": "/shared/infra"}),
            Path("/shared/infra"),
        )
        with self.assertRaises(DiscoveryError):
            runtime_root(platform="linux", environment={"INFRA_PROTOCOL_RUNTIME_DIR": "relative"})

    def test_registration_is_strict_and_matches_exact_offer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "registrations").mkdir(mode=0o700)
            (root / "sockets").mkdir(mode=0o700)
            path = write_manifest(root, manifest())

            registration = read_registration(path, now=NOW)

            matches = registration.compatible_offers(
                "pcp.runtime.observer",
                ["v2", "20260810.1"],
                ["infra.local.unix-socket"],
            )
            self.assertEqual(matches[0][1], "v2")
            self.assertEqual(
                resolve_unix_socket(discovery_paths(root), matches[0][0]),
                root / "sockets" / "pcp-gen-1.sock",
            )

    def test_unknown_protocol_is_structurally_valid_but_not_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "registrations").mkdir(mode=0o700)
            (root / "sockets").mkdir(mode=0o700)
            registration = read_registration(
                write_manifest(root, manifest(protocol="vendor.future/status")),
                now=NOW,
            )
            self.assertEqual(
                registration.compatible_offers(
                    "pcp.runtime.observer",
                    ["20260810.1"],
                    ["infra.local.unix-socket"],
                ),
                [],
            )

    def test_expired_lease_is_available_only_for_stale_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "registrations").mkdir(mode=0o700)
            (root / "sockets").mkdir(mode=0o700)
            value = manifest(expires=45)
            path = write_manifest(root, value)
            later = NOW + timedelta(seconds=60)
            with self.assertRaises(LeaseExpired):
                read_registration(path, now=later)
            self.assertEqual(read_registration(path, now=later, require_live=False).generation, "gen-1")

    def test_permissions_and_filename_are_part_of_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "registrations").mkdir(mode=0o700)
            (root / "sockets").mkdir(mode=0o700)
            validate_runtime_paths(discovery_paths(root))
            path = write_manifest(root, manifest())
            path.chmod(0o644)
            with self.assertRaises(DiscoveryError):
                read_registration(path, now=NOW)


if __name__ == "__main__":
    unittest.main()
