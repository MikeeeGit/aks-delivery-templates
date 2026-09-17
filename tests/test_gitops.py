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
            "application_prefix": "platform-demo",
            "cluster_api_servers": {
                "pprd/uks/aks01": "https://aks01.example.test:443",
                "pprd/uks/aks02": "https://aks02.example.test:443"}}
        self.config_file = self.work / "gitops.json"
        self.config_file.write_text(json.dumps(self.config))

    def prepare(self):
        return gitops.prepare(self.f.root, "delivery.apps.json", self.build, gitops.sha(self.build),
                              "pprd", "uks", "aks01", self.proposal)

    def commit_proposal(self):
        self.prepare()
        gitops.stage(self.proposal, self.f.root)
        return self.commit_gitops()

    def commit_gitops(self):
        subprocess.run(["git", "-C", str(self.f.root), "add", "-f", "gitops"], check=True)
        subprocess.run(["git", "-C", str(self.f.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-qm", "Reviewed synthetic GitOps state"], check=True)
        return subprocess.check_output(["git", "-C", str(self.f.root), "rev-parse", "HEAD"], text=True).strip()

    def kubeconfig_value(self, **overrides):
        cluster = {"server": self.config["cluster_api_servers"]["pprd/uks/aks01"]}
        cluster.update(overrides)
        return {"clusters": [{"name": "reviewed-cluster", "cluster": cluster}]}

    def verify_call(self, revision, **kwargs):
        kubeconfig = self.work / "kubeconfig"
        kubeconfig.write_text("synthetic fixture")
        return gitops.verify_release(
            self.proposal, gitops.sha(self.proposal / "release.json"), kubeconfig,
            "expected-context", "argocd", gitops.application_name(self.config, self.receipt["target"]),
            revision, gitops_config=self.config_file, gitops_source=self.f.root, sync=True, **kwargs)

    def test_real_kustomize_output_equivalent_and_source_untouched(self):
        before = subprocess.check_output(["git", "status", "--porcelain"], cwd=self.f.root)
        self.prepare()
        self.assertFalse((self.proposal / "kustomization.yaml").exists())
        self.assertEqual(list(yaml.safe_load_all((self.proposal / "manifest.yaml").read_text())),
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
        path.write_text("resources: [https://example.test/unsafe.yaml]")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            gitops.validate_proposal(self.proposal)
        path.unlink()
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

    def test_cluster_endpoint_bindings_require_distinct_https_targets(self):
        invalid = [None, {}, {"aks01": "https://aks01.example.test"},
                   {"pprd/uks/aks03": "https://aks03.example.test"},
                   {"pprd/uks/aks01": "http://aks01.example.test"},
                   {"pprd/uks/aks01": "https://token@aks01.example.test"},
                   {"pprd/uks/aks01": "https://aks01.example.test?token=secret"},
                   {"pprd/uks/aks01": "https://aks01.example.test/path"},
                   {"pprd/uks/aks01": "https://aks01.example.test:99999"},
                   {"pprd/uks/aks01": "https://same.example.test",
                    "pprd/uks/aks02": "https://same.example.test:443/"}]
        for servers in invalid:
            with self.subTest(servers=servers):
                value = dict(self.config, cluster_api_servers=servers)
                self.config_file.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    gitops.load_config(self.config_file)

    def test_application_scoped_manual_without_pruning_and_project_denies_cluster_objects(self):
        app = gitops.application(self.config, self.receipt["target"])
        project = gitops.project(self.config, self.receipt["target"])
        self.assertEqual(app["spec"]["source"]["directory"], {"include": "manifest.yaml", "recurse": False})
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

    def test_cached_healthy_status_cannot_finish_an_active_operation(self):
        for phase in ("Running", "Succeeded"):
            with self.subTest(phase=phase):
                value = {"operation": {"sync": {"revision": "a"*40}}, "status": {
                    "sync": {"status": "Synced", "revision": "a"*40}, "health": {"status": "Healthy"},
                    "operationState": {"phase": phase, "syncResult": {"revision": "a"*40}}}}
                with patch.object(gitops, "run", return_value=json.dumps(value)):
                    with self.assertRaises(TimeoutError):
                        gitops.wait_application(["kubectl"], "app", "a"*40, timeout=0)

    def test_recovery_waits_for_stale_conditions_to_clear_and_current_operation_to_finish(self):
        stale = {"operation": {"sync": {"revision": "a"*40}}, "status": {
            "conditions": [{"type": "SyncError", "message": "Old revision rejected"}],
            "operationState": {"phase": "Failed", "syncResult": {"revision": "b"*40}}}}
        running = copy.deepcopy(stale)
        running["status"]["operationState"] = {"phase": "Running", "syncResult": {"revision": "a"*40}}
        success = {"status": {"sync": {"status": "Synced", "revision": "a"*40},
                              "health": {"status": "Healthy"},
                              "operationState": {"phase": "Succeeded", "syncResult": {"revision": "a"*40}}}}
        with patch.object(gitops, "run", side_effect=[json.dumps(x) for x in (stale, running, success)]) as run, \
                patch.object(gitops.time, "sleep"):
            self.assertEqual(gitops.wait_application(["kubectl"], "app", "a"*40), success)
            self.assertEqual(run.call_count, 3)

    def test_same_commit_retry_ignores_old_failure_while_new_operation_is_pending(self):
        pending = {"operation": {"sync": {"revision": "a"*40}}, "status": {
            "conditions": [{"type": "SyncError", "message": "Previous attempt failed"}],
            "operationState": {"phase": "Failed", "syncResult": {"revision": "a"*40}}}}
        success = {"status": {"sync": {"status": "Synced", "revision": "a"*40},
                              "health": {"status": "Healthy"},
                              "operationState": {"phase": "Succeeded", "syncResult": {"revision": "a"*40}}}}
        with patch.object(gitops, "run", side_effect=[json.dumps(pending), json.dumps(success)]) as run, \
                patch.object(gitops.time, "sleep"):
            self.assertEqual(gitops.wait_application(["kubectl"], "app", "a"*40), success)
            self.assertEqual(run.call_count, 2)

    def test_sync_rejects_automated_owner_and_never_prunes(self):
        base = ["kubectl", "-n", "argocd"]
        for policy in ({}, {"selfHeal": True}, {"enabled": False}):
            with self.subTest(policy=policy):
                auto = {"spec": {"syncPolicy": {"automated": policy}}}
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
        revision = self.commit_proposal()
        config = gitops.application(self.config, self.receipt["target"])
        config["spec"]["source"]["repoURL"] = "https://github.com/other/wrong.git"
        original_run = gitops.run
        def dispatch(args, **kwargs):
            if args[0] == "git":
                return original_run(args, **kwargs)
            return json.dumps(self.kubeconfig_value() if "config" in args else config)
        with patch.object(gitops, "run", side_effect=dispatch) as run:
            with self.assertRaisesRegex(ValueError, "source/project/destination"):
                self.verify_call(revision)
            self.assertFalse(any("patch" in call.args[0] for call in run.call_args_list))

    def test_real_git_revision_mismatch_stops_before_any_cluster_command(self):
        reviewed = self.commit_proposal()
        self.assertEqual(gitops.verify_git_revision(self.f.root, reviewed, self.proposal,
                                                  self.receipt["target"]), reviewed)
        path = self.f.root / gitops.target_path(self.receipt["target"]) / "manifest.yaml"
        path.write_text(path.read_text() + "# a different reviewed Git revision\n")
        changed = self.commit_gitops()
        original_run = gitops.run
        def local_git_only(args, **kwargs):
            self.assertEqual(args[0], "git", "A mismatched Git revision must stop before kubectl")
            return original_run(args, **kwargs)
        with patch.object(gitops, "run", side_effect=local_git_only), \
                patch.object(gitops, "request_sync") as sync:
            with self.assertRaisesRegex(ValueError, "differs from the reviewed release"):
                self.verify_call(changed)
            sync.assert_not_called()

    def test_missing_git_slot_and_symlink_are_rejected(self):
        revision = self.commit_proposal()
        other = dict(self.receipt["target"], slot="aks02")
        with self.assertRaisesRegex(ValueError, "differs from the reviewed release"):
            gitops.verify_git_revision(self.f.root, revision, self.proposal, other)
        path = self.f.root / gitops.target_path(self.receipt["target"]) / "manifest.yaml"
        path.unlink()
        path.symlink_to(self.proposal / "manifest.yaml")
        revision = self.commit_gitops()
        with self.assertRaisesRegex(ValueError, "unexpected files or symlinks"):
            gitops.verify_git_revision(self.f.root, revision, self.proposal, self.receipt["target"])

    def test_wrong_cluster_and_insecure_tls_stop_before_api_or_sync(self):
        revision = self.commit_proposal()
        for value in (self.kubeconfig_value(server="https://aks02.example.test:443"),
                      self.kubeconfig_value(**{"insecure-skip-tls-verify": True}),
                      self.kubeconfig_value(**{"tls-server-name": "other.example.test"})):
            with self.subTest(value=value):
                original_run = gitops.run
                def local_only(args, **kwargs):
                    if args[0] == "git":
                        return original_run(args, **kwargs)
                    self.assertIn("config", args, "Only local kubeconfig inspection may run")
                    self.assertIn("--minify", args)
                    self.assertNotIn("--raw", args)
                    return json.dumps(value)
                with patch.object(gitops, "run", side_effect=local_only), \
                        patch.object(gitops, "request_sync") as sync:
                    with self.assertRaises(ValueError):
                        self.verify_call(revision)
                    sync.assert_not_called()

    def test_missing_cluster_binding_stops_before_kubeconfig_read(self):
        config = dict(self.config, cluster_api_servers={"pprd/uks/aks02": "https://aks02.example.test"})
        with patch.object(gitops, "run") as run:
            with self.assertRaisesRegex(ValueError, "no reviewed cluster API"):
                gitops.verify_cluster_context(["kubectl"], config, self.receipt["target"])
            run.assert_not_called()

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
        responses = [{"value": [{"success": True, "updateStatus": "succeeded",
                                  "name": "refs/heads/gitops/pprd-uks-aks01-123",
                                  "newObjectId": self.f.commit}], "count": 1},
                     {"commits": [{"commitId": "c"*40}]}, {"pullRequestId": 1}]
        with patch.dict(os.environ, env, clear=True), patch.object(gitops_pr, "check"), patch.object(gitops_pr, "api", side_effect=responses) as api:
            gitops_pr.publish("azure-devops", self.f.root, self.proposal)
            branch = api.call_args_list[0].kwargs["payload"][0]
            self.assertEqual(branch["oldObjectId"], "0"*40)
            self.assertEqual(branch["newObjectId"], self.f.commit)
            push = api.call_args_list[1].kwargs["payload"]
            self.assertEqual(push["refUpdates"][0]["oldObjectId"], self.f.commit)
            self.assertTrue(all(c["item"]["path"].startswith("/gitops/releases/pprd/uks/aks01/")
                                for c in push["commits"][0]["changes"]))

    def test_azure_ref_failure_in_http_200_stops_before_push_or_pull_request(self):
        self.prepare()
        env = {"BUILD_SOURCEVERSION": self.f.commit, "BUILD_BUILDID": "123",
               "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example/",
               "SYSTEM_TEAMPROJECTID": "project", "BUILD_REPOSITORY_ID": "repo",
               "SYSTEM_ACCESSTOKEN": "synthetic-fixture"}
        expected = {"success": True, "updateStatus": "succeeded",
                    "name": "refs/heads/gitops/pprd-uks-aks01-123", "newObjectId": self.f.commit}
        results = [{"value": [dict(expected, success=False, updateStatus="rejectedByPolicy")]},
                   {"value": [dict(expected, updateStatus="staleOldObjectId")]},
                   {"value": [dict(expected, newObjectId="b"*40)]},
                   {"value": [dict(expected, name="refs/heads/unrelated")]},
                   {"value": []}, [expected]]
        for result in results:
            with self.subTest(result=result), patch.dict(os.environ, env, clear=True), \
                    patch.object(gitops_pr, "check"), \
                    patch.object(gitops_pr, "api", return_value=result) as api:
                with self.assertRaisesRegex(ValueError, "branch creation did not succeed"):
                    gitops_pr.publish("azure-devops", self.f.root, self.proposal)
                self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
