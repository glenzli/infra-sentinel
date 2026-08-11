from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.facilities.discovery import (  # noqa: E402
    DiscoveryError,
    DiscoveryOffer,
    discovery_paths,
    read_registration,
    resolve_unix_socket,
    runtime_root,
    validate_runtime_paths,
)


def manifest(*, protocol: str = "pcp.runtime.observer") -> dict[str, object]:
    return {
        "schema": "infra.discovery.registration",
        "schema_version": "20260812.1",
        "service": {"kind": "pcp", "instance_id": "local", "generation": "gen-1"},
        "offers": [{
            "protocol": protocol,
            "protocol_versions": ["20260810.1", "v2"],
            "binding": "infra.local.unix-socket",
            "endpoint": "sockets/pcp-gen1.sock",
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

            registration = read_registration(path)

            matches = registration.compatible_offers(
                "pcp.runtime.observer",
                ["v2", "20260810.1"],
                ["infra.local.unix-socket"],
            )
            self.assertEqual(matches[0][1], "v2")
            self.assertEqual(
                resolve_unix_socket(discovery_paths(root), matches[0][0]),
                root / "sockets" / "pcp-gen1.sock",
            )

    def test_unknown_protocol_is_structurally_valid_but_not_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "registrations").mkdir(mode=0o700)
            (root / "sockets").mkdir(mode=0o700)
            registration = read_registration(
                write_manifest(root, manifest(protocol="vendor.future/status")),
            )
            self.assertEqual(
                registration.compatible_offers(
                    "pcp.runtime.observer",
                    ["20260810.1"],
                    ["infra.local.unix-socket"],
                ),
                [],
            )

    def test_lease_field_is_rejected_in_the_canonical_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "registrations").mkdir(mode=0o700)
            (root / "sockets").mkdir(mode=0o700)
            value = manifest()
            value["lease"] = {
                "renewed_at": "2026-08-12T08:00:00Z",
                "expires_at": "2026-08-12T08:00:45Z",
            }
            path = write_manifest(root, value)
            with self.assertRaisesRegex(DiscoveryError, "unsupported fields"):
                read_registration(path)

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
                read_registration(path)

    def test_unix_endpoint_uses_short_opaque_and_final_path_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "registrations").mkdir(mode=0o700)
            (root / "sockets").mkdir(mode=0o700)
            value = manifest()
            value["offers"][0]["endpoint"] = "sockets/opaque-name-is-too-long.sock"  # type: ignore[index]
            with self.assertRaisesRegex(DiscoveryError, "Unix endpoint"):
                read_registration(write_manifest(root, value))

        long_root = Path("/" + "x" * 110)
        offer = DiscoveryOffer(
            "pcp.runtime.observer",
            ("20260810.1",),
            "infra.local.unix-socket",
            "sockets/short.sock",
        )
        with self.assertRaisesRegex(DiscoveryError, "requires"):
            resolve_unix_socket(discovery_paths(long_root), offer)


if __name__ == "__main__":
    unittest.main()
