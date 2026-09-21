import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_delivery
import bootstrap


class BootstrapTests(unittest.TestCase):
    setUp = test_delivery.DeliveryTests.setUp
    save = test_delivery.DeliveryTests.save

    def prepare(self, native=False):
        self.settings = dict(
            environment="pprd",
            region="uks",
            slot="aks01",
            approval_environment="bootstrap-pprd-uks-aks01",
            deploy_principal_object_ids=["00000000-0000-0000-0000-000000000101"],
            pod_security_version="v1.35",
            require_key_vault_csi=True,
        )
        if native:
            self.settings["authorization_mode"] = "kubernetes_rbac"
            self.settings["deploy_kubernetes_usernames"] = {self.settings["deploy_principal_object_ids"][0]: "observed-entra-user"}
        self.path = self.root / "bootstrap.apps.json"
        self.path.write_text(
            json.dumps(dict(schema_version=1, targets=[self.settings]))
        )
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "Bootstrap fixture",
            ],
            check=True,
        )
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()
        target = self.config["targets"][0]
        self.cluster = dict(
            id=f"/subscriptions/{target['subscription_id']}/resourceGroups/{target['resource_group']}/providers/Microsoft.ContainerService/managedClusters/{target['cluster_name']}",
            provisioningState="Succeeded",
            aadProfile=dict(
                managed=True, enableAzureRbac=not native, tenantId=self.config["tenant_id"]
            ),
            disableLocalAccounts=True,
            apiServerAccessProfile=dict(enablePrivateCluster=True),
            oidcIssuerProfile=dict(enabled=True),
            securityProfile=dict(workloadIdentity=dict(enabled=True)),
            currentKubernetesVersion="1.35.6",
            addonProfiles=dict(azureKeyvaultSecretsProvider=dict(enabled=True)),
        )

    def execute(self, fail_dry_run=False):
        self.calls = []
        self.manifests = []

        def run(args, **kwargs):
            self.calls.append(args)
            if args[:3] == ["az", "aks", "show"]:
                return json.dumps(self.cluster)
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text("synthetic")
            if "--dry-run=server" in args:
                manifest = json.loads(Path(args[-1]).read_text())
                self.manifests.append(manifest)
                if manifest["kind"] == "Namespace":
                    self.namespace = manifest
                if fail_dry_run:
                    raise subprocess.CalledProcessError(1, args)
            return ""

        with patch.object(bootstrap, "account"), patch.object(
            bootstrap, "run", side_effect=run
        ):
            return bootstrap.apply(
                self.root,
                "delivery.apps.json",
                "bootstrap.apps.json",
                self.commit,
                "pprd",
                "uks",
                "aks01",
                yes=True,
            )

    def test_bootstrap_scopes_roles_and_restricted_namespace(self):
        self.prepare()
        result = self.execute()
        assignments = [
            args
            for args in self.calls
            if args[:4] == ["az", "role", "assignment", "create"]
        ]
        self.assertEqual(len(assignments), 2)
        self.assertEqual(
            assignments[0][assignments[0].index("--scope") + 1], self.cluster["id"]
        )
        self.assertEqual(
            assignments[1][assignments[1].index("--scope") + 1],
            self.cluster["id"] + "/namespaces/platform-demo",
        )
        self.assertEqual(
            self.namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"],
            "restricted",
        )
        self.assertEqual(result["role_assignments"], 2)
        self.assertFalse(any("--admin" in args for args in self.calls))
        first_names = [args[args.index("--name") + 1] for args in assignments]
        self.execute()
        self.assertEqual(
            first_names,
            [
                args[args.index("--name") + 1]
                for args in self.calls
                if args[:4] == ["az", "role", "assignment", "create"]
            ],
        )

    def test_wrong_tenant_stops_before_namespace_or_role_mutation(self):
        self.prepare()
        self.cluster["aadProfile"]["tenantId"] = "00000000-0000-0000-0000-000000000099"
        with self.assertRaisesRegex(ValueError, "selected tenant"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_missing_csi_stops_before_mutation(self):
        self.prepare()
        self.cluster["addonProfiles"] = {}
        with self.assertRaisesRegex(ValueError, "CSI"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_pod_security_minor_stops_before_mutation(self):
        self.prepare()
        self.cluster["currentKubernetesVersion"] = "1.36.1"
        with self.assertRaisesRegex(ValueError, "Kubernetes minor"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_failed_namespace_dryrun_prevents_apply_and_roles(self):
        self.prepare()
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(fail_dry_run=True)
        self.assertFalse(
            any(
                "apply" in args and "--dry-run=server" not in args
                for args in self.calls
            )
        )
        self.assertFalse(
            any(args[:3] == ["az", "role", "assignment"] for args in self.calls)
        )

    def test_native_bootstrap_only_mutates_namespace_and_scoped_kubernetes_roles(self):
        self.prepare(native=True)
        result = self.execute()
        self.assertEqual(result["role_assignments"], 0)
        self.assertEqual([item["kind"] for item in self.manifests], ["Namespace", "Role", "RoleBinding"])
        self.assertFalse(any(args[:3] == ["az", "role", "assignment"] for args in self.calls))
        self.assertFalse(any("--admin" in args for args in self.calls))
        binding = self.manifests[-1]
        self.assertEqual(binding["subjects"][0]["name"], "observed-entra-user")
        self.assertEqual(binding["metadata"]["namespace"], "platform-demo")
        authorization_calls = [args for args in self.calls if "--server-side" in args]
        self.assertEqual(len(authorization_calls), 4)
        self.assertTrue(all("--field-manager=aks-delivery-authorization" in args for args in authorization_calls))
        self.assertFalse(any("--force-conflicts" in args for args in self.calls))

    def test_native_mode_mismatch_stops_before_namespace_mutation(self):
        self.prepare(native=True)
        self.cluster["aadProfile"]["enableAzureRbac"] = True
        with self.assertRaisesRegex(ValueError, "authorization mode"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_bootstrap_cannot_reuse_application_approval(self):
        self.prepare()
        self.settings["approval_environment"] = "pprd-uks-aks01"
        self.path.write_text(
            json.dumps(dict(schema_version=1, targets=[self.settings]))
        )
        with self.assertRaisesRegex(ValueError, "separate approval"):
            bootstrap.configuration(
                self.root,
                "delivery.apps.json",
                "bootstrap.apps.json",
                "pprd",
                "uks",
                "aks01",
            )


if __name__ == "__main__":
    unittest.main()
