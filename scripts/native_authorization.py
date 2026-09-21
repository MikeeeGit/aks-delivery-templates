"""Reviewed native Kubernetes authorization for managed-Entra AKS."""
import json
import os
from pathlib import Path
import tempfile

from delivery import UUID, account, need, run


def usernames(settings, prefix="deploy"):
    principals = settings.get(prefix + "_principal_object_ids", [])
    names = settings.get(prefix + "_kubernetes_usernames", {})
    need(
        isinstance(principals, list)
        and prefix + "_principal_object_ids" in settings
        and len(principals) == len(set(principals))
        and all(isinstance(value, str) and UUID.fullmatch(value) for value in principals),
        "Provide explicit unique " + prefix + " principal object UUIDs",
    )
    need(
        isinstance(names, dict)
        and prefix + "_kubernetes_usernames" in settings
        and set(names) == set(principals)
        and all(
            isinstance(value, str)
            and 0 < len(value) <= 512
            and value == value.strip()
            and not any(character.isspace() for character in value)
            and not value.lower().startswith("system:")
            for value in names.values()
        )
        and len(set(names.values())) == len(names),
        "Provide one observed Kubernetes username per " + prefix + " principal; system identities are forbidden",
    )
    return sorted(names.values())


def subjects(settings, prefix="deploy"):
    return [
        {"kind": "User", "apiGroup": "rbac.authorization.k8s.io", "name": name}
        for name in usernames(settings, prefix)
    ]


def application_resources(settings, namespace):
    """One authoritative binding per application namespace, with no role escalation."""
    write = ["get", "list", "watch", "create", "update", "patch"]
    read = ["get", "list", "watch"]
    rules = [
        {"apiGroups": [""], "resources": ["services", "configmaps", "serviceaccounts"], "verbs": write},
        {"apiGroups": [""], "resources": ["pods", "events"], "verbs": read},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods/portforward"], "verbs": ["create"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": write},
        {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": read},
        {"apiGroups": ["networking.k8s.io"], "resources": ["ingresses", "networkpolicies"], "verbs": write},
        {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"], "verbs": write},
        {"apiGroups": ["policy"], "resources": ["poddisruptionbudgets"], "verbs": write},
        {"apiGroups": ["gateway.networking.k8s.io"], "resources": ["httproutes"], "verbs": write},
        {"apiGroups": ["gateway.networking.k8s.io"], "resources": ["gateways"], "verbs": ["get"]},
        {"apiGroups": ["secrets-store.csi.x-k8s.io"], "resources": ["secretproviderclasses"], "verbs": write},
        {"apiGroups": ["secrets-store.csi.x-k8s.io"], "resources": ["secretproviderclasspodstatuses"], "verbs": read},
    ]
    metadata = {"name": "aks-delivery-application", "namespace": namespace}
    return [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": metadata, "rules": rules},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": metadata,
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": metadata["name"]},
            "subjects": subjects(settings),
        },
    ]


def discover(app, target, client_id):
    """Read the authenticated CI identity; never infer the Kubernetes subject."""
    need(UUID.fullmatch(client_id or ""), "An expected CI client UUID is required")
    account(app["tenant_id"], target["subscription_id"])
    signed_in = json.loads(run(["az", "account", "show", "--output", "json"], capture=True))
    user = signed_in.get("user", {})
    need(
        user.get("type", "").lower() == "serviceprincipal"
        and user.get("name", "").lower() == client_id.lower(),
        "Authenticate as the expected CI service principal before identity discovery",
    )
    cluster_id = f"/subscriptions/{target['subscription_id']}/resourceGroups/{target['resource_group']}/providers/Microsoft.ContainerService/managedClusters/{target['cluster_name']}"
    with tempfile.TemporaryDirectory(prefix="aks-native-identity-") as temporary:
        kubeconfig = Path(temporary) / "kubeconfig"
        run([
            "az", "aks", "get-credentials", "--subscription", target["subscription_id"],
            "--resource-group", target["resource_group"], "--name", target["cluster_name"],
            "--file", str(kubeconfig), "--format", "exec", "--overwrite-existing",
        ])
        kubeconfig.chmod(0o600)
        run(["kubelogin", "convert-kubeconfig", "--login", "azurecli", "--kubeconfig", str(kubeconfig)])
        result = json.loads(run(
            ["kubectl", "--kubeconfig", str(kubeconfig), "auth", "whoami", "--output", "json"],
            env=dict(os.environ, KUBECONFIG=str(kubeconfig)), capture=True,
        ))
    need(result.get("kind") == "SelfSubjectReview", "Expected the API server's SelfSubjectReview")
    info = result.get("status", {}).get("userInfo", {})
    username = info.get("username", "")
    usernames({"deploy_principal_object_ids": [client_id], "deploy_kubernetes_usernames": {client_id: username}})
    return {
        "schema_version": 1, "kind": "aks-kubernetes-identity", "tenant_id": app["tenant_id"],
        "subscription_id": target["subscription_id"], "cluster_id": cluster_id,
        "client_id": client_id, "username": username,
    }
