#!/usr/bin/env python3
"""Fail closed before authenticated/persistent CI; recheck branch before apply."""
import argparse, base64, json, os, re, subprocess, urllib.parse, urllib.request


def get(url, token, basic=False):
    auth = (
        "Basic " + base64.b64encode((":" + token).encode()).decode()
        if basic
        else "Bearer " + token
    )
    request = urllib.request.Request(
        url, headers={"Authorization": auth, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def check(platform, latest=False):
    branch = os.environ.get("DEPLOYMENT_BRANCH", "main")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,100}", branch) or ".." in branch:
        raise ValueError("invalid deployment branch")
    if platform == "github":
        if (
            os.environ.get("PRIVATE_REPOSITORY") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/" + branch
        ):
            raise ValueError(
                "delivery requires workflow_dispatch of the protected branch in a PRIVATE consumer repository"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("TEMPLATE_REF", "")):
            raise ValueError("template-ref must be a full commit SHA")
        labels = json.loads(os.environ.get("RUNNER_LABELS", '["ubuntu-24.04"]'))
        if (
            not isinstance(labels, list)
            or not labels
            or any(
                not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", x)
                for x in labels
            )
        ):
            raise ValueError("runs-on must be a JSON array of runner labels")
        if latest:
            ref = urllib.parse.quote("heads/" + branch, safe="/")
            data = get(
                f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}/git/ref/{ref}",
                os.environ["GH_TOKEN"],
            )
            if data["object"]["sha"] != os.environ["GITHUB_SHA"]:
                raise ValueError("protected branch changed; start a new reviewed run")
    else:
        if (
            os.environ.get("BUILD_REASON") == "PullRequest"
            or os.environ.get("BUILD_SOURCEBRANCH") != "refs/heads/" + branch
            or os.environ.get("BUILD_REPOSITORY_PROVIDER") != "TfsGit"
        ):
            raise ValueError(
                "delivery requires the protected Azure Repos branch; pull requests are rejected"
            )
        base = os.environ["SYSTEM_COLLECTIONURI"] + urllib.parse.quote(
            os.environ["SYSTEM_TEAMPROJECTID"], safe=""
        )
        token = os.environ["SYSTEM_ACCESSTOKEN"]
        project = get(
            os.environ["SYSTEM_COLLECTIONURI"]
            + "_apis/projects/"
            + os.environ["SYSTEM_TEAMPROJECTID"]
            + "?api-version=7.1",
            token,
            True,
        )
        if project.get("visibility") != "private":
            raise ValueError(
                "authenticated delivery requires a PRIVATE Azure DevOps project"
            )
        if latest:
            query = urllib.parse.urlencode(
                {"filter": "heads/" + branch, "api-version": "7.1"}
            )
            refs = get(
                base
                + "/_apis/git/repositories/"
                + os.environ["BUILD_REPOSITORY_ID"]
                + "/refs?"
                + query,
                token,
                True,
            )["value"]
            matches = [
                x["objectId"] for x in refs if x["name"] == "refs/heads/" + branch
            ]
            if matches != [os.environ["BUILD_SOURCEVERSION"]]:
                raise ValueError("protected branch changed; start a new reviewed run")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=["github", "azure-devops"])
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--source-commit")
    parser.add_argument("--source", default=".")
    args = parser.parse_args()
    try:
        check(args.platform, args.latest)
        if args.source_commit:
            if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
                raise ValueError("source-commit must be a full SHA")
            expected_head = (
                os.environ["GITHUB_SHA"]
                if args.platform == "github"
                else os.environ["BUILD_SOURCEVERSION"]
            )
            actual_head = subprocess.check_output(
                ["git", "-C", args.source, "rev-parse", "HEAD"], text=True
            ).strip()
            if actual_head != expected_head:
                raise ValueError(
                    "guard source checkout must be the triggering protected branch commit"
                )
            subprocess.run(
                [
                    "git",
                    "-C",
                    args.source,
                    "merge-base",
                    "--is-ancestor",
                    args.source_commit,
                    actual_head,
                ],
                check=True,
            )
    except Exception as error:
        raise SystemExit("Delivery guard stopped: " + str(error))
