import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml
import test_delivery
import gitops
import gitops_pr


class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_delivery.DeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.receipt = self.f.render()
        self.work = Path(self.f.temp.name)
        self.approved = {key: self.receipt[key] for key in
                         ["schema_version", "source_commit", "image", "image_digest", "image_repository"]}
        self.approved["security_scan"] = {
            "status": "passed", "scanner": "aquasec/trivy@sha256:" + "d"*64,
            "severities": ["HIGH", "CRITICAL"], "report_sha256": "e"*64}
        self.approved["producer"] = {"platform": "github", "run_id": "42"}
        self.build = self.work / "build-release.json"
        self.build.write_text(json.dumps(self.approved))
        self.proposal = self.work / "proposal"
        self.config = {
            "schema_version": 1, "repository_url": "https://github.com/example/private.git",
            "revision": "main", "argo_namespace": "argocd", "project": "platform-demo",
            "application_prefix": "platform-demo"}
        self.config_file = self.work / "gitops.json"
        self.config_file.write_text(json.dumps(self.config))

    def prepare(self):
        return gitops.prepare(self.f.root, "delivery.apps.json", self.build, gitops.sha(self.build),
                              "pprd", "uks", "aks01", self.proposal)

    def test_real_kustomize_output_equivalent_and_source_untouched(self):
        before = subprocess.check_output(["git", "status", "--porcelain"], cwd=self.f.root)
        self.prepare()
        output = subprocess.check_output(["kubectl", "kustomize", str(self.proposal)], text=True)
        self.assertEqual(list(yaml.safe_load_all(output)),
                         list(yaml.safe_load_all((self.f.output / "manifest.yaml").read_text())))
        self.assertEqual(before, subprocess.check_output(["git", "status", "--porcelain"], cwd=self.f.root))
        self.assertEqual(gitops.validate_proposal(self.proposal)["image"], self.approved["image"])

    def test_failed_security_gate_rejected_before_render(self):
        self.approved["security_scan"]["status"] = "failed"
        self.build.write_text(json.dumps(self.approved))
        with patch.object(gitops, "render") as render:
            with self.assertRaisesRegex(ValueError, "security gate"):
                self.prepare()
            render.assert_not_called()

    def test_receipt_tampering_rejected_before_render(self):
        expected = gitops.sha(self.build)
        self.build.write_text(self.build.read_text() + " ")
        with patch.object(gitops, "render") as render:
            with self.assertRaisesRegex(ValueError, "changed"):
                gitops.prepare(self.f.root, "delivery.apps.json", self.build, expected,
                               "pprd", "uks", "aks01", self.proposal)
            render.assert_not_called()

    def test_wrong_registry_receipt_cannot_publish_rendered_image(self):
        self.approved["image_repository"] = "otherexampleacr.azurecr.io/aks-platform-demo"
        self.approved["image"] = self.approved["image_repository"] + "@" + self.approved["image_digest"]
        self.build.write_text(json.dumps(self.approved))
        with self.assertRaisesRegex(ValueError, "registry/repository"):
            self.prepare()
        self.assertFalse(self.proposal.exists())

    def test_remote_resource_or_manifest_tampering_rejected(self):
        self.prepare()
        path = self.proposal / "kustomization.yaml"
        original = path.read_text()
        path.write_text("resources: [https://example.test/unsafe.yaml]")
        with self.assertRaisesRegex(ValueError, "wrapper"):
            gitops.validate_proposal(self.proposal)
        path.write_text(original)
        with (self.proposal / "manifest.yaml").open("a") as out:
            out.write("# tampered\n")
        with self.assertRaisesRegex(ValueError, "Manifest changed"):
            gitops.validate_proposal(self.proposal)

    def test_staging_is_scoped_to_selected_slot_and_rejects_unknown_existing_file(self):
        self.prepare()
        other = self.f.root / "gitops/releases/pprd/uks/aks02"
        other.mkdir(parents=True)
        (other / "keep.txt").write_text("other slot remains unchanged")
        destination = gitops.stage(self.proposal, self.f.root)
        self.assertEqual((other / "keep.txt").read_text(), "other slot remains unchanged")
        (destination / "unowned.txt").write_text("do not remove")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            gitops.stage(self.proposal, self.f.root)

    def test_parent_traversal_rejected(self):
        target = copy.deepcopy(self.receipt["target"])
        target["environment"] = "../outside"
        with self.assertRaises(ValueError):
            gitops.target_path(target)

    def test_clone_urls_and_credentials(self):
        for url in ["https://github.com/example/private.git",
                    "https://dev.azure.com/example/Project/_git/app",
                    "https://example.visualstudio.com/Project/_git/app"]:
            self.config["repository_url"] = url
            self.config_file.write_text(json.dumps(self.config))
            self.assertEqual(gitops.load_config(self.config_file)["repository_url"], url)
        for url in ["http://github.com/example/private.git", "https://token@github.com/example/private.git",
                    "https://github.com/example/private.git?token=secret"]:
            self.config["repository_url"] = url
            self.config_file.write_text(json.dumps(self.config))
            with self.assertRaises(ValueError):
                gitops.load_config(self.config_file)

    def test_application_scoped_manual_without_pruning_and_project_denies_cluster_objects(self):
        app = gitops.application(self.config, self.receipt["target"])
        project = gitops.project(self.config, self.receipt["target"])
        self.assertNotIn("automated", app["spec"]["syncPolicy"])
        self.assertNotIn("finalizers", app["metadata"])
        self.assertEqual(app["spec"]["source"]["path"], "gitops/releases/pprd/uks/aks01")
        self.assertEqual(project["spec"]["clusterResourceWhitelist"], [])
        allowed = {x["kind"] for x in project["spec"]["namespaceResourceWhitelist"]}
        self.assertTrue({"HTTPRoute", "Deployment", "SecretProviderClass"}.issubset(allowed))
        self.assertFalse({"Secret", "Role", "Gateway", "Namespace", "Ingress"} & allowed)

    def test_stale_sync_revision_not_accepted_as_success(self):
        value = {"status": {"sync": {"status": "Synced", "revision": "b"*40},
                            "health": {"status": "Healthy"}}}
        with patch.object(gitops, "run", return_value=json.dumps(value)):
            with self.assertRaises(TimeoutError):
                gitops.wait_application(["kubectl"], "app", "a"*40, timeout=0)

    def test_failed_sync_not_accepted_as_success(self):
        value = {"status": {"sync": {"status": "Synced", "revision": "a"*40},
                            "health": {"status": "Healthy"},
                            "operationState": {"phase": "Failed", "syncResult": {"revision": "a"*40}}}}
        with patch.object(gitops, "run", return_value=json.dumps(value)):
            with self.assertRaisesRegex(ValueError, "failed"):
                gitops.wait_application(["kubectl"], "app", "a"*40)

    def test_sync_rejects_automated_owner_and_never_prunes(self):
        base = ["kubectl", "-n", "argocd"]
        auto = {"spec": {"syncPolicy": {"automated": {"selfHeal": True}}}}
        with patch.object(gitops, "run", return_value=json.dumps(auto)) as run:
            with self.assertRaisesRegex(ValueError, "manual sync"):
                gitops.request_sync(base, "app", "a"*40)
            self.assertEqual(run.call_count, 1)
        with patch.object(gitops, "run", side_effect=[json.dumps({"spec": {}}), ""]) as run:
            gitops.request_sync(base, "app", "a"*40)
            sent = json.loads(run.call_args.args[0][-1])
            self.assertFalse(sent["operation"]["sync"]["prune"])
            self.assertEqual(sent["operation"]["sync"]["revision"], "a"*40)

    def test_wrong_application_binding_stops_before_sync_mutation(self):
        config = gitops.application(self.config, self.receipt["target"])
        config["spec"]["source"]["repoURL"] = "https://github.com/other/wrong.git"
        kubeconfig = self.work / "kubeconfig"
        kubeconfig.write_text("synthetic fixture")
        with patch.object(gitops, "run", return_value=json.dumps(config)) as run:
            with self.assertRaisesRegex(ValueError, "source/project/destination"):
                gitops.verify_release(self.f.output, gitops.sha(self.f.output/"release.json"), kubeconfig,
                    "expected-context", "argocd", gitops.application_name(self.config,self.receipt["target"]),
                    "a"*40, gitops_config=self.config_file, sync=True)
            self.assertEqual(run.call_count, 1)

    def test_pr_guard_failure_causes_no_api_write(self):
        self.prepare()
        with patch.object(gitops_pr, "check", side_effect=ValueError("public caller")), patch.object(gitops_pr, "api") as api:
            with self.assertRaisesRegex(ValueError, "public caller"):
                gitops_pr.publish("github", self.f.root, self.proposal)
            api.assert_not_called()

    def test_github_pr_contains_only_selected_slot_and_never_merges(self):
        self.prepare()
        env = {"GITHUB_SHA": self.f.commit, "GITHUB_RUN_ID": "123", "GITHUB_API_URL": "https://api.github.com",
               "GITHUB_REPOSITORY": "example/private", "GH_TOKEN": "synthetic-fixture"}
        responses = [{"tree": {"sha": "a"*40}}, {"sha": "b"*40}, {"sha": "c"*40},
                     {"ref": "refs/heads/gitops/pprd-uks-aks01-123"}, {"html_url": "https://github.com/example/private/pull/1"}]
        with patch.dict(os.environ, env, clear=True), patch.object(gitops_pr, "check"), patch.object(gitops_pr, "api", side_effect=responses) as api:
            result = gitops_pr.publish("github", self.f.root, self.proposal)
            self.assertTrue(result["pull_request"].endswith("/pull/1"))
            tree = api.call_args_list[1].kwargs["payload"]["tree"]
            self.assertTrue(all(x["path"].startswith("gitops/releases/pprd/uks/aks01/") for x in tree))
            self.assertEqual(api.call_args_list[-1].kwargs["payload"]["base"], "main")
            self.assertFalse(any("/merge" in c.args[0] for c in api.call_args_list))

    def test_azure_pr_branch_starts_at_reviewed_head(self):
        self.prepare()
        env = {"BUILD_SOURCEVERSION": self.f.commit, "BUILD_BUILDID": "123",
               "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example/",
               "SYSTEM_TEAMPROJECTID": "project", "BUILD_REPOSITORY_ID": "repo",
               "SYSTEM_ACCESSTOKEN": "synthetic-fixture"}
        responses = [[{"success": True}], {"commits": [{"commitId": "c"*40}]}, {"pullRequestId": 1}]
        with patch.dict(os.environ, env, clear=True), patch.object(gitops_pr, "check"), patch.object(gitops_pr, "api", side_effect=responses) as api:
            gitops_pr.publish("azure-devops", self.f.root, self.proposal)
            branch = api.call_args_list[0].kwargs["payload"][0]
            self.assertEqual(branch["oldObjectId"], "0"*40)
            self.assertEqual(branch["newObjectId"], self.f.commit)
            push = api.call_args_list[1].kwargs["payload"]
            self.assertEqual(push["refUpdates"][0]["oldObjectId"], self.f.commit)
            self.assertTrue(all(c["item"]["path"].startswith("/gitops/releases/pprd/uks/aks01/")
                                for c in push["commits"][0]["changes"]))


if __name__ == "__main__":
    unittest.main()
