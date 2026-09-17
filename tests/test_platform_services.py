import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import platform_services as platform

ROOT = Path(__file__).resolve().parents[1]


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        shutil.copytree(ROOT / "examples/platform", self.root)
        self.output = Path(self.temp.name) / "bundle"
        self.config = json.loads((self.root / "platform.services.json").read_text())
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.commit()

    def commit(self):
        (self.root / "platform.services.json").write_text(json.dumps(self.config))
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
                "Reviewed synthetic platform fixture",
            ],
            check=True,
        )
        self.sha = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()

    def prepare(self, slot="aks01"):
        self.assertTrue(
            shutil.which("helm"), "Install pinned Helm for real offline rendering tests"
        )
        return platform.prepare(
            self.root,
            "platform.services.json",
            self.sha,
            "pprd",
            "uks",
            slot,
            self.output,
        )

    def expected(self):
        return hashlib.sha256((self.output / "platform.json").read_bytes()).hexdigest()

    def test_real_helm_renders_pinned_package_and_slot_values(self):
        receipt = self.prepare("aks02")
        text = (self.output / "release-0-rendered.yaml").read_text()
        objects = [x for x in yaml.safe_load_all(text) if x]
        self.assertEqual(objects[0]["data"]["slot"], "aks02")
        self.assertEqual(receipt["releases"][0]["chart_version"], "0.1.0")
        self.assertEqual(
            platform.verify(self.output, self.expected())["source_commit"], self.sha
        )

    def test_uncommitted_values_do_not_change_reviewed_snapshot(self):
        (self.root / "values/aks01.yaml").write_text("slot: unreviewed\n")
        self.prepare()
        self.assertIn(
            'slot: "aks01"', (self.output / "release-0-rendered.yaml").read_text()
        )

    def test_wrong_chart_checksum_stops_before_helm(self):
        self.config["targets"][0]["releases"][0]["chart"]["sha256"] = "0" * 64
        self.commit()
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.prepare()

    def test_version_cannot_disagree_with_package(self):
        self.config["targets"][0]["releases"][0]["chart"]["version"] = "0.2.0"
        self.commit()
        with self.assertRaisesRegex(ValueError, "name/version"):
            self.prepare()

    def test_values_tampering_invalidates_platform_bundle(self):
        self.prepare()
        digest = self.expected()
        (self.output / "release-0-values-0.yaml").write_text("slot: changed\n")
        with self.assertRaisesRegex(ValueError, "bundle changed"):
            platform.verify(self.output, digest)

    def test_replaced_receipt_rejected_against_job_output(self):
        self.prepare()
        digest = self.expected()
        with (self.output / "platform.json").open("a") as stream:
            stream.write(" ")
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            platform.verify(self.output, digest)

    def test_output_directory_cannot_reuse_unbound_stale_files(self):
        self.output.mkdir()
        (self.output / "old-unreviewed.yaml").write_text("stale input")
        with self.assertRaisesRegex(ValueError, "new empty"):
            self.prepare()

    def test_every_fixed_manifest_must_be_bound(self):
        self.prepare()
        original = (self.output / "platform.json").read_text()
        for name in ["namespaces.yaml", "serviceaccounts.yaml", "prerequisites.yaml"]:
            with self.subTest(name=name):
                receipt = json.loads(original)
                del receipt["files"][name]
                (self.output / "platform.json").write_text(json.dumps(receipt))
                with self.assertRaisesRegex(ValueError, "Unbound platform"):
                    platform.verify(self.output, self.expected())

    def test_crd_change_needs_explicit_lifecycle_acknowledgment(self):
        self.config["targets"][0]["crd_bundles"] = [
            {
                "url": "https://example.invalid/crds.yaml",
                "sha256": "a" * 64,
            }
        ]
        self.commit()
        with self.assertRaisesRegex(ValueError, "manage_crds"):
            platform.load_config(self.root, "platform.services.json")

    def test_existing_namespace_is_never_reapplied_or_recreated(self):
        self.prepare()
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text("synthetic kubeconfig")
            if "--ignore-not-found" in args:
                # Existing bootstrap-owned namespace has restricted Pod Security labels.
                return "namespace/platform-system"
            return ""

        with patch.object(platform, "account"), patch.object(
            platform, "run", side_effect=run
        ):
            platform.apply(self.output, self.expected(), yes=True)
        self.assertFalse(
            any("create" in args and "namespace" in args for args in calls)
        )
        self.assertFalse(
            any(str(self.output / "namespaces.yaml") in args for args in calls)
        )
        self.assertTrue(any(args[:2] == ["helm", "upgrade"] for args in calls))

    def test_crd_bytes_bound_and_first_install_ordering(self):
        raw = yaml.safe_dump_all(
            [
                dict(
                    apiVersion="apiextensions.k8s.io/v1",
                    kind="CustomResourceDefinition",
                    metadata=dict(name="fixtures.example.test"),
                ),
                dict(
                    apiVersion="admissionregistration.k8s.io/v1",
                    kind="ValidatingAdmissionPolicy",
                    metadata=dict(name="safe-upgrade"),
                ),
            ]
        ).encode()
        target = self.config["targets"][0]
        target["manage_crds"] = True
        target["crd_bundles"] = [
            {
                "url": "https://example.invalid/crds.yaml",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        ]
        self.commit()
        response = io.BytesIO(raw)
        response.url = target["crd_bundles"][0]["url"]
        with patch.object(platform.urllib.request, "urlopen", return_value=response):
            self.prepare()
        receipt = platform.verify(self.output, self.expected())
        self.assertEqual(
            receipt["files"]["crds-0-source.yaml"], hashlib.sha256(raw).hexdigest()
        )
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text("synthetic kubeconfig")
            return "namespace/platform-system" if "--ignore-not-found" in args else ""

        with patch.object(platform, "account"), patch.object(
            platform, "run", side_effect=run
        ):
            platform.apply(self.output, self.expected(), yes=True)
        crd_calls = [
            (i, args)
            for i, args in enumerate(calls)
            if str(self.output / "crds-0.yaml") in args
        ]
        self.assertEqual(len(crd_calls), 3)
        self.assertIn("--dry-run=server", crd_calls[0][1])
        self.assertNotIn("--dry-run=server", crd_calls[1][1])
        self.assertIn("--for=condition=Established", crd_calls[2][1])
        helm_index = next(
            i for i, args in enumerate(calls) if args[:2] == ["helm", "upgrade"]
        )
        policy_indices = [
            i
            for i, args in enumerate(calls)
            if str(self.output / "crds-0-policies.yaml") in args
        ]
        custom_indices = [
            i
            for i, args in enumerate(calls)
            if str(self.output / "prerequisites.yaml") in args
        ]
        self.assertLess(crd_calls[-1][0], min(policy_indices))
        self.assertLess(max(policy_indices), helm_index)
        self.assertLess(helm_index, min(custom_indices))
        self.assertFalse(any("--force-conflicts" in args for args in calls))
        digest = self.expected()
        (self.output / "crds-0-source.yaml").write_bytes(raw + b"# changed\n")
        with self.assertRaisesRegex(ValueError, "bundle changed"):
            platform.verify(self.output, digest)

    def test_values_path_cannot_escape_source(self):
        self.config["targets"][0]["releases"][0]["values"] = ["../outside.yaml"]
        self.commit()
        with self.assertRaisesRegex(ValueError, "traversal"):
            platform.load_config(self.root, "platform.services.json")

    def test_retired_controller_needs_explicit_acknowledgment(self):
        self.config["targets"][0]["releases"][0]["chart"]["name"] = "ingress-nginx"
        self.commit()
        with self.assertRaisesRegex(ValueError, "retired"):
            platform.load_config(self.root, "platform.services.json")

    def test_common_serviceaccount_binds_explicit_identity(self):
        self.config["targets"][0]["workload_service_accounts"] = [
            dict(
                name="platform-workload",
                namespace="platform-system",
                client_id="00000000-0000-0000-0000-000000000201",
            )
        ]
        self.commit()
        self.prepare()
        objects = [
            x
            for x in yaml.safe_load_all(
                (self.output / "serviceaccounts.yaml").read_text()
            )
            if x
        ]
        account = next(x for x in objects if x["kind"] == "ServiceAccount")
        self.assertEqual(
            account["metadata"]["annotations"]["azure.workload.identity/client-id"],
            "00000000-0000-0000-0000-000000000201",
        )

    def test_inline_common_secret_is_rejected(self):
        (self.root / "manifests/platform-settings.yaml").write_text(
            "apiVersion: v1\nkind: Secret\nmetadata: {name: unsupported, namespace: platform-system}\n"
        )
        self.commit()
        with self.assertRaisesRegex(ValueError, "inline Secret"):
            self.prepare()

    def test_failed_helm_upgrade_stops_later_releases_and_keeps_isolation(self):
        target = self.config["targets"][0]
        target["releases"].append(
            dict(target["releases"][0], name="second-platform-release")
        )
        self.commit()
        self.prepare()
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[:3] == ["az", "aks", "get-credentials"]:
                Path(args[args.index("--file") + 1]).write_text("synthetic kubeconfig")
            if args[:2] == ["helm", "upgrade"]:
                raise subprocess.CalledProcessError(1, args)
            return ""

        with patch.object(platform, "account"), patch.object(
            platform, "run", side_effect=run
        ), patch.dict(os.environ, {"HELM_DRIVER": "ambient-driver"}):
            with self.assertRaises(subprocess.CalledProcessError):
                platform.apply(self.output, self.expected(), yes=True)
        upgrades = [
            (args, kwargs) for args, kwargs in calls if args[:2] == ["helm", "upgrade"]
        ]
        self.assertEqual(len(upgrades), 1)
        args, kwargs = upgrades[0]
        self.assertIn("--wait=watcher", args)
        self.assertIn("--skip-crds", args)
        self.assertIn("--timeout", args)
        self.assertNotIn("HELM_DRIVER", kwargs["env"])
        self.assertFalse(Path(kwargs["env"]["KUBECONFIG"]).exists())
        self.assertFalse(any("--admin" in args for args, _ in calls))


if __name__ == "__main__":
    unittest.main()
