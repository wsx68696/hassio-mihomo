import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from common import IMAGE, APP_SLUG, REPOSITORY_ID
from image_identity import saved_image_identity
from package_installer import write_backup, add_bytes


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.image = self.folder / "image.tar"
        with tarfile.open(self.image, "w:") as archive:
            add_bytes(archive, "config.json", json.dumps({"architecture": "amd64", "os": "linux",
                      "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "b" * 64]}}).encode())
            add_bytes(archive, "manifest.json", json.dumps([{"Config": "config.json", "RepoTags": [IMAGE], "Layers": ["layer.tar"]}]).encode())
        with self.image.open("rb") as stream:
            self.expected = saved_image_identity(stream, IMAGE)

    def tearDown(self):
        self.temporary.cleanup()

    def test_installer_contains_only_app_image_defaults_and_policy(self):
        output = self.folder / "backup.tar"
        slug = write_backup(output, self.image, self.expected, "2026.11.0", "a" * 40)
        self.assertEqual(len(slug), 8)
        with tarfile.open(output) as outer:
            self.assertEqual(set(outer.getnames()), {"./backup.json", "./" + APP_SLUG + ".tar.gz"})
            metadata = json.load(outer.extractfile("./backup.json"))
            self.assertEqual(metadata["type"], "partial")
            self.assertIsNone(metadata["homeassistant"])
            self.assertEqual(metadata["folders"], [])
            self.assertEqual(metadata["repositories"], [])
            self.assertEqual(metadata["addons"][0]["slug"], APP_SLUG)
            with tarfile.open(fileobj=outer.extractfile("./" + APP_SLUG + ".tar.gz"), mode="r:gz") as inner:
                self.assertEqual(set(inner.getnames()), {"./addon.json", "./apparmor.txt", "./image.tar"})
                data = json.load(inner.extractfile("./addon.json"))
                self.assertEqual(data["system"]["repository"], REPOSITORY_ID)
                self.assertEqual(data["user"]["options"], {})
                self.assertTrue(data["user"]["protected"])
                self.assertEqual(data["user"]["boot"], "auto")
                self.assertEqual(data["system"]["options"]["controller_secret"], "")
                self.assertEqual(data["system"]["options"]["subscription_url"], "")
                self.assertEqual(inner.extractfile("./image.tar").read(), self.image.read_bytes())

    def test_changed_image_cannot_be_packaged(self):
        with self.assertRaises(ValueError):
            write_backup(self.folder / "bad.tar", self.image, {**self.expected, "config_sha256": "bad"}, "2026.11.0", "a" * 40)

    def test_installer_needs_an_exact_source_revision(self):
        with self.assertRaises(ValueError):
            write_backup(self.folder / "bad.tar", self.image, self.expected, "2026.11.0", "main")
