#!/usr/bin/env python3
"""Open a review-only GitOps release PR in the current private consumer; never merge or sync."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from ci_guard import check
from delivery import COMMIT, need
from gitops import FILES, target_path, validate_proposal


def api(url, token, *, method="GET", payload=None, basic=False):
    authorization = ("Basic " + base64.b64encode((":" + token).encode()).decode()
                     if basic else "Bearer " + token)
    request = urllib.request.Request(url, method=method,
        headers={"Authorization": authorization, "Accept": "application/json",
                 "Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None)
    # Repository APIs should not redirect credentials to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            return json.loads(response.read(8 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        raise ValueError(f"GitOps repository API failed (HTTP {error.code}); no merge or sync was attempted") from None


def changes_for(source, proposal):
    receipt = validate_proposal(proposal)
    prefix = target_path(receipt["target"])
    raw = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "HEAD", "--", prefix],
                                  cwd=source, text=True)
    existing = raw.splitlines()
    need(all(path.startswith(prefix + "/") and path[len(prefix)+1:] in FILES for path in existing),
         "Existing GitOps slot contains files outside the generated release contract")
    files = {prefix + "/" + p.name: p.read_text() for p in proposal.iterdir()}
    changes = [{"path": path, "content": value} for path, value in sorted(files.items())]
    changes += [{"path": path, "content": None} for path in existing if path not in files]
    return receipt, changes


def publish(platform, source, proposal):
    # Guard runs again inside the only mutating helper, immediately before API writes.
    check(platform, latest=True)
    source_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    expected = os.environ["GITHUB_SHA"] if platform == "github" else os.environ["BUILD_SOURCEVERSION"]
    need(COMMIT.fullmatch(expected) and source_head == expected, "Checkout is not the triggering protected commit")
    need(not subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip(),
         "Proposal publication requires a clean source checkout")
    receipt, changes = changes_for(source, proposal)
    target = receipt["target"]
    run_id = os.environ["GITHUB_RUN_ID"] if platform == "github" else os.environ["BUILD_BUILDID"]
    need(re.fullmatch(r"[1-9][0-9]{0,19}", run_id), "Invalid CI run ID")
    base_branch = os.environ.get("DEPLOYMENT_BRANCH", "main")
    branch = "gitops/" + "-".join(target[x] for x in ("environment", "region", "slot")) + "-" + run_id
    title = "Promote " + target["slot"] + " to " + receipt["source_commit"][:12]
    body = ("Promote the selected successful, security-gated image build through Argo CD.\n\n"
            f"Target: {target['environment']}/{target['region']}/{target['slot']}\n"
            f"Application source: {receipt['source_commit']}\nImage: {receipt['image']}\n\n"
            "Review the exact generated manifests and receipt. This PR changes one slot only. "
            "Merging does not sync the default manual Argo Application or change traffic. "
            "After merge, sync the reviewed GitOps commit and verify HTTPS/slot/revision before "
            "promoting the next slot or reviewing a separate traffic cutover.")
    if platform == "github":
        base = os.environ["GITHUB_API_URL"].rstrip("/") + "/repos/" + os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GH_TOKEN"]
        commit = api(base + "/git/commits/" + expected, token)
        tree_items = [{"path": x["path"], "mode": "100644", "type": "blob",
                       **({"content": x["content"]} if x["content"] is not None else {"sha": None})}
                      for x in changes]
        tree = api(base + "/git/trees", token, method="POST",
                   payload={"base_tree": commit["tree"]["sha"], "tree": tree_items})
        need(tree["sha"] != commit["tree"]["sha"], "Selected slot already contains this release; no PR needed")
        created = api(base + "/git/commits", token, method="POST",
                      payload={"message": title, "tree": tree["sha"], "parents": [expected]})
        api(base + "/git/refs", token, method="POST",
            payload={"ref": "refs/heads/" + branch, "sha": created["sha"]})
        pull = api(base + "/pulls", token, method="POST",
                   payload={"title": title, "body": body, "head": branch, "base": base_branch})
        return {"pull_request": pull["html_url"], "branch": branch, "commit": created["sha"]}
    base = (os.environ["SYSTEM_COLLECTIONURI"].rstrip("/") + "/"
            + urllib.parse.quote(os.environ["SYSTEM_TEAMPROJECTID"], safe="")
            + "/_apis/git/repositories/" + os.environ["BUILD_REPOSITORY_ID"])
    token = os.environ["SYSTEM_ACCESSTOKEN"]
    existing = set(subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", target_path(target)],
        cwd=source, text=True).splitlines())
    ref_name = "refs/heads/" + branch
    created_refs = api(base + "/refs?api-version=7.1", token, method="POST", basic=True,
        payload=[{"name": ref_name, "oldObjectId": "0"*40, "newObjectId": expected}])
    results = created_refs.get("value", []) if isinstance(created_refs, dict) else []
    need(len(results) == 1 and results[0].get("name") == ref_name
         and results[0].get("success") is True and results[0].get("updateStatus") == "succeeded"
         and results[0].get("newObjectId") == expected,
         "Azure GitOps branch creation did not succeed at the reviewed commit; no push attempted")
    azure_changes = []
    for item in changes:
        change = {"changeType": "delete" if item["content"] is None else
                  ("edit" if item["path"] in existing else "add"),
                  "item": {"path": "/" + item["path"]}}
        if item["content"] is not None:
            change["newContent"] = {"content": item["content"], "contentType": "rawtext"}
        azure_changes.append(change)
    pushed = api(base + "/pushes?api-version=7.1", token, method="POST", basic=True,
                 payload={"refUpdates": [{"name": "refs/heads/" + branch, "oldObjectId": expected}],
                          "commits": [{"comment": title, "changes": azure_changes}]})
    pull = api(base + "/pullrequests?api-version=7.1", token, method="POST", basic=True,
               payload={"sourceRefName": "refs/heads/" + branch,
                        "targetRefName": "refs/heads/" + base_branch,
                        "title": title, "description": body})
    web = (os.environ["SYSTEM_COLLECTIONURI"].rstrip("/") + "/"
           + urllib.parse.quote(os.environ["SYSTEM_TEAMPROJECTID"], safe="")
           + "/_git/" + os.environ["BUILD_REPOSITORY_ID"]
           + "/pullrequest/" + str(pull["pullRequestId"]))
    return {"pull_request": web, "branch": branch, "commit": pushed["commits"][0]["commitId"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=["github", "azure-devops"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.platform, args.source.resolve(), args.proposal.resolve())))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("GitOps proposal stopped: " + str(error))
