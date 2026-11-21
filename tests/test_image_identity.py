import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from image_identity import saved_image_identity


class ImageIdentityTests(unittest.TestCase):
    reference = "ghcr.io/home-assistant/generic-x86-64-homeassistant:test"

    def archive(self, config, *, containerd=False, reference=None):
        content = json.dumps(config, separators=(",", ":")).encode()
        checksum = hashlib.sha256(content).hexdigest()
        name = "blobs/sha256/" + checksum if containerd else checksum + ".json"
        manifest = json.dumps([{"Config": name, "RepoTags": [reference or self.reference], "Layers": ["layer.tar"]}]).encode()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as output:
            # Both orders occur in Docker exports; stream parsing must accept
            # a manifest after its content-addressed configuration.
            entries = [(name, content), ("manifest.json", manifest)]
            if not containerd:
                entries.reverse()
            for path, body in entries:
                entry = tarfile.TarInfo(path)
                entry.size = len(body)
                output.addfile(entry, io.BytesIO(body))
        stream.seek(0)
        return stream

    def config(self):
        return {"architecture": "amd64", "os": "linux", "config": {"Env": ["LANG=C.UTF-8"]},
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "a" * 64]}}

    def test_classic_and_containerd_exports_have_the_same_content_identity(self):
        config = self.config()
        self.assertEqual(saved_image_identity(self.archive(config), self.reference),
                         saved_image_identity(self.archive(config, containerd=True), self.reference))

    def test_changed_configuration_or_layer_is_detected(self):
        original = saved_image_identity(self.archive(self.config()), self.reference)
        changed = self.config()
        changed["config"]["Env"] = ["LANG=modified"]
        self.assertNotEqual(original, saved_image_identity(self.archive(changed), self.reference))
        changed = self.config()
        changed["rootfs"]["diff_ids"] = ["sha256:" + "b" * 64]
        self.assertNotEqual(original, saved_image_identity(self.archive(changed), self.reference))

    def test_another_tag_cannot_satisfy_the_requested_identity(self):
        with self.assertRaises(ValueError):
            saved_image_identity(self.archive(self.config(), reference="other:tag"), self.reference)


if __name__ == "__main__":
    unittest.main()
