import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


class BootstrapProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "kubeconfig"
        self.config = {
            "apiVersion": "v1", "kind": "Config", "current-context": "selected-context",
            "contexts": [
                {"name": "other-context", "context": {"cluster": "other", "user": "other-user"}},
                {"name": "selected-context", "context": {"cluster": "selected", "user": "selected-user", "namespace": "default"}},
            ],
            "clusters": [
                {"name": "other", "cluster": {"server": "https://other.example.invalid", "proxy-url": "http://unchanged.invalid"}},
                {"name": "selected", "cluster": {"server": "https://selected.private.example.invalid:443",
                    "certificate-authority-data": "cHVibGljLWNh", "tls-server-name": "selected.private.example.invalid",
                    "insecure-skip-tls-verify": False}},
            ],
            "users": [{"name": "selected-user", "user": {
                "exec": {"command": "kubelogin", "args": ["get-token", "--login", "azurecli"]}}}],
        }

    def apply(self, proxy="socks5://127.0.0.1:1080", hostname="selected.private.example.invalid"):
        self.path.write_text(json.dumps(self.config))
        return bootstrap.configure_kubernetes_proxy(self.path, proxy, "selected", hostname)

    def test_only_explicit_loopback_urls_and_valid_ports(self):
        for value in [None, "socks5://127.0.0.1:1", "socks5://127.0.0.1:65535", "socks5://[::1]:1080"]:
            with self.subTest(value=value):
                self.assertEqual(bootstrap.kubernetes_proxy_url(value), value)
        for value in ["", "http://127.0.0.1:1080", "socks5h://127.0.0.1:1080",
                      "socks5://localhost:1080", "socks5://0.0.0.0:1080", "socks5://10.0.0.1:1080",
                      "socks5://external.invalid:1080", "socks5://[::]:1080", "socks5://127.0.0.1",
                      "socks5://127.0.0.1:0", "socks5://127.0.0.1:65536", "socks5://127.0.0.1:-1",
                      "socks5://127.0.0.1:01", "socks5://127.0.0.1:abc", "socks5://u:p@127.0.0.1:1080",
                      "socks5://127.0.0.1:1080/", "socks5://127.0.0.1:1080?x=1", "socks5://127.0.0.1:1080#x",
                      " socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080\n", 1080]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bootstrap.kubernetes_proxy_url(value)

    def test_default_does_not_parse_or_rewrite_file(self):
        self.path.write_text("unchanged opaque fixture")
        bootstrap.configure_kubernetes_proxy(self.path, None, "selected", None)
        self.assertEqual(self.path.read_text(), "unchanged opaque fixture")

    def test_updates_only_selected_cluster_preserving_tls_users_and_context(self):
        expected = copy.deepcopy(self.config)
        expected["clusters"][1]["cluster"]["proxy-url"] = "socks5://[::1]:1080"
        self.apply("socks5://[::1]:1080")
        self.assertEqual(json.loads(self.path.read_text()), expected)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_current_context_must_uniquely_select_requested_cluster(self):
        for case in ["missing", "other", "duplicate", "wrong-reference"]:
            with self.subTest(case=case):
                saved = copy.deepcopy(self.config)
                if case == "missing": self.config.pop("current-context")
                elif case == "other": self.config["current-context"] = "other-context"
                elif case == "duplicate": self.config["contexts"].append(copy.deepcopy(self.config["contexts"][1]))
                else: self.config["contexts"][1]["context"]["cluster"] = "other"
                before = json.dumps(self.config)
                with self.assertRaises(ValueError): self.apply()
                self.assertEqual(self.path.read_text(), before)
                self.config = saved

    def test_selected_cluster_must_be_unique(self):
        self.config["clusters"].append(copy.deepcopy(self.config["clusters"][1]))
        with self.assertRaisesRegex(ValueError, "exactly one"): self.apply()

    def test_api_address_must_match_actual_private_cluster(self):
        for server in ["http://selected.private.example.invalid",
                       "https://wrong.private.example.invalid",
                       "https://selected.private.example.invalid:8443",
                       "https://user@selected.private.example.invalid",
                       "https://selected.private.example.invalid/path",
                       "https://selected.private.example.invalid?q=1"]:
            with self.subTest(server=server):
                self.config["clusters"][1]["cluster"]["server"] = server
                before = json.dumps(self.config)
                with self.assertRaises(ValueError): self.apply()
                self.assertEqual(self.path.read_text(), before)

    def test_missing_private_hostname_and_unsafe_tls_are_rejected(self):
        with self.assertRaises(ValueError): self.apply(hostname=None)
        for field, value in [("insecure-skip-tls-verify", True),
                             ("tls-server-name", "different.invalid"),
                             ("tls-server-name", 42),
                             ("certificate-authority-data", "")]:
            with self.subTest(field=field):
                saved = copy.deepcopy(self.config)
                self.config["clusters"][1]["cluster"][field] = value
                with self.assertRaisesRegex(ValueError, "certificate"): self.apply()
                self.config = saved

    def test_cli_rejects_option_on_other_bootstrap_commands(self):
        argv = ["bootstrap.py", "identity", "--environment", "pprd", "--region", "uks",
                "--slot", "aks01", "--kubernetes-proxy-url", "socks5://127.0.0.1:1080"]
        with patch("sys.argv", argv), patch.object(bootstrap, "run") as command:
            with self.assertRaisesRegex(ValueError, "only supported"):
                bootstrap.main()
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
