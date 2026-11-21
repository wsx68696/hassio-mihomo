import importlib.util
import ipaddress
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ADDON = Path(__file__).resolve().parents[1] / "mihomo"
sys.path.insert(0, str(ADDON))
from config import PRIVATE_ROUTES, load_profile, make_config, trusted_ingress
from run import Controller, RELOAD


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.options = json.loads((ADDON / "config.json").read_text(encoding="utf-8"))["options"]
        RELOAD.clear()

    def tearDown(self):
        self.temporary.cleanup()

    def test_startup_imports_own_modules_with_isolated_python_paths(self):
        script = "import runpy; runpy.run_path(" + repr(str(ADDON / "run.py")) + ",run_name='verify_imports')"
        subprocess.run([sys.executable, "-I", "-c", script], check=True, capture_output=True)

    def test_local_discovery_docker_dns_and_ipv6_stay_direct(self):
        networks = [ipaddress.ip_network(value) for value in PRIVATE_ROUTES]
        for address in ["192.168.1.1", "172.30.32.3", "172.30.232.1", "10.0.0.20", "224.0.0.251",
                        "239.255.255.250", "127.0.0.53", "100.64.1.2", "fe80::1", "fd12::3", "fec0::3", "ff02::fb"]:
            peer = ipaddress.ip_address(address)
            self.assertTrue(any(peer.version == network.version and peer in network for network in networks), address)
        public = ipaddress.ip_address("198.51.100.3")
        self.assertFalse(any(public.version == network.version and public in network for network in networks))
        config = make_config(load_profile(self.data, self.options), "test", self.data)
        self.assertTrue(config["ipv6"])
        self.assertTrue(config["dns"]["ipv6"])
        self.assertEqual(config["tun"]["dns-mode"], "disabled")
        self.assertFalse(config["tun"]["strict-route"])

    def test_missing_nodes_never_enable_tun(self):
        self.options["subscription_url"] = "https://example.invalid/subscription"
        profile = load_profile(self.data, self.options)
        config = make_config(profile, "test", self.data, start_tun=True)
        self.assertFalse(config["tun"]["enable"])
        self.assertNotIn("proxy-providers", config)
        self.assertEqual(config["proxy-groups"][0]["proxies"], ["DIRECT"])

    def test_cached_nodes_and_settings_survive_restart(self):
        (self.data / "nodes.yaml").write_text('{"proxies": [{"name": "node", "type": "socks5", "server": "127.0.0.1", "port": 1080}]}')
        controller = Controller(self.data, self.options)
        secret = controller.secret
        controller.save_runtime({"mode": "global", "tun": {"enable": False}}, True)
        restarted = Controller(self.data, self.options)
        self.assertEqual(secret, restarted.secret)
        self.assertEqual(restarted.profile["mode"], "global")
        self.assertFalse(restarted.profile["tun_enable"])
        config = make_config(restarted.profile, restarted.secret, self.data)
        self.assertEqual(config["proxy-providers"]["subscription"]["type"], "file")
        self.assertEqual(config["mixed-port"], 17890)
        self.assertEqual(config["bind-address"], "127.0.0.1")

    def test_bad_replacement_preserves_working_nodes(self):
        old = b'{"proxies": [{"name":"working"}]}'
        (self.data / "nodes.yaml").write_bytes(old)
        controller = Controller(self.data, self.options)
        with patch("run.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(ValueError):
                controller.upload_nodes(b'{"proxies":[{"name":"invalid","type":"not-a-real-proxy"}]}')
        self.assertEqual((self.data / "nodes.yaml").read_bytes(), old)
        self.assertFalse(list(self.data.glob("incoming-*")))

    def test_subscription_change_cannot_commit_previous_download(self):
        old = b'{"proxies":[{"name":"old","type":"socks5","server":"127.0.0.1","port":1080}]}'
        (self.data / "nodes.yaml").write_bytes(old)
        controller = Controller(self.data, self.options)
        controller.update({"subscription_url": "https://new.invalid/subscription"})
        with patch("run.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            with self.assertRaises(ValueError):
                controller.upload_nodes(b'{"proxies":[{"name":"stale","type":"socks5","server":"127.0.0.1","port":1080}]}', clear_subscription=False,
                                        expected_url="https://old.invalid/subscription")
        self.assertEqual((self.data / "nodes.yaml").read_bytes(), old)

    def test_requested_tun_change_cannot_be_overwritten_by_inflight_poll(self):
        controller = Controller(self.data, self.options)
        controller.update({"tun_enable": False})
        controller.save_runtime({"mode": "rule", "tun": {"enable": True}}, True)
        self.assertFalse(controller.profile["tun_enable"])

    def test_ingress_credentials_are_not_available_from_lan(self):
        self.assertTrue(trusted_ingress("172.30.32.2"))
        self.assertTrue(trusted_ingress("::ffff:172.30.32.2"))
        for address in ["127.0.0.1", "192.168.1.50", "172.30.32.3", "::1", "malformed"]:
            self.assertFalse(trusted_ingress(address))


if __name__ == "__main__":
    unittest.main()
