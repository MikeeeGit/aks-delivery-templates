#!/usr/bin/env python3
"""Separately approved namespace/RBAC bootstrap for an existing private AKS cluster."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import native_authorization

from delivery import (
    UUID,
    NAME,
    account,
    load_config,
    need,
    relative,
    run,
    select,
    snapshot,
)

WRITER = "a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb"
CLUSTER_USER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"


def configuration(source, config_name, bootstrap_name, environment, region, slot):
    app = load_config(source, config_name)
    target = select(app, environment, region, slot)
    data = json.loads(relative(source, bootstrap_name).read_text())
    need(data.get("schema_version") == 1, "Unsupported bootstrap schema")
    selected = select(data, environment, region, slot)
    need(
        NAME.fullmatch(selected.get("approval_environment", "")),
        "Invalid bootstrap approval environment",
    )
    need(
        selected["approval_environment"] != target["approval_environment"],
        "Bootstrap must use a separate approval environment",
    )
    mode = selected.get("authorization_mode", "azure_rbac")
    need(mode in {"azure_rbac", "kubernetes_rbac"}, "Unsupported authorization_mode")
    principals = selected.get("deploy_principal_object_ids", [])
    need(
        isinstance(principals, list)
        and (principals or mode == "kubernetes_rbac")
        and len(principals) == len(set(principals))
        and all(isinstance(x, str) and UUID.fullmatch(x) for x in principals),
        "Provide unique deployment service-principal object UUIDs, not client IDs",
    )
    need(
        re.fullmatch(r"v1\.[0-9]{2}", selected.get("pod_security_version", "")),
        "Pin pod_security_version to the cluster Kubernetes minor, for example v1.35",
    )
    need(
        isinstance(selected.get("require_key_vault_csi", True), bool),
        "require_key_vault_csi must be boolean",
    )
    if mode == "kubernetes_rbac":
        native_authorization.usernames(selected)
    return app, target, selected


def apply(
    source, config_name, bootstrap_name, commit, environment, region, slot, *, yes=False
):
    with tempfile.TemporaryDirectory(prefix="aks-bootstrap-") as temporary:
        root = Path(temporary)
        checkout = root / "source"
        checkout.mkdir()
        snapshot(source, commit, checkout)
        app, target, settings = configuration(
            checkout, config_name, bootstrap_name, environment, region, slot
        )
        mode = settings.get("authorization_mode", "azure_rbac")
        if not yes:
            need(
                sys.stdin.isatty()
                and input(
                    f"Bootstrap namespace {target['namespace']} on {slot}? Type bootstrap: "
                )
                == "bootstrap",
                "Bootstrap cancelled; CI requires --yes and separate environment approval",
            )
        account(app["tenant_id"], target["subscription_id"])
        cluster_id = f"/subscriptions/{target['subscription_id']}/resourceGroups/{target['resource_group']}/providers/Microsoft.ContainerService/managedClusters/{target['cluster_name']}"
        azure_target = [
            "--subscription",
            target["subscription_id"],
            "--resource-group",
            target["resource_group"],
            "--name",
            target["cluster_name"],
        ]
        cluster = json.loads(
            run(["az", "aks", "show", *azure_target, "--output", "json"], capture=True)
        )
        need(
            cluster.get("id", "").lower() == cluster_id.lower(),
            "Cluster identity mismatch",
        )
        need(
            cluster.get("provisioningState") == "Succeeded",
            "AKS provisioning has not succeeded",
        )
        aad = cluster.get("aadProfile") or {}
        need(
            aad.get("managed") is True
            and aad.get("enableAzureRbac") is (mode == "azure_rbac")
            and aad.get("tenantId", "").lower() == app["tenant_id"].lower(),
            "Managed Entra and the selected authorization mode in the selected tenant are required",
        )
        need(
            cluster.get("disableLocalAccounts") is True
            and (cluster.get("apiServerAccessProfile") or {}).get(
                "enablePrivateCluster"
            )
            is True,
            "Private AKS with local accounts disabled is required",
        )
        need(
            (cluster.get("oidcIssuerProfile") or {}).get("enabled") is True
            and (cluster.get("securityProfile") or {})
            .get("workloadIdentity", {})
            .get("enabled")
            is True,
            "Enable AKS OIDC and workload identity in the infrastructure layer",
        )
        version = cluster.get("currentKubernetesVersion") or cluster.get(
            "kubernetesVersion", ""
        )
        need(
            settings["pod_security_version"] == "v" + ".".join(version.split(".")[:2]),
            "Pod Security version must match the cluster Kubernetes minor",
        )
        if settings.get("require_key_vault_csi", True):
            need(
                (cluster.get("addonProfiles") or {})
                .get("azureKeyvaultSecretsProvider", {})
                .get("enabled")
                is True,
                "Enable the Key Vault CSI add-on in the infrastructure layer",
            )
        kubeconfig = root / "kubeconfig"
        run(
            [
                "az",
                "aks",
                "get-credentials",
                *azure_target,
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
        env = dict(os.environ, KUBECONFIG=str(kubeconfig))
        base = ["kubectl", "--kubeconfig", str(kubeconfig)]
        run(base + ["auth", "can-i", "create", "namespaces"], env=env)
        if settings.get("require_key_vault_csi", True):
            run(
                base
                + [
                    "get",
                    "customresourcedefinition",
                    "secretproviderclasses.secrets-store.csi.x-k8s.io",
                    "--output",
                    "name",
                ],
                env=env,
            )
        namespace = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": target["namespace"],
                "labels": {
                    "app.kubernetes.io/managed-by": "aks-delivery-templates",
                    "pod-security.kubernetes.io/enforce": "restricted",
                    "pod-security.kubernetes.io/enforce-version": settings[
                        "pod_security_version"
                    ],
                    "pod-security.kubernetes.io/audit": "restricted",
                    "pod-security.kubernetes.io/audit-version": settings[
                        "pod_security_version"
                    ],
                    "pod-security.kubernetes.io/warn": "restricted",
                    "pod-security.kubernetes.io/warn-version": settings[
                        "pod_security_version"
                    ],
                },
            },
        }
        manifest = root / "namespace.json"
        manifest.write_text(json.dumps(namespace))
        run(
            base
            + ["apply", "--dry-run=server", "--validate=strict", "-f", str(manifest)],
            env=env,
        )
        run(base + ["apply", "--validate=strict", "-f", str(manifest)], env=env)
        if mode == "kubernetes_rbac":
            for index, resource in enumerate(native_authorization.application_resources(settings, target["namespace"])):
                authorization = root / f"authorization-{index}.json"
                authorization.write_text(json.dumps(resource))
                options = ["--server-side", "--field-manager=aks-delivery-authorization", "--validate=strict"]
                run(base + ["apply", *options, "--dry-run=server", "-f", str(authorization)], env=env)
                run(base + ["apply", *options, "-f", str(authorization)], env=env)
            return {
                "schema_version": 1, "source_commit": commit, "target": target,
                "bootstrap_environment": settings["approval_environment"],
                "namespace": target["namespace"], "authorization_mode": mode,
                "role_assignments": 0, "role_binding": "aks-delivery-application",
            }
        for principal in settings["deploy_principal_object_ids"]:
            for role, scope in [
                (CLUSTER_USER, cluster_id),
                (WRITER, cluster_id + "/namespaces/" + target["namespace"]),
            ]:
                assignment = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "|".join([scope.lower(), principal.lower(), role]),
                    )
                )
                run(
                    [
                        "az",
                        "role",
                        "assignment",
                        "create",
                        "--name",
                        assignment,
                        "--assignee-object-id",
                        principal,
                        "--assignee-principal-type",
                        "ServicePrincipal",
                        "--role",
                        role,
                        "--scope",
                        scope,
                        "--subscription",
                        target["subscription_id"],
                        "--output",
                        "none",
                    ]
                )
        return {
            "schema_version": 1,
            "source_commit": commit,
            "target": target,
            "bootstrap_environment": settings["approval_environment"],
            "namespace": target["namespace"],
            "role_assignments": 2 * len(settings["deploy_principal_object_ids"]),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["resolve", "apply", "identity"])
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--config", default="delivery.apps.json")
    parser.add_argument("--bootstrap-config", default="bootstrap.apps.json")
    parser.add_argument("--commit")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--slot", choices=["aks01", "aks02"], required=True)
    parser.add_argument("--github-output")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--client-id", help="Expected CI client UUID for identity discovery")
    parser.add_argument("--identity-output", type=Path, help="Create a new private identity record")
    args = parser.parse_args()
    if args.command == "identity":
        need(args.identity_output is not None, "--identity-output is required")
        app = load_config(args.source, args.config)
        target = select(app, args.environment, args.region, args.slot)
        result = native_authorization.discover(app, target, args.client_id)
        with args.identity_output.open("x") as output:
            json.dump(result, output, indent=2)
            output.write("\n")
        args.identity_output.chmod(0o600)
    elif args.command == "resolve":
        app, target, settings = configuration(
            args.source,
            args.config,
            args.bootstrap_config,
            args.environment,
            args.region,
            args.slot,
        )
        result = {
            "tenant_id": app["tenant_id"],
            "subscription_id": target["subscription_id"],
            "approval_environment": settings["approval_environment"],
        }
    else:
        result = apply(
            args.source,
            args.config,
            args.bootstrap_config,
            args.commit,
            args.environment,
            args.region,
            args.slot,
            yes=args.yes,
        )
    if args.github_output:
        with open(args.github_output, "a") as output:
            for key, value in result.items():
                need(
                    isinstance(value, str) and "\n" not in value,
                    "Invalid workflow output",
                )
                output.write(f"{key}={value}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit("Bootstrap stopped: " + str(error))
