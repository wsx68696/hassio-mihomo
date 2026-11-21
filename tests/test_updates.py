"""Update downloads must not destroy the only working proxy."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ADDON = Path(__file__).resolve().parents[1] / "mihomo"
sys.path.insert(0, str(ADDON))
from run import Controller


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        options = json.loads((ADDON / "config.json").read_text(encoding="utf-8"))["options"]
        self.controller = Controller(self.data, options)
        self.updates = self.controller.updates
        self.raw = gzip.compress(b"\x7fELFtest-binary")
        self.release = ("1.19.34", "https://github.com/fixture.gz", hashlib.sha256(self.raw).hexdigest())

    def tearDown(self):
        self.temporary.cleanup()

    def test_bad_download_cannot_replace_the_working_binary(self):
        previous = self.controller.binary
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", return_value=(b"bad hash", "proxy")):
            with self.assertRaises(ValueError):
                self.updates.install("mihomo")
        self.assertEqual(previous, self.controller.binary)
        self.assertIsNone(self.updates.pending)
        self.assertFalse((self.data / "software.json").exists())

    def test_invalid_config_cannot_replace_the_working_binary(self):
        previous = self.controller.binary
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", return_value=(self.raw, "proxy")), patch("updates.subprocess.run", side_effect=[SimpleNamespace(returncode=0, stdout=b"Mihomo v1.19.34"), SimpleNamespace(returncode=1)]):
            with self.assertRaises(ValueError):
                self.updates.install("mihomo")
        self.assertEqual(previous, self.controller.binary)
        self.assertFalse(list((self.data / "bin").glob("mihomo-incoming-*")))

    def test_new_binary_is_persisted_only_after_startup_confirmation(self):
        previous = self.controller.binary
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", return_value=(self.raw, "proxy")), patch("updates.validate_runtime", return_value=True), patch("updates.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"Mihomo v1.19.34")):
            self.assertEqual(self.updates.install("mihomo"), "reload")
        self.assertNotEqual(previous, self.controller.binary)
        self.assertFalse((self.data / "software.json").exists())
        self.assertTrue(self.updates.rollback())
        self.assertEqual(previous, self.controller.binary)
        self.assertEqual(json.loads((self.data / "software.json").read_text(encoding="utf-8")), {})


if __name__ == "__main__":
    unittest.main()
