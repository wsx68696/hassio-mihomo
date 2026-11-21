import copy
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ADDON = Path(__file__).resolve().parents[1] / "mihomo"
sys.path.insert(0, str(ADDON))
from config import atomic_json, load_profile, make_config, PRIVATE_ROUTES
from networking import NetworkOwner, INPUT_MARK, OUTPUT_MARK
from policies import normalize_policy, prepare_providers
from run import Controller, MihomoAPIError, handler_for, stop_child, RELOAD


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.options = json.loads((ADDON / "config.json").read_text(encoding="utf-8"))["options"]
        self.controller = Controller(self.data, self.options)
        RELOAD.clear()

    def tearDown(self):
        self.temporary.cleanup()

    def test_secret_change_preserves_panel_subscription_and_dns(self):
        self.controller.update({"subscription_url": "https://example.test/sub", "dns_servers": ["1.1.1.1"], "direct_domains": ["local.test"]})
        changed = {**self.options, "controller_secret": "new-secret"}
        restarted = Controller(self.data, changed)
        self.assertEqual(restarted.profile["subscription_url"], "https://example.test/sub")
        self.assertEqual(restarted.profile["dns_servers"], ["1.1.1.1"])
        self.assertEqual(restarted.profile["direct_domains"], ["local.test"])
        self.assertEqual(restarted.secret, "new-secret")

    def test_one_ha_option_change_does_not_reset_other_panel_values(self):
        self.controller.update({"subscription_url": "https://example.test/sub", "dns_servers": ["1.1.1.1"]})
        changed = {**self.options, "tun_enable": False}
        profile = load_profile(self.data, changed)
        self.assertFalse(profile["tun_enable"])
        self.assertEqual(profile["dns_servers"], ["1.1.1.1"])
        self.assertEqual(profile["subscription_url"], "https://example.test/sub")

    @unittest.skipIf(os.name == "nt", "HAOS uses POSIX concurrent read/rename semantics")
    def test_parallel_atomic_writers_never_share_a_temporary_file(self):
        path = self.data / "state.json"
        def write(value):
            atomic_json(path, {"value": value, "payload": "x" * 8192})
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["payload"], "x" * 8192)
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write, range(80)))
        self.assertFalse(list(self.data.glob("state.json.*.tmp")))

    def test_empty_invalid_and_non_object_api_results_are_rejected(self):
        for content in (b"", b"not-json", b"[]", b'{"mode":"rule","tun":null}'):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = content
            with self.subTest(content=content), patch.object(self.controller.http, "open", return_value=response):
                with self.assertRaises(MihomoAPIError):
                    self.controller.api("/configs")

    def test_normal_exit_and_forced_exit_both_clean_owned_network(self):
        for already_exited in (False, True):
            process = MagicMock()
            process.poll.return_value = 0 if already_exited else None
            ownership = MagicMock()
            stop_child(process, ownership)
            ownership.cleanup.assert_called_once()
            if not already_exited:
                process.terminate.assert_called_once()

    def test_old_owned_table_is_recognized_and_foreign_marker_wins(self):
        atomic_json(self.data / "config.json", {"tun": {"device": "ha-mihomo", "auto-redirect-output-mark": OUTPUT_MARK}})
        owner = NetworkOwner(self.data)
        document = {"nftables": [{"table": {"family": "inet", "name": "mihomo"}}, {"rule": {
            "expr": [{"mangle": {"key": {"meta": {"key": "mark"}}, "value": OUTPUT_MARK}}]}}]}
        self.assertTrue(owner.owns(document))
        foreign = copy.deepcopy(document)
        foreign["nftables"].append({"chain": {"name": "haos_mihomo_" + "b" * 32}})
        self.assertFalse(owner.owns(foreign))

    def test_unclaimed_foreign_table_with_similar_marks_is_not_owned(self):
        owner = NetworkOwner(self.data)
        document = {"nftables": [{"rule": {"expr": [
            {"mangle": {"key": {"meta": {"key": "mark"}}, "value": INPUT_MARK}},
            {"mangle": {"key": {"meta": {"key": "mark"}}, "value": OUTPUT_MARK}}]}}]}
        self.assertFalse(owner.owns(document))
        owner.claimed = True
        self.assertTrue(owner.owns(document))

    def test_update_and_profile_arrays_return_json_errors(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.controller))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with patch("run.trusted_ingress", return_value=True):
            thread.start()
            try:
                for path, body in [("/profile", b"[]"), ("/update", b"[]"), ("/update", b'{"component":[]}')]:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertIn("error", json.loads(response.read()))
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_reserved_update_cannot_be_reserved_twice(self):
        self.controller.updates.reserve("geodata")
        with self.assertRaises(ValueError):
            self.controller.updates.reserve("zashboard")

    def test_imported_rules_groups_and_geodata_are_in_actual_config(self):
        policy = normalize_policy(json.dumps({"proxies": [{"name": "node", "type": "socks5", "server": "127.0.0.1", "port": 1080}],
            "proxy-groups": [{"name": "SELECT", "type": "select", "proxies": ["node"]}],
            "rules": ["GEOSITE,cn,SELECT", "GEOIP,CN,SELECT", "RULE-SET,ads,REJECT", "MATCH,DIRECT"],
            "rule-providers": {"ads": {"type": "inline", "behavior": "domain", "payload": ["+.ads.test"]}},
            "geodata-mode": True, "external-controller": "0.0.0.0:9999", "tun": {"enable": False}}).encode())
        config = make_config(self.controller.profile, self.controller.secret, self.data, policy=policy, start_tun=True)
        self.assertEqual(config["proxy-groups"], policy["proxy-groups"])
        self.assertEqual(config["rule-providers"], policy["rule-providers"])
        self.assertEqual(config["rules"][-4:], policy["rules"])
        self.assertEqual(config["external-controller"], "0.0.0.0:9090")
        self.assertTrue(config["tun"]["enable"])
        self.assertEqual(config["tun"]["route-exclude-address"], PRIVATE_ROUTES)
        for network in PRIVATE_ROUTES:
            self.assertTrue(any(rule.endswith("," + network + ",DIRECT,no-resolve") for rule in config["rules"]))

    def test_explicit_empty_rules_do_not_add_hidden_match_proxy(self):
        policy = normalize_policy(b'{"proxies":[{"name":"node","type":"socks5","server":"127.0.0.1","port":1080}],"rules":[]}')
        config = make_config(self.controller.profile, self.controller.secret, self.data, policy=policy)
        self.assertFalse(any(rule.startswith("MATCH,") for rule in config["rules"]))

    def test_provider_cannot_read_controller_secret_or_traverse_paths(self):
        for path in ("../controller-secret", "/data/controller-secret", "../../etc/passwd"):
            policy = normalize_policy(json.dumps({"rules": ["RULE-SET,x,DIRECT"], "rule-providers": {"x": {
                "type": "file", "behavior": "domain", "path": path}}}).encode())
            with self.subTest(path=path), self.assertRaises(ValueError):
                prepare_providers(policy, self.data, lambda *_: b"")
        self.assertFalse(list(self.data.glob("policy-assets-*")))
