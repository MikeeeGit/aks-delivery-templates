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


def platform_contract(wrapped_outputs, delivery_target, tenant_id):
    """Validate the applied native-AKS target before evaluating privileged access."""
    def output(name):
        item = wrapped_outputs.get(name) if isinstance(wrapped_outputs, dict) else None
        need(isinstance(item, dict) and item.get("sensitive") is False and isinstance(item.get("value"), dict),
             "Use non-sensitive applied terraform output -json for " + name)
        return item["value"]

    need(isinstance(tenant_id, str) and UUID.fullmatch(tenant_id), "Expected tenant UUID")
    context = output("deployment_context")
    for key in ["environment", "region", "subscription_id"]:
        need(context.get(key) == delivery_target.get(key), "Applied deployment context mismatch: " + key)
    need(str(context.get("tenant_id", "")).lower() == tenant_id.lower(), "Applied tenant mismatch")
    cluster_id = (
        f"/subscriptions/{delivery_target['subscription_id']}/resourceGroups/"
        f"{delivery_target['resource_group']}/providers/Microsoft.ContainerService/"
        f"managedClusters/{delivery_target['cluster_name']}"
    )
    authorization = output("delivery_authorization")
    need(authorization.get("mode") == "kubernetes_rbac", "Native kubernetes_rbac authorization is required")
    groups = authorization.get("admin_group_object_ids")
    need(isinstance(groups, list) and groups and all(isinstance(group, str) and UUID.fullmatch(group) for group in groups)
         and len(set(group.lower() for group in groups)) == len(groups), "Explicit unique Entra administrator group UUIDs are required")
    declared = authorization.get("targets", {}).get(delivery_target.get("slot"), {})
    need(str(declared.get("cluster_id", "")).lower() == cluster_id.lower()
         and declared.get("cluster_name") == delivery_target["cluster_name"]
         and declared.get("resource_group_name") == delivery_target["resource_group"],
         "Applied cluster/slot target mismatch")
    need(isinstance(authorization.get("principals"), dict)
         and isinstance(authorization.get("cluster_user_assignments"), dict), "Applied CI authorization outputs are required")
    return authorization, cluster_id


def platform_resources(wrapped_outputs, discovery_records_list, delivery_target, tenant_id,
                       *, platform_principal_keys, allow_platform_admin=False):
    """Opt-in consolidated binding for only explicitly selected platform principals."""
    need(allow_platform_admin is True, "Platform cluster-admin requires explicit --allow-platform-admin")
    authorization, cluster_id = platform_contract(wrapped_outputs, delivery_target, tenant_id)
    need(isinstance(platform_principal_keys, list)
         and all(isinstance(key, str) and key for key in platform_principal_keys)
         and len(set(platform_principal_keys)) == len(platform_principal_keys),
         "Provide explicit unique platform_principal_keys, or [] to revoke this binding")
    need(isinstance(discovery_records_list, list)
         and all(isinstance(record, dict) for record in discovery_records_list),
         "Identity records must be a list of observed discovery records")
    names = {}
    selected_clients = set()
    for key in platform_principal_keys:
        principal = authorization["principals"].get(key, {})
        client_id = principal.get("client_id", "")
        principal_id = principal.get("principal_id", "")
        need(principal.get("purpose") == "platform"
             and isinstance(client_id, str) and UUID.fullmatch(client_id)
             and isinstance(principal_id, str) and UUID.fullmatch(principal_id)
             and delivery_target["slot"] in principal.get("clusters", [])
             and principal.get("namespaces") == [],
             "Selected principal must be a dedicated platform identity on this slot: " + key)
        need(client_id.lower() not in selected_clients and principal_id.lower() not in names,
             "Platform principals must have distinct client and object IDs")
        for other_key, other in authorization["principals"].items():
            if other_key != key:
                need(str(other.get("client_id", "")).lower() != client_id.lower()
                     and str(other.get("principal_id", "")).lower() != principal_id.lower(),
                     "Platform identity must not also be declared for another purpose/key")
        assignment = authorization["cluster_user_assignments"].get(key + "/" + delivery_target["slot"], {})
        assignment_prefix = cluster_id.lower() + "/providers/microsoft.authorization/roleassignments/"
        assignment_id = str(assignment.get("id", "")).lower()
        need(str(assignment.get("principal_id", "")).lower() == principal_id.lower()
             and str(assignment.get("cluster_id", "")).lower() == cluster_id.lower()
             and assignment_id.startswith(assignment_prefix)
             and UUID.fullmatch(assignment_id[len(assignment_prefix):]),
             "Missing applied Cluster User assignment for selected platform identity")
        records = [record for record in discovery_records_list
                   if str(record.get("client_id", "")).lower() == client_id.lower()
                   and str(record.get("cluster_id", "")).lower() == cluster_id.lower()]
        need(len(records) == 1, "Exactly one discovery record is required per selected platform identity and cluster")
        record = records[0]
        need(record.get("schema_version") == 1 and record.get("kind") == "aks-kubernetes-identity"
             and str(record.get("tenant_id", "")).lower() == tenant_id.lower()
             and record.get("subscription_id") == delivery_target["subscription_id"],
             "Discovery tenant/subscription/schema provenance mismatch")
        names[principal_id.lower()] = record.get("username")
        selected_clients.add(client_id.lower())
    settings = {"platform_principal_object_ids": list(names), "platform_kubernetes_usernames": names}
    return [{
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": "aks-delivery-platform",
            "labels": {"app.kubernetes.io/managed-by": "aks-delivery-templates"},
        },
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": subjects(settings, "platform"),
    }]
