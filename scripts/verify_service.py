"""Verify the selected cluster Service, independently of the active gateway slot."""

from contextlib import contextmanager
import http.client
import socket
import ssl
import json
import re
import selectors
import subprocess
import time
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_identity(value, slot, revision):
    if (
        not isinstance(value, dict)
        or value.get("slot") != slot
        or value.get("revision") != revision
    ):
        raise ValueError(
            "Application response does not match selected slot and source revision"
        )


@contextmanager
def port_forward(base, env, service, remote_port):
    command = base + [
        "port-forward",
        "--address=127.0.0.1",
        "service/" + service,
        ":" + str(remote_port),
    ]
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        deadline = time.monotonic() + 60
        port = None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline and process.poll() is None:
                for key, _ in selector.select(timeout=1):
                    line = key.fileobj.readline()
                    match = re.search(r"Forwarding from 127\.0\.0\.1:([0-9]+) ->", line)
                    if match:
                        port = int(match[1])
                        break
                if port:
                    break
        if not port:
            raise ValueError(
                "Could not establish selected Service port-forward within 60 seconds"
            )
        yield port
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()


def verify_service(base, env, settings, slot, revision):
    with port_forward(base, env, settings["service"], settings["port"]) as port:
        # Only loopback; authenticated Kubernetes transport selects the cluster and namespace.
        # Ignore ambient HTTP proxies so the local port cannot be redirected externally.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        for path in [settings["readiness_path"], settings["version_path"]]:
            with opener.open(f"http://127.0.0.1:{port}" + path, timeout=20) as response:
                if response.status != 200:
                    raise ValueError("Selected application health verification failed")
                data = response.read(65537)
                if len(data) > 65536:
                    raise ValueError("Application verification response is too large")
            if path == settings["version_path"]:
                check_identity(json.loads(data), slot, revision)


def current_condition(conditions, name, generation):
    return any(
        item.get("type") == name
        and item.get("status") == "True"
        and item.get("observedGeneration") == generation
        for item in conditions
    )


def check_gateway_status(gateway, routes, namespace, gateway_name):
    if not current_condition(
        gateway.get("status", {}).get("conditions", []),
        "Programmed",
        gateway["metadata"]["generation"],
    ):
        raise ValueError("Gateway is not Programmed for the current generation")
    for route in routes:

        def parent_key(reference):
            return (
                reference.get("group", "gateway.networking.k8s.io"),
                reference.get("kind", "Gateway"),
                reference.get("namespace", namespace),
                reference.get("name"),
                reference.get("sectionName"),
                reference.get("port"),
            )

        expected = {
            parent_key(item)
            for item in route.get("spec", {}).get("parentRefs", [])
            if item.get("name") == gateway_name
            and item.get("namespace", namespace) == namespace
            and item.get("group", "gateway.networking.k8s.io")
            == "gateway.networking.k8s.io"
            and item.get("kind", "Gateway") == "Gateway"
        }
        parents = [
            item
            for item in route.get("status", {}).get("parents", [])
            if parent_key(item.get("parentRef", {})) in expected
            and item.get("controllerName")
            == "gateway.envoyproxy.io/gatewayclass-controller"
        ]
        if (
            not expected
            or {parent_key(item["parentRef"]) for item in parents} != expected
            or not all(
                all(
                    current_condition(
                        item.get("conditions", []),
                        condition,
                        route["metadata"]["generation"],
                    )
                    for condition in ["Accepted", "ResolvedRefs"]
                )
                for item in parents
            )
        ):
            raise ValueError(
                "HTTPRoute is not Accepted/ResolvedRefs for the selected Gateway and current generation"
            )


class LoopbackTLS(http.client.HTTPSConnection):
    """Connect only to the local forward while validating the real hostname/SNI."""

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def verify_ingress(
    base, env, settings, slot, revision, namespace, run_command, ca_file=None
):
    ingress = settings["ingress"]
    gateway_name = ingress["gateway"]
    # Application delivery can get platform-owned Gateways, but cannot list or
    # watch them. kubectl wait starts an informer even for one named resource.
    # Poll exact named GETs instead; command/authentication errors propagate
    # immediately, and only not-yet-current controller status is retried.
    deadline = time.monotonic() + 600
    while True:
        gateway = json.loads(
            run_command(
                base
                + [
                    "get",
                    "gateway.gateway.networking.k8s.io",
                    gateway_name,
                    "--output=json",
                    "--request-timeout=30s",
                ],
                env=env,
                capture=True,
            )
        )
        routes = [
            json.loads(
                run_command(
                    base
                    + [
                        "get",
                        "httproute.gateway.networking.k8s.io",
                        name,
                        "--output=json",
                        "--request-timeout=30s",
                    ],
                    env=env,
                    capture=True,
                )
            )
            for name in ingress["http_routes"]
        ]
        try:
            check_gateway_status(gateway, routes, namespace, gateway_name)
            break
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    selector = (
        "gateway.envoyproxy.io/owning-gateway-name="
        + gateway_name
        + ",gateway.envoyproxy.io/owning-gateway-namespace="
        + namespace
    )
    services = json.loads(
        run_command(
            base + ["get", "services", "--selector", selector, "--output=json"],
            env=env,
            capture=True,
        )
    )["items"]
    if len(services) != 1:
        raise ValueError(
            "Expected exactly one Envoy Service owned by the selected Gateway"
        )
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    with port_forward(base, env, services[0]["metadata"]["name"], 443) as port:
        for hostname in ingress["hosts"]:
            for path in [settings["readiness_path"], settings["version_path"]]:
                connection = LoopbackTLS(
                    hostname, port=port, timeout=20, context=context
                )
                try:
                    connection.request("GET", path, headers={"Host": hostname})
                    response = connection.getresponse()
                    if response.status != 200:
                        raise ValueError(
                            "Selected HTTPS Gateway route did not return HTTP200; redirects are rejected"
                        )
                    data = response.read(65537)
                    if len(data) > 65536:
                        raise ValueError("Gateway verification response is too large")
                    if path == settings["version_path"]:
                        check_identity(json.loads(data), slot, revision)
                finally:
                    connection.close()
