import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_delivery
import native_authorization


class NativeAuthorizationTests(unittest.TestCase):
    def settings(self, names=None):
        names = names if names is not None else {"00000000-0000-0000-0000-000000000101": "observed-ci-user"}
        return {"deploy_principal_object_ids": list(names), "deploy_kubernetes_usernames": names}

    def permitted(self, group, resource, verb):
        role = native_authorization.application_resources(self.settings(), "platform-demo")[0]
        return any(group in item["apiGroups"] and resource in item["resources"] and verb in item["verbs"] for item in role["rules"])

    def test_required_application_operations_and_platform_denials(self):
        for group, resource, verb in [
            ("apps", "deployments", "patch"), ("", "pods/portforward", "create"),
            ("", "pods/log", "get"), ("", "services", "list"), ("", "serviceaccounts", "patch"),
            ("gateway.networking.k8s.io", "httproutes", "create"),
            ("gateway.networking.k8s.io", "gateways", "get"),
            ("secrets-store.csi.x-k8s.io", "secretproviderclasses", "patch"),
            ("autoscaling", "horizontalpodautoscalers", "patch"),
            ("policy", "poddisruptionbudgets", "patch"),
            ("networking.k8s.io", "networkpolicies", "patch"),
        ]:
            self.assertTrue(self.permitted(group, resource, verb), (group, resource, verb))
        for group, resource, verb in [
            ("gateway.networking.k8s.io", "gateways", "patch"),
            ("gateway.envoyproxy.io", "envoyproxies", "patch"),
            ("rbac.authorization.k8s.io", "roles", "create"),
            ("rbac.authorization.k8s.io", "rolebindings", "create"),
            ("", "secrets", "get"), ("", "pods/exec", "create"), ("", "namespaces", "create"),
            ("apiextensions.k8s.io", "customresourcedefinitions", "patch"),
            ("apps", "deployments", "delete"),
        ]:
            self.assertFalse(self.permitted(group, resource, verb), (group, resource, verb))
        for resource in native_authorization.application_resources(self.settings(), "platform-demo"):
            self.assertEqual(resource["metadata"]["namespace"], "platform-demo")

    def test_binding_reconciles_removed_subjects_including_last_principal(self):
        for names in [
            {"00000000-0000-0000-0000-000000000101": "first", "00000000-0000-0000-0000-000000000102": "second"},
            {"00000000-0000-0000-0000-000000000102": "second"},
            {},
        ]:
            binding = native_authorization.application_resources(self.settings(names), "platform-demo")[1]
            self.assertEqual([item["name"] for item in binding["subjects"]], sorted(names.values()))
            self.assertEqual(binding["metadata"]["name"], "aks-delivery-application")

    def test_subjects_must_match_explicit_principals_and_never_system_users(self):
        for settings in [
            {"deploy_principal_object_ids": []},
            self.settings({"00000000-0000-0000-0000-000000000101": "system:serviceaccount:kube-system:admin"}),
            self.settings({"00000000-0000-0000-0000-000000000101": "bad user"}),
            self.settings({"00000000-0000-0000-0000-000000000101": "duplicate", "00000000-0000-0000-0000-000000000102": "duplicate"}),
            {"deploy_principal_object_ids": ["00000000-0000-0000-0000-000000000101"], "deploy_kubernetes_usernames": {}},
        ]:
            with self.assertRaises(ValueError):
                native_authorization.usernames(settings)


class IdentityDiscoveryTests(unittest.TestCase):
    client = "00000000-0000-0000-0000-000000000201"
    app = {"tenant_id": "00000000-0000-0000-0000-000000000001"}
    target = {
        "subscription_id": "00000000-0000-0000-0000-000000000002",
        "resource_group": "demo-rg", "cluster_name": "demo-aks",
    }

    def discover(self, signed_in=None, username="observed-spn-username"):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[:3] == ["az", "account", "show"]:
                return json.dumps(signed_in or {"user": {"type": "servicePrincipal", "name": self.client}})
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text("fixture")
            if "whoami" in args:
                return json.dumps({"kind": "SelfSubjectReview", "status": {"userInfo": {"username": username}}})
            return ""
        with patch.object(native_authorization, "account"), patch.object(native_authorization, "run", side_effect=command):
            result = native_authorization.discover(self.app, self.target, self.client)
        return result, calls

    def test_uses_api_observation_not_client_id_as_kubernetes_subject(self):
        result, calls = self.discover()
        self.assertEqual(result["username"], "observed-spn-username")
        self.assertEqual(result["client_id"], self.client)
        self.assertTrue(result["cluster_id"].endswith("/managedClusters/demo-aks"))
        self.assertFalse(any("--admin" in call for call in calls))

    def test_wrong_ci_identity_fails_before_cluster_connection(self):
        with patch.object(native_authorization, "account"), patch.object(
            native_authorization, "run", return_value=json.dumps({"user": {"type": "user", "name": "operator@example.invalid"}})
        ) as command:
            with self.assertRaisesRegex(ValueError, "expected CI"):
                native_authorization.discover(self.app, self.target, self.client)
        self.assertEqual(command.call_count, 1)

    def test_rejects_system_identity_from_cluster(self):
        with self.assertRaisesRegex(ValueError, "system identities"):
            self.discover(username="system:anonymous")


if __name__ == "__main__":
    unittest.main()
