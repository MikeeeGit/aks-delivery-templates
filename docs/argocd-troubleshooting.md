# Troubleshooting Argo CD, dual AKS and the application path

Use this with [deployment](argocd-deployment.md) and [operations](argocd-operations.md). First identify the failing layer; Argo is responsible for application reconciliation, while Terraform, platform controllers, Azure networking and runtime identity have separate owners.

## Start with the exact target and evidence

Record the expected context, Application/slot, GitOps commit, application source commit, image digest and last successful operation. Verify the cluster API endpoint before changing anything. The helper compares it against the committed `cluster_api_servers` entry; a context/API mismatch must be corrected at the intended target rather than bypassed. Insecure kubeconfig TLS is rejected. Keep the healthy/live slot intact.

Use bounded, selected diagnostics. Do not dump Secrets, raw kubeconfigs, whole namespace exports or CI environment variables into tickets/public artifacts:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  get application "$APP" -o jsonpath='{.status.sync.status}{"\n"}{.status.sync.revision}{"\n"}{.status.health.status}{"\n"}{.status.conditions}{"\n"}{.status.operationState.phase}{"\n"}{.status.operationState.message}{"\n"}'
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  get pods,deployments,statefulsets
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  logs statefulset/argocd-application-controller --tail=120
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  logs deployment/argocd-repo-server --tail=120
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  get events --sort-by=.metadata.creationTimestamp
```

Review log/error contents before sharing: repository URLs and resource metadata can still identify a private environment. Inspect logs for the time and Application involved rather than enabling unrestricted debug tracing.

| Symptom | Start here |
|---|---|
| Proposal fails before a PR exists | Trusted build/receipt and repository permissions |
| InvalidSpecError or permission denied | Project binding and Kubernetes Role scope |
| ComparisonError / cannot fetch repository | Git auth, DNS, CA trust, repo-server network |
| OutOfSync after merge | Default manual sync; confirm intended revision |
| Synced but Progressing/Degraded | Deployment/HPA/CSI workload conditions |
| Argo is green but HTTPS fails | Gateway/route/certificate/backend verification |
| Exact revision check fails | GitOps branch moved or wrong receipt/slot |
| Controller unavailable | Installation, image pulls, Redis, RBAC/cache and capacity |

## Proposal and receipt failures

A producer-run mismatch, expired/missing artifact, unsuccessful scan, source-commit mismatch or changed receipt should stop before publication. Confirm the numeric build ID, trusted workflow/definition and artifact name; use the actual successful build's immutable receipt. Do not edit JSON to manufacture a pass. If the old image is being promoted, the source must remain in the protected branch's accepted history.

A “protected branch changed” failure means the checkout is stale relative to the release guard. Start a fresh run after reviewing the new head. If a GitHub PR exists but validation does not start, check that its publisher used the required scoped `gitops-token` rather than the built-in GITHUB_TOKEN and inspect workflow event/policy configuration. For Azure, check the repository-scoped build identity can create branches, contribute and contribute to PRs. A rejected proposal API write needs repository branch/PR permissions and organization policy checks; it does not need Azure login or a broader Kubernetes role. A proposal can create a branch and then fail before PR creation; inspect the specific run's branch before rerunning, without merging it automatically.

A committed-tree mismatch means `--gitops-source`, `--gitops-commit` and `--bundle` do not describe the same exact slot files. Fetch/check out the reviewed merge and select its unchanged bundle; do not alter the expected hashes. A file/manifest hash mismatch means the generated directory no longer matches the approved render contract. Regenerate it from the original source and selected successful build. Review-only PRs must not introduce untracked files, credentials or another slot's content. The Application must select directory mode with only `manifest.yaml` and `recurse: false`; receipt JSON is metadata, not a deployable resource. Do not add a Kustomize/Helm detection file to the generated release directory.

## Wrong Application, Project or RBAC

Inspect only the non-secret binding fields:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  get application "$APP" -o jsonpath='{.spec.project}{"\n"}{.spec.source.repoURL}{"\n"}{.spec.source.targetRevision}{"\n"}{.spec.source.path}{"\n"}{.spec.destination}{"\n"}'
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  get appproject platform-demo -o yaml
```

