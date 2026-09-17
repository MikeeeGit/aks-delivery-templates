# Independently versioned platform services

This is the second deployment tier between Terraform and application releases. Namespace/RBAC [bootstrap](bootstrap.md) prepares identities and security settings; it does **not** replace controller, CRD, metrics/logging or common-resource lifecycle. Platform changes have their own repository commit, versioned chart inputs, privileged identity and `platform-<environment>-<region>-<slot>` approval environment. Application releases keep their own namespace-scoped identity and do not select controller versions.

Use [the maintained Envoy profile](../examples/platform-envoy/README.md) for the demonstrated private Gateway API path. [The tiny local chart](../examples/platform/README.md) proves the engine offline. [NGINX compatibility](../examples/platform-compatibility/README.md) is an explicitly retired archival/migration profile. Additional monitoring charts fit the same pinned-release interface, but no organization-specific monitoring agent, paid account or commercial token is supplied. Helm support alone does not establish production observability.

## Prepare the private platform consumer

Copy the **contents** of the selected example into a private platform repository so `platform.services.json`, `values/` and `manifests/` are at its root. Keep application source in the separate app repository. Replace every synthetic tenant/subscription/resource/identity reference from applied infrastructure outputs. Commit reviewed configuration before rendering; helpers archive the exact commit, not working-tree edits.

The platform identity needs AKS Cluster User to obtain Entra user credentials and sufficient cluster API privileges for its reviewed CRDs, admission policies, RBAC, namespaces and chart resources. An operator initially grants scoped AKS RBAC Cluster Admin on the intended clusters; this privileged identity must be separate from application deployment. Restrict its OIDC subjects, branches, environments and private worker pool. Use disposable trusted workers with private API DNS/routing. Public PR code never runs there. [GitHub](github.md) and [Azure DevOps](azure-devops.md) describe the trust setup.

