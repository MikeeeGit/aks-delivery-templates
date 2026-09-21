import copy
import json
from pathlib import Path
import subprocess
import yaml
import unittest
from unittest.mock import patch

import bootstrap
import native_authorization
import test_delivery


TENANT = "00000000-0000-0000-0000-000000000001"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000002"
GROUP = "00000000-0000-0000-0000-000000000301"
CLIENT = "00000000-0000-0000-0000-000000000201"
PRINCIPAL = "00000000-0000-0000-0000-000000000101"


def fixture(target=None, tenant=TENANT):
    target = target or dict(environment="pprd", region="uks", slot="aks01", subscription_id=SUBSCRIPTION,
                            resource_group="demo-rg", cluster_name="demo-aks")
    cluster_id = f"/subscriptions/{target['subscription_id']}/resourceGroups/{target['resource_group']}/providers/Microsoft.ContainerService/managedClusters/{target['cluster_name']}"
    authorization = {
        "mode": "kubernetes_rbac", "admin_group_object_ids": [GROUP],
        "principals": {"platform": {"client_id": CLIENT, "principal_id": PRINCIPAL,
                                   "purpose": "platform", "namespaces": [], "clusters": ["aks01", "aks02"]}},
        "targets": {target["slot"]: {"cluster_id": cluster_id, "cluster_name": target["cluster_name"],
                                    "resource_group_name": target["resource_group"]}},
        "cluster_user_assignments": {"platform/" + target["slot"]: {
            "id": cluster_id + "/providers/Microsoft.Authorization/roleAssignments/00000000-0000-0000-0000-000000000501",
            "principal_id": PRINCIPAL, "cluster_id": cluster_id}},
    }
    context = {name: target[name] for name in ["environment", "region", "subscription_id"]}
    context["tenant_id"] = tenant
    outputs = {key: {"sensitive": False, "value": value} for key, value in
               {"deployment_context": context, "delivery_authorization": authorization}.items()}
    records = [{"schema_version": 1, "kind": "aks-kubernetes-identity", "tenant_id": tenant,
                "subscription_id": target["subscription_id"], "cluster_id": cluster_id,
                "client_id": CLIENT, "username": "observed-platform-ci"}]
    return target, outputs, records


class PlatformResourcesTests(unittest.TestCase):
    def setUp(self):
        self.target, self.outputs, self.records = fixture()

    def resources(self, keys=None, **kwargs):
        return native_authorization.platform_resources(
            self.outputs, self.records, self.target, TENANT,
            platform_principal_keys=["platform"] if keys is None else keys,
            allow_platform_admin=kwargs.get("allow", True))

    def test_requires_opt_in_even_for_empty_binding(self):
        for keys in [["platform"], []]:
            with self.assertRaisesRegex(ValueError, "explicit"):
                self.resources(keys, allow=False)

    def test_only_explicit_platform_subject_and_revoke_all(self):
        resource = self.resources()[0]
        self.assertEqual(resource["kind"], "ClusterRoleBinding")
        self.assertEqual(resource["metadata"]["name"], "aks-delivery-platform")
        self.assertNotIn("namespace", resource["metadata"])
        self.assertEqual(resource["roleRef"]["name"], "cluster-admin")
        self.assertEqual(resource["subjects"], [{"kind": "User", "apiGroup": "rbac.authorization.k8s.io",
                                                 "name": "observed-platform-ci"}])
        self.records = []
        self.assertEqual(self.resources([])[0]["subjects"], [])

    def test_rejects_wrong_applied_context_and_sensitive_input(self):
        for field in ["tenant_id", "environment", "region", "subscription_id"]:
            with self.subTest(field=field):
                saved = copy.deepcopy(self.outputs)
                self.outputs["deployment_context"]["value"][field] = "wrong"
                with self.assertRaises(ValueError):
                    self.resources()
                self.outputs = saved
        self.outputs["delivery_authorization"]["sensitive"] = True
        with self.assertRaisesRegex(ValueError, "non-sensitive"):
            self.resources()

    def test_rejects_application_principal_wrong_slot_or_shared_identity(self):
        auth = self.outputs["delivery_authorization"]["value"]
        for field, value in [("purpose", "application"), ("clusters", ["aks02"]), ("namespaces", ["platform-demo"])]:
            with self.subTest(field=field):
                saved = copy.deepcopy(auth["principals"]["platform"])
                auth["principals"]["platform"][field] = value
                with self.assertRaisesRegex(ValueError, "dedicated platform"):
                    self.resources()
                auth["principals"]["platform"] = saved
        auth["principals"]["application"] = dict(auth["principals"]["platform"], purpose="application")
        with self.assertRaisesRegex(ValueError, "another purpose"):
            self.resources()

    def test_rejects_wrong_cluster_mode_or_absent_assignment(self):
        auth = self.outputs["delivery_authorization"]["value"]
        auth["mode"] = "azure_rbac"
        with self.assertRaisesRegex(ValueError, "kubernetes_rbac"):
            self.resources()
        auth["mode"] = "kubernetes_rbac"
        auth["cluster_user_assignments"] = {}
        with self.assertRaisesRegex(ValueError, "Cluster User"):
            self.resources()

    def test_requires_exact_selected_record_provenance_and_safe_username(self):
        for field, value in [
            ("tenant_id", SUBSCRIPTION), ("subscription_id", TENANT), ("cluster_id", "wrong"),
            ("client_id", PRINCIPAL), ("kind", "arbitrary"), ("schema_version", 2),
            ("username", "system:masters"), ("username", "bad name"), ("username", None),
        ]:
            with self.subTest(field=field, value=value):
                saved = copy.deepcopy(self.records)
                self.records[0][field] = value
                with self.assertRaises(ValueError):
                    self.resources()
                self.records = saved
        self.records.append(copy.deepcopy(self.records[0]))
        with self.assertRaisesRegex(ValueError, "Exactly one"):
            self.resources()

    def test_multiple_cluster_records_do_not_expand_target_authority(self):
        other = dict(self.records[0], cluster_id=self.records[0]["cluster_id"] + "-aks02", username="other-user")
        self.records.append(other)
        self.assertEqual(self.resources()[0]["subjects"][0]["name"], "observed-platform-ci")
        with self.assertRaisesRegex(ValueError, "unique"):
            self.resources(["platform", "platform"])


