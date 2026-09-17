#!/usr/bin/env python3
"""Select a successful CI build's immutable release receipt without rebuilding."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from delivery import COMMIT, DIGEST, IMAGE, need

LIMIT = 8 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, authorization, *, archive=False):
    need(urllib.parse.urlparse(url).scheme == "https", "CI API must use HTTPS")
    headers = {"Accept": "application/zip" if archive else "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as error:
        if archive and error.code in (301, 302, 303, 307, 308):
            # Signed artifact storage URLs never receive the CI token.
            location = error.headers.get("Location", "")
            need(
                urllib.parse.urlparse(location).scheme == "https",
                "Invalid artifact redirect",
            )
            response = urllib.request.urlopen(location, timeout=60)
        else:
            raise ValueError(f"CI API request failed (HTTP {error.code})") from None
    with response:
        body = response.read(LIMIT + 1)
    need(len(body) <= LIMIT, "Release artifact exceeds the size limit")
    return body if archive else json.loads(body)


def receipt_from_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        need(
            len(files) == 1 and Path(files[0].filename).name == "release.json",
            "Build artifact must contain only release.json",
        )
        item = files[0]
        need(
            item.file_size <= 65536
            and not item.filename.startswith("/")
            and ".." not in Path(item.filename).parts,
            "Invalid release receipt archive",
        )
        data = archive.read(item)
    value = json.loads(data)
    need(
        isinstance(value, dict) and value.get("schema_version") == 1,
        "Invalid build receipt schema",
    )
    need(
        COMMIT.fullmatch(value.get("source_commit", ""))
        and DIGEST.fullmatch(value.get("image_digest", "")),
        "Invalid build receipt revision/digest",
    )
    need(
        IMAGE.fullmatch(value.get("image", ""))
        and value["image"]
        == value.get("image_repository", "") + "@" + value["image_digest"],
        "Invalid build receipt image binding",
    )
    scan = value.get("security_scan", {})
    need(
        isinstance(scan, dict)
        and scan.get("status") == "passed"
        and isinstance(scan.get("scanner"), str)
        and IMAGE.fullmatch(scan["scanner"])
        and scan.get("severities") == ["HIGH", "CRITICAL"]
        and isinstance(scan.get("report_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", scan["report_sha256"]),
        "Build receipt must attest a passed HIGH/CRITICAL image security gate",
    )
    return value, data


def select(platform, run_id, definition, artifact_name, output):
    need(
        re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)),
        "Select a numeric CI build run ID",
    )
    need(
        re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", artifact_name),
        "Invalid release artifact name",
    )
    branch = os.environ.get("DEPLOYMENT_BRANCH", "main")
    if platform == "github":
        repository = os.environ["GITHUB_REPOSITORY"]
        base = os.environ["GITHUB_API_URL"].rstrip("/") + "/repos/" + repository
        auth = "Bearer " + os.environ["GH_TOKEN"]
        need(
            re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", definition),
            "Provide the trusted caller build workflow filename",
        )
        run = request(base + "/actions/runs/" + str(run_id), auth)
        need(
            run.get("status") == "completed" and run.get("conclusion") == "success",
            "Selected build must have completed successfully",
        )
        need(
            run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == branch,
            "Selected build must be a manual protected-branch run",
        )
        need(
            run.get("repository", {}).get("full_name") == repository
            and run.get("head_repository", {}).get("full_name") == repository,
            "Build belongs to another repository",
        )
        need(
            run.get("path") == ".github/workflows/" + definition,
            "Build workflow is not the configured trusted producer",
        )
        artifacts = request(
            base + "/actions/runs/" + str(run_id) + "/artifacts?per_page=100", auth
        )
        need(
            artifacts.get("total_count", 0) <= 100,
            "Too many artifacts; split the build workflow",
        )
        matches = [
            x
            for x in artifacts.get("artifacts", [])
            if x.get("name") == artifact_name and not x.get("expired")
        ]
        need(
            len(matches) == 1,
            "The selected build has no unique unexpired release artifact",
        )
        artifact = matches[0]
        raw = request(
            base + "/actions/artifacts/" + str(artifact["id"]) + "/zip",
            auth,
            archive=True,
        )
        if artifact.get("digest"):
            need(
                artifact["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest(),
                "Downloaded build artifact digest mismatch",
            )
        commit = run["head_sha"]
    else:
        need(
            re.fullmatch(r"[1-9][0-9]{0,19}", definition),
            "Provide the trusted Azure build definition ID",
        )
        base = (
            os.environ["SYSTEM_COLLECTIONURI"].rstrip("/")
            + "/"
            + urllib.parse.quote(os.environ["SYSTEM_TEAMPROJECTID"], safe="")
        )
        auth = (
            "Basic "
            + base64.b64encode(
                (":" + os.environ["SYSTEM_ACCESSTOKEN"]).encode()
            ).decode()
        )
        run = request(
            base + "/_apis/build/builds/" + str(run_id) + "?api-version=7.1", auth
        )
        need(
            run.get("status") == "completed" and run.get("result") == "succeeded",
            "Selected build must have succeeded",
        )
        need(
            str(run.get("definition", {}).get("id")) == definition
            and run.get("repository", {}).get("id", "").lower()
            == os.environ["BUILD_REPOSITORY_ID"].lower(),
            "Build does not belong to the trusted definition and source repository",
        )
        need(
            run.get("sourceBranch") == "refs/heads/" + branch
            and run.get("reason") != "pullRequest",
            "Selected build must use the protected branch",
        )
        query = urllib.parse.urlencode(
            {"artifactName": artifact_name, "api-version": "7.1", "$format": "zip"}
        )
        raw = request(
            base + "/_apis/build/builds/" + str(run_id) + "/artifacts?" + query,
            auth,
            archive=True,
        )
        commit = run["sourceVersion"]
    receipt, data = receipt_from_zip(raw)
    need(
        receipt["source_commit"] == commit,
        "Build receipt source revision differs from the successful CI run",
    )
    producer = receipt.get("producer", {})
    need(
        producer.get("platform") == platform
        and str(producer.get("run_id")) == str(run_id),
        "Receipt is not bound to the selected build run",
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "release.json").write_bytes(data)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=["github", "azure-devops"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--definition", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output")
    args = parser.parse_args()
    result = select(
        args.platform, args.run_id, args.definition, args.artifact_name, args.output
    )
    if args.github_output:
        with open(args.github_output, "a") as output:
            for key in ["source_commit", "image_digest"]:
                output.write(f"{key}={result[key]}\n")
    print(
        json.dumps(
            {key: result[key] for key in ["source_commit", "image_digest", "image"]}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as error:
        raise SystemExit("Release selection stopped: " + str(error))
