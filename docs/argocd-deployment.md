# Deploy the dual AKS application through Argo CD

For the complete Azure lab, use the [direct-or-Argo pipeline run list](https://github.com/MikeeeGit/aks-platform-demo/blob/main/docs/AZURE-ARGOCD.md). It includes Azure workload identity/CSI checks, scoped Argo sync permissions and retirement before cloud teardown.

This guide adds the Argo route to the existing system. Start with [design](argocd-design.md); keep [operations](argocd-operations.md) and [troubleshooting](argocd-troubleshooting.md) available during the trial. The direct method remains documented in [GitHub](github.md) and [Azure DevOps](azure-devops.md).

## 1. Prepare a private consumer and applied infrastructure

Create a **private** application consumer from [aks-platform-demo](https://github.com/MikeeeGit/aks-platform-demo). Public template validation has no Azure trust. Replace reserved hosts, all-zero IDs and example resource names in that private consumer. Commit the reviewed changes; helpers use committed snapshots.

Complete the same first two tiers as direct delivery: network/firewall, state/identity, ACR, both AKS clusters, namespace permissions, workload identity federation on both issuers, Key Vault certificate access and the pinned Envoy/Gateway platform. Follow [the system walkthrough](three-tier-deployment-system.md) and [platform services](platform-services.md). The Argo addition does not create these Azure prerequisites.

Before app deployment, confirm the private worker/workstation reaches both API servers, nodes can pull the approved ACR digest, the platform-owned Gateway and CRDs exist, and the actual certificate hostnames match the app routes. An initial Gateway may await its CSI-synchronized certificate until the app starts. Ensure the cluster metrics API is available for the supplied HPA.

Choose the inactive cluster for the first trial, for example aks02. Record the current live traffic destination. Updating the inactive application must not implicitly change Application Gateway or DNS.

## 2. Pin templates and install the Argo platform

Check out a reviewed full shared-template commit; use the same SHA in reusable workflow references and `template-ref`. Install its hashed Python dependency and verified clients as described in [getting started](getting-started.md). In the following examples:

```bash
TEMPLATE_DIR=/absolute/path/to/reviewed/aks-delivery-templates
APP_DIR=/absolute/path/to/private-app
PYTHON="$TEMPLATE_DIR/.venv/bin/python"
KUBECONFIG=/absolute/private/path/to/selected-kubeconfig
EXPECTED_CONTEXT=the-reviewed-aks02-context
```

Create the virtual environment under the shared checkout if needed; `PYTHON` must contain its pinned requirements. Confirm the kubeconfig/context resolves to the intended cluster. Keep credentials outside either repository.

Prepare a credential-free Argo platform bundle:

```bash
"$PYTHON" "$TEMPLATE_DIR/scripts/argocd_bootstrap.py" prepare \
  --namespace argocd --app-namespace platform-demo --profile evaluation \
  --output "$APP_DIR/.delivery/argocd-platform"
sha256sum "$APP_DIR/.delivery/argocd-platform/bootstrap.json"
```

Review `crds.yaml`, `install.yaml`, `access.yaml` and the receipt. Save the approved checksum separately. Then apply using the platform operator:

```bash
"$PYTHON" "$TEMPLATE_DIR/scripts/argocd_bootstrap.py" apply \
  --output "$APP_DIR/.delivery/argocd-platform" \
  --receipt-sha256 "$APPROVED_BOOTSTRAP_SHA256" \
  --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT"
```

The helper waits for real CRD/workload readiness and stops on failure. Repeat against the other explicitly selected context when ready. There is no ambient current-context fallback or automatic cross-cluster apply. The [bootstrap README](../examples/argocd-platform/README.md) covers its exact RBAC, egress and upgrade contract.

`evaluation` is intentionally non-HA. `--profile ha` prepares the official HA namespace installation; review at least three schedulable nodes, resources and failure-domain placement. The two-cluster acceptance test does not demonstrate HA failover. Keep Dex/ApplicationSet/notifications disabled until their separately reviewed configuration exists.

For optional evaluation UI access, keep the tunnel on loopback:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  port-forward --address 127.0.0.1 service/argocd-server 8443:443
```

Use the private browser session, review/trust the server certificate, and retrieve the generated initial admin credential through an authorized private workflow. Do not paste credentials into Git or logs. UI login is optional for the shared Kubernetes-based sync/verification helper. Before shared use, follow the SSO/access requirements in [operations](argocd-operations.md#access-git-credentials-and-sso).

## 3. Bind the private Git repository

Create and commit `gitops.config.json` in the private application repository:

```json
{
  "schema_version": 1,
  "repository_url": "https://github.com/YOUR-OWNER/YOUR-PRIVATE-APP.git",
  "revision": "main",
  "argo_namespace": "argocd",
  "project": "platform-demo",
  "application_prefix": "platform-demo",
  "cluster_api_servers": {
    "pprd/uks/aks01": "https://aks01-api.private.example.test",
    "pprd/uks/aks02": "https://aks02-api.private.example.test"
  }
}
```

Replace both reserved API URLs with the exact HTTPS API endpoints from applied infrastructure. Obtain each actual private FQDN using the explicitly selected tenant/subscription and `az aks show --resource-group "$AKS_RESOURCE_GROUP" --name "$AKS_NAME" --query privateFqdn -o tsv`; prefix it with `https://`. Review these against the expected cluster before committing. The sync/verify helper compares the selected kubeconfig server with this target map and rejects disabled TLS verification before API access. A context name alone is not a target identity.

Use the provider's exact HTTPS clone URL and protected branch. Use the equivalent Azure Repos clone URL when hosting there. Never embed a token in the URL. Argo's repository URL, Project source allowance and credential record must match. The expected Application for this example is `platform-demo-pprd-uks-aks02`.

Give each cluster's Argo instance read-only access to that private Git repository. For GitHub, prefer a repository-scoped GitHub App with content-read permissions. For Azure Repos, use the organization's approved read-only Git identity and rotation procedure. A short-lived, repository-restricted credential is acceptable for an evaluation when permitted by the organization. See [Argo private repository authentication](https://argo-cd.readthedocs.io/en/stable/user-guide/private-repositories/).

A concrete HTTPS username/password bootstrap can consume files supplied by the operator's secret manager; these files and Secret content must never enter Git, artifacts or shell trace:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  create secret generic private-app-git \
  --from-literal=type=git --from-literal=url="$PRIVATE_GIT_URL" \
  --from-file=username="$PRIVATE_USERNAME_FILE" \
  --from-file=password="$PRIVATE_READ_TOKEN_FILE"
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  label secret private-app-git argocd.argoproj.io/secret-type=repository
```

This is an initial-create example, not a rotation script. Create the same scoped credential record independently in each Argo namespace. Verify Git connectivity without dumping the Secret. A GitHub App uses the documented App fields/private-key credential instead of this username/password example.

Private Git DNS/TLS, proxy CA trust and firewall egress must work from the **repo-server pod**, not just the workstation. Keep real Git HTTPS validation enabled. The isolated CI HTTP Git service is a test fixture and is not a production configuration.

<a id="4-build-once-and-propose-one-slot"></a>
## 4. Build once and propose one cluster

Run the existing build pipeline in the private app repo. It builds the chosen source revision, pushes the image, scans the exact digest and publishes a release receipt only after the security gate passes.

Use [GitHub gitops-propose.yml](../.github/workflows/gitops-propose.yml) or [Azure gitops-propose.yml](../azure-pipelines/stages/gitops-propose.yml) to select the numeric successful build run, trusted producer workflow/definition and correct artifact. For GitHub, pass the required `gitops-token` secret from a repository-scoped GitHub App installation token or appropriately scoped fine-grained PAT with Contents/PR write permission; the built-in GITHUB_TOKEN reads the selected build. The separate publisher credential allows its PR to trigger ordinary validation under the provider's event rules. See [GitHub token event behavior](https://docs.github.com/en/actions/concepts/security/github_token). Scope it only to this private consumer. For Azure, grant the project build identity only this repository's Create branch, Contribute and Contribute to pull requests permissions, with no branch-policy bypass; System.AccessToken authenticates those proposal writes. Give the proposal job only the documented repository/build read and proposal branch/PR permissions. It needs no Azure identity, private Kubernetes worker or Argo credential. One invocation proposes one cluster.

The proposal validates the selected build and uses the original Kustomize overlays to render the **same** `delivery.gateway.apps.json` configuration used by direct deployment. Argo consumes that committed plain `manifest.yaml` through explicit directory include selection; it does not rerun Kustomize. It opens a review-only PR under `gitops/releases/pprd/uks/aks02`. Configure required reviewers, validation and protected-branch rules outside YAML. Review the exact diff before merging.

For a local review using an already verified build artifact, the equivalent preparation is:

```bash
"$PYTHON" "$TEMPLATE_DIR/scripts/gitops.py" prepare \
  --source "$APP_DIR" --config delivery.gateway.apps.json \
  --build-receipt "$VERIFIED_BUILD_RECEIPT" \
  --build-receipt-sha256 "$APPROVED_BUILD_RECEIPT_SHA256" \
  --environment pprd --region uks --slot aks02 \
  --output "$APP_DIR/.delivery/proposal-aks02"

"$PYTHON" "$TEMPLATE_DIR/scripts/gitops.py" stage \
  --proposal "$APP_DIR/.delivery/proposal-aks02" --source "$APP_DIR"
git -C "$APP_DIR" diff -- gitops/releases/pprd/uks/aks02
```

`prepare` checks receipt bytes and release fields; it does not independently query the CI provider. The reusable proposal workflows perform the trusted successful-build selection. A locally invented receipt is not equivalent evidence. Keep the build artifact/scan report and review record.

Normal PR validation should run the committed-folder validator with the pinned shared checkout:

```bash
"$PYTHON" "$TEMPLATE_DIR/scripts/validate_gitops.py" --source "$APP_DIR"
```

This checks HEAD's Git objects, including all committed cluster folders, manifest/receipt hashes, required passed-build attestations, allowed files and target/path matching. It rejects links and unknown layout. Uncommitted edits are deliberately excluded; commit the proposed files before this check. An empty public template has no releases and is valid. This is integrity validation, not independent authentication of the receipt producer; trusted build selection and protected review remain required.

Commit only the intended cluster directory to a normal review branch and merge through the protected PR process. A successful proposal is **not** a deployment. Merging leaves the default Application waiting for an explicit sync.

<a id="5-create-the-slot-application-and-sync-the-reviewed-merge"></a>
## 5. Create the cluster Application and sync the reviewed merge

Fetch the reviewed merge and use a clean checkout at that full GitOps commit. Set `GITOPS_COMMIT` to that 40-character merge SHA; it differs from the source commit inside `release.json`. Set `BUNDLE` to the selected cluster directory in this checkout:

```bash
BUNDLE="$APP_DIR/gitops/releases/pprd/uks/aks02"
APP=platform-demo-pprd-uks-aks02

"$PYTHON" "$TEMPLATE_DIR/scripts/gitops.py" bootstrap \
  --gitops-config "$APP_DIR/gitops.config.json" --bundle "$BUNDLE" \
  --output "$APP_DIR/.delivery/argo-aks02"

kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" \
  apply -f "$APP_DIR/.delivery/argo-aks02/project.yaml"
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" \
  apply -f "$APP_DIR/.delivery/argo-aks02/application.yaml"
```

Review these generated files first: the Project must name only the intended Git URL and namespace; the Application must name this cluster's cluster path and select only `manifest.yaml` in nonrecursive directory mode. Creating them requires trusted platform/operator access to `argocd`. Do not deploy the aks01 Application into aks02.

Inspect `release.json` and retain its approved SHA256 separately, then request sync and verification:

```bash
"$PYTHON" "$TEMPLATE_DIR/scripts/gitops.py" sync \
  --gitops-config "$APP_DIR/gitops.config.json" --gitops-source "$APP_DIR" \
  --bundle "$BUNDLE" --receipt-sha256 "$APPROVED_RELEASE_RECEIPT_SHA256" \
  --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" \
  --argo-namespace argocd --application "$APP" --gitops-commit "$GITOPS_COMMIT"
```

This helper uses the operator's Kubernetes credentials to request an Argo operation. Before cluster mutation it compares every reviewed cluster file with the committed tree at that full GitOps SHA in `--gitops-source` and verifies the configured private API endpoint/TLS and application binding. It then requires exact Git revision and Synced/Healthy state, checks the approved Deployment digest and runs Service plus real Gateway HTTPS verification. It does not directly apply the app. It requires the maintained Gateway profile.

Keep the protected desired-state branch stable during the release/verification window. If it advances, review the new head and select the appropriate exact commit again; do not weaken the revision assertion to accept arbitrary `main`.

To verify again without a new sync, use the same command with `verify` instead of `sync`. Keep each cluster's kubeconfig, Application, path and approved receipt together in the release record. After aks02 passes, repeat proposal/review/sync/verification for aks01 using the same selected build digest if both should receive the release.

## Qualify the live Azure path

The helper's port-forward probes prove the in-cluster application and Envoy route; they bypass the Azure frontend. Before traffic cutover, also verify both actual Envoy ILB addresses, source-range restrictions, private DNS, node/ACR access, Entra/workload identity, Key Vault CSI certificate rotation and HPA metrics.

From an allowed private source, test the real private HTTPS address with the actual certificate SAN and trusted CA. Then verify the Application Gateway preview listener, backend health, WAF and full web/API response through that frontend. Confirm cluster and full application source revision. Never disable TLS validation to make a smoke check pass.

Use the companion Application Gateway Terraform candidate and reviewed cutover procedures to switch live traffic separately. Keep the prior healthy cluster and Git bundle through the rollback window. An HTTP-to-HTTPS migration rollback restores both the old endpoint and protocol. The Argo app sync never changes this traffic contract.

Record exactly what ran: template SHA, app source SHA, image digest, GitOps merge SHA, selected context/cluster, Argo status, HTTPS results and Azure frontend evidence. Treat outstanding Azure checks as outstanding even when hosted kind acceptance is green.
