import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_gitops


class CommittedGitOpsValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "consumer"
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / "README.md").write_text("Synthetic private consumer fixture\n")
        self.commit()
        self.image = "exampleacr.azurecr.io/demo@sha256:" + "a" * 64

    def commit(self):
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Synthetic fixture"], check=True)
        return subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()

    def release(self, slot="aks01"):
        directory = self.root / "gitops/releases/pprd/uks" / slot
        directory.mkdir(parents=True)
        manifest = json.dumps({"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "platform-demo", "namespace": "platform-demo"},
            "spec": {"template": {"spec": {"containers": [{"name": "app", "image": self.image}]}}}})
        receipt = {"schema_version": 1, "source_commit": "b" * 40,
            "image_digest": "sha256:" + "a" * 64, "image_repository": "exampleacr.azurecr.io/demo",
            "image": self.image, "tenant_id": "00000000-0000-0000-0000-000000000001",
            "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            "target": {"environment": "pprd", "region": "uks", "slot": slot,
                "subscription_id": "00000000-0000-0000-0000-000000000003",
                "resource_group": "example-aks-rg", "cluster_name": "example-" + slot,
                "namespace": "platform-demo", "deployment": "platform-demo"}}
        build = {key: receipt[key] for key in
                 ("schema_version", "source_commit", "image", "image_digest", "image_repository")}
        build["security_scan"] = {"status": "passed", "scanner": "aquasec/trivy@sha256:" + "c" * 64,
            "severities": ["HIGH", "CRITICAL"], "report_sha256": "d" * 64}
        (directory / "manifest.yaml").write_text(manifest)
        (directory / "release.json").write_text(json.dumps(receipt))
        (directory / "build-release.json").write_text(json.dumps(build))
        return directory

    def test_public_base_without_releases_passes(self):
        result = validate_gitops.validate(self.root)
        self.assertEqual(result["release_count"], 0)
        self.assertEqual(result["result"], "passed")

    def test_two_committed_slots_validate_at_exact_commit(self):
        self.release("aks01")
        self.release("aks02")
        revision = self.commit()
        result = validate_gitops.validate(self.root)
        self.assertEqual(result["gitops_commit"], revision)
        self.assertEqual(result["release_count"], 2)
        self.assertEqual([item["path"] for item in result["releases"]],
                         ["gitops/releases/pprd/uks/aks01", "gitops/releases/pprd/uks/aks02"])

    def test_directory_and_receipt_slot_must_match(self):
        directory = self.release()
        receipt = json.loads((directory / "release.json").read_text())
        receipt["target"]["slot"] = "aks02"
        (directory / "release.json").write_text(json.dumps(receipt))
        self.commit()
        with self.assertRaisesRegex(ValueError, "path differs from receipt target"):
            validate_gitops.validate(self.root)

    def test_missing_build_receipt_fails(self):
        directory = self.release()
        (directory / "build-release.json").unlink()
        self.commit()
        with self.assertRaises(FileNotFoundError):
            validate_gitops.validate(self.root)

    def test_failed_scan_attestation_fails(self):
        directory = self.release()
        build = json.loads((directory / "build-release.json").read_text())
        build["security_scan"]["status"] = "failed"
        (directory / "build-release.json").write_text(json.dumps(build))
        self.commit()
        with self.assertRaisesRegex(ValueError, "security gate"):
            validate_gitops.validate(self.root)

    def test_uncommitted_repair_cannot_hide_a_broken_committed_manifest(self):
        directory = self.release()
        manifest = directory / "manifest.yaml"
        original = manifest.read_text()
        manifest.write_text(original + "\n# changed after receipt\n")
        self.commit()
        manifest.write_text(original)
        with self.assertRaisesRegex(ValueError, "Manifest changed"):
            validate_gitops.validate(self.root)

    def test_uncommitted_working_tree_edits_do_not_change_snapshot(self):
        directory = self.release()
        self.commit()
        (directory / "manifest.yaml").write_text("not part of the committed snapshot")
        self.assertEqual(validate_gitops.validate(self.root)["release_count"], 1)

    def test_unknown_committed_file_is_rejected(self):
        directory = self.release()
        (directory / "unowned.yaml").write_text("kind: Secret")
        self.commit()
        with self.assertRaisesRegex(ValueError, "Unexpected GitOps release folder or file"):
            validate_gitops.validate(self.root)

    def test_unknown_nested_folder_is_rejected(self):
        directory = self.release()
        (directory / "nested").mkdir()
        (directory / "nested/manifest.yaml").write_text("kind: Secret")
        self.commit()
        with self.assertRaisesRegex(ValueError, "Unexpected GitOps release folder"):
            validate_gitops.validate(self.root)

    def test_invalid_slot_folder_is_rejected(self):
        directory = self.release()
        directory.rename(directory.with_name("aks03"))
        self.commit()
        with self.assertRaisesRegex(ValueError, "Unexpected GitOps release folder"):
            validate_gitops.validate(self.root)

    def test_committed_file_symlink_is_not_followed(self):
        directory = self.release()
        (directory / "manifest.yaml").unlink()
        (directory / "manifest.yaml").symlink_to("/does-not-exist/outside.yaml")
        self.commit()
        with self.assertRaisesRegex(ValueError, "normal files, never links"):
            validate_gitops.validate(self.root)

    def test_committed_release_ancestor_symlink_is_rejected(self):
        (self.root / "gitops").symlink_to("/does-not-exist")
        self.commit()
        with self.assertRaisesRegex(ValueError, "ancestors must be committed directories"):
            validate_gitops.validate(self.root)

    def test_subdirectory_is_not_accepted_as_an_empty_consumer(self):
        self.release()
        self.commit()
        with self.assertRaisesRegex(ValueError, "repository root"):
            validate_gitops.validate(self.root / "gitops")


if __name__ == "__main__":
    unittest.main()
