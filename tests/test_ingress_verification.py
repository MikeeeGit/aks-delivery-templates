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


class GatewayReadinessPollingTests(unittest.TestCase):
    def setUp(self):
        self.base = ["kubectl", "--context", "owned", "--namespace", "demo"]
        self.settings = dict(ingress=dict(gateway="private", http_routes=["demo"]))
        self.calls = []

    def verify(self, command):
        verification.verify_ingress(
            self.base, {}, self.settings, "aks01", "a" * 40, "demo", command
        )

    def test_exact_named_get_retries_stale_gateway_and_route_without_watch(self):
        for stale_kind in ["gateway", "route"]:
            with self.subTest(stale_kind=stale_kind):
                self.calls.clear()
                gateway, route = cluster_status()
                stale_gateway, stale_route = deepcopy(gateway), deepcopy(route)
                if stale_kind == "gateway":
                    stale_gateway["status"]["conditions"][0]["observedGeneration"] = 2
                else:
                    stale_route["status"]["parents"][0]["conditions"][0]["observedGeneration"] = 6
                gateways, routes = iter([stale_gateway, gateway]), iter([stale_route, route])

                def command(args, **kwargs):
                    self.calls.append(args)
                    self.assertNotIn("wait", args)
                    self.assertNotIn("watch", args)
                    if "gateway.gateway.networking.k8s.io" in args:
                        self.assertEqual(args, self.base + [
                            "get", "gateway.gateway.networking.k8s.io", "private",
                            "--output=json", "--request-timeout=30s",
                        ])
                        return json.dumps(next(gateways))
                    if "httproute.gateway.networking.k8s.io" in args:
                        self.assertEqual(args, self.base + [
                            "get", "httproute.gateway.networking.k8s.io", "demo",
                            "--output=json", "--request-timeout=30s",
                        ])
                        return json.dumps(next(routes))
                    self.assertIn("services", args)
                    raise RuntimeError("reached-service-verification")

                with patch.object(verification.time, "sleep") as sleep:
                    with self.assertRaisesRegex(RuntimeError, "reached-service-verification"):
                        self.verify(command)
                sleep.assert_called_once_with(2)
                self.assertEqual(len(self.calls), 5)

    def test_authorization_failure_is_immediate_for_gateway_and_route(self):
        for denied in ["gateway.gateway.networking.k8s.io", "httproute.gateway.networking.k8s.io"]:
            with self.subTest(denied=denied):
                self.calls.clear()
                gateway, _ = cluster_status()

                def command(args, **kwargs):
                    self.calls.append(args)
                    if denied in args:
                        raise subprocess.CalledProcessError(1, args, stderr="Forbidden")
                    self.assertIn("gateway.gateway.networking.k8s.io", args)
                    return json.dumps(gateway)

                with patch.object(verification.time, "sleep") as sleep:
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.verify(command)
                sleep.assert_not_called()
                self.assertEqual(len(self.calls), 1 if denied.startswith("gateway.") else 2)

    def test_stale_programmed_status_has_bounded_timeout(self):
        gateway, route = cluster_status()
        gateway["status"]["conditions"][0]["observedGeneration"] = 2

        def command(args, **kwargs):
            if "gateway.gateway.networking.k8s.io" in args:
                return json.dumps(gateway)
            self.assertIn("httproute.gateway.networking.k8s.io", args)
            return json.dumps(route)

        with patch.object(verification.time, "monotonic", side_effect=[0, 600]), \
                patch.object(verification.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "Programmed for the current generation"):
                self.verify(command)
        sleep.assert_not_called()


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
                identity = cls.identity
                if self.path == "/version" and cls.version_sequence:
                    identity = cls.version_sequence.pop(0)
                data = cls.raw_response if cls.raw_response is not None else json.dumps(identity).encode()
                self.wfile.write(data)

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
        type(self).version_sequence = []
        type(self).raw_response = None
        self.clock = 0
        self.sleeps = []
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
            self.assertNotIn("wait", args)
            self.assertNotIn("watch", args)
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

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += seconds

        with patch.object(verification, "port_forward", forward), \
                patch.object(verification.time, "monotonic", side_effect=lambda: self.clock), \
                patch.object(verification.time, "sleep", side_effect=sleep):
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
            ] * 3,
        )
        self.assertEqual(self.sni, (["web.example.test"] * 2 + ["api.example.test"] * 2) * 3)
        self.assertEqual(self.sleeps, [2, 2])

    def test_wrong_hostname_rejected(self):
        self.settings["ingress"]["hosts"] = ["wrong.example.test"]
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.verify()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.sleeps, [])

    def test_untrusted_certificate_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.verify(trusted=False)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.sleeps, [])

    def test_wrong_slot_rejected_without_retry(self):
        type(self).identity = {"slot": "aks02", "revision": "a" * 40}
        with self.assertRaisesRegex(ValueError, "slot and source revision"):
            self.verify()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.requests), 2)

    def test_valid_old_revision_converges_to_three_complete_current_sweeps(self):
        old = {"slot": "aks01", "revision": "b" * 40}
        current = {"slot": "aks01", "revision": "a" * 40}
        type(self).version_sequence = [old, old] + [current] * 6
        self.verify()
        self.assertEqual(self.version_sequence, [])
        self.assertEqual(len(self.requests), 16)
        self.assertEqual(self.sleeps, [2, 2, 2])

    def test_one_lagging_host_resets_consecutive_whole_sweeps(self):
        old = {"slot": "aks01", "revision": "b" * 40}
        current = {"slot": "aks01", "revision": "a" * 40}
        type(self).version_sequence = [current, current, current, old] + [current] * 6
        self.verify()
        self.assertEqual(self.version_sequence, [])
        self.assertEqual(len(self.requests), 20)
        self.assertEqual(self.sleeps, [2, 2, 2, 2])

    def test_stale_revision_forever_fails_at_bounded_deadline(self):
        type(self).identity = {"slot": "aks01", "revision": "b" * 40}
        with self.assertRaisesRegex(ValueError, "within 120 seconds") as error:
            self.verify()
        self.assertIn("b" * 40, str(error.exception))
        self.assertEqual(self.clock, 120)
        self.assertEqual(len(self.requests), 240)

    def test_malformed_revision_json_or_nonobject_is_not_retried(self):
        for identity, raw in [
            ({"slot": "aks01", "revision": "not-a-source-sha"}, None),
            ({"slot": "aks01", "revision": None}, None),
            ([], None), (None, b"not json"),
        ]:
            with self.subTest(identity=identity, raw=raw):
                type(self).identity = identity
                type(self).raw_response = raw
                with self.assertRaises(ValueError):
                    self.verify()
                self.assertEqual(self.sleeps, [])

    def test_wrong_slot_on_second_host_does_not_hide_behind_first_host_lag(self):
        type(self).version_sequence = [
            {"slot": "aks01", "revision": "b" * 40},
            {"slot": "aks02", "revision": "a" * 40},
        ]
        with self.assertRaisesRegex(ValueError, "slot and source revision"):
            self.verify()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.requests), 4)

    def test_redirect_rejected_without_following_location(self):
        type(self).status = 302
        with self.assertRaisesRegex(ValueError, "redirects are rejected"):
            self.verify()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
