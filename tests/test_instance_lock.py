from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from instance_lock import acquire_process_lock  # noqa: E402


class InstanceLockTests(unittest.TestCase):
    def test_second_owner_is_rejected_until_first_handle_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent.lock"
            first = acquire_process_lock(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(acquire_process_lock(path))
                self.assertEqual(path.read_bytes()[1:].decode("ascii").strip(), str(__import__("os").getpid()))
            finally:
                assert first is not None
                first.close()
            second = acquire_process_lock(path)
            self.assertIsNotNone(second)
            assert second is not None
            second.close()


if __name__ == "__main__":
    unittest.main()