The destination server is the local Kubernetes service, namespace is `platform-demo`, and path ends in the correct slot. A default-project rejection is intentional; generate/apply the dedicated Project. No Namespace, CRD, Gateway, Secret or RBAC object belongs in the app release. Add needed platform resources through tier2 instead of weakening the app project.

For controller “forbidden” errors, test the specific verb/resource using the checks in [bootstrap](../examples/argocd-platform/README.md). The controller should write app Deployments/HTTPRoutes and be denied other namespaces, Secrets, Gateways and cluster roles. If it attempts cluster-wide watches, inspect the local-cluster registration's namespaces/clusterResources fields privately, the RoleBinding namespace and resource-inclusion configuration; never replace the limited setup with cluster-admin merely to silence the error.

Argo UI SSO permissions and Kubernetes permissions are separate. A successful Argo login does not authorize `gitops.py sync`, which uses Kubernetes credentials. An operator may be allowed to read the Application but lack app port-forward access. Grant the precise intended capability to the correct identity, then retest positive and negative access.

## Repository authentication, DNS and TLS

The repo-server must reach the exact configured HTTPS clone URL. Check private DNS resolution, permitted egress/proxy, CA chain and URL spelling from its network path. Verify the credential type, expiry, repository scope and URL match. GitHub App installation access differs from a user PAT; Azure Repos credentials need the intended repository's read permission.

An authentication/401/403 error points to credentials or provider authorization. A timeout often points to routing/firewall/proxy. A certificate verification error points to hostname, trust chain or TLS interception. Fix the trust/configuration rather than disabling TLS or embedding a token in the URL. The reference helper supports approved HTTPS Git URLs; CI's temporary HTTP server is confined to the isolated fixture.

For a deliberate refresh after fixing access:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  annotate application "$APP" argocd.argoproj.io/refresh=hard --overwrite
```

Refresh fetches/compares desired state; it is not a sync approval. Under the default manual policy an OutOfSync app remains waiting.

## Stalled sync, missing CRDs or immutable fields

During a fresh bootstrap, a CRD can exist before its status conditions have been populated. Azure-hosted acceptance exposed a `kubectl wait` failure reporting a nil `.status.conditions` accessor immediately after creation. The maintained bootstrap polls the actual CRD until `Established=True`, with a bounded deadline and request timeout. Missing initial conditions are pending; rejected names, deletion, authentication/API errors and deadline expiry still stop the bootstrap before controllers or application access are applied. Inspect the selected CRD and retained failure output if that gate fails. Kubernetes documents the [delay between CRD creation and API establishment](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/#create-a-customresourcedefinition).

Look at Application conditions and operation results before retrying. Confirm all platform CRDs were Established before the Application started, and the correct Gateway API/CSI APIs exist. A missing API should be fixed in the platform tier; do not add `SkipDryRunOnMissingResource` globally to hide an incomplete cluster.

An immutable field conflict can indicate an incompatible Service/selector change or adoption of a different resource. Review the diff and design a migration. Do not globally enable Force/Replace or server-side force-conflicts: those options can recreate resources or take ownership away from another controller.

If an operation is genuinely stuck, use the approved Argo UI or [terminate-operation CLI](https://argo-cd.readthedocs.io/en/stable/user-guide/commands/argocd_app_terminate-op/) on the explicitly selected Argo server. Confirm it has stopped, inspect partial resources, then choose a reviewed retry or rollback. Deleting the Application/namespace is not the standard way to clear an operation.

## Image pulls, pods and rollout

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  get deployment platform-demo -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{" "}{.image}{"\n"}{end}'
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  get pods -l app.kubernetes.io/name=platform-demo -o wide
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  describe deployment platform-demo
```

For ImagePullBackOff, compare the exact digest with the build receipt, verify the digest still exists, then inspect kubelet/ACR pull permissions, registry mode, private endpoint/DNS and node firewall/CDN reachability. Argo's Git credential is unrelated to ACR pull identity. A passing local registry test does not prove Azure access.

For Pending, inspect node capacity, taints/tolerations, scheduling events and requests/limits. For CrashLoopBackOff, inspect bounded current/previous application logs and its health probes. Keep application source/digest consistent; substituting a mutable tag makes rollback evidence ambiguous.

