import json
from pathlib import Path
import sys
import tempfile
import unittest
import os
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import BUNDLE_NAME, IMAGE, APP_SLUG, VERSION
from publish_release import (verify_reports, render_installer, NETWORK_REQUIRED, VM_REQUIRED,
                             preflight, check_tag, publish_image)


class PublishTests(unittest.TestCase):
    def fixture(self, folder):
        build = {"backup_contains_only_app": True, "repository_cache_has_no_credentials": True,
                 "revision": "a" * 40, "version": VERSION, "image": IMAGE, "app_slug": APP_SLUG,
                 "image_content": {"architecture": "amd64", "os": "linux", "config_sha256": "b" * 64,
                     "labels": {"org.opencontainers.image.revision": "a" * 40, "io.hass.version": VERSION}}}
        network = {key: True for key in NETWORK_REQUIRED}
        vm = {
            "core_onboarding_before_install": True, "core_onboarding_after_install": True,
            "core_onboarding_reachable_through_qemu": True,
            "installed_with_public_egress_blocked": True, "core_and_supervisor_containers_unchanged": True,
            "registered_public_update_repository": True, "protected_mode_and_auto_start": True,
            "tun_and_apparmor_on_stock_haos": True, "supervisor_traffic_proxied": True,
            "core_traffic_proxied": True, "haos_traffic_proxied": True,
            "repeat_install_preserves_configuration": True, "mihomo_update_through_existing_proxy": True,
            "official_ota_preserves_app_and_configuration": True, "official_base_image_unchanged": True,
            "no_preloaded_or_overridden_core": True,
            "after_install": {"supported": True, "healthy": True, "unsupported": []},
            "after_ota": {"supported": True, "healthy": True, "unsupported": []},
        }
        vm.update({key: True for key in VM_REQUIRED})
        vm["before_install"] = {"supported": True, "healthy": True, "unsupported": []}
        for name, data in [("build-report.json", build), ("network-test.json", network), ("vm-test.json", vm)]:
            (folder / name).write_text(json.dumps(data), encoding="utf-8")
        (folder / BUNDLE_NAME).write_bytes(b"app installer fixture")
        return vm

    def test_valid_original_haos_installation_is_publishable(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.fixture(folder)
            verify_reports(folder, "a" * 40)

    def test_an_unsupported_reason_blocks_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vm = self.fixture(folder)
            vm["after_ota"]["unsupported"] = ["software"]
            (folder / "vm-test.json").write_text(json.dumps(vm), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_reports(folder, "a" * 40)

    def test_a_system_image_cannot_be_distributed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.fixture(folder)
            (folder / "haos.qcow2.xz").write_bytes(b"test system disk")
            with self.assertRaises(ValueError):
                verify_reports(folder, "a" * 40)

    def test_wrong_revision_missing_phase_and_missing_rule_evidence_block_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for filename, key in (("build-report.json", "revision"), ("vm-test.json", "after_ota"),
                                  ("network-test.json", "geosite_matches")):
                self.fixture(folder)
                data = json.loads((folder / filename).read_text(encoding="utf-8"))
                data.pop(key)
                (folder / filename).write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    verify_reports(folder, "a" * 40)

    def test_release_entry_embeds_one_version_and_exact_package_digest(self):
        source = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
        rendered = render_installer(source, "c" * 64)
        self.assertIn("BUNDLE_SHA256=" + "c" * 64, rendered)
        self.assertIn("INSTALLER_VERSION=" + VERSION, rendered)
        self.assertNotIn("__BUNDLE_SHA256__", rendered)
        with self.assertRaises(ValueError):
            render_installer(source, "invalid")

    def test_an_existing_tag_cannot_move_to_a_new_commit(self):
        with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}), patch("publish_release.tag_revision", return_value="b" * 40):
            with self.assertRaises(ValueError):
                check_tag()

    def test_an_already_published_same_commit_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40, "GITHUB_OUTPUT": str(output)}), \
                    patch("publish_release.tag_revision", return_value="a" * 40), \
                    patch("publish_release.api", return_value={"draft": False}):
                preflight()
            self.assertIn("needed=false", output.read_text(encoding="utf-8"))

    def test_an_existing_image_with_different_content_cannot_be_overwritten(self):
        with patch("publish_release.verify_assets", return_value=({"image_content": {"config_sha256": "a" * 64}}, {})), \
                patch("publish_release.check_tag"), patch("publish_release.registry_manifest", return_value={"config": {"digest": "sha256:" + "b" * 64}}), \
                patch("publish_release.run") as command:
            with self.assertRaises(ValueError):
                publish_image()
            command.assert_not_called()
