#!/usr/bin/env python3
"""Prepare reviewed GitOps releases and verify Argo reconciliation of the same Kustomize bundle."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
import yaml

from delivery import (ALLOWED_KINDS, COMMIT, NAME, need, relative, render,
                      verify_bundle, run)
from releases import validate_receipt
from verify_service import verify_service, verify_ingress

FILES = {"kustomization.yaml", "manifest.yaml", "release.json",
         "build-release.json", "ingress-ca.pem"}
SERVER = "https://kubernetes.default.svc"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path):
    config = json.loads(path.read_text())
    need(config.get("schema_version") == 1, "Unsupported GitOps configuration")
    url = urlparse(config.get("repository_url", ""))
    azure_url = ((url.hostname == "dev.azure.com" and
                  re.fullmatch(r"/[^/]+/[^/]+/_git/[^/]+", url.path)) or
                 (url.hostname and url.hostname.endswith(".visualstudio.com") and
                  re.fullmatch(r"/(?:[^/]+/)?_git/[^/]+", url.path)))
    need(url.scheme == "https" and url.hostname and not url.username and not url.password
         and not url.query and not url.fragment and (url.path.endswith(".git") or azure_url),
         "GitOps repository must be an HTTPS clone URL without credentials/query")
    for key in ["argo_namespace", "project", "application_prefix"]:
        need(NAME.fullmatch(config.get(key, "")), "Invalid GitOps " + key)
    branch = config.get("revision", "")
    need(isinstance(branch, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,100}", branch)
         and ".." not in branch and not branch.endswith("/"),
         "An explicit protected GitOps branch is required")
    return config


def target_path(target):
    for field in ("environment", "region", "namespace", "deployment"):
        need(NAME.fullmatch(target.get(field, "")), "Invalid GitOps target " + field)
    need(target.get("slot") in ("aks01", "aks02"), "Select an explicit cluster slot")
    return "gitops/releases/" + "/".join(target[k] for k in ("environment", "region", "slot"))


def application_name(config, target):
    name = "-".join([config["application_prefix"], target["environment"],
                     target["region"], target["slot"]])
    need(NAME.fullmatch(name), "Argo Application name exceeds DNS-label limits")
    return name


def project(config, target):
    target_path(target)
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
        "metadata": {"name": config["project"], "namespace": config["argo_namespace"]},
        "spec": {
            "description": "Application-only delivery; platform and traffic remain separately owned.",
            "sourceRepos": [config["repository_url"]],
            "destinations": [{"server": SERVER, "namespace": target["namespace"]}],
            "clusterResourceWhitelist": [],
            "clusterResourceBlacklist": [{"group": "*", "kind": "*"}],
            "namespaceResourceWhitelist": [
                {"group": api.split("/")[0] if "/" in api else "", "kind": kind}
                for kind, api in sorted(ALLOWED_KINDS.items()) if kind != "Ingress"
            ],
        },
    }


def application(config, target):
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": application_name(config, target),
                     "namespace": config["argo_namespace"]},
        "spec": {
            "project": config["project"],
            "source": {"repoURL": config["repository_url"],
                       "targetRevision": config["revision"], "path": target_path(target)},
            "destination": {"server": SERVER, "namespace": target["namespace"]},
            "revisionHistoryLimit": 10,
            # No automated sync, pruning, namespace creation or deletion finalizer.
            "syncPolicy": {
                "syncOptions": ["CreateNamespace=false", "FailOnSharedResource=true",
                                "RespectIgnoreDifferences=true"],
                "retry": {"limit": 2, "backoff": {"duration": "5s", "factor": 2,
                                                "maxDuration": "30s"}},
            },
            "ignoreDifferences": [{"group": "apps", "kind": "Deployment",
                                  "name": target["deployment"],
                                  "namespace": target["namespace"],
                                  "jsonPointers": ["/spec/replicas"]}],
        },
    }


def materialize(bundle: Path, receipt_sha256: str, output: Path):
    receipt = verify_bundle(bundle, receipt_sha256)
    target_path(receipt["target"])
    need(not output.exists(), "Proposal output already exists; use a fresh directory")
    need(not any(p.is_symlink() for p in [output, *output.parents]),
         "Symlink proposal output is forbidden")
    output.mkdir(parents=True)
    for name in ["manifest.yaml", "release.json", "ingress-ca.pem"]:
        path = bundle / name
        if path.exists():
            need(path.is_file() and not path.is_symlink(), "Invalid bundle file")
            shutil.copyfile(path, output / name)
    (output / "kustomization.yaml").write_text(yaml.safe_dump({
        "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
        "resources": ["manifest.yaml"]}, sort_keys=False))
    return receipt


def prepare(source, config_name, build_receipt, expected_sha, environment, region, slot, output):
    need(re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""), "Independent build receipt SHA256 required")
    need(sha(build_receipt) == expected_sha, "Selected build receipt changed after CI selection")
    approved = validate_receipt(json.loads(build_receipt.read_text()))
    with tempfile.TemporaryDirectory(prefix="aks-gitops-render-") as tmp:
        bundle = Path(tmp) / "bundle"
        receipt = render(source, config_name, approved["source_commit"], approved["image_digest"],
                         environment, region, slot, bundle)
        need(receipt["image"] == approved["image"], "Build registry/repository differs from selected target")
        materialize(bundle, sha(bundle / "release.json"), output)
    shutil.copyfile(build_receipt, output / "build-release.json")
    return receipt


def validate_proposal(proposal, *, require_build=True):
    need(proposal.is_dir() and not proposal.is_symlink(), "Missing or linked proposal")
    paths = list(proposal.iterdir())
    need(all(p.is_file() and not p.is_symlink() and p.name in FILES for p in paths),
         "GitOps proposal contains unexpected files, links or directories")
    receipt = verify_bundle(proposal, sha(proposal / "release.json"))
    expected = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                "resources": ["manifest.yaml"]}
    need(yaml.safe_load((proposal / "kustomization.yaml").read_text()) == expected,
         "GitOps wrapper must reference only its validated local manifest")
    if require_build:
        build = validate_receipt(json.loads((proposal / "build-release.json").read_text()))
        need(all(build[key] == receipt[key] for key in ("source_commit", "image", "image_digest")),
             "GitOps bundle differs from approved build")
    return receipt


def stage(proposal, source):
    """Replace only the exact slot directory; unrelated Git content is never removed."""
    receipt = validate_proposal(proposal)
    destination = relative(source, target_path(receipt["target"]), exists=False)
    if destination.exists():
        need(destination.is_dir(), "GitOps slot path is not a directory")
        need(all(p.name in FILES and p.is_file() and not p.is_symlink()
                 for p in destination.iterdir()),
             "Refusing to replace unexpected content in GitOps slot")
    destination.mkdir(parents=True, exist_ok=True)
    names = {p.name for p in proposal.iterdir()}
    for old in destination.iterdir():
        if old.name not in names:
            old.unlink()
    for path in proposal.iterdir():
        shutil.copyfile(path, destination / path.name)
    return destination


def wait_application(base, name, revision, *, timeout=600):
    need(COMMIT.fullmatch(revision), "Expected GitOps revision must be a full commit SHA")
    deadline = time.monotonic() + timeout
    while True:
        value = json.loads(run(base + ["get", "application", name, "-o", "json"], capture=True))
        status = value.get("status", {})
        conditions = status.get("conditions", [])
        errors = [c.get("message", c.get("type")) for c in conditions
                  if c.get("type") in ("InvalidSpecError", "ComparisonError", "SyncError")]
        operation = status.get("operationState", {})
        current_operation = operation.get("syncResult", {}).get("revision") == revision
        if errors or (current_operation and operation.get("phase") in ("Error", "Failed")):
            raise ValueError("Argo reconciliation failed: " + "; ".join(errors or [operation.get("message", "")]))
        if (status.get("sync", {}).get("status") == "Synced"
                and status.get("sync", {}).get("revision") == revision
                and status.get("health", {}).get("status") == "Healthy"):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("Argo did not reach Synced/Healthy at the exact requested Git commit")
        time.sleep(2)


def request_sync(base, name, revision):
    need(COMMIT.fullmatch(revision), "Sync requires a full reviewed GitOps commit SHA")
    value = json.loads(run(base + ["get", "application", name, "-o", "json"], capture=True))
    need(not value.get("spec", {}).get("syncPolicy", {}).get("automated"),
         "This operator command requires manual sync; disable automated sync before explicit release control")
    need(not value.get("operation"), "Another Argo operation is already running")
    patch = {"operation": {"initiatedBy": {"username": "reviewed-gitops-operator"},
                           "sync": {"revision": revision, "prune": False}}}
    run(base + ["patch", "application", name, "--type=merge", "-p", json.dumps(patch)])


def verify_release(bundle, expected_receipt, kubeconfig, context, argo_namespace, app_name,
                   git_revision, *, gitops_config, sync=False, timeout=600):
    receipt = verify_bundle(bundle, expected_receipt)
    target = receipt["target"]
    need(context and kubeconfig.is_file(), "Explicit existing kubeconfig and context required")
    base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]
    argo = base + ["--namespace", argo_namespace]
    config = load_config(gitops_config)
    need(config["argo_namespace"] == argo_namespace and application_name(config, target) == app_name,
         "Argo namespace/Application name differs from reviewed GitOps configuration")
    current = json.loads(run(argo + ["get", "application", app_name, "-o", "json"], capture=True))
    wanted = application(config, target)["spec"]
    need(all(current["spec"].get(k) == wanted[k] for k in ("source", "destination", "project"))
         and not current["spec"].get("sources"),
         "Argo source/project/destination differs from reviewed GitOps configuration")
    need(0 < timeout <= 3600, "Verification timeout must be between1 and3600 seconds")
    if sync:
        request_sync(argo, app_name, git_revision)
    app = wait_application(argo, app_name, git_revision, timeout=timeout)
    need(app["spec"]["destination"] == {"server": SERVER, "namespace": target["namespace"]},
         "Argo Application destination differs from approved release")
    need(app["spec"]["source"]["path"] == target_path(target), "Argo source path differs from approved slot")
    workload = base + ["--namespace", target["namespace"]]
    run(workload + ["rollout", "status", "deployment/" + target["deployment"], "--timeout=600s"])
    deployment = json.loads(run(workload + ["get", "deployment", target["deployment"], "-o", "json"], capture=True))
    need(any(c.get("image") == receipt["image"] for c in deployment["spec"]["template"]["spec"]["containers"]),
         "Observed Deployment does not use the approved image digest")
    settings = target.get("verification")
    need(settings and settings.get("ingress"), "Equivalent Argo verification requires the HTTPS Gateway profile")
    env = dict(os.environ, KUBECONFIG=str(kubeconfig))
    verify_service(workload, env, settings, target["slot"], receipt["source_commit"])
    ca = bundle / "ingress-ca.pem" if settings["ingress"].get("ca_file") else None
    verify_ingress(workload, env, settings, target["slot"], receipt["source_commit"],
                   target["namespace"], run, ca)
    return {"result": "passed", "gitops_commit": git_revision, "source_commit": receipt["source_commit"],
            "image": receipt["image"], "slot": target["slot"], "argo_sync": "Synced", "argo_health": "Healthy"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--config", default="delivery.gateway.apps.json")
    p.add_argument("--build-receipt", type=Path, required=True)
    p.add_argument("--build-receipt-sha256", required=True)
    for key in ("environment", "region", "slot"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("stage")
    p.add_argument("--proposal", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p = commands.add_parser("bootstrap")
    p.add_argument("--gitops-config", type=Path, required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    for name in ["sync", "verify"]:
        p = commands.add_parser(name)
        p.add_argument("--bundle", type=Path, required=True)
        p.add_argument("--gitops-config", type=Path, required=True)
        p.add_argument("--receipt-sha256", required=True)
        p.add_argument("--kubeconfig", type=Path, required=True)
        p.add_argument("--context", required=True)
        p.add_argument("--argo-namespace", default="argocd")
        p.add_argument("--application", required=True)
        p.add_argument("--gitops-commit", required=True)
        p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.command == "prepare":
        value = prepare(args.source.resolve(), args.config, args.build_receipt,
                        args.build_receipt_sha256, args.environment, args.region, args.slot, args.output)
    elif args.command == "stage":
        value = {"path": str(stage(args.proposal, args.source.resolve()))}
    elif args.command == "bootstrap":
        config = load_config(args.gitops_config)
        receipt = validate_proposal(args.bundle)
        need(not args.output.exists(), "Bootstrap output already exists")
        args.output.mkdir(parents=True)
        for filename, resource in [("project.yaml", project(config, receipt["target"])),
                                   ("application.yaml", application(config, receipt["target"]))]:
            (args.output / filename).write_text(yaml.safe_dump(resource, sort_keys=False))
        value = {"application": application_name(config, receipt["target"])}
    else:
        value = verify_release(args.bundle, args.receipt_sha256, args.kubeconfig, args.context,
                               args.argo_namespace, args.application, args.gitops_commit,
                               gitops_config=args.gitops_config, sync=args.command == "sync", timeout=args.timeout)
    print(json.dumps(value))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, TimeoutError, subprocess.CalledProcessError) as error:
        raise SystemExit("GitOps delivery failed: " + str(error))