## Key Vault CSI and workload identity

A pod stuck in ContainerCreating with FailedMount/CSI errors needs platform and identity checks: CSI driver/add-on readiness, actual SecretProviderClass, tenant/vault/object/certificate names, workload ServiceAccount client ID, pod workload-identity label, and federated subjects/issuer for **this** AKS cluster. Both clusters need federation even when they share the same UAMI.

Verify vault network/DNS access and the identity's approved permission to read the required object. The CSI-mounted application creates/synchronizes the TLS Secret only when its volume is mounted by a running pod. A Gateway waiting for a certificate on first deployment can therefore be downstream of a CSI mount failure.

Check only the Secret's existence/type and certificate metadata through an authorized secure workflow. Never print the TLS private key. The controller's lack of app Secret read permission is intentional and is not a reason to broaden its Role. The test fixture's local TLS Secret proves routing/verification, not real Key Vault integration.

## HPA, metrics and replica drift

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  describe hpa platform-demo
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" \
  get apiservice v1beta1.metrics.k8s.io
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  top pods
```

Check AbleToScale, ScalingActive and ScalingLimited, the metrics API, workload CPU requests and available capacity. Initial metric collection can take time. Do not delete the HPA or falsify Argo health to pass a test. The acceptance fixture installs its own pinned metrics-server; AKS should use the supported cluster metrics service.

A replica-count difference is expected under HPA control and is narrowly ignored. Image/route/config drift must remain visible. If unrelated fields are ignored, compare the Application with the generated definition and remove unreviewed ignore rules.

## Gateway, HTTPRoute and HTTPS

Inspect current-generation conditions and Service endpoints:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  get gateway platform-demo-private -o yaml
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  get httproute platform-demo -o yaml
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  get service,endpointslices -l app.kubernetes.io/name=platform-demo
```

Check Gateway Programmed and HTTPRoute Accepted/ResolvedRefs against current observed generation. Verify listener hostname, TLS Secret reference, route parent/section name, backend Service/port and healthy pod endpoints. Discover the Envoy Service with the owning-Gateway labels documented in the app's Gateway guide; do not assume a generated proxy name is stable.

A valid certificate must match the request hostname/SNI and chain to the chosen trust root. A Host header override cannot fix certificate-name validation. Check the certificate's public validity/SAN metadata securely; keep private keys out of diagnostics. Wrong Host/SNI should fail in the negative checks.

If port-forward HTTPS passes but the real private IP fails, investigate ILB allocation, source ranges, subnet permissions, UDR/firewall and CNI policy. If the private IP works but Application Gateway fails, inspect preview/live backend settings, Host/SNI, trusted root, probe path/status and WAF/backend health. DNS and frontend cutover remain separate Terraform operations.

## Revision mismatch and unchanged slots

Compare the three identifiers in the release record. The application returns the **source commit**, while Argo reports the **GitOps commit**. A wrong-slot response can indicate the wrong context, route/backend mapping, stale frontend DNS or an application overlay mismatch.

An unrelated protected-branch commit can advance Argo's comparison revision while the selected slot's manifests remain unchanged. Review current Git desired state; use that new GitOps head with the unchanged slot's original release receipt for read-only verification. During an active exact-SHA sync, hold the branch steady or repeat after reviewing the advance. Never accept a different digest or source revision merely because Argo is green.

## Argo platform unavailable

For pod ImagePullBackOff, inspect the pinned image host and node egress. For repo/controller errors, check Redis readiness, pod restarts/OOM and denied permissions. The HA profile needs enough schedulable nodes for its anti-affinity/quorum; a one-node evaluation cluster cannot demonstrate that.

The bootstrap applies CRDs without forcing conflicts. An ownership conflict on an existing Argo installation requires a reviewed adoption/upgrade plan, not an automatic override. Do not uninstall CRDs: that can remove Applications/Projects and their control history.

When the controller is down, existing app pods may continue serving. Verify the actual application and traffic path before declaring an application outage, then restore the Argo/platform component. Preserve the last known Git revision and app release, and use [recovery operations](argocd-operations.md#backup-and-cluster-recovery) to re-establish reconciliation.
