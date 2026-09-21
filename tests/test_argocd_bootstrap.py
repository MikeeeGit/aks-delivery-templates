import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import argocd_bootstrap as argo


class ArgoBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "bundle"
        self.lock = argo.load_lock()
        self.install = [
            argo.obj("ConfigMap", "argocd-cm", "argocd"),
            argo.obj("ConfigMap", "argocd-rbac-cm", "argocd"),
            argo.obj("Service", "argocd-server", "argocd", spec={"type": "ClusterIP"}),
            argo.obj("RoleBinding", "argocd-application-controller", "argocd",
                     "rbac.authorization.k8s.io/v1",
                     subjects=[{"kind": "ServiceAccount", "name": "argocd-application-controller"}],
                     roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "controller"}),
        ]
        for name in ["argocd-application-controller", "argocd-repo-server", "argocd-server",
                     "argocd-applicationset-controller"]:
            kind = "StatefulSet" if name == "argocd-application-controller" else "Deployment"
            self.install.append(argo.obj(kind, name, "argocd", "apps/v1", spec={
                "replicas": 1, "template": {"spec": {"containers": [
                    {"name": name, "image": "quay.io/argoproj/argocd:v3.5.3"}
                ]}}
            }))

    def fake_fetch(self, lock, entry):
        if "/crds/" in entry["path"]:
            name = entry["path"].rsplit("/", 1)[-1].replace("-crd.yaml", "")
            return yaml.safe_dump({"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                                   "metadata": {"name": name + "s.argoproj.io"}}).encode()
        return yaml.safe_dump_all(self.install).encode()

    def prepare(self, **kwargs):
        with patch.object(argo, "fetch", side_effect=self.fake_fetch):
            argo.prepare(self.output, **kwargs)
        return argo.digest((self.output / "bootstrap.json").read_bytes())

    def objects(self, name):
        return argo.objects((self.output / name).read_bytes())

    def test_prepare_enforces_namespace_controller_and_project_boundaries(self):
        expected = self.prepare(namespace="delivery", app_namespace="example-app")
        receipt = argo.verify(self.output, expected)
        self.assertEqual(receipt["namespace"], "delivery")
        docs = self.objects("access.yaml")
        role = next(x for x in docs if x["kind"] == "Role" and x["metadata"]["name"] == "argocd-application-delivery")
        self.assertEqual(role["metadata"]["namespace"], "example-app")
        resources = {resource for rule in role["rules"] for resource in rule["resources"]}
        self.assertFalse(resources & {"secrets", "roles", "rolebindings", "namespaces", "gateways",
                                      "envoyproxies", "customresourcedefinitions"})
        self.assertIn("deployments", resources)
        self.assertIn("httproutes", resources)
        for rule in role["rules"]:
            if "pods" in rule["resources"]:
                self.assertEqual(rule["verbs"], ["get", "list", "watch"])
        local = next(x for x in docs if x["kind"] == "Secret")
        self.assertEqual(local["stringData"]["config"], "{}")
        self.assertEqual(local["stringData"]["clusterResources"], "false")
        self.assertEqual(local["stringData"]["namespaces"], "example-app")
        default = next(x for x in docs if x["kind"] == "AppProject")
        self.assertEqual(default["spec"]["destinations"], [])
        self.assertEqual(default["spec"]["sourceRepos"], [])
        self.assertFalse(any(x["kind"] in ["ClusterRole", "ClusterRoleBinding", "Namespace"] for x in docs))

    def test_install_preserves_slot_labels_using_annotation_tracking_and_exact_images(self):
        self.prepare(namespace="delivery")
        docs = self.objects("install.yaml")
        cm = next(x for x in docs if x["metadata"]["name"] == "argocd-cm")
        self.assertEqual(cm["data"]["application.resourceTrackingMethod"], "annotation")
        self.assertEqual(cm["data"]["resource.respectRBAC"], "strict")
        inclusions = yaml.safe_load(cm["data"]["resource.inclusions"])
        self.assertFalse(any("Secret" in x["kinds"] or "*" in x["kinds"] for x in inclusions))
        for item in docs:
            self.assertEqual(item["metadata"]["namespace"], "delivery")
            if item["kind"] == "RoleBinding":
                self.assertEqual(item["subjects"][0]["namespace"], "delivery")
            if item["kind"] in ["StatefulSet", "Deployment"]:
                image = item["spec"]["template"]["spec"]["containers"][0]["image"]
                self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")

    def test_unsupported_image_or_clusterwide_grant_fails_closed(self):
        bad = copy.deepcopy(self.install)
        bad[-1]["spec"]["template"]["spec"]["containers"][0]["image"] = "unknown:latest"
        with self.assertRaisesRegex(ValueError, "unpinned"):
            argo.customize_install(bad, self.lock, "argocd", "platform-demo", "evaluation")
        for kind in ["ClusterRole", "ClusterRoleBinding", "Namespace", "CustomResourceDefinition"]:
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "cluster-wide"):
                argo.customize_install([{"kind": kind, "metadata": {}}], self.lock,
                                       "argocd", "platform-demo", "evaluation")

    def test_public_service_is_rejected(self):
        bad = copy.deepcopy(self.install)
        bad[2]["spec"]["type"] = "LoadBalancer"
        with self.assertRaisesRegex(ValueError, "public"):
            argo.customize_install(bad, self.lock, "argocd", "platform-demo", "evaluation")

    def test_bundle_tampering_and_unbound_files_fail_before_kubectl(self):
        expected = self.prepare()
        for name in sorted(argo.FILES):
            original = (self.output / name).read_bytes()
            (self.output / name).write_bytes(original + b"\n# changed")
            with self.subTest(name=name), patch.object(argo, "run") as run:
                with self.assertRaisesRegex(ValueError, "bundle changed"):
                    argo.apply_bundle(self.output, expected, "missing", "kind-test")
                run.assert_not_called()
            (self.output / name).write_bytes(original)
        (self.output / "unreviewed.yaml").write_text("kind: Namespace")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            argo.verify(self.output, expected)

    def test_receipt_tampering_and_symlink_rejected(self):
        expected = self.prepare()
        original = (self.output / "bootstrap.json").read_bytes()
        (self.output / "bootstrap.json").write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            argo.verify(self.output, expected)
        (self.output / "bootstrap.json").write_bytes(original)
        content = (self.output / "install.yaml").read_bytes()
        (self.output / "install.yaml").unlink()
        (self.root / "outside.yaml").write_bytes(content)
        (self.output / "install.yaml").symlink_to(self.root / "outside.yaml")
        with self.assertRaisesRegex(ValueError, "bundle changed"):
            argo.verify(self.output, expected)

    def test_apply_preserves_existing_namespaces_and_uses_explicit_target(self):
        expected = self.prepare()
        kubeconfig = self.root / "kubeconfig"
        kubeconfig.write_text("synthetic fixture")
        calls = []
        with patch.object(argo, "run", side_effect=lambda args: calls.append(args)), patch.object(
            argo.subprocess, "check_output", return_value="namespace/existing\n"
        ), patch.object(argo, "wait_crd_established") as established:
            argo.apply_bundle(self.output, expected, str(kubeconfig), "kind-explicit")
        self.assertEqual(established.call_count, 3)
        self.assertTrue(all(call.args[0][-2:] == ["--context", "kind-explicit"]
                            for call in established.call_args_list))
        self.assertFalse(any("create" in call for call in calls))
        self.assertTrue(all("--kubeconfig" in call and "--context" in call for call in calls))
        self.assertTrue(all(call[call.index("--context") + 1] == "kind-explicit" for call in calls))
        self.assertFalse(any("--force-conflicts" in call for call in calls))
        applied = [Path(call[-1]).name for call in calls if "apply" in call]
        self.assertEqual(applied, ["crds.yaml", "access.yaml", "install.yaml"])
        self.assertTrue(any("statefulset/argocd-application-controller" in call for call in calls))
        self.assertTrue(any("deployment/argocd-repo-server" in call for call in calls))

    def test_failed_crd_establishment_stops_before_controller_apply(self):
        expected = self.prepare()
        kubeconfig = self.root / "kubeconfig"
        kubeconfig.write_text("fixture")
        calls = []

        with patch.object(argo, "run", side_effect=lambda args: calls.append(args)), patch.object(
            argo.subprocess, "check_output", return_value="namespace/existing\n"
        ), patch.object(argo, "wait_crd_established", side_effect=ValueError("CRD not established")), self.assertRaises(ValueError):
            argo.apply_bundle(self.output, expected, str(kubeconfig), "kind-explicit")
        self.assertFalse(any("access.yaml" in str(call) or "install.yaml" in str(call) for call in calls))

    def test_crd_initial_absent_and_null_conditions_wait_for_establishment(self):
        observations = [{}, {"status": {"conditions": None}},
                        {"status": {"conditions": [{"type": "Established", "status": "False"}]}},
                        {"status": {"conditions": [{"type": "Established", "status": "True"}]}}]
        with patch.object(argo.subprocess, "check_output", side_effect=[json.dumps(x) for x in observations]) as read, \
                patch.object(argo.time, "sleep") as sleep:
            argo.wait_crd_established(["kubectl", "--context", "explicit"], "applicationsets.argoproj.io", 60)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(all(0 < c.kwargs["timeout"] <= 60 for c in read.call_args_list))

    def test_crd_never_established_has_a_deadline(self):
        with patch.object(argo.time, "monotonic", side_effect=[0, 0, 0, 1, 1, 2]), \
                patch.object(argo.time, "sleep"), \
                patch.object(argo.subprocess, "check_output", return_value='{"status": {}}') as read:
            with self.assertRaisesRegex(ValueError, "Timed out.*Established"):
                argo.wait_crd_established(["kubectl"], "applications.argoproj.io", 2)
        self.assertEqual(read.call_count, 2)

    def test_crd_rejection_or_termination_is_not_retried_as_pending(self):
        for condition in ({"type": "NamesAccepted", "status": "False", "reason": "NameConflict"},
                          {"type": "Terminating", "status": "True"}):
            row = {"status": {"conditions": [condition, {"type": "Established", "status": "True"}]}}
            with self.subTest(condition=condition), \
                    patch.object(argo.subprocess, "check_output", return_value=json.dumps(row)), \
                    patch.object(argo.time, "sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "cannot become Established"):
                    argo.wait_crd_established(["kubectl"], "applications.argoproj.io", 60)
                sleep.assert_not_called()

    def test_crd_api_failure_is_not_hidden_by_polling(self):
        with patch.object(argo.subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, ["kubectl"])), \
                patch.object(argo.time, "sleep") as sleep:
            with self.assertRaises(subprocess.CalledProcessError):
                argo.wait_crd_established(["kubectl"], "applications.argoproj.io", 60)
            sleep.assert_not_called()

    def test_scope_inputs_and_output_reuse_rejected(self):
        for value in ["argocd", "kube-system", "../outside"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.prepare(app_namespace=value)
        self.output.mkdir(exist_ok=True)
        (self.output / "old").write_text("stale")
        with self.assertRaisesRegex(ValueError, "new empty"):
            self.prepare()

    def test_network_policy_adds_only_requested_git_port(self):
        docs = argo.access_objects("argocd", "platform-demo", [443, 8080])
        policy = next(x for x in docs if x["metadata"]["name"] == "argocd-repository-egress")
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"], "argocd-repo-server")
        self.assertEqual([x["port"] for x in policy["spec"]["egress"][0]["ports"]], [443, 8080])

    def test_hash_mismatch_rejected_on_download(self):
        class Response:
            url = "https://raw.githubusercontent.com/example"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def read(self, limit):
                return b"untrusted altered manifest"
        with patch.object(argo.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                argo.fetch(self.lock, self.lock["manifests"]["evaluation"])


if __name__ == "__main__":
    unittest.main()