For the UDR scenario, apply the reviewed [platform egress profile](https://github.com/MikeeeGit/azure-firewall/blob/main/examples/aks-platform/README.md): node image pulls require the selected registry's auth/registry/CDN path and CSI requires the actual vault path. Controller Helm package access is a worker prerequisite, separate from node image egress. Mirroring images to ACR requires pinning the mirrored digests and reviewing chart/proxy values; allowing only ACR does not permit Docker Hub.

## Configuration contract

| Field | Contract |
|---|---|
| `schema_version`, `tenant_id` | `1`, explicit expected tenant UUID |
| `targets[].environment`, `.region`, `.slot` | Unique target; slot is `aks01` or `aks02` |
| `.subscription_id`, `.resource_group`, `.cluster_name` | Actual target from infrastructure; no ambient subscription fallback |
| `.approval_environment` | Separate `platform-` prefixed environment |
| `.namespaces` | Explicit namespace set; existing metadata is preserved |
| `.workload_service_accounts` | Name, declared namespace and applied UAMI `client_id` |
| `.manifests` | Ordered local reviewed YAML, applied after charts; no inline Secret, Namespace or CRD objects |
| `.manage_crds`, `.crd_bundles` | Explicit lifecycle opt-in; each HTTPS bundle has an exact SHA256 |
| `.releases[]` | Unique release name/declared namespace, chart, ordered local values and bounded timeout |
| `.releases[].chart` | Exact name/version/package SHA256 and HTTPS `.tgz`, digest-pinned `oci://...@sha256:...`, or committed `repo:` package |
| `.kubernetes_version` | Optional full version for offline Helm rendering; does not upgrade AKS |

The shared demo UAMI is federated to both cluster OIDC issuers. Both slot ServiceAccounts therefore use the **same applied client ID**. The platform creates the ServiceAccount before application delivery; the CSI application profile reapplies that same reviewed binding. Keep both declarations aligned from the infrastructure handoff. An annotation alone does not create federation or grant vault access.

Namespace bootstrap assigns ordinary Writer permissions. The Gateway API/CSI app additionally requires tested custom-resource access. See [conditional authorization examples](../examples/authorization/README.md), including the current preview qualification, positive/negative checks and same-namespace trust boundary. GatewayNamespace mode places Envoy pods/Service in the application namespace; a trusted namespace Writer can affect these ordinary resources and Secrets even when it cannot edit EnvoyProxy or Gateway. This profile is not a hostile multi-tenant isolation boundary.

## Review and execute

Install the pinned clients/dependency using [getting started](getting-started.md), then from the private platform repository:

```bash
.venv/bin/python ../aks-delivery-templates/scripts/platform_services.py validate --source .
.venv/bin/python ../aks-delivery-templates/scripts/platform_services.py prepare \
  --source . --commit FULL_REVIEWED_COMMIT --environment pprd --region uks --slot aks01 \
  --output .aks-delivery/platform-aks01
sha256sum .aks-delivery/platform-aks01/platform.json
```

Preparation is cloud-credential-free, but may download publicly pinned chart/CRD artifacts. Review `platform.json`, original/normalized CRDs, chart bytes, values, rendered chart preview, ServiceAccounts and common manifests. Every input is hash-bound to the separately retained receipt checksum. Reusing a nonempty output directory fails. Helm rendering previews reviewed inputs; it is **not an exact saved execution plan**. Hooks, cluster capabilities and lookups can affect installation.

After operator review, authenticate as the platform identity and apply the exact bundle:

```bash
.venv/bin/python ../aks-delivery-templates/scripts/platform_services.py apply \
  --output .aks-delivery/platform-aks01 --receipt-sha256 REVIEWED_RECEIPT_SHA256
```

Local apply asks for confirmation; CI supplies `--yes` behind the separate environment gate. The operation verifies target tenant/subscription, creates a temporary Entra kubeconfig and isolated Helm directories, then:

1. Creates only missing namespaces; existing Pod Security labels and metadata are untouched.
2. Server-validates/applies reviewed CRDs with a dedicated field manager and waits for `Established`.
3. Applies bundled safe-upgrade admission policies after CRDs exist.
4. Applies reviewed workload ServiceAccounts.
5. Runs `helm upgrade --install` for each pinned chart with `--skip-crds`, explicit values, wait/watch, job waiting and a bounded timeout.
6. Server-validates/applies common manifests, then reports namespaced workload/Service diagnostics.

Failure propagates and stops later releases/slots in sequential mode. No unconditional uninstall, ignored failure, force-conflict override, CRD deletion or automatic cross-slot rollback occurs. A Helm failure can leave partial resources; inspect release status before repeating a reviewed operation. CRD downgrade/deletion, ownership conflicts and data migration require an explicit operator procedure. Upgrading a chart does not automatically upgrade its CRDs: review and update both pinned inputs together.

For first Envoy installation, the controller can be ready before the platform-owned Gateway has its TLS Secret. The CSI application mounts create/synchronize that Secret after app rollout. The platform stage does not wait for a missing first-use certificate; application delivery subsequently requires current-generation Gateway/route conditions and validated HTTPS responses before success. Monitoring integrations need acceptance checks appropriate to those releases; generic Helm readiness does not prove external telemetry ingestion.

## CI entry points

Call [GitHub platform-services.yml](../.github/workflows/platform-services.yml) with `template-ref` (full shared SHA), `config`, `environment`, `region`, `cluster-slots` JSON, platform `client-id` and private `runs-on` JSON. `deploy-sequentially` defaults true; the second slot waits for the first successful platform operation. Hosted prepare has no Azure identity. The approved job downloads the immutable same-run artifact, checks the independent receipt SHA and current protected branch, then authenticates. [A complete inactive caller](../examples/github-platform.yml) is supplied.

Call [Azure platform-services.yml](../azure-pipelines/stages/platform-services.yml) with `serviceConnection`, `environment`, `region`, `clusterSlots`, optional `deploySequentially`, `configFile`, `pool`, `stageName`, `dependsOn` and `stageVariables`. Precreate `platform-<environment>-<region>-<slot>` environments with reviews, branch checks and exclusive locks. Use a repository resource named `aksTemplates` pinned to a reviewed full SHA; [caller example](../examples/azure-platform.yml). The helper requires the configured approval name to match. Parallel mode is explicit and has independent outcomes, not a cross-cluster transaction.

Current local evidence covers real Helm rendering, pinned upstream CRD schema checks, receipt tampering, failure ordering, existing-namespace preservation and real HTTPS trust/Host/SNI checks. Successful Azure first install, node pulls, preview RBAC conditions, Key Vault CSI synchronization and ILB allocation remain live qualification requirements.