class PlatformAccessTests(unittest.TestCase):
    setUp = test_delivery.DeliveryTests.setUp
    save = test_delivery.DeliveryTests.save

    def prepare(self):
        self.target = self.config["targets"][0]
        _, self.outputs, self.records = fixture(self.target, self.config["tenant_id"])
        self.cluster_id = self.records[0]["cluster_id"]
        self.access = {"schema_version": 1, "targets": [{
            "environment": "pprd", "region": "uks", "slot": "aks01",
            "platform_principal_keys": ["platform"],
        }]}
        self.cluster = {
            "id": self.cluster_id, "provisioningState": "Succeeded",
            "aadProfile": {"managed": True, "enableAzureRbac": False, "tenantId": self.config["tenant_id"],
                           "adminGroupObjectIDs": [GROUP]},
            "disableLocalAccounts": True, "apiServerAccessProfile": {"enablePrivateCluster": True},
        }
        self.cluster["privateFqdn"] = "selected.private.example.invalid"
        self.kubeconfig = {
            "apiVersion": "v1", "kind": "Config", "current-context": "selected",
            "contexts": [{"name": "selected", "context": {"cluster": self.target["cluster_name"], "user": "operator"}}],
            "clusters": [{"name": self.target["cluster_name"], "cluster": {
                "server": "https://" + self.cluster["privateFqdn"] + ":443",
                "certificate-authority-data": "cHVibGljLWNh"}}],
            "users": [{"name": "operator", "user": {"exec": {"command": "kubelogin", "args": ["get-token", "--login", "azurecli"]}}}],
        }
        self.kubectl_configs = []
        self.account_type = "user"
        self.groups = [GROUP, "system:authenticated"]
        self.fail_dry_run = False
        self.calls = []
        self.manifests = []

    def execute(self, allow=True, proxy=None):
        for name, value in [("platform.access.json", self.access), ("applied.json", self.outputs), ("records.json", self.records)]:
            (self.root / name).write_text(json.dumps(value))

        def command(args, **kwargs):
            self.calls.append(args)
            if args[:3] == ["az", "account", "show"]:
                return json.dumps({"user": {"type": self.account_type}})
            if args[:3] == ["az", "aks", "show"]:
                return json.dumps(self.cluster)
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text(yaml.safe_dump(self.kubeconfig))
            if args[0] == "kubectl":
                self.kubectl_configs.append(yaml.safe_load(Path(args[args.index("--kubeconfig") + 1]).read_text()))
            if "whoami" in args:
                return json.dumps({"kind": "SelfSubjectReview", "status": {"userInfo": {
                    "username": "existing-operator", "groups": self.groups}}})
            if "apply" in args:
                self.manifests.append(json.loads(Path(args[-1]).read_text()))
                if "--dry-run=server" in args and self.fail_dry_run:
                    raise subprocess.CalledProcessError(1, args)
            return ""

        with patch.object(bootstrap, "account"), patch.object(bootstrap, "run", side_effect=command):
            return bootstrap.platform_access(
                self.root, "delivery.apps.json", "platform.access.json", "applied.json", "records.json",
                "pprd", "uks", "aks01", allow_platform_admin=allow, yes=True, kubernetes_proxy=proxy)

    def test_operator_applies_only_reviewed_binding_with_dryrun_and_ssa(self):
        self.prepare()
        result = self.execute()
        self.assertEqual(result["subject_count"], 1)
        self.assertEqual(result["azure_role_assignments_created"], 0)
        self.assertEqual(len(self.manifests), 2)
        self.assertTrue(all(manifest["kind"] == "ClusterRoleBinding" for manifest in self.manifests))
        apply_calls = [call for call in self.calls if "apply" in call]
        self.assertIn("--dry-run=server", apply_calls[0])
        self.assertNotIn("--dry-run=server", apply_calls[1])
        self.assertTrue(all("--server-side" in call and "--field-manager=aks-delivery-platform-access" in call for call in apply_calls))
        self.assertFalse(any("--admin" in call or "--force-conflicts" in call or "--as" in call for call in self.calls))
        self.assertFalse(any(call[:3] == ["az", "role", "assignment"] for call in self.calls))

    def test_default_transport_preserves_fresh_kubeconfig(self):
        self.prepare()
        self.execute()
        self.assertTrue(self.kubectl_configs)
        self.assertTrue(all(config == self.kubeconfig for config in self.kubectl_configs))

    def test_proxy_is_in_place_before_every_kubernetes_request(self):
        self.prepare()
        result = self.execute(proxy="socks5://127.0.0.1:1080")
        expected = copy.deepcopy(self.kubeconfig)
        expected["clusters"][0]["cluster"]["proxy-url"] = "socks5://127.0.0.1:1080"
        self.assertEqual(result["subject_count"], 1)
        self.assertTrue(all(config == expected for config in self.kubectl_configs))
        self.assertFalse(any("--admin" in call for call in self.calls))
        self.assertFalse(any("socks5://" in str(call) for call in self.calls))
        self.assertLess(next(i for i, call in enumerate(self.calls) if call[0] == "kubelogin"),
                        next(i for i, call in enumerate(self.calls) if call[0] == "kubectl"))

    def test_bad_proxy_fails_before_azure_or_kubernetes_calls(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "Kubernetes proxy"):
            self.execute(proxy="socks5://external.example.invalid:1080")
        self.assertEqual(self.calls, [])

    def test_proxy_does_not_bypass_operator_membership(self):
        self.prepare()
        self.groups = ["system:masters"]
        with self.assertRaisesRegex(ValueError, "member"):
            self.execute(proxy="socks5://127.0.0.1:1080")
        self.assertEqual(self.manifests, [])

    def test_ci_principal_cannot_grant_itself_initial_access(self):
        self.prepare()
        self.account_type = "servicePrincipal"
        with self.assertRaisesRegex(ValueError, "operator user"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_operator_must_be_observed_in_applied_admin_group(self):
        self.prepare()
        self.groups = ["system:masters"]
        with self.assertRaisesRegex(ValueError, "member"):
            self.execute()
        self.assertEqual(self.manifests, [])

    def test_live_cluster_contract_drift_fails_before_credentials(self):
        for field in ["mode", "groups", "private", "local", "tenant"]:
            with self.subTest(field=field):
                self.prepare()
                if field == "mode":
                    self.cluster["aadProfile"]["enableAzureRbac"] = True
                if field == "groups":
                    self.cluster["aadProfile"]["adminGroupObjectIDs"] = [PRINCIPAL]
                if field == "private":
                    self.cluster["apiServerAccessProfile"]["enablePrivateCluster"] = False
                if field == "local":
                    self.cluster["disableLocalAccounts"] = False
                if field == "tenant":
                    self.cluster["aadProfile"]["tenantId"] = PRINCIPAL
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertFalse(any(call[:3] == ["az", "aks", "get-credentials"] for call in self.calls))

    def test_dryrun_failure_prevents_mutation(self):
        self.prepare()
        self.fail_dry_run = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        self.assertFalse(any("apply" in call and "--dry-run=server" not in call for call in self.calls))

    def test_empty_selection_clears_subjects_and_opt_in_required(self):
        self.prepare()
        self.access["targets"][0]["platform_principal_keys"] = []
        self.records = []
        result = self.execute()
        self.assertEqual(result["subject_count"], 0)
        self.assertTrue(all(manifest["subjects"] == [] for manifest in self.manifests))
        self.calls = []
        with self.assertRaisesRegex(ValueError, "explicit"):
            self.execute(allow=False)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
