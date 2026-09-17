import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import delivery
import ci_guard


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        (self.root / "Dockerfile").write_text("FROM scratch\n")
        (self.root / "deploy").mkdir()
        (self.root / "deploy/kustomization.yaml").write_text(
            "resources: [deployment.yaml]\nnamespace: platform-demo\n"
        )
        self.manifest = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: platform-demo
spec:
  selector:
    matchLabels: {app: demo}
  template:
    metadata:
      labels: {app: demo}
    spec:
      containers:
        - name: app
          image: aks-platform-demo:placeholder
"""
        (self.root / "deploy/deployment.yaml").write_text(self.manifest)
        target = dict(
            environment="pprd",
            region="uks",
            slot="aks01",
            subscription_id="00000000-0000-0000-0000-000000000003",
            resource_group="example-aks-rg",
            cluster_name="example-aks01",
            namespace="platform-demo",
            deployment="platform-demo",
            overlay="deploy",
            approval_environment="pprd-uks-aks01",
        )
        self.config = dict(
            schema_version=1,
            tenant_id="00000000-0000-0000-0000-000000000001",
            registry=dict(
                name="exampleplatformacr",
                login_server="exampleplatformacr.azurecr.io",
                repository="aks-platform-demo",
                subscription_id="00000000-0000-0000-0000-000000000002",
            ),
            build=dict(
                context=".", dockerfile="Dockerfile", image_name="aks-platform-demo"
            ),
            targets=[target],
        )
        self.save()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
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
                "Synthetic test fixture",
            ],
            check=True,
        )
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()
        self.digest = "sha256:" + "a" * 64
        self.output = Path(self.temp.name) / "bundle"

    def save(self):
        (self.root / "delivery.apps.json").write_text(json.dumps(self.config))

    def render(self):
        if not shutil.which("kubectl"):
            self.fail("Pinned kubectl is required for real offline render tests")
        return delivery.render(
            self.root,
            "delivery.apps.json",
            self.commit,
            self.digest,
            "pprd",
            "uks",
            "aks01",
            self.output,
        )

    def test_real_render_uses_committed_source_and_immutable_digest(self):
        before = (self.root / "deploy/deployment.yaml").read_bytes()
        (self.root / "untracked.yaml").write_text("do not include")
        receipt = self.render()
        text = (self.output / "manifest.yaml").read_text()
        self.assertIn(
            "exampleplatformacr.azurecr.io/aks-platform-demo@" + self.digest, text
        )
        self.assertNotIn(":placeholder", text)
        self.assertEqual(before, (self.root / "deploy/deployment.yaml").read_bytes())
        self.assertEqual(receipt["target"]["slot"], "aks01")
        self.assertEqual(
            receipt["target"]["subscription_id"],
            self.config["targets"][0]["subscription_id"],
        )

    def test_manifest_tampering_rejected(self):
        self.render()
        expected = hashlib.sha256(
            (self.output / "release.json").read_bytes()
        ).hexdigest()
        with (self.output / "manifest.yaml").open("a") as stream:
            stream.write("# changed\n")
        with self.assertRaisesRegex(ValueError, "Manifest changed"):
            delivery.verify_bundle(self.output, expected)

    def test_receipt_replacement_rejected(self):
        self.render()
        expected = hashlib.sha256(
            (self.output / "release.json").read_bytes()
        ).hexdigest()
        value = json.loads((self.output / "release.json").read_text())
        value["target"]["cluster_name"] = "another-cluster"
        (self.output / "release.json").write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "receipt digest"):
            delivery.verify_bundle(self.output, expected)

    def test_mutable_image_tag_rejected(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            delivery.render(
                self.root,
                "delivery.apps.json",
                self.commit,
                "latest",
                "pprd",
                "uks",
                "aks01",
                self.output,
            )

    def test_cluster_scoped_kind_rejected(self):
        with self.assertRaisesRegex(ValueError, "namespace-scoped"):
            delivery.inspect_manifests(
                "kind: Namespace\nmetadata: {name: example}",
                self.config["targets"][0],
                "unused",
            )

    def test_cross_namespace_rejected(self):
        with self.assertRaisesRegex(ValueError, "selected namespace"):
            delivery.inspect_manifests(
                "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: example, namespace: other}",
                self.config["targets"][0],
                "unused",
            )

    def test_wrong_slot_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            delivery.select(self.config, "pprd", "uks", "aks02")

    def test_duplicate_target_rejected(self):
        self.config["targets"].append(dict(self.config["targets"][0]))
        self.save()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            delivery.load_config(self.root)

    def test_ambient_subscription_fallback_rejected(self):
        self.config["subscription_id"] = self.config["registry"]["subscription_id"]
        self.save()
        with self.assertRaisesRegex(ValueError, "explicitly"):
            delivery.load_config(self.root)

    def test_target_subscription_required(self):
        del self.config["targets"][0]["subscription_id"]
        self.save()
        with self.assertRaisesRegex(ValueError, "target subscription"):
            delivery.load_config(self.root)

    def test_registry_host_binding(self):
        self.config["registry"]["login_server"] = "other.example.com"
        self.save()
        with self.assertRaisesRegex(ValueError, "login server"):
            delivery.load_config(self.root)

    def test_path_traversal_rejected(self):
        with self.assertRaisesRegex(ValueError, "traversal"):
            delivery.relative(self.root, "../escape")

    def test_untracked_source_changes_are_excluded(self):
        (self.root / "deploy/deployment.yaml").write_text(
            "malformed uncommitted changes"
        )
        self.render()
        self.assertIn("kind: Deployment", (self.output / "manifest.yaml").read_text())

    def test_public_pr_guard_fails_before_auth(self):
        with patch.dict(
            os.environ,
            {
                "PRIVATE_REPOSITORY": "false",
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_REF": "refs/pull/1/merge",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "PRIVATE"):
                ci_guard.check("github")

    def test_wrong_api_group_rejected(self):
        with self.assertRaisesRegex(ValueError, "API group"):
            delivery.inspect_manifests(
                "apiVersion: arbitrary.example/v1\nkind: Deployment\nmetadata: {name: platform-demo, namespace: platform-demo}",
                self.config["targets"][0],
                "unused",
            )

    def test_remote_component_rejected(self):
        path = self.root / "deploy/kustomization.yaml"
        path.write_text("components: [https://example.invalid/component]")
        with self.assertRaisesRegex(ValueError, "Vendor every"):
            delivery.inspect_kustomization(path, self.root)

    def test_remote_patch_rejected(self):
        path = self.root / "deploy/kustomization.yaml"
        path.write_text("patches: [{path: https://example.invalid/patch.yaml}]")
        with self.assertRaisesRegex(ValueError, "Vendor every"):
            delivery.inspect_kustomization(path, self.root)

    def test_incomplete_receipt_rejected_cleanly(self):
        self.output.mkdir()
        path = self.output / "release.json"
        path.write_text("{}")
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Incomplete release receipt"):
            delivery.verify_bundle(self.output, expected)

    def test_stale_azure_tenant_rejected(self):
        with patch.object(
            delivery,
            "run",
            return_value=json.dumps(
                {
                    "id": self.config["targets"][0]["subscription_id"],
                    "tenantId": "00000000-0000-0000-0000-000000000099",
                }
            ),
        ) as call:
            with self.assertRaisesRegex(ValueError, "tenant/subscription"):
                delivery.account(
                    self.config["tenant_id"],
                    self.config["targets"][0]["subscription_id"],
                )
            self.assertEqual(call.call_count, 1)

    def test_failed_server_validation_stops_apply(self):
        self.render()
        expected = hashlib.sha256(
            (self.output / "release.json").read_bytes()
        ).hexdigest()
        calls = []

        def stub(args, **kwargs):
            calls.append(args)
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text("synthetic kubeconfig")
            if "--dry-run=server" in args:
                raise subprocess.CalledProcessError(1, args)
            return ""

        with patch.object(delivery, "account"), patch.object(
            delivery, "run", side_effect=stub
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                delivery.deploy(self.output, expected, yes=True)
        self.assertFalse(
            any("apply" in args and "--dry-run=server" not in args for args in calls)
        )
        self.assertFalse(any("--admin" in args for args in calls))

    def test_validation_window_cannot_replace_approved_manifest(self):
        self.render()
        expected = hashlib.sha256(
            (self.output / "release.json").read_bytes()
        ).hexdigest()
        calls = []

        def stub(args, **kwargs):
            calls.append(args)
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text("synthetic kubeconfig")
            if "--dry-run=server" in args:
                with (self.output / "manifest.yaml").open("a") as stream:
                    stream.write("# changed during server validation\n")
            return ""

        with patch.object(delivery, "account"), patch.object(
            delivery, "run", side_effect=stub
        ):
            with self.assertRaisesRegex(ValueError, "Manifest changed"):
                delivery.deploy(self.output, expected, yes=True)
        self.assertFalse(
            any("apply" in args and "--dry-run=server" not in args for args in calls)
        )

    def test_build_isolates_docker_credentials_and_removes_builder(self):
        calls = []

        def stub(args, **kwargs):
            calls.append((args, kwargs))
            if "--metadata-file" in args:
                Path(args[args.index("--metadata-file") + 1]).write_text(
                    json.dumps({"containerimage.digest": self.digest})
                )
            return ""

        original_run = delivery.run

        def dispatch(args, **kwargs):
            if args[0] == "git":
                return original_run(args, **kwargs)
            return stub(args, **kwargs)

        with patch.object(delivery, "account"), patch("image_scan.scan_image", return_value={"status": "passed"}), patch.object(
            delivery, "run", side_effect=dispatch
        ):
            receipt = delivery.build(
                self.root, "delivery.apps.json", self.commit, self.output
            )
        configs = {kwargs["env"]["DOCKER_CONFIG"] for _, kwargs in calls}
        self.assertEqual(len(configs), 1)
        self.assertFalse(Path(next(iter(configs))).exists())
        self.assertTrue(
            any(
                args[:3] == ["docker", "buildx", "create"]
                and "docker-container" in args
                for args, _ in calls
            )
        )
        self.assertEqual(calls[-1][0][:3], ["docker", "buildx", "rm"])
        self.assertEqual(receipt["image_digest"], self.digest)


if __name__ == "__main__":
    unittest.main()
