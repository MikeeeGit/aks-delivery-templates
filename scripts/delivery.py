#!/usr/bin/env python3
"""Explicit immutable-image Kustomize delivery; cloud calls only in build/deploy/rollback."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import yaml

from verify_service import verify_service, verify_ingress

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}$")
ALLOWED_KINDS = {
    "Deployment": "apps/v1",
    "Ingress": "networking.k8s.io/v1",
    "HTTPRoute": "gateway.networking.k8s.io/v1",
    "Service": "v1",
    "ConfigMap": "v1",
    "ServiceAccount": "v1",
    "SecretProviderClass": "secrets-store.csi.x-k8s.io/v1",
    "NetworkPolicy": "networking.k8s.io/v1",
    "HorizontalPodAutoscaler": "autoscaling/v2",
    "PodDisruptionBudget": "policy/v1",
}


def run(args, *, cwd=None, env=None, capture=False):
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def need(condition, message):
    if not condition:
        raise ValueError(message)


def relative(root: Path, name: str, *, exists=True) -> Path:
    need(
        isinstance(name, str) and name and not Path(name).is_absolute(),
        "Paths must be relative to the source repository",
    )
    path = root / name
    need(".." not in Path(name).parts, "Parent path traversal is forbidden")
    need(
        not any(
            part.is_symlink() for part in [path, *path.parents] if part != root.parent
        ),
        "Symlink inputs are forbidden",
    )
    need(
        path.resolve().is_relative_to(root.resolve()), "Input escapes source repository"
    )
    if exists:
        need(path.exists(), "Required source input is missing")
    return path


def load_config(root: Path, name="delivery.apps.json"):
    value = json.loads(relative(root, name).read_text())
    need(value.get("schema_version") == 1, "Unsupported delivery config schema")
    need(
        UUID.fullmatch(value.get("tenant_id", "")),
        "An explicit tenant UUID is required",
    )
    need(
        "subscription_id" not in value,
        "Use registry.subscription_id and each target.subscription_id explicitly",
    )
    registry = value["registry"]
    need(
        UUID.fullmatch(registry.get("subscription_id", "")),
        "Explicit registry subscription UUID is required",
    )
    need(re.fullmatch(r"[a-z0-9]{5,50}", registry["name"]), "Invalid ACR name")
    need(
        registry["login_server"] == registry["name"] + ".azurecr.io",
        "ACR login server must match its name (public Azure)",
    )
    need(
        re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", registry["repository"]),
        "Invalid image repository",
    )
    for field in ["context", "dockerfile"]:
        relative(root, value["build"][field])
    need(
        re.fullmatch(r"[a-z0-9][a-z0-9./_-]*", value["build"]["image_name"]),
        "Invalid Kustomize image name",
    )
    keys = set()
    for target in value["targets"]:
        need(
            UUID.fullmatch(target.get("subscription_id", "")),
            "Explicit target subscription UUID is required",
        )
        for field in [
            "environment",
            "region",
            "namespace",
            "deployment",
            "approval_environment",
        ]:
            need(NAME.fullmatch(target.get(field, "")), "Invalid target identifier")
        need(
            target.get("slot") in ["aks01", "aks02"], "Use explicit aks01 or aks02 slot"
        )
        for field in ["resource_group", "cluster_name"]:
            need(
                re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.()-]{0,89}", target.get(field, "")
                ),
                "Invalid Azure target name",
            )
        relative(root, target["overlay"])
        if "verification" in target:
            verification = target["verification"]
            need(
                isinstance(verification, dict)
                and NAME.fullmatch(verification.get("service", "")),
                "Invalid verification service",
            )
            need(
                type(verification.get("port")) is int
                and 1 <= verification["port"] <= 65535,
                "Invalid verification service port",
            )
            for field in ["readiness_path", "version_path"]:
                need(
                    isinstance(verification.get(field), str)
                    and re.fullmatch(r"/[A-Za-z0-9/_-]{1,100}", verification[field]),
                    "Verification paths must be literal relative HTTP paths",
                )
            if "ingress" in verification:
                ingress = verification["ingress"]
                need(
                    isinstance(ingress, dict)
                    and NAME.fullmatch(ingress.get("gateway", "")),
                    "Invalid verification Gateway",
                )
                need(
                    isinstance(ingress.get("http_routes"), list)
                    and ingress["http_routes"]
                    and all(
                        isinstance(x, str) and NAME.fullmatch(x)
                        for x in ingress["http_routes"]
                    ),
                    "Explicit HTTPRoute names are required",
                )
                need(
                    isinstance(ingress.get("hosts"), list)
                    and ingress["hosts"]
                    and all(
                        isinstance(x, str)
                        and len(x) <= 253
                        and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", x)
                        for x in ingress["hosts"]
                    ),
                    "Explicit TLS verification hostnames are required",
                )
                if ingress.get("ca_file"):
                    need(
                        relative(root, ingress["ca_file"]).is_file(),
                        "Verification CA must be a committed public PEM file",
                    )
        key = (target["environment"], target["region"], target["slot"])
        need(key not in keys, "Duplicate environment/region/slot target")
        keys.add(key)
    need(keys, "At least one explicit target is required")
    return value


def select(config, environment, region, slot):
    matches = [
        t
        for t in config["targets"]
        if (t["environment"], t["region"], t["slot"]) == (environment, region, slot)
    ]
    need(len(matches) == 1, "Requested environment/region/slot does not exist")
    return matches[0]


def snapshot(root: Path, commit: str, destination: Path):
    need(COMMIT.fullmatch(commit), "Source commit must be a full lowercase SHA")
    found = run(["git", "rev-parse", f"{commit}^{{commit}}"], cwd=root, capture=True)
    need(found == commit, "Source commit does not resolve exactly")
    raw = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            need(
                member.isfile() or member.isdir(),
                "Source archives must not contain links or special files",
            )
            target = destination / member.name
            need(
                target.resolve().is_relative_to(destination.resolve()),
                "Archive path escapes snapshot",
            )
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())


def inspect_manifests(text, target, image):
    objects = [item for item in yaml.safe_load_all(text) if item is not None]
    found = False
    for item in objects:
        need(
            isinstance(item, dict) and item.get("kind") in ALLOWED_KINDS,
            "Only reviewed namespace-scoped application kinds are allowed; bootstrap namespaces separately",
        )
        need(
            item.get("apiVersion") == ALLOWED_KINDS[item["kind"]],
            "Unapproved resource API group/version",
        )
        metadata = item.get("metadata", {})
        need(
            metadata.get("namespace") == target["namespace"],
            "Every object must explicitly target the selected namespace",
        )
        need(NAME.fullmatch(metadata.get("name", "")), "Invalid resource name")
        if item["kind"] == "Deployment":
            template = item["spec"]["template"]
            containers = template["spec"].get("containers", []) + template["spec"].get(
                "initContainers", []
            )
            need(
                containers
                and all(IMAGE.fullmatch(c.get("image", "")) for c in containers),
                "All pod images must use immutable repository@sha256 references",
            )
            if metadata["name"] == target["deployment"]:
                need(
                    any(c["image"] == image for c in containers),
                    "Selected deployment does not contain the promoted image digest",
                )
                found = True
    need(found, "Selected Deployment is absent")
    return objects


def inspect_kustomization(path, source):
    data = yaml.safe_load(path.read_text())
    need(isinstance(data, dict), "Invalid Kustomization")
    need(
        not any(
            data.get(key)
            for key in [
                "generators",
                "transformers",
                "validators",
                "helmCharts",
                "secretGenerator",
            ]
        ),
        "External plugins, Helm and generated Secrets are outside this contract",
    )

    def local(name):
        need(
            isinstance(name, str) and name and not Path(name).is_absolute(),
            "Kustomize references must be local relative paths",
        )
        candidate = (path.parent / name).resolve()
        need(
            candidate.is_relative_to(source.resolve()) and candidate.exists(),
            "Vendor every Kustomize input into the reviewed source commit",
        )

    for key in ["resources", "bases", "components", "configurations", "crds"]:
        for name in data.get(key, []):
            local(name)
    for key in ["patches", "patchesJson6902", "replacements"]:
        for item in data.get(key, []):
            need(
                isinstance(item, dict),
                "Use explicit inline patch or local path objects",
            )
            if "path" in item:
                local(item["path"])
    for item in data.get("patchesStrategicMerge", []):
        need(isinstance(item, str), "Invalid strategic merge patch")
        if "\n" not in item:
            local(item)
    if data.get("openapi", {}).get("path"):
        local(data["openapi"]["path"])
    for generator in data.get("configMapGenerator", []):
        for name in generator.get("files", []):
            local(name.split("=", 1)[-1])
        for name in generator.get("envs", []):
            local(name)
        if generator.get("env"):
            local(generator["env"])


def render(root, config_name, commit, digest, environment, region, slot, output):
    need(
        DIGEST.fullmatch(digest),
        "Image digest must be sha256 plus 64 lowercase hex digits",
    )
    with tempfile.TemporaryDirectory(prefix="aks-render-") as temporary:
        wrapper = Path(temporary)
        source = wrapper / "source"
        source.mkdir()
        snapshot(root, commit, source)
        config = load_config(source, config_name)
        target = select(config, environment, region, slot)
        image_repository = (
            config["registry"]["login_server"] + "/" + config["registry"]["repository"]
        )
        image = image_repository + "@" + digest
        # Reject network bases and plugins: all app manifests must be included in this source commit.
        for path in source.rglob("*"):
            if path.name.lower() in [
                "kustomization.yaml",
                "kustomization.yml",
                "kustomization",
            ]:
                inspect_kustomization(path, source)
        (wrapper / "kustomization.yaml").write_text(
            json.dumps(
                {
                    "apiVersion": "kustomize.config.k8s.io/v1beta1",
                    "kind": "Kustomization",
                    "resources": ["source/" + target["overlay"]],
                    "namespace": target["namespace"],
                    "images": [
                        {
                            "name": config["build"]["image_name"],
                            "newName": image_repository,
                            "digest": digest,
                        }
                    ],
                }
            )
        )
        text = run(
            [
                "kubectl",
                "kustomize",
                str(wrapper),
                "--load-restrictor=LoadRestrictionsRootOnly",
            ],
            capture=True,
        )
        inspect_manifests(text, target, image)
        output.mkdir(parents=True, exist_ok=True)
        (output / "manifest.yaml").write_text(text + "\n")
        receipt = {
            "schema_version": 1,
            "source_commit": commit,
            "image": image,
            "image_digest": digest,
            "image_repository": image_repository,
            "target": target,
            "tenant_id": config["tenant_id"],
            "subscription_id": target["subscription_id"],
            "manifest_sha256": hashlib.sha256((text + "\n").encode()).hexdigest(),
        }
        ingress = target.get("verification", {}).get("ingress", {})
        if ingress.get("ca_file"):
            ca = relative(source, ingress["ca_file"]).read_bytes()
            (output / "ingress-ca.pem").write_bytes(ca)
            receipt["verification_ca_sha256"] = hashlib.sha256(ca).hexdigest()
        (output / "release.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt


def account(tenant, subscription):
    current = json.loads(
        run(
            [
                "az",
                "account",
                "show",
                "--subscription",
                subscription,
                "--output",
                "json",
            ],
            capture=True,
        )
    )
    need(
        current["tenantId"].lower() == tenant.lower()
        and current["id"].lower() == subscription.lower(),
        "Azure account does not match the explicit tenant/subscription",
    )
    run(["az", "account", "set", "--subscription", subscription])


def build(root, config_name, commit, output):
    need(
        not output.exists(),
        "Build output already exists; choose a fresh output directory",
    )
    with tempfile.TemporaryDirectory(prefix="aks-build-") as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        snapshot(root, commit, source)
        config = load_config(source, config_name)
        registry = config["registry"]
        account(config["tenant_id"], registry["subscription_id"])
        build_env = dict(
            os.environ, DOCKER_CONFIG=str(Path(temporary) / "docker-config")
        )
        Path(build_env["DOCKER_CONFIG"]).mkdir(mode=0o700)
        run(
            [
                "az",
                "acr",
                "login",
                "--name",
                registry["name"],
                "--subscription",
                registry["subscription_id"],
            ],
            env=build_env,
        )
        repository = registry["login_server"] + "/" + registry["repository"]
        metadata = Path(temporary) / "metadata.json"
        # A unique tag is only a push handle. All downstream deployment uses the returned digest.
        tag = "build-" + commit[:12] + "-" + os.urandom(6).hex()
        builder = "aks-" + os.urandom(8).hex()
        buildkit = json.loads(
            (Path(__file__).resolve().parents[1] / "build-tools.json").read_text()
        )["buildkit_image"]
        need(IMAGE.fullmatch(buildkit), "BuildKit must be pinned by digest")
        run(
            [
                "docker",
                "buildx",
                "create",
                "--name",
                builder,
                "--driver",
                "docker-container",
                "--driver-opt",
                "image=" + buildkit,
            ],
            env=build_env,
        )
        try:
            run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--builder",
                    builder,
                    "--push",
                    "--platform",
                    "linux/amd64",
                    "--provenance=mode=min",
                    "--metadata-file",
                    str(metadata),
                    "--tag",
                    repository + ":" + tag,
                    "--label",
                    "org.opencontainers.image.revision=" + commit,
                    "--build-arg",
                    "BUILD_REVISION=" + commit,
                    "--file",
                    str(relative(source, config["build"]["dockerfile"])),
                    str(relative(source, config["build"]["context"])),
                ],
                env=build_env,
            )
        finally:
            run(["docker", "buildx", "rm", "--force", builder], env=build_env)
        digest = json.loads(metadata.read_text())["containerimage.digest"]
        need(
            DIGEST.fullmatch(digest), "Builder did not return an immutable image digest"
        )
        from image_scan import scan_image

        scan = scan_image(
            repository + "@" + digest,
            build_env["DOCKER_CONFIG"],
            output / "image-scan.json",
        )
        receipt = {
            "schema_version": 1,
            "source_commit": commit,
            "image_digest": digest,
            "image_repository": repository,
            "image": repository + "@" + digest,
            "security_scan": scan,
        }
        if os.environ.get("GITHUB_ACTIONS") == "true":
            receipt["producer"] = {
                "platform": "github",
                "run_id": os.environ["GITHUB_RUN_ID"],
            }
        elif os.environ.get("TF_BUILD", "").lower() == "true":
            receipt["producer"] = {
                "platform": "azure-devops",
                "run_id": os.environ["BUILD_BUILDID"],
            }
        output.mkdir(parents=True, exist_ok=True)
        (output / "release.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt


def verify_bundle(bundle, expected):
    receipt_path = bundle / "release.json"
    need(
        re.fullmatch(r"[0-9a-f]{64}", expected),
        "An independent release receipt SHA256 is required",
    )
    need(
        hashlib.sha256(receipt_path.read_bytes()).hexdigest() == expected,
        "Release receipt digest differs from the render job output",
    )
    receipt = json.loads(receipt_path.read_text())
    required = {
        "schema_version",
        "source_commit",
        "image_digest",
        "image_repository",
        "image",
        "target",
        "tenant_id",
        "manifest_sha256",
    }
    need(
        isinstance(receipt, dict) and required.issubset(receipt),
        "Incomplete release receipt",
    )
    target = receipt["target"]
    need(
        isinstance(target, dict)
        and {
            "environment",
            "region",
            "slot",
            "subscription_id",
            "resource_group",
            "cluster_name",
            "namespace",
            "deployment",
        }.issubset(target),
        "Incomplete release target",
    )
    need(
        UUID.fullmatch(receipt["tenant_id"])
        and UUID.fullmatch(target["subscription_id"]),
        "Invalid receipt tenant/subscription",
    )
    need(
        receipt["schema_version"] == 1 and COMMIT.fullmatch(receipt["source_commit"]),
        "Invalid release receipt",
    )
    need(
        DIGEST.fullmatch(receipt["image_digest"])
        and receipt["image"]
        == receipt["image_repository"] + "@" + receipt["image_digest"],
        "Invalid image binding",
    )
    manifest = bundle / "manifest.yaml"
    need(
        hashlib.sha256(manifest.read_bytes()).hexdigest() == receipt["manifest_sha256"],
        "Manifest changed after rendering",
    )
    inspect_manifests(manifest.read_text(), receipt["target"], receipt["image"])
    if target.get("verification", {}).get("ingress", {}).get("ca_file"):
        need(
            receipt.get("verification_ca_sha256")
            == hashlib.sha256((bundle / "ingress-ca.pem").read_bytes()).hexdigest(),
            "Verification CA changed after review",
        )
    return receipt


def _deploy_to_context(bundle, expected, *, kubeconfig, context):
    """Deploy a verified bundle through an adapter-verified Kubernetes context.

    The Azure CLI path retains its account and user-credential gates. A local
    adapter must independently prove ownership before invoking this internal API.
    """
    receipt = verify_bundle(bundle, expected)
    target = receipt["target"]
    kubeconfig = Path(kubeconfig)
    need(kubeconfig.is_file(), "An isolated kubeconfig file is required")
    need(
        isinstance(context, str) and context and not context.startswith("-")
        and not any(character.isspace() for character in context),
        "An explicit Kubernetes context is required",
    )
    env = dict(os.environ, KUBECONFIG=str(kubeconfig))
    base = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        context,
        "--namespace",
        target["namespace"],
    ]
    # default ServiceAccount proves namespace bootstrap without cluster-scoped Namespace privileges.
    run(base + ["get", "serviceaccount", "default", "--output", "name"], env=env)
    run(base + ["auth", "can-i", "patch", "deployments"], env=env)
    verify_bundle(bundle, expected)
    run(
        base
        + [
            "apply",
            "--dry-run=server",
            "--validate=strict",
            "--filename",
            str(bundle / "manifest.yaml"),
        ],
        env=env,
    )
    verify_bundle(bundle, expected)
    run(
        base
        + [
            "apply",
            "--validate=strict",
            "--filename",
            str(bundle / "manifest.yaml"),
        ],
        env=env,
    )
    run(
        base
        + [
            "rollout",
            "status",
            "deployment/" + target["deployment"],
            "--timeout=600s",
        ],
        env=env,
    )
    if target.get("verification"):
        verify_service(
            base,
            env,
            target["verification"],
            target["slot"],
            receipt["source_commit"],
        )
        if target["verification"].get("ingress"):
            verify_bundle(bundle, expected)
            ca = (
                bundle / "ingress-ca.pem"
                if target["verification"]["ingress"].get("ca_file")
                else None
            )
            verify_ingress(
                base,
                env,
                target["verification"],
                target["slot"],
                receipt["source_commit"],
                target["namespace"],
                run,
                ca,
            )
    return receipt


def deploy(bundle, expected, *, yes=False):
    receipt = verify_bundle(bundle, expected)
    target = receipt["target"]
    if not yes:
        need(
            sys.stdin.isatty(),
            "Non-interactive deployment requires explicit --yes and external approval controls",
        )
        confirmation = input(
            f"Deploy {receipt['image']} to {target['environment']}/{target['region']}/{target['slot']}? Type deploy: "
        )
        need(confirmation == "deploy", "Deployment cancelled")
    account(receipt["tenant_id"], target["subscription_id"])
    with tempfile.TemporaryDirectory(prefix="aks-context-") as temporary:
        kubeconfig = Path(temporary) / "config"
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
                "--context",
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
        return _deploy_to_context(
            bundle, expected, kubeconfig=kubeconfig, context=target["cluster_name"],
        )


def outputs(receipt, output, destination=None):
    values = {k: receipt[k] for k in ["source_commit", "image", "image_digest"]}
    values["receipt_sha256"] = hashlib.sha256(
        (output / "release.json").read_bytes()
    ).hexdigest()
    if "target" in receipt:
        values["approval_environment"] = receipt["target"]["approval_environment"]
        values["subscription_id"] = receipt["target"]["subscription_id"]
        values["tenant_id"] = receipt["tenant_id"]
    if destination:
        with open(destination, "a") as handle:
            for key, value in values.items():
                need("\n" not in value and "\r" not in value, "Invalid workflow output")
                handle.write(f"{key}={value}\n")
    print(json.dumps(values))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["validate", "resolve", "build", "render", "deploy"]
    )
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--config", default="delivery.apps.json")
    parser.add_argument("--commit")
    parser.add_argument("--digest")
    parser.add_argument("--environment")
    parser.add_argument("--region")
    parser.add_argument("--slot", choices=["aks01", "aks02"])
    parser.add_argument("--output", type=Path, default=Path(".aks-delivery"))
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--github-output")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.command in ["validate", "resolve"]:
        config = load_config(args.source.resolve(), args.config)
        if args.github_output:
            values = {
                "tenant_id": config["tenant_id"],
                "registry_subscription_id": config["registry"]["subscription_id"],
            }
            if args.command == "resolve":
                target = select(config, args.environment, args.region, args.slot)
                values.update(
                    {
                        key: target[key]
                        for key in ["approval_environment", "subscription_id"]
                    }
                )
            with open(args.github_output, "a") as handle:
                for key, value in values.items():
                    handle.write(f"{key}={value}\n")
        if args.command == "resolve":
            target = select(config, args.environment, args.region, args.slot)
            print(json.dumps(target))
        else:
            print("Application delivery configuration is valid")
        return
    if args.command == "build":
        result = build(args.source.resolve(), args.config, args.commit, args.output)
    elif args.command == "render":
        result = render(
            args.source.resolve(),
            args.config,
            args.commit,
            args.digest,
            args.environment,
            args.region,
            args.slot,
            args.output,
        )
    else:
        result = deploy(args.output, args.receipt_sha256, yes=args.yes)
    outputs(result, args.output, args.github_output)


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        OSError,
    ) as error:
        print(f"Delivery failed: {error}", file=sys.stderr)
        sys.exit(1)
