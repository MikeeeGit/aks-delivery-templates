import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import releases


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "selected"
        self.commit = "a" * 40
        self.receipt = dict(
            schema_version=1,
            source_commit=self.commit,
            image_digest="sha256:" + "b" * 64,
            image_repository="exampleplatformacr.azurecr.io/aks-platform-demo",
            producer=dict(platform="github", run_id="42"),
            security_scan=dict(
                status="passed",
                scanner="aquasec/trivy@sha256:" + "d" * 64,
                severities=["HIGH", "CRITICAL"],
                report_sha256="e" * 64,
            ),
        )
        self.receipt["image"] = (
            self.receipt["image_repository"] + "@" + self.receipt["image_digest"]
        )
        self.run = dict(
            status="completed",
            conclusion="success",
            event="workflow_dispatch",
            head_branch="main",
            head_sha=self.commit,
            repository=dict(full_name="example/private-app"),
            head_repository=dict(full_name="example/private-app"),
            path=".github/workflows/image-build.yml",
        )
        self.env = dict(
            GITHUB_REPOSITORY="example/private-app",
            GITHUB_API_URL="https://api.github.com",
            GH_TOKEN="synthetic-noncredential",
            DEPLOYMENT_BRANCH="main",
            SYSTEM_COLLECTIONURI="https://dev.azure.com/example/",
            SYSTEM_TEAMPROJECTID="synthetic-project",
            SYSTEM_ACCESSTOKEN="synthetic-noncredential",
            BUILD_REPOSITORY_ID="synthetic-repository",
        )

    def zip(self, name="release.json"):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(name, json.dumps(self.receipt))
        return stream.getvalue()

    def github(self):
        raw = self.zip()
        self.calls = []

        def request(url, authorization, **kwargs):
            self.calls.append(url)
            if kwargs.get("archive"):
                return raw
            if "/artifacts?" in url:
                return dict(
                    total_count=1,
                    artifacts=[
                        dict(
                            id=12,
                            name="image-release-42",
                            expired=False,
                            digest="sha256:" + hashlib.sha256(raw).hexdigest(),
                        )
                    ],
                )
            return self.run

        with patch.dict(os.environ, self.env, clear=True), patch.object(
            releases, "request", side_effect=request
        ):
            return releases.select(
                "github", "42", "image-build.yml", "image-release-42", self.output
            )

    def test_selects_successful_build_receipt_without_rebuild(self):
        self.assertEqual(self.github()["source_commit"], self.commit)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue((self.output / "release.json").is_file())

    def test_failed_run_is_rejected_before_artifact_download(self):
        self.run["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "successfully"):
            self.github()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_workflow_rejected(self):
        self.run["path"] = ".github/workflows/untrusted.yml"
        with self.assertRaisesRegex(ValueError, "trusted producer"):
            self.github()

    def test_pr_build_rejected(self):
        self.run["event"] = "pull_request"
        with self.assertRaisesRegex(ValueError, "protected-branch"):
            self.github()

    def test_receipt_different_commit_rejected(self):
        self.receipt["source_commit"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "source revision"):
            self.github()

    def test_receipt_from_another_run_rejected(self):
        self.receipt["producer"]["run_id"] = "41"
        with self.assertRaisesRegex(ValueError, "selected build"):
            self.github()

    def test_receipt_archive_traversal_rejected(self):
        with self.assertRaisesRegex(ValueError, "archive"):
            releases.receipt_from_zip(self.zip("../release.json"))

    def test_unscanned_or_incomplete_security_gate_cannot_promote(self):
        for scan in (
            None,
            {},
            dict(self.receipt["security_scan"], status="failed"),
            dict(self.receipt["security_scan"], severities=["CRITICAL"]),
            dict(self.receipt["security_scan"], scanner="aquasec/trivy:latest"),
            dict(self.receipt["security_scan"], report_sha256="invalid"),
        ):
            with self.subTest(scan=scan):
                current = self.receipt["security_scan"]
                self.receipt["security_scan"] = scan
                with self.assertRaisesRegex(ValueError, "security gate"):
                    self.github()
                self.assertFalse((self.output / "release.json").exists())
                self.receipt["security_scan"] = current

    def test_azure_definition_and_commit_binding(self):
        self.receipt["producer"]["platform"] = "azure-devops"
        build = dict(
            status="completed",
            result="succeeded",
            definition=dict(id=12),
            repository=dict(id="synthetic-repository"),
            sourceBranch="refs/heads/main",
            sourceVersion=self.commit,
            reason="manual",
        )
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            releases, "request", side_effect=[build, {"name":"image-release-BuildApplication", "resource":{"type":"PipelineArtifact", "downloadUrl":"https://example.artifacts.visualstudio.com/synthetic-project/_apis/artifact/content?format=zip"}}, self.zip()]
        ):
            self.assertEqual(
                releases.select(
                    "azure-devops",
                    "42",
                    "12",
                    "image-release-BuildApplication",
                    self.output,
                )["source_commit"],
                self.commit,
            )

    def test_azure_artifact_url_cannot_send_credentials_to_another_host_or_project(self):
        self.receipt["producer"]["platform"] = "azure-devops"
        build = dict(status="completed", result="succeeded", definition=dict(id=12),
                     repository=dict(id="synthetic-repository"), sourceBranch="refs/heads/main",
                     sourceVersion=self.commit, reason="manual")
        urls = [
            "https://attacker.example/synthetic-project/_apis/artifact/content",
            "https://example.artifacts.visualstudio.com/other-project/_apis/artifact/content",
            "https://example.artifacts.visualstudio.com@attacker.example/synthetic-project/_apis/artifact/content",
            "http://example.artifacts.visualstudio.com/synthetic-project/_apis/artifact/content",
            "https://dev.azure.com/example/another-project/_apis/build/artifacts",
        ]
        for url in urls:
            artifact = {"name":"image-release-BuildApplication", "resource":{"type":"PipelineArtifact", "downloadUrl":url}}
            with self.subTest(url=url), patch.dict(os.environ, self.env, clear=True), patch.object(
                releases, "request", side_effect=[build, artifact]
            ) as call:
                with self.assertRaisesRegex(ValueError, "outside the selected Azure project"):
                    releases.select("azure-devops", "42", "12", artifact["name"], self.output)
                self.assertEqual(call.call_count, 2)
                self.assertFalse((self.output / "release.json").exists())

    def test_azure_other_repository_rejected_before_artifact(self):
        build = dict(
            status="completed",
            result="succeeded",
            definition=dict(id=12),
            repository=dict(id="other-repository"),
        )
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            releases, "request", return_value=build
        ) as request:
            with self.assertRaisesRegex(ValueError, "source repository"):
                releases.select(
                    "azure-devops",
                    "42",
                    "12",
                    "image-release-BuildApplication",
                    self.output,
                )
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
