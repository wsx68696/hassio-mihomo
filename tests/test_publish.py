import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import BUNDLE_NAME
from publish_release import verify_reports


class PublishTests(unittest.TestCase):
    def fixture(self, folder):
        build = {"backup_contains_only_app": True, "repository_cache_has_no_credentials": True}
        network = {"local_output_proxied": True, "forwarded_container_output_proxied": True}
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
        for name, data in [("build-report.json", build), ("network-test.json", network), ("vm-test.json", vm)]:
            (folder / name).write_text(json.dumps(data), encoding="utf-8")
        (folder / BUNDLE_NAME).write_bytes(b"app installer fixture")
        return vm

    def test_valid_original_haos_installation_is_publishable(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.fixture(folder)
            verify_reports(folder)

    def test_an_unsupported_reason_blocks_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vm = self.fixture(folder)
            vm["after_ota"]["unsupported"] = ["software"]
            (folder / "vm-test.json").write_text(json.dumps(vm), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_reports(folder)

    def test_a_system_image_cannot_be_distributed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.fixture(folder)
            (folder / "haos.qcow2.xz").write_bytes(b"test system disk")
            with self.assertRaises(ValueError):
                verify_reports(folder)
