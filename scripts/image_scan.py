"""Gate a pushed immutable image before producing a promotable release receipt."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")


def scan_image(image, docker_config, report):
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("Scan an immutable image reference.")
    config = Path(docker_config).resolve()
    if not config.is_dir() or not (config / "config.json").is_file():
        raise ValueError("An isolated registry login is required for image scanning.")
    lock = json.loads((Path(__file__).resolve().parents[1] / "build-tools.json").read_text())
    scanner = lock["trivy_image"]
    if not IMAGE.fullmatch(scanner):
        raise ValueError("Trivy must be pinned by image digest.")
    # Vulnerability databases expand beyond 1 GiB. Use isolated runner disk,
    # owned by the runner UID, so a read-only container can cleanly remove it.
    with tempfile.TemporaryDirectory(prefix="aks-image-scan-") as cache:
        command = [
            "docker", "run", "--rm", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--mount", f"type=bind,source={config},target=/run/registry,readonly",
            "--mount", f"type=bind,source={cache},target=/tmp",
            "--env", "DOCKER_CONFIG=/run/registry", "--env", "HOME=/tmp",
            scanner, "image", "--image-src", "remote", "--cache-dir", "/tmp/trivy",
            "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--exit-code", "1",
            "--timeout", "10m", "--format", "json", image,
        ]
        result = subprocess.run(
            command, check=False, text=True, stdout=subprocess.PIPE,
            timeout=660, env=dict(os.environ, DOCKER_CONFIG=str(config)),
        )
    report = Path(report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(result.stdout)
    if result.returncode:
        raise ValueError("Image security gate failed; no release receipt was created. Review the private image-scan.json report and scanner logs.")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Image scanner did not return a valid JSON report.") from error
    if data.get("ArtifactName") != image or not isinstance(data.get("Results", []), list):
        raise ValueError("Image scanner report does not identify the requested immutable image.")
    if any(v.get("Severity") in ("HIGH", "CRITICAL")
           for entry in data.get("Results", [])
           for v in entry.get("Vulnerabilities", []) or []):
        raise ValueError("Image report contains prohibited vulnerabilities despite scanner success.")
    return {
        "status": "passed", "scanner": scanner, "severities": ["HIGH", "CRITICAL"],
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
    }
