# Verification

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
python3 scripts/install_tools.py --directory "$PWD/.aks-delivery/tools"
export PATH="$PWD/.aks-delivery/tools:$PATH"
.venv/bin/python -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```

Tests create temporary synthetic Git fixtures and run the real pinned Kustomize client offline. They check snapshot isolation, immutable image injection, target selection, namespace/API restrictions, local reference constraints, manifest/receipt tampering, tenant mismatch, private-PR guards and apply short-circuiting after failed server validation. Build authentication and Docker calls are stubbed; their arguments and temporary credential/builder cleanup are checked.

Additional regressions cover bootstrap target/tenant/CSI/Kubernetes-minor checks before mutation, namespace dry-run failure, scoped deterministic role assignments, successful-build workflow/repository/run/commit bindings, unsafe receipt archives, real execution of selection-job output scripts, sequential failure gating, exact service identity and rejected redirects. These mocks verify command contracts and failure handling; they do not establish Azure permissions or network reachability.

Platform tests use the real pinned Helm client and deterministic local chart. They reject chart/version/SHA mismatches, stale output reuse, unbound manifests and changed receipts/CRD bytes; verify existing namespace metadata is not reapplied; and check CRD apply/Established ordering before Helm/custom resources and stop-on-failure behavior. The maintained profile was rendered from the pinned official chart and its custom manifests checked against official pinned CRD OpenAPI schemas; this is not live server/CEL validation.

HTTPS tests run a real temporary loopback TLS server with an ephemeral certificate. They check trusted Host/SNI, untrusted certificates, wrong hostnames, redirects, wrong slot/revision, stale Gateway/HTTPRoute generations, wrong controller/parent and missing listener status. Kubernetes transport is stubbed in these tests. The sample's two-cluster acceptance harness separately exercises the real controller when Docker/Kubernetes execution is available; local TLS success is not evidence that Azure ILBs, CSI or OIDC work.

[Image-security tests](image-security.md) verify the remote-digest scanner invocation, isolated registry credentials, failure propagation and required receipt metadata. No live vulnerability database or registry access is inferred from those command mocks.

YAML parsing and embedded Bash syntax are checked locally. Azure pipeline schema/runtime, OIDC login, Docker push, private API reachability and real workload rollout still need a separately approved private execution. Public tests do not contact Azure or a Kubernetes cluster. The sample application's own hosted container build test qualifies its Dockerfile separately from registry publication.

Clients and PyYAML are checksum/hash pinned. BuildKit is pinned by the verified Docker manifest digest in `build-tools.json`; upstream release metadata is retained there. Azure CLI/Docker/Buildx come from the reviewed worker image. Keep kubectl within supported server version skew and review pins deliberately when upgrading AKS.
