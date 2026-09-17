"""Real loopback TLS qualification; Kubernetes transport is stubbed, not TLS."""

from contextlib import contextmanager
from copy import deepcopy
import http.server
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_service as verification


def cluster_status():
    gateway = {
        "metadata": {"generation": 3},
        "status": {
            "conditions": [dict(type="Programmed", status="True", observedGeneration=3)]
        },
    }
    references = [
        dict(name="private", sectionName=section)
        for section in ["https-web", "https-api"]
    ]
    route = {
        "metadata": {"generation": 7},
        "spec": {"parentRefs": references},
        "status": {
            "parents": [
                dict(
                    parentRef=reference,
                    controllerName="gateway.envoyproxy.io/gatewayclass-controller",
                    conditions=[
                        dict(type=name, status="True", observedGeneration=7)
                        for name in ["Accepted", "ResolvedRefs"]
                    ],
                )
                for reference in references
            ]
        },
    }
    return gateway, route


class GatewayStatusTests(unittest.TestCase):
    def test_both_listeners_must_have_current_status(self):
        gateway, route = cluster_status()
        verification.check_gateway_status(gateway, [route], "demo", "private")
        route["status"]["parents"].pop()
        with self.assertRaisesRegex(ValueError, "Accepted/ResolvedRefs"):
            verification.check_gateway_status(gateway, [route], "demo", "private")

    def test_stale_gateway_and_route_generations_fail(self):
        for kind in ["gateway", "route"]:
            with self.subTest(kind=kind):
                gateway, route = cluster_status()
                if kind == "gateway":
                    gateway["status"]["conditions"][0]["observedGeneration"] = 2
                else:
                    route["status"]["parents"][1]["conditions"][1][
                        "observedGeneration"
                    ] = 6
                with self.assertRaises(ValueError):
                    verification.check_gateway_status(
                        gateway, [route], "demo", "private"
                    )

    def test_other_gateway_or_controller_cannot_satisfy_parent(self):
        for field, value in [("name", "other"), ("namespace", "other")]:
            gateway, route = cluster_status()
            route["status"]["parents"][1]["parentRef"] = dict(
                route["status"]["parents"][1]["parentRef"], **{field: value}
            )
            with self.assertRaises(ValueError):
                verification.check_gateway_status(gateway, [route], "demo", "private")
        gateway, route = cluster_status()
        route["status"]["parents"][1]["controllerName"] = "other-controller"
        with self.assertRaises(ValueError):
            verification.check_gateway_status(gateway, [route], "demo", "private")


class RealTLSGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="aks-tls-test-")
        cls.root = Path(cls.temp.name)
        cls.cert = cls.root / "certificate.pem"
        key = cls.root / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cls.cert),
                "-subj",
                "/CN=web.example.test",
                "-addext",
                "subjectAltName=DNS:web.example.test,DNS:api.example.test",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.requests = []
        cls.sni = []
        cls.status = 200
        cls.identity = {"slot": "aks01", "revision": "a" * 40}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.headers.get("Host"), self.path))
                self.send_response(cls.status)
                if cls.status == 302:
                    self.send_header(
                        "Location", "https://must-never-be-contacted.invalid/version"
                    )
                self.end_headers()
                self.wfile.write(json.dumps(cls.identity).encode())

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.cert), str(key))
        context.set_servername_callback(lambda sock, name, ctx: cls.sni.append(name))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def setUp(self):
        type(self).requests.clear()
        type(self).sni.clear()
        type(self).status = 200
        type(self).identity = {"slot": "aks01", "revision": "a" * 40}
        self.settings = dict(
            readiness_path="/readyz",
            version_path="/version",
            ingress=dict(
                gateway="private",
                http_routes=["demo"],
                hosts=["web.example.test", "api.example.test"],
            ),
        )

    def verify(self, *, trusted=True):
        gateway, route = cluster_status()

        def command(args, **kwargs):
            if "wait" in args:
                return ""
            if "gateway.gateway.networking.k8s.io" in args:
                return json.dumps(gateway)
            if "httproute.gateway.networking.k8s.io" in args:
                return json.dumps(route)
            self.assertIn(
                "gateway.envoyproxy.io/owning-gateway-name=private,gateway.envoyproxy.io/owning-gateway-namespace=demo",
                args,
            )
            return json.dumps(
                dict(items=[dict(metadata=dict(name="generated-envoy-service"))])
            )

        @contextmanager
        def forward(base, env, name, port):
            self.assertEqual((name, port), ("generated-envoy-service", 443))
            yield self.server.server_port

        with patch.object(verification, "port_forward", forward):
            verification.verify_ingress(
                ["kubectl", "--namespace", "demo"],
                {},
                self.settings,
                "aks01",
                "a" * 40,
                "demo",
                command,
                ca_file=self.cert if trusted else None,
            )

    def test_real_tls_validates_trust_hostname_sni_and_both_hosts(self):
        self.verify()
        self.assertEqual(
            self.requests,
            [
                (host, path)
                for host in ["web.example.test", "api.example.test"]
                for path in ["/readyz", "/version"]
            ],
        )
        self.assertEqual(self.sni, ["web.example.test"] * 2 + ["api.example.test"] * 2)

    def test_wrong_hostname_rejected(self):
        self.settings["ingress"]["hosts"] = ["wrong.example.test"]
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.verify()
        self.assertEqual(self.requests, [])

    def test_untrusted_certificate_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.verify(trusted=False)
        self.assertEqual(self.requests, [])

    def test_wrong_slot_and_revision_rejected(self):
        for field, value in [("slot", "aks02"), ("revision", "b" * 40)]:
            with self.subTest(field=field):
                type(self).identity = {
                    "slot": "aks01",
                    "revision": "a" * 40,
                    field: value,
                }
                with self.assertRaisesRegex(ValueError, "slot and source revision"):
                    self.verify()

    def test_redirect_rejected_without_following_location(self):
        type(self).status = 302
        with self.assertRaisesRegex(ValueError, "redirects are rejected"):
            self.verify()
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
