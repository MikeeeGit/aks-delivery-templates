import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import image_scan
import delivery


class ImageGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "docker"
        self.config.mkdir(mode=0o700)
        (self.config / "config.json").write_text("{}")
        self.report = self.root / "output/image-scan.json"
        self.image = "exampleplatformacr.azurecr.io/demo@sha256:" + "a" * 64

    def result(self, code=0, vulnerabilities=None, image=None):
        body = {"ArtifactName": image or self.image, "Results": [{"Vulnerabilities": vulnerabilities or []}]}
        return subprocess.CompletedProcess([], code, json.dumps(body))

    def test_scans_immutable_image_without_docker_socket_or_writable_credentials(self):
        with patch.object(image_scan.subprocess, "run", return_value=self.result()) as call:
            proof = image_scan.scan_image(self.image, self.config, self.report)
        args = call.call_args.args[0]
        self.assertEqual(args[-1], self.image)
        self.assertIn("HIGH,CRITICAL", args)
        self.assertIn("--read-only", args)
        self.assertIn("--image-src", args)
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertTrue(args[args.index("--mount")+1].endswith(",readonly"))
        self.assertEqual(proof["status"], "passed")
        self.assertEqual(len(proof["report_sha256"]), 64)

    def test_disk_cache_is_private_and_removed_after_success_or_failure(self):
        for exit_code in (0, 1):
            observed = []
            def scan(args, **kwargs):
                mount = next(x for x in args if x.startswith("type=bind,source=") and x.endswith(",target=/tmp"))
                cache = Path(mount.split("source=", 1)[1].rsplit(",target=", 1)[0])
                observed.append(cache)
                self.assertTrue(cache.is_dir())
                self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
                (cache / "database").write_text("temporary data")
                return self.result(exit_code)
            with self.subTest(exit_code=exit_code), patch.object(image_scan.subprocess, "run", side_effect=scan):
                if exit_code:
                    with self.assertRaisesRegex(ValueError, "security gate failed"):
                        image_scan.scan_image(self.image, self.config, self.report)
                else:
                    image_scan.scan_image(self.image, self.config, self.report)
            self.assertEqual(len(observed), 1)
            self.assertFalse(observed[0].exists())

    def test_vulnerability_or_scanner_failure_blocks_release(self):
        with patch.object(image_scan.subprocess, "run", return_value=self.result(1, [{"Severity":"CRITICAL"}])):
            with self.assertRaisesRegex(ValueError, "security gate failed"):
                image_scan.scan_image(self.image, self.config, self.report)
        self.assertTrue(self.report.is_file())

    def test_mismatched_artifact_or_false_success_is_rejected(self):
        for result in [self.result(image="another"), self.result(vulnerabilities=[{"Severity":"HIGH"}])]:
            with self.subTest(result=result), patch.object(image_scan.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError):
                    image_scan.scan_image(self.image, self.config, self.report)

    def test_tag_only_image_never_starts_scanner(self):
        with patch.object(image_scan.subprocess, "run") as call:
            with self.assertRaises(ValueError):
                image_scan.scan_image("exampleplatformacr.azurecr.io/demo:latest", self.config, self.report)
        call.assert_not_called()

    def test_failed_scan_writes_no_promotable_build_receipt(self):
        output = self.root / "build-output"
        config = {"tenant_id":"00000000-0000-0000-0000-000000000001",
                  "registry":{"name":"exampleplatformacr","subscription_id":"00000000-0000-0000-0000-000000000002","login_server":"exampleplatformacr.azurecr.io","repository":"demo"},
                  "build":{"dockerfile":"Dockerfile","context":"."}}
        def snapshot(_, __, source):
            (source / "Dockerfile").write_text("FROM scratch")
        def run(args, **kwargs):
            if "--metadata-file" in args:
                Path(args[args.index("--metadata-file")+1]).write_text(json.dumps({"containerimage.digest":"sha256:"+"a"*64}))
            return ""
        with patch.object(delivery, "snapshot", side_effect=snapshot), patch.object(delivery, "load_config", return_value=config), patch.object(delivery, "account"), patch.object(delivery, "run", side_effect=run), patch("image_scan.scan_image", side_effect=ValueError("security gate failed")):
            with self.assertRaisesRegex(ValueError, "security gate failed"):
                delivery.build(self.root, "delivery.apps.json", "a"*40, output)
        self.assertFalse((output / "release.json").exists())

    def test_existing_output_cannot_be_mistaken_for_new_release(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "release.json").write_text("previous release")
        with patch.object(delivery, "account") as auth:
            with self.assertRaisesRegex(ValueError, "fresh output"):
                delivery.build(self.root, "delivery.apps.json", "a"*40, output)
        auth.assert_not_called()
        self.assertEqual((output / "release.json").read_text(), "previous release")
