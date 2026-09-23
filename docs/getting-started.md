# First deployment

This walkthrough describes the original direct pipeline method. The additive [Argo CD method](argocd-deployment.md) shares the same infrastructure, platform and Kustomize source. [Choose one application writer](delivery-methods.md) before deployment.

Prepare the network and [Azure AKS Foundation](https://github.com/MikeeeGit/azure-aks-foundation), with working private API DNS/routing, node egress, Entra RBAC and registry pull rights. ACR/private Key Vault endpoints need their own network path. The AKS CSI add-on does not create application workload federation, permissions or SecretProviderClasses automatically.

Run the separate [bootstrap workflow/stage](bootstrap.md) for each selected cluster. It validates the private AKS/Entra/workload-identity baseline, creates the namespace with restricted Pod Security labels and assigns deployment identities Cluster User plus namespace-scoped AKS RBAC Writer. Infrastructure supplies the CSI add-on when used; bootstrap verifies it. Normal application deployment rejects Namespace and cluster-scoped objects and never requests admin credentials.

For the maintained application profile, prepare a private platform consumer from [Envoy Gateway](../examples/platform-envoy/README.md) and run [platform services](platform-services.md) independently. This installs pinned CRDs/controller, workload ServiceAccounts and the private Gateway/EnvoyProxy. Configure the application's CSI TLS certificate and [custom-resource permissions](../examples/authorization/README.md). The direct-Service example remains optional; it does not demonstrate the complete controller/platform tier. Review the [three-tier operator walkthrough](operators-walkthrough.md) for ordering and the network/identity handoff.

Use ephemeral trusted Linux workers with private API reachability. Public PRs must never use this pool. Builds require Docker/Buildx, Azure CLI and registry access; a private ACR may require its own private build worker. Separate build and deploy identities: deploy does not need registry push rights.

Create a private app consumer. Fill `delivery.apps.json` from real infrastructure outputs; the public example uses synthetic IDs. Keep base and cluster overlays in the same commit, with an image placeholder matching `build.image_name`. ConfigMaps must be non-sensitive; use CSI/workload identity for secrets. The one-day private rendered artifact can still contain application configuration.

Configure OIDC, protected branches and pre-existing approval environments using the platform guide. Reviews and exclusive locks are external settings, not created by YAML. If a GitHub private-repository plan cannot enforce required environment reviewers, keep unattended deployment disabled until equivalent approval protection exists. Restrict Azure service connections and private pools to approved pipelines.

Commit configuration before using helpers: snapshots deliberately exclude working-tree changes. Install local dependencies and pinned clients:

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r ../aks-delivery-templates/requirements.txt
python3 ../aks-delivery-templates/scripts/install_tools.py --directory "$PWD/.aks-delivery/tools"
export PATH="$PWD/.aks-delivery/tools:$PATH"
```

With the build identity explicitly logged in, this builds and pushes once:

```bash
.venv/bin/python ../aks-delivery-templates/scripts/delivery.py build --source . --commit FULL_SOURCE_COMMIT --output .aks-delivery/build
```

Hosted builds always build their triggering protected-branch commit. For routine later promotion, use `promote.yml` to select the successful build run ID and its saved receipt; the scripts reject a failed run, another producer/repository, mismatched source or expired/missing artifact. A build receipt created outside CI supports the explicit local commands below but cannot masquerade as a CI-produced release.

Retain the successful receipt's source commit and digest. Render offline for one cluster:

```bash
.venv/bin/python ../aks-delivery-templates/scripts/delivery.py render --source . --commit FULL_SOURCE_COMMIT --digest sha256:FULL_64_HEX_DIGEST --environment pprd --region uks --slot aks01 --output .aks-delivery/aks01
sha256sum .aks-delivery/aks01/release.json
```

Review the manifest and exact target, retaining its independent receipt checksum. With deployment identity authentication and private API access:

```bash
.venv/bin/python ../aks-delivery-templates/scripts/delivery.py deploy --output .aks-delivery/aks01 --receipt-sha256 REVIEWED_RECEIPT_SHA256
```

Local apply confirms interactively. `--yes` is an explicit automation opt-in behind external CI approval. The helper checks the namespace's default ServiceAccount, checks permissions, performs server dry-run, applies and waits ten minutes for rollout. Configured verification checks readiness and exact source revision/cluster through the selected Service. With `verification.ingress`, it additionally requires current-generation Gateway/HTTPRoute conditions for all declared listeners and verifies HTTPS through that cluster's Envoy Service using actual Host/SNI and certificate trust. Redirects are rejected. Apply is not transactional: inspect partial changes after failure. It does not prune removed resources or automatically undo a failed release.

Use the selected-cluster wrapper for `aks01`, `aks02` or both. Sequential mode is the default: the second cluster starts only after the first completes rollout and configured verification successfully. Explicit parallel mode has independent per-cluster approval/results and no cross-cluster failure rollback. Verify actual load balancer IPs and follow [promotion](promotion.md) for traffic cutover. Hosted tests cannot prove live Azure/API/app behavior.
