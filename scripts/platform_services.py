#!/usr/bin/env python3
"""Reviewed, independently versioned Helm platform services for explicit AKS slots."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

import yaml
from delivery import NAME, UUID, COMMIT, account, need, relative, run, select, snapshot

SHA = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(
    r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?$"
)
LIMIT = 64 * 1024 * 1024


def load_config(source, name):
    data = json.loads(relative(source, name).read_text())
    need(
        data.get("schema_version") == 1 and UUID.fullmatch(data.get("tenant_id", "")),
        "Invalid platform schema or tenant UUID",
    )
    need(
        isinstance(data.get("targets"), list) and data["targets"],
        "Explicit platform targets are required",
    )
    keys = set()
    for target in data["targets"]:
        for field in ["environment", "region", "approval_environment"]:
            need(
                NAME.fullmatch(target.get(field, "")),
                "Invalid platform target identifier",
            )
        need(target.get("slot") in ["aks01", "aks02"], "Select an explicit AKS slot")
        need(
            UUID.fullmatch(target.get("subscription_id", "")),
            "Explicit platform subscription UUID is required",
        )
        for field in ["resource_group", "cluster_name"]:
            need(
                re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.()-]{0,89}", target.get(field, "")
                ),
                "Invalid platform cluster identifier",
            )
        key = (target["environment"], target["region"], target["slot"])
        need(key not in keys, "Duplicate platform target")
        keys.add(key)
        need(
            target["approval_environment"].startswith("platform-"),
            "Use a separate platform- approval environment",
        )
        if target.get("kubernetes_version"):
            need(
                VERSION.fullmatch(target["kubernetes_version"]),
                "Pin a full Kubernetes version for chart rendering",
            )
        namespaces = target.get("namespaces", [])
        need(
            isinstance(namespaces, list)
            and namespaces
            and len(namespaces) == len(set(namespaces))
            and all(isinstance(x, str) and NAME.fullmatch(x) for x in namespaces),
            "Declare unique platform/application namespace names",
        )
        for name in target.get("manifests", []):
            need(
                relative(source, name).is_file(),
                "Common manifests must be committed local files",
            )
        crds = target.get("crd_bundles", [])
        need(isinstance(crds, list), "crd_bundles must be a list")
        need(
            not crds or target.get("manage_crds") is True,
            "CRD creation/upgrades require explicit manage_crds=true",
        )
        for bundle in crds:
            parsed = urllib.parse.urlparse(bundle.get("url", ""))
            need(
                parsed.scheme == "https"
                and parsed.netloc
                and not parsed.query
                and not parsed.username
                and SHA.fullmatch(bundle.get("sha256", "")),
                "CRD bundles require an HTTPS URL and exact SHA256",
            )
        for identity in target.get("workload_service_accounts", []):
            need(
                NAME.fullmatch(identity.get("name", ""))
                and identity.get("namespace") in namespaces
                and UUID.fullmatch(identity.get("client_id", "")),
                "Workload ServiceAccounts need a declared namespace and explicit managed-identity client ID",
            )
        releases = target.get("releases", [])
        need(
            isinstance(releases, list) and releases,
            "At least one platform release is required",
        )
        names = set()
        for release in releases:
            need(
                NAME.fullmatch(release.get("name", ""))
                and len(release["name"]) <= 53
                and NAME.fullmatch(release.get("namespace", "")),
                "Invalid Helm release or namespace",
            )
            pair = (release["namespace"], release["name"])
            need(
                release["namespace"] in namespaces,
                "Every release namespace must be declared",
            )
            need(pair not in names, "Duplicate Helm release")
            names.add(pair)
            chart = release.get("chart", {})
            need(
                NAME.fullmatch(chart.get("name", ""))
                and VERSION.fullmatch(chart.get("version", ""))
                and SHA.fullmatch(chart.get("sha256", "")),
                "Pin chart name, exact version and SHA256",
            )
            need(
                chart["name"] != "ingress-nginx"
                or target.get("allow_retired_ingress_nginx") is True,
                "ingress-nginx is retired; compatibility use requires explicit allow_retired_ingress_nginx=true",
            )
            url = chart.get("url", "")
            if url.startswith("repo:"):
                need(
                    relative(source, url[5:]).is_file(),
                    "Committed chart package is missing",
                )
            elif url.startswith("oci://"):
                need(
                    re.fullmatch(
                        r"oci://[a-z0-9.-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", url
                    ),
                    "OCI chart URLs must pin the manifest digest",
                )
            else:
                parsed = urllib.parse.urlparse(url)
                need(
                    parsed.scheme == "https"
                    and parsed.netloc
                    and not parsed.username
                    and not parsed.password
                    and not parsed.query
                    and not parsed.fragment
                    and parsed.path.endswith(".tgz"),
                    "Use an HTTPS chart package URL without credentials/query or explicit repo: package",
                )
            values = release.get("values", [])
            need(
                isinstance(values, list) and all(isinstance(x, str) for x in values),
                "Values must be a list of committed relative files",
            )
            for name in values:
                path = relative(source, name)
                need(
                    path.is_file()
                    and isinstance(yaml.safe_load(path.read_text()), dict),
                    "Helm values must be local YAML objects",
                )
            need(
                type(release.get("timeout_seconds", 600)) is int
                and 30 <= release.get("timeout_seconds", 600) <= 3600,
                "Helm timeout must be 30–3600 seconds",
            )
    return data


def package(source, chart):
    if chart["url"].startswith("repo:"):
        path = relative(source, chart["url"][5:])
        need(path.stat().st_size <= LIMIT, "Chart package too large")
        data = path.read_bytes()
    elif chart["url"].startswith("oci://"):
        with tempfile.TemporaryDirectory(prefix="aks-platform-chart-") as temporary:
            root = Path(temporary)
            run(
                ["helm", "pull", chart["url"], "--destination", str(root)],
                env=helm_environment(root / "helm"),
            )
            archives = list(root.glob("*.tgz"))
            need(
                len(archives) == 1 and archives[0].stat().st_size <= LIMIT,
                "Unexpected OCI chart result",
            )
            data = archives[0].read_bytes()
    else:
        with urllib.request.urlopen(chart["url"], timeout=60) as response:
            need(
                urllib.parse.urlparse(response.url).scheme == "https",
                "Chart download redirected away from HTTPS",
            )
            data = response.read(LIMIT + 1)
    need(
        len(data) <= LIMIT and hashlib.sha256(data).hexdigest() == chart["sha256"],
        "Chart package SHA256 mismatch or oversized package",
    )
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        need(
            sum(x.size for x in members) <= 4 * LIMIT,
            "Expanded chart package too large",
        )
        for member in members:
            path = PurePosixPath(member.name)
            need(
                (member.isfile() or member.isdir())
                and not path.is_absolute()
                and ".." not in path.parts,
                "Chart archive contains unsafe paths or links",
            )
        metadata = archive.getmember(chart["name"] + "/Chart.yaml")
        need(metadata.isfile(), "Chart metadata is missing")
        definition = yaml.safe_load(archive.extractfile(metadata).read())
        need(
            definition.get("name") == chart["name"]
            and definition.get("version") == chart["version"],
            "Chart metadata differs from pinned name/version",
        )
    return data


def helm_environment(directory):
    directory.mkdir(parents=True, exist_ok=True)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("HELM_")
    }
    for name, suffix in [
        ("HELM_CONFIG_HOME", "config"),
        ("HELM_CACHE_HOME", "cache"),
        ("HELM_DATA_HOME", "data"),
        ("HELM_PLUGINS", "plugins"),
        ("DOCKER_CONFIG", "docker"),
    ]:
        path = directory / suffix
        path.mkdir(mode=0o700, exist_ok=True)
        env[name] = str(path)
    return env


def prepare(source, config_name, commit, environment, region, slot, output):
    need(
        not output.exists() or (output.is_dir() and not any(output.iterdir())),
        "Use a new empty platform output directory",
    )
    with tempfile.TemporaryDirectory(prefix="aks-platform-render-") as temporary:
        root = Path(temporary)
        checkout = root / "source"
        checkout.mkdir()
        snapshot(source, commit, checkout)
        config = load_config(checkout, config_name)
        target = select(config, environment, region, slot)
        output.mkdir(parents=True, exist_ok=True)
        env = helm_environment(root / "helm")
        files = {}
        releases = []
        crds = []
        crd_policies = []
        for index, bundle in enumerate(target.get("crd_bundles", [])):
            with urllib.request.urlopen(bundle["url"], timeout=60) as response:
                need(
                    urllib.parse.urlparse(response.url).scheme == "https",
                    "CRD redirect must stay HTTPS",
                )
                data = response.read(LIMIT + 1)
            need(
                len(data) <= LIMIT
                and hashlib.sha256(data).hexdigest() == bundle["sha256"],
                "CRD bundle SHA256 mismatch",
            )
            objects = [item for item in yaml.safe_load_all(data) if item]
            allowed = {
                "CustomResourceDefinition": "apiextensions.k8s.io/v1",
                "ValidatingAdmissionPolicy": "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicyBinding": "admissionregistration.k8s.io/v1",
            }
            need(
                objects
                and all(
                    isinstance(item, dict)
                    and item.get("kind") in allowed
                    and item.get("apiVersion") == allowed[item["kind"]]
                    for item in objects
                ),
                "CRD bundle may contain only v1 definitions and their reviewed safe-upgrade admission policies",
            )
            original_name = f"crds-{index}-source.yaml"
            (output / original_name).write_bytes(data)
            files[original_name] = hashlib.sha256(data).hexdigest()
            filename = f"crds-{index}.yaml"
            definitions = [
                item for item in objects if item["kind"] == "CustomResourceDefinition"
            ]
            need(definitions, "CRD bundle contains no definitions")
            (output / filename).write_text(
                yaml.safe_dump_all(definitions, sort_keys=False)
            )
            files[filename] = hashlib.sha256(
                (output / filename).read_bytes()
            ).hexdigest()
            crds.append(filename)
            policies = [
                item for item in objects if item["kind"] != "CustomResourceDefinition"
            ]
            if policies:
                policy_file = f"crds-{index}-policies.yaml"
                (output / policy_file).write_text(
                    yaml.safe_dump_all(policies, sort_keys=False)
                )
                files[policy_file] = hashlib.sha256(
                    (output / policy_file).read_bytes()
                ).hexdigest()
                crd_policies.append(policy_file)
        namespace_objects = [
            dict(apiVersion="v1", kind="Namespace", metadata=dict(name=name))
            for name in target["namespaces"]
        ]
        prerequisites = []
        for name in target.get("manifests", []):
            for item in yaml.safe_load_all(relative(checkout, name).read_text()):
                if item is None:
                    continue
                need(
                    isinstance(item, dict)
                    and item.get("kind")
                    not in ["CustomResourceDefinition", "Namespace", "Secret"],
                    "Common manifests must not contain CRDs, Namespace or inline Secret objects",
                )
                namespace = item.get("metadata", {}).get("namespace")
                need(
                    namespace is None or namespace in target["namespaces"],
                    "Common manifest targets an undeclared namespace",
                )
                prerequisites.append(item)
        serviceaccounts = []
        for identity in target.get("workload_service_accounts", []):
            serviceaccounts.append(
                dict(
                    apiVersion="v1",
                    kind="ServiceAccount",
                    metadata=dict(
                        name=identity["name"],
                        namespace=identity["namespace"],
                        annotations={
                            "azure.workload.identity/client-id": identity["client_id"],
                            "azure.workload.identity/tenant-id": config["tenant_id"],
                        },
                    ),
                )
            )
        for filename, objects in [
            ("namespaces.yaml", namespace_objects),
            ("prerequisites.yaml", prerequisites),
            ("serviceaccounts.yaml", serviceaccounts),
        ]:
            (output / filename).write_text(yaml.safe_dump_all(objects, sort_keys=False))
            files[filename] = hashlib.sha256(
                (output / filename).read_bytes()
            ).hexdigest()
        for index, release in enumerate(target["releases"]):
            chart_name = f"release-{index}.tgz"
            chart_path = output / chart_name
            chart_path.write_bytes(package(checkout, release["chart"]))
            values = []
            for position, name in enumerate(release.get("values", [])):
                filename = f"release-{index}-values-{position}.yaml"
                (output / filename).write_bytes(relative(checkout, name).read_bytes())
                values.append(filename)
            command = [
                "helm",
                "template",
                release["name"],
                str(chart_path),
                "--namespace",
                release["namespace"],
                "--skip-crds",
            ]
            if target.get("kubernetes_version"):
                command += ["--kube-version", target["kubernetes_version"]]
            for name in values:
                command += ["--values", str(output / name)]
            rendered = run(command, env=env, capture=True)
            objects = [x for x in yaml.safe_load_all(rendered) if x is not None]
            need(
                objects
                and all(
                    isinstance(x, dict) and x.get("kind") != "CustomResourceDefinition"
                    for x in objects
                ),
                "CRD lifecycle must be managed separately; templated CRDs are rejected",
            )
            rendered_name = f"release-{index}-rendered.yaml"
            (output / rendered_name).write_text(rendered + "\n")
            for filename in [chart_name, *values, rendered_name]:
                files[filename] = hashlib.sha256(
                    (output / filename).read_bytes()
                ).hexdigest()
            releases.append(
                dict(
                    name=release["name"],
                    namespace=release["namespace"],
                    chart=chart_name,
                    chart_name=release["chart"]["name"],
                    chart_version=release["chart"]["version"],
                    values=values,
                    timeout_seconds=release.get("timeout_seconds", 600),
                )
            )
        receipt = dict(
            schema_version=1,
            source_commit=commit,
            tenant_id=config["tenant_id"],
            target={k: v for k, v in target.items() if k != "releases"},
            releases=releases,
            crds=crds,
            crd_policies=crd_policies,
            files=files,
        )
        (output / "platform.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt


def verify(bundle, expected):
    need(
        isinstance(expected, str) and SHA.fullmatch(expected),
        "Independent platform receipt SHA256 required",
    )
    path = bundle / "platform.json"
    need(
        hashlib.sha256(path.read_bytes()).hexdigest() == expected,
        "Platform receipt changed after review",
    )
    receipt = json.loads(path.read_text())
    need(
        receipt.get("schema_version") == 1
        and COMMIT.fullmatch(receipt.get("source_commit", ""))
        and isinstance(receipt.get("target"), dict)
        and isinstance(receipt.get("releases"), list)
        and receipt["releases"],
        "Invalid platform receipt",
    )
    for name, digest in receipt["files"].items():
        need(
            SHA.fullmatch(digest)
            and hashlib.sha256(relative(bundle, name).read_bytes()).hexdigest()
            == digest,
            "Platform bundle changed after review",
        )
    need(
        all(
            name in receipt["files"]
            for name in [
                "namespaces.yaml",
                "prerequisites.yaml",
                "serviceaccounts.yaml",
                *receipt.get("crds", []),
                *receipt.get("crd_policies", []),
            ]
        ),
        "Unbound platform manifest or CRD input",
    )
    for release in receipt["releases"]:
        need(
            all(
                name in receipt["files"]
                for name in [release["chart"], *release["values"]]
            ),
            "Unbound chart or values input",
        )
    return receipt


def apply(bundle, expected, *, yes=False):
    receipt = verify(bundle, expected)
    target = receipt["target"]
    if not yes:
        need(
            sys.stdin.isatty()
            and input(
                f"Upgrade platform services on {target['environment']}/{target['region']}/{target['slot']}? Type platform: "
            )
            == "platform",
            "Platform operation cancelled",
        )
    account(receipt["tenant_id"], target["subscription_id"])
    with tempfile.TemporaryDirectory(prefix="aks-platform-context-") as temporary:
        root = Path(temporary)
        kubeconfig = root / "kubeconfig"
        run(
            [
                "az",
                "aks",
                "get-credentials",
                "--subscription",
                target["subscription_id"],
                "--resource-group",
                target["resource_group"],
                "--name",
                target["cluster_name"],
                "--file",
                str(kubeconfig),
                "--format",
                "exec",
                "--overwrite-existing",
            ]
        )
        kubeconfig.chmod(0o600)
        run(
            [
                "kubelogin",
                "convert-kubeconfig",
                "--login",
                "azurecli",
                "--kubeconfig",
                str(kubeconfig),
            ]
        )
        env = helm_environment(root / "helm")
        env["KUBECONFIG"] = str(kubeconfig)
        kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]

        def apply_manifest(name, *, crd=False):
            path = bundle / name
            if not path.read_text().strip():
                return
            verify(bundle, expected)
            options = (
                ["--server-side", "--field-manager=aks-platform-crds"] if crd else []
            )
            run(
                kubectl
                + [
                    "apply",
                    "--dry-run=server",
                    "--validate=strict",
                    "--filename",
                    str(path),
                ]
                + options,
                env=env,
            )
            verify(bundle, expected)
            run(
                kubectl
                + ["apply", "--validate=strict", "--filename", str(path)]
                + options,
                env=env,
            )
            if crd:
                run(
                    kubectl
                    + [
                        "wait",
                        "--for=condition=Established",
                        "--timeout=120s",
                        "--filename",
                        str(path),
                    ],
                    env=env,
                )

        # Existing namespaces can carry bootstrap-owned Pod Security labels. Never
        # apply a name-only namespace over another owner's last-applied metadata.
        for namespace in target["namespaces"]:
            existing = run(
                kubectl
                + [
                    "get",
                    "namespace",
                    namespace,
                    "--ignore-not-found",
                    "--output=name",
                ],
                env=env,
                capture=True,
            )
            if not existing:
                verify(bundle, expected)
                run(
                    kubectl + ["create", "namespace", namespace, "--dry-run=server"],
                    env=env,
                )
                run(kubectl + ["create", "namespace", namespace], env=env)
        for filename in receipt.get("crds", []):
            need(filename in receipt["files"], "Unbound CRD bundle")
            apply_manifest(filename, crd=True)
        for filename in receipt.get("crd_policies", []):
            apply_manifest(filename)
        apply_manifest("serviceaccounts.yaml")
        for release in receipt["releases"]:
            verify(bundle, expected)
            command = [
                "helm",
                "upgrade",
                "--install",
                release["name"],
                str(bundle / release["chart"]),
                "--namespace",
                release["namespace"],
                "--create-namespace",
                "--kubeconfig",
                str(kubeconfig),
                "--wait=watcher",
                "--wait-for-jobs",
                "--timeout",
                str(release["timeout_seconds"]) + "s",
                "--history-max",
                "10",
                "--hide-notes",
                "--skip-crds",
            ]
            for name in release["values"]:
                command += ["--values", str(bundle / name)]
            run(command, env=env)
        apply_manifest("prerequisites.yaml")
        for namespace in target["namespaces"]:
            run(
                kubectl
                + [
                    "get",
                    "deployments,statefulsets,daemonsets,services",
                    "--namespace",
                    namespace,
                    "--output",
                    "wide",
                ],
                env=env,
            )
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "prepare", "apply"])
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--config", default="platform.services.json")
    parser.add_argument("--commit")
    parser.add_argument("--environment")
    parser.add_argument("--region")
    parser.add_argument("--slot", choices=["aks01", "aks02"])
    parser.add_argument("--output", type=Path, default=Path(".aks-delivery/platform"))
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--github-output")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.command == "validate":
        load_config(args.source.resolve(), args.config)
        print("Platform services configuration is valid")
        return
    receipt = (
        prepare(
            args.source.resolve(),
            args.config,
            args.commit,
            args.environment,
            args.region,
            args.slot,
            args.output,
        )
        if args.command == "prepare"
        else apply(args.output, args.receipt_sha256, yes=args.yes)
    )
    result = dict(
        receipt_sha256=hashlib.sha256(
            (args.output / "platform.json").read_bytes()
        ).hexdigest(),
        approval_environment=receipt["target"]["approval_environment"],
        tenant_id=receipt["tenant_id"],
        subscription_id=receipt["target"]["subscription_id"],
    )
    if args.github_output:
        with open(args.github_output, "a") as output:
            for key, value in result.items():
                need(
                    isinstance(value, str) and "\n" not in value,
                    "Invalid platform workflow output",
                )
                output.write(f"{key}={value}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, tarfile.TarError) as error:
        raise SystemExit("Platform services stopped: " + str(error))
