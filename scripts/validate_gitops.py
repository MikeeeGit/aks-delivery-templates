#!/usr/bin/env python3
"""Validate committed GitOps release folders at HEAD without cloud credentials.

Checks manifest/receipt hashes, required build-scan attestations and exact target
paths. This is not independent proof of the build producer; release selection and
protected-branch review retain that responsibility. Working-tree edits are not
part of the committed snapshot being checked.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

from delivery import COMMIT, NAME, need
from gitops import FILES, target_path, validate_proposal


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args])


def tree_entries(data):
    for entry in data.split(b"\0"):
        if entry:
            attributes, path = entry.decode("utf-8").split("\t", 1)
            mode, kind, object_id = attributes.split()
            yield mode, kind, object_id, path


def validate(source):
    source = Path(source).resolve()
    repository_root = Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    need(source == repository_root, "--source must select the private consumer repository root")
    revision = git(source, "rev-parse", "HEAD^{commit}").decode().strip()
    need(COMMIT.fullmatch(revision), "Source HEAD must resolve to a full Git commit")
    # Detect an ancestor file/symlink/gitlink before querying the nested prefix.
    for ancestor in ("gitops", "gitops/releases"):
        for mode, kind, _, path in tree_entries(git(source, "ls-tree", "-z", revision, "--", ancestor)):
            need(path == ancestor and mode == "040000" and kind == "tree",
                 "GitOps release ancestors must be committed directories, never links or files")

    releases = {}
    for mode, kind, object_id, path in tree_entries(
            git(source, "ls-tree", "-rtz", revision, "--", "gitops/releases")):
        parts = path.split("/")
        if kind == "tree":
            if path == "gitops":
                continue  # ls-tree -t includes this already-validated ancestor.
            need(mode == "040000" and 2 <= len(parts) <= 5 and parts[:2] == ["gitops", "releases"]
                 and all(NAME.fullmatch(part) for part in parts[2:4])
                 and (len(parts) < 5 or parts[4] in ("aks01", "aks02")),
                 "Unexpected GitOps release folder: " + path)
            continue
        need(mode == "100644" and kind == "blob", "GitOps releases must contain normal files, never links")
        need(len(parts) == 6 and parts[:2] == ["gitops", "releases"]
             and NAME.fullmatch(parts[2]) and NAME.fullmatch(parts[3])
             and parts[4] in ("aks01", "aks02") and parts[5] in FILES,
             "Unexpected GitOps release folder or file: " + path)
        key = "/".join(parts[:5])
        releases.setdefault(key, {})[parts[5]] = object_id

    checked = []
    with tempfile.TemporaryDirectory(prefix="aks-gitops-validation-") as temporary:
        for index, (path, files) in enumerate(sorted(releases.items())):
            proposal = Path(temporary) / str(index)
            proposal.mkdir()
            for name, object_id in files.items():
                (proposal / name).write_bytes(git(source, "cat-file", "blob", object_id))
            receipt = validate_proposal(proposal, require_build=True)
            need(target_path(receipt["target"]) == path,
                 "Committed GitOps path differs from receipt target: " + path)
            checked.append({"path": path, "source_commit": receipt["source_commit"],
                            "image": receipt["image"]})
    return {"result": "passed", "gitops_commit": revision, "release_count": len(checked),
            "releases": checked,
            "validation_scope": "Committed release integrity and scan attestations; producer provenance is verified during build selection."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.source), indent=2))


if __name__ == "__main__":
    try:
        main()
    except yaml.YAMLError:
        raise SystemExit("Committed GitOps validation failed: invalid manifest YAML") from None
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("Committed GitOps validation failed: " + str(error))
