#!/usr/bin/env python3
"""Prepare and explicitly apply a digest-pinned, namespace-scoped Argo CD platform."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SHA = re.compile(r"^[0-9a-f]{64}$")
LIMIT = 8 * 1024 * 1024
FILES = {"crds.yaml", "install.yaml", "access.yaml"}
WRITABLE = [
    ("", ["services", "configmaps", "serviceaccounts"], ["Service", "ConfigMap", "ServiceAccount"]),
    ("apps", ["deployments"], ["Deployment"]),
    ("networking.k8s.io", ["networkpolicies"], ["NetworkPolicy"]),
    ("policy", ["poddisruptionbudgets"], ["PodDisruptionBudget"]),
    ("autoscaling", ["horizontalpodautoscalers"], ["HorizontalPodAutoscaler"]),
    ("gateway.networking.k8s.io", ["httproutes"], ["HTTPRoute"]),
    ("secrets-store.csi.x-k8s.io", ["secretproviderclasses"], ["SecretProviderClass"]),
]
READABLE = [
    ("", ["pods", "endpoints", "events"], ["Pod", "Endpoints", "Event"]),
    ("apps", ["replicasets"], ["ReplicaSet"]),
    ("discovery.k8s.io", ["endpointslices"], ["EndpointSlice"]),
]
WRITE_VERBS = ["get", "list", "watch", "create", "update", "patch", "delete"]
READ_VERBS = ["get", "list", "watch"]


def need(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_lock():
    lock = json.loads((ROOT / "argocd-lock.json").read_text())
    need(lock.get("schema_version") == 1, "Unsupported Argo lock schema")
    need(re.fullmatch(r"v\d+\.\d+\.\d+", lock.get("version", "")), "Pin a stable Argo release")
    for source, target in lock["images"].items():
        need(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", target), "Pin every Argo image digest")
        need(source.split(":")[0] == target.split("@")[0], "Image pin must retain its repository")
    return lock


def fetch(lock, entry):
    need(SHA.fullmatch(entry["sha256"]), "Pin an exact upstream manifest SHA256")
    url = "https://raw.githubusercontent.com/argoproj/argo-cd/" + lock["version"] + "/" + entry["path"]
    with urllib.request.urlopen(url, timeout=60) as response:
        need(response.url.startswith("https://"), "Reject insecure manifest redirects")
        data = response.read(LIMIT + 1)
    need(len(data) <= LIMIT and digest(data) == entry["sha256"], "Upstream manifest SHA256 mismatch")
    return data


def objects(data):
    result = [item for item in yaml.safe_load_all(data) if item is not None]
    need(all(isinstance(item, dict) for item in result), "Expected Kubernetes object documents")
    return result


def pin_images(value, pins):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "image":
                need(item in pins, "Unexpected unpinned upstream container image")
                value[key] = pins[item]
            else:
                pin_images(item, pins)
    elif isinstance(value, list):
        for item in value:
            pin_images(item, pins)


def obj(kind, name, namespace, api="v1", **kwargs):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": namespace}, **kwargs}


def role_rules(write):
    return [{"apiGroups": [group], "resources": resources, "verbs": WRITE_VERBS if write else READ_VERBS}
            for group, resources, _ in WRITABLE] + [
        {"apiGroups": [group], "resources": resources, "verbs": READ_VERBS}
        for group, resources, _ in READABLE
    ]


def access_objects(namespace, app_namespace, git_ports):
    rbac = "rbac.authorization.k8s.io/v1"
    result = [
        obj("Role", "argocd-application-delivery", app_namespace, rbac, rules=role_rules(True)),
        obj("RoleBinding", "argocd-application-delivery", app_namespace, rbac,
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "argocd-application-delivery"},
            subjects=[{"kind": "ServiceAccount", "name": "argocd-application-controller", "namespace": namespace}]),
        obj("Role", "argocd-application-reader", app_namespace, rbac, rules=role_rules(False) + [
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}
        ]),
        obj("RoleBinding", "argocd-application-reader", app_namespace, rbac,
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "argocd-application-reader"},
            subjects=[{"kind": "ServiceAccount", "name": "argocd-server", "namespace": namespace}]),
        obj("Secret", "local-application-cluster", namespace, type="Opaque", stringData={
            "name": "in-cluster", "server": "https://kubernetes.default.svc",
            "namespaces": app_namespace, "clusterResources": "false", "config": "{}"
        }),
        obj("AppProject", "default", namespace, "argoproj.io/v1alpha1", spec={
            "sourceRepos": [], "sourceNamespaces": [], "destinations": [],
            "clusterResourceWhitelist": [],
            "namespaceResourceBlacklist": [{"group": "*", "kind": "*"}],
        }),
    ]
    result[4]["metadata"]["labels"] = {"argocd.argoproj.io/secret-type": "cluster"}
    # Transport allowlist, not an FQDN firewall. Azure firewall/proxy supplies destination filtering.
    result.append(obj("NetworkPolicy", "argocd-common-egress", namespace, "networking.k8s.io/v1", spec={
        "podSelector": {}, "policyTypes": ["Egress"], "egress": [
            {"to": [{"podSelector": {}}]},
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
             "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
            {"ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 6443}]},
        ]
    }))
    result.append(obj("NetworkPolicy", "argocd-repository-egress", namespace, "networking.k8s.io/v1", spec={
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "argocd-repo-server"}},
        "policyTypes": ["Egress"],
        "egress": [{"ports": [{"protocol": "TCP", "port": port} for port in git_ports]}],
    }))
    return result


def customize_install(documents, lock, namespace, app_namespace, profile):
    result = copy.deepcopy(documents)
    inclusions = [{"apiGroups": [group], "kinds": kinds,
                   "clusters": ["https://kubernetes.default.svc"]}
                  for group, _, kinds in WRITABLE + READABLE]
    for item in result:
        kind = item["kind"]
        need(kind not in ["ClusterRole", "ClusterRoleBinding", "Namespace", "CustomResourceDefinition"],
             "Namespace install must not grant cluster-wide access")
        item["metadata"]["namespace"] = namespace
        if kind == "RoleBinding":
            for subject in item.get("subjects", []):
                if subject["kind"] == "ServiceAccount":
                    subject["namespace"] = namespace
        if kind == "Service":
            need(item.get("spec", {}).get("type", "ClusterIP") == "ClusterIP",
                 "Argo control plane must not expose a public Service")
        if kind == "ConfigMap" and item["metadata"]["name"] == "argocd-cm":
            item.setdefault("data", {}).update({
                "application.resourceTrackingMethod": "annotation",
                "resource.respectRBAC": "strict",
                "resource.inclusions": yaml.safe_dump(inclusions, sort_keys=False),
                "users.anonymous.enabled": "false",
                "timeout.reconciliation": "60s",
            })
        if kind == "ConfigMap" and item["metadata"]["name"] == "argocd-rbac-cm":
            item.setdefault("data", {}).update({"policy.default": "role:no-access"})
        if kind in ["Deployment", "StatefulSet"]:
            # These optional services are deliberately not part of the reference design.
            if item["metadata"]["name"] in [
                "argocd-dex-server", "argocd-applicationset-controller", "argocd-notifications-controller"
            ]:
                item["spec"]["replicas"] = 0
            if profile == "evaluation":
                for container in item["spec"]["template"]["spec"].get("containers", []):
                    container.setdefault("resources", {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"memory": "1Gi"},
                    })
        pin_images(item, lock["images"])
    return result


def prepare(output, namespace="argocd", app_namespace="platform-demo", profile="evaluation", git_ports=None):
    output = Path(output)
    need(NAME.fullmatch(namespace) and NAME.fullmatch(app_namespace), "Use valid namespace names")
    need(namespace != app_namespace and not namespace.startswith("kube-") and not app_namespace.startswith("kube-"),
         "Argo and application need separate non-system namespaces")
    need(profile in ["evaluation", "ha"], "Select evaluation or ha")
    git_ports = sorted(set(git_ports or [443]))
    need(all(type(port) is int and 1 <= port <= 65535 for port in git_ports), "Invalid Git egress port")
    need(not output.exists() or not any(output.iterdir()), "Use a new empty bootstrap output directory")
    output.mkdir(parents=True, exist_ok=True)
    lock = load_lock()
    crds = []
    for entry in lock["crds"]:
        crds.extend(objects(fetch(lock, entry)))
    need(len(crds) == 3 and all(x["kind"] == "CustomResourceDefinition" for x in crds),
         "Expected the three separately reviewed Argo CRDs")
    install = customize_install(objects(fetch(lock, lock["manifests"][profile])),
                                lock, namespace, app_namespace, profile)
    files = {
        "crds.yaml": crds,
        "install.yaml": install,
        "access.yaml": access_objects(namespace, app_namespace, git_ports),
    }
    for name, docs in files.items():
        (output / name).write_text(yaml.safe_dump_all(docs, sort_keys=False))
    receipt = {
        "schema_version": 1, "argo_version": lock["version"], "profile": profile,
        "namespace": namespace, "app_namespace": app_namespace, "git_egress_ports": git_ports,
        "lock_sha256": digest((ROOT / "argocd-lock.json").read_bytes()),
        "files": {name: digest((output / name).read_bytes()) for name in sorted(FILES)},
        "images": lock["images"],
    }
    (output / "bootstrap.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def verify(output, expected_sha):
    output = Path(output)
    need(SHA.fullmatch(expected_sha), "Supply the independently approved bootstrap receipt SHA256")
    data = (output / "bootstrap.json").read_bytes()
    need(digest(data) == expected_sha, "Bootstrap receipt changed")
    receipt = json.loads(data)
    need(receipt.get("schema_version") == 1 and set(receipt.get("files", {})) == FILES,
         "Unbound or unexpected bootstrap file")
    need(set(path.name for path in output.iterdir()) == FILES | {"bootstrap.json"},
         "Unexpected bootstrap output files")
    for name, expected in receipt["files"].items():
        path = output / name
        need(path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == expected,
             "Bootstrap bundle changed")
    need(NAME.fullmatch(receipt["namespace"]) and NAME.fullmatch(receipt["app_namespace"]),
         "Invalid bootstrap namespaces")
    return receipt


def run(args):
    subprocess.run(args, check=True)


def wait_crd_established(base, name, timeout):
    """Wait through the initial CRD status gap without accepting a failed CRD."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        need(remaining > 0, f"Timed out waiting for CRD {name} to become Established")
        # Newly applied CRDs can have no status.conditions yet. Some kubectl
        # wait versions reject that initial state instead of continuing to wait.
        raw = subprocess.check_output(
            base + ["get", "crd", name, "-o", "json",
                    f"--request-timeout={min(30, max(1, int(remaining)))}s"],
            text=True, timeout=remaining,
        )
        crd = json.loads(raw)
        need(not crd.get("metadata", {}).get("deletionTimestamp"),
             f"CRD {name} is being deleted")
        status = crd.get("status") or {}
        need(isinstance(status, dict), f"Invalid CRD status for {name}")
        conditions = status.get("conditions")
        if conditions is None:
            conditions = []
        need(isinstance(conditions, list) and all(isinstance(c, dict) for c in conditions),
             f"Invalid CRD conditions for {name}")
        for condition in conditions:
            rejected = condition.get("type") == "NamesAccepted" and condition.get("status") == "False"
            terminating = condition.get("type") == "Terminating" and condition.get("status") == "True"
            need(not (rejected or terminating), f"CRD {name} cannot become Established: {condition.get('reason', condition.get('type'))}")
        if any(c.get("type") == "Established" and c.get("status") == "True" for c in conditions):
            return
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def apply_bundle(output, expected_sha, kubeconfig, context, timeout=600):
    output = Path(output).resolve()
    receipt = verify(output, expected_sha)
    need(Path(kubeconfig).is_file() and context and not context.startswith("-"),
         "Supply an explicit kubeconfig file and context")
    need(type(timeout) is int and 60 <= timeout <= 3600, "Timeout must be 60-3600 seconds")
    kubectl = os.environ.get("KUBECTL_BIN", "kubectl")
    base = [kubectl, "--kubeconfig", str(Path(kubeconfig).resolve()), "--context", context]
    # Existing namespace labels/PSA policy are preserved. Never reapply a Namespace object.
    for namespace in [receipt["namespace"], receipt["app_namespace"]]:
        found = subprocess.check_output(base + ["get", "namespace", namespace, "--ignore-not-found", "-o", "name"], text=True)
        if not found.strip():
            run(base + ["create", "namespace", namespace])
    run(base + ["apply", "--server-side", "--field-manager=aks-argocd-bootstrap", "-f", str(output / "crds.yaml")])
    for name in ["applications.argoproj.io", "appprojects.argoproj.io", "applicationsets.argoproj.io"]:
        wait_crd_established(base, name, timeout)
    # Configure limited cache/RBAC before the controller starts.
    run(base + ["apply", "--server-side", "--field-manager=aks-argocd-bootstrap", "-f", str(output / "access.yaml")])
    run(base + ["apply", "--server-side", "--field-manager=aks-argocd-bootstrap", "-f", str(output / "install.yaml")])
    for item in objects((output / "install.yaml").read_bytes()):
        if item["kind"] in ["Deployment", "StatefulSet"] and item["spec"].get("replicas", 1) > 0:
            run(base + ["-n", receipt["namespace"], "rollout", "status",
                        item["kind"].lower() + "/" + item["metadata"]["name"], f"--timeout={timeout}s"])
    print(json.dumps({"result": "passed", "argo_version": receipt["argo_version"],
                      "context": context, "namespace": receipt["namespace"], "profile": receipt["profile"]}))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Download and bind reviewed Argo manifests; no cluster access")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--namespace", default="argocd")
    p.add_argument("--app-namespace", default="platform-demo")
    p.add_argument("--profile", choices=["evaluation", "ha"], default="evaluation")
    p.add_argument("--git-egress-port", action="append", type=int)
    p = commands.add_parser("apply", help="Apply the reviewed platform to an explicit cluster")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--receipt-sha256", required=True)
    p.add_argument("--kubeconfig", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        receipt = prepare(args.output, args.namespace, args.app_namespace, args.profile, args.git_egress_port)
        print(json.dumps({"argo_version": receipt["argo_version"], "profile": receipt["profile"],
                          "receipt_sha256": digest((args.output / "bootstrap.json").read_bytes())}))
    else:
        verify(args.output, args.receipt_sha256)
        if not args.yes:
            need(sys.stdin.isatty() and input(f"Apply Argo platform to {args.context}? Type apply: ") == "apply",
                 "Apply cancelled; CI needs its platform approval gate and --yes")
        apply_bundle(args.output, args.receipt_sha256, args.kubeconfig, args.context, args.timeout)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"Argo bootstrap failed: {error}", file=sys.stderr)
        sys.exit(1)
