"""A failed Geo download or reload must preserve the entire previous dataset."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ADDON = Path(__file__).resolve().parents[1] / "mihomo"
sys.path.insert(0, str(ADDON))
from config import GEO_FILES, GEO_ASSETS, atomic_json
from geodata import atomic_link, prepare_geodata
from run import Controller


@unittest.skipIf(os.name == "nt", "HAOS data pointers use POSIX symlinks")
class GeoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.old = self.data / "geodata-versions/old"
        self.old.mkdir(parents=True)
        for name in GEO_FILES.values():
            (self.old / name).write_bytes(b"old-" + name.encode())
        hashes = {name: hashlib.sha256((self.old / name).read_bytes()).hexdigest() for name in GEO_FILES.values()}
        atomic_json(self.old / "manifest.json", {"version": "old", "sha256": hashes})
        self.confirmed = {"version": "old", "path": str(self.old), "assets": hashes}
        atomic_json(self.data / "software.json", {"geodata": self.confirmed})
        atomic_link(self.data / "geo-current", self.old)
        for name in GEO_FILES.values():
            atomic_link(self.data / name, self.data / "geo-current" / name)
        options = json.loads((ADDON / "config.json").read_text(encoding="utf-8"))["options"]
        self.controller = Controller(self.data, options)
        self.updates = self.controller.updates
        self.raw = {name: b"new-" + name.encode() for name in GEO_FILES.values()}
        self.release = {"tag_name": "latest", "published_at": "2026-11-21T00:00:00Z", "assets": [
            {"name": GEO_ASSETS[key], "digest": "sha256:" + hashlib.sha256(self.raw[name]).hexdigest(),
             "browser_download_url": "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/" + GEO_ASSETS[key]}
            for key, name in GEO_FILES.items()]}

    def tearDown(self):
        self.temporary.cleanup()

    def download(self, url, *_, **__):
        key = next(key for key in GEO_FILES if url.endswith("/" + GEO_ASSETS[key]))
        return self.raw[GEO_FILES[key]], "direct"

    def assert_old(self):
        self.assertEqual((self.data / "geo-current").resolve(), self.old)
        for name in GEO_FILES.values():
            self.assertEqual((self.data / name).read_bytes(), b"old-" + name.encode())
        self.assertEqual(json.loads((self.data / "software.json").read_text())["geodata"], self.confirmed)

    def test_one_bad_file_preserves_all_old_files(self):
        self.raw["GeoSite.dat"] = b"corrupt"
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", side_effect=self.download):
            with self.assertRaises(ValueError):
                self.updates.install("geodata")
        self.assert_old()
        self.assertIsNone(self.updates.pending)
        self.assertFalse(self.updates.reserved)

    def test_switch_is_confirmed_only_by_the_reloaded_process_and_can_roll_back(self):
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", side_effect=self.download), \
                patch("updates.validate_geodata", return_value=True):
            self.assertEqual(self.updates.install("geodata"), "reload")
        self.assertFalse(self.updates.confirm(running_update="old-process"))
        self.assertNotEqual((self.data / "geo-current").resolve(), self.old)
        self.assertTrue(self.updates.rollback())
        self.assert_old()

    def test_process_death_before_confirmation_recovers_committed_data(self):
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", side_effect=self.download), \
                patch("updates.validate_geodata", return_value=True):
            self.updates.install("geodata")
        prepare_geodata(self.data, self.old, self.confirmed)
        self.assert_old()

    def test_confirmed_dataset_and_schedule_survive_restart(self):
        self.controller.update({"geodata_auto_update": True, "geodata_update_interval": 12})
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", side_effect=self.download), \
                patch("updates.validate_geodata", return_value=True):
            self.updates.install("geodata")
        self.assertTrue(self.updates.confirm(running_update=self.updates.pending["id"]))
        saved = json.loads((self.data / "software.json").read_text())["geodata"]
        prepare_geodata(self.data, self.old, saved)
        options = json.loads((ADDON / "config.json").read_text())["options"]
        restarted = Controller(self.data, options)
        self.assertEqual(restarted.updates.snapshot()["installed"]["geodata"], self.release["published_at"])
        self.assertTrue(restarted.profile["geodata_auto_update"])
        self.assertEqual(restarted.profile["geodata_update_interval"], 12)

    def test_collection_keeps_current_previous_and_unknown_directories(self):
        orphan = self.data / ("geodata-versions/" + "f" * 24 + "-" + "e" * 8)
        orphan.mkdir()
        (orphan / ".ha-mihomo-geodata").write_text("managed-geodata-v1\n")
        unknown = self.data / "geodata-versions/user-backup"
        unknown.mkdir()
        with patch.object(self.updates, "release", return_value=self.release), patch("updates.fetch", side_effect=self.download), \
                patch("updates.validate_geodata", return_value=True):
            self.updates.install("geodata")
        current = (self.data / "geo-current").resolve()
        self.assertTrue(self.updates.confirm(running_update=self.updates.pending["id"]))
        self.assertTrue(current.exists())
        self.assertTrue(self.old.exists())
        self.assertTrue(unknown.exists())
        self.assertFalse(orphan.exists())

    def test_zashboard_failed_pointer_switch_retains_previous_directory(self):
        old_ui = self.data / "ui"
        old_ui.mkdir()
        (old_ui / "index.html").write_text("old dashboard")
        atomic_link(self.data / "ui-current", old_ui)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("index.html", "<html>new dashboard</html>")
        raw = stream.getvalue()
        release = ("3.30.0", "https://github.com/fixture.zip", hashlib.sha256(raw).hexdigest())
        def switch(path, target):
            if "ui-versions" in str(target):
                raise OSError("injected pointer switch failure")
            return atomic_link(path, target)
        with patch.object(self.updates, "release", return_value=release), patch("updates.fetch", return_value=(raw, "direct")), \
                patch("updates.atomic_link", side_effect=switch):
            with self.assertRaises(OSError):
                self.updates.install("zashboard")
        self.assertEqual((self.data / "ui-current").resolve(), old_ui)
        self.assertEqual((old_ui / "index.html").read_text(), "old dashboard")


if __name__ == "__main__":
    unittest.main()
