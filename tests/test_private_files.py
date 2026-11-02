from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.platform.private_files import private_write


class PrivateFileTests(unittest.TestCase):
    def test_replacement_publishes_private_bytes_without_inheriting_old_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b"old data")
            private_write(path, b"private data")
            if os.name == "nt":
                from infra_sentinel.resources.facilities.windows_security import read_private_file
                self.assertEqual(read_private_file(path.resolve(), 128), b"private data")
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_bytes(), b"private data")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_publication_keeps_old_file_and_removes_unpublished_private_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            private_write(path, b"old data")
            with patch.object(Path, "replace", side_effect=PermissionError("in use")):
                with self.assertRaises(PermissionError):
                    private_write(path, b"new private data")
            self.assertEqual(path.read_bytes(), b"old data")
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
