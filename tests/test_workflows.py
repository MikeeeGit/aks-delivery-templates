import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.request

import yaml
import verify_service

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_selected_slot_output_script_executes_with_real_newlines(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/deploy-selected.yml").read_text()
        )
        script = workflow["jobs"]["selection"]["steps"][0]["run"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output"
            env = dict(
                os.environ,
                SLOTS='["aks02","aks01"]',
                PRIVATE_REPOSITORY="true",
                GITHUB_OUTPUT=str(path),
            )
            subprocess.run(["bash", "-c", script], env=env, check=True)
            output = dict(line.split("=", 1) for line in path.read_text().splitlines())
            self.assertEqual(json.loads(output["slots"]), ["aks02", "aks01"])
            self.assertEqual(output["first"], "aks02")
            self.assertEqual(output["second"], "aks01")
            for slots, private in [
                ("[]", "true"),
                ('["aks01","aks01"]', "true"),
                ('["aks01"]', "false"),
            ]:
                result = subprocess.run(
                    ["bash", "-c", script],
                    env=dict(env, SLOTS=slots, PRIVATE_REPOSITORY=private),
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_second_slot_depends_on_successful_first(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/deploy-selected.yml").read_text()
        )
        second = workflow["jobs"]["second"]
        self.assertIn("first", second["needs"])
        self.assertIn("needs.first.result == 'success'", second["if"])
        self.assertEqual(workflow["jobs"]["parallel"]["strategy"]["fail-fast"], False)

    def test_selected_service_identity_requires_exact_slot_and_revision(self):
        value = dict(slot="aks01", revision="a" * 40)
        verify_service.check_identity(value, "aks01", "a" * 40)
        for slot, revision in [("aks02", "a" * 40), ("aks01", "b" * 40)]:
            with self.assertRaisesRegex(ValueError, "slot and source"):
                verify_service.check_identity(value, slot, revision)

    def test_selected_service_verification_never_follows_redirects(self):
        handler = verify_service.NoRedirect()
        request = urllib.request.Request("http://127.0.0.1:12345/version")
        self.assertIsNone(
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://example.invalid/version"
            )
        )

    def test_identity_discovery_is_guarded_before_private_authentication(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/discover-identity.yml").read_text())
        guard = workflow["jobs"]["guard"]
        discovery = workflow["jobs"]["discover"]
        self.assertEqual(guard["runs-on"], "ubuntu-24.04")
        self.assertEqual(discovery["needs"], "guard")
        self.assertEqual(discovery["environment"], "$" + "{{ inputs.approval-environment }}")
        steps = discovery["steps"]
        login = next(i for i, item in enumerate(steps) if item.get("uses", "").startswith("azure/login@"))
        latest = next(i for i, item in enumerate(steps) if "ci_guard.py github --latest" in item.get("run", ""))
        self.assertLess(latest, login)
        command = next(item["run"] for item in steps if "bootstrap.py identity" in item.get("run", ""))
        self.assertIn("--client-id", command)
        self.assertNotIn("bootstrap.py apply", command)
        azure = (ROOT / "azure-pipelines/stages/discover-identity.yml").read_text()
        self.assertIn("bootstrap.py\" identity", azure)
        self.assertIn("--client-id", azure)

    def test_builds_use_trigger_commit_not_user_supplied_revision(self):
        github = (ROOT / ".github/workflows/build.yml").read_text()
        azure = (ROOT / "azure-pipelines/stages/build.yml").read_text()
        self.assertIn("SOURCE_COMMIT: ${{ github.sha }}", github)
        self.assertIn("SOURCE_COMMIT: $(Build.SourceVersion)", azure)


if __name__ == "__main__":
    unittest.main()
