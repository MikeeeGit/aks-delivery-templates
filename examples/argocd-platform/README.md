# Optional Argo CD platform installation

This adds Argo CD to each cluster after the existing Terraform, namespace/RBAC, and Envoy platform stages. It does not replace those stages or the direct delivery pipelines. CI renders the original application Kustomize overlays once; Argo CD reconciles that same reviewed output as plain YAML using directory mode. Argo itself does not require Kustomize, and this example adds no runtime Kustomize wrapper. Choose one application delivery owner per cluster; do not run direct apply and Argo against the same resources simultaneously.

The shared [bootstrap helper](../../scripts/argocd_bootstrap.py) installs Argo CD v3.5.3 using the official namespace installation. [The lock file](../../argocd-lock.json) binds upstream manifest bytes and every runtime image to SHA256 digests. The separate Application, AppProject and ApplicationSet CRDs require an initial platform administrator. No Argo ServiceAccount receives a ClusterRoleBinding.

## Prepare and review

Use the pinned Python requirements and kubectl described in [getting started](../../docs/getting-started.md). Preparation downloads public inputs and makes no cluster calls:

```bash
python scripts/argocd_bootstrap.py prepare \
  --namespace argocd --app-namespace platform-demo \
  --profile evaluation --output .aks-delivery/argocd-platform

sha256sum .aks-delivery/argocd-platform/bootstrap.json
```

Review all three YAML files and retain the receipt checksum independently. The default `evaluation` profile has one controller, repository server, Redis and API/UI server. Optional Dex, ApplicationSet and notification controllers are supplied but scaled to zero because this example does not configure SSO, generated Applications or outbound notifications. Their CRD/configuration presence is not evidence those capabilities were tested.

For production resilience, prepare a separate bundle with `--profile ha`. This selects the official HA namespace installation and its pinned Redis/HAProxy images. Upstream Redis anti-affinity requires at least three suitably schedulable nodes. Review resources, topology, disruption budgets and capacity before choosing this profile. Hosted two-cluster acceptance exercises the evaluation profile; it does not prove HA failover.

## Install independently on each selected cluster

Obtain the correct kubeconfig through the existing private platform identity. Confirm its API endpoint and context against the applied infrastructure. The helper does not authenticate to Azure or fall back to a current ambient context.

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" config view --minify
python scripts/argocd_bootstrap.py apply \
  --output .aks-delivery/argocd-platform \
  --receipt-sha256 "$APPROVED_BOOTSTRAP_SHA256" \
  --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT"
```

Use `--yes` only in the existing privileged platform approval stage. Select aks01 and aks02 explicitly and run sequentially. The same generic namespace bundle can be used on each intended cluster; the **context selects the actual target** and must be reviewed for every apply.

Apply preserves existing namespace metadata, installs CRDs with server-side apply without force-conflict overrides, waits for establishment, applies restricted access/configuration, then waits for controller/server/Redis workloads. A failure stops execution. It never deletes namespaces, CRDs, application resources or a previous installation. Existing Argo installations with other owners must be reviewed for field ownership rather than forcibly adopted.

## Access boundary

The in-cluster registration is a Kubernetes Secret containing only the server address, namespace allowlist and `config: {}`. There is no embedded token, client secret, certificate key or cross-cluster credential. Argo uses the rotating projected ServiceAccount credential in its own cluster.

The controller can write Deployment, Service, ConfigMap, ServiceAccount, NetworkPolicy, PodDisruptionBudget, HorizontalPodAutoscaler, HTTPRoute and SecretProviderClass objects only in the named application namespace. It can read Pods, ReplicaSets, endpoints and events for health. It cannot manage Gateway, EnvoyProxy, Secrets, Roles, RoleBindings, CRDs, Namespaces or other application namespaces. These permissions cover allowed resource kinds throughout the application namespace; they can affect ordinary Envoy proxy Deployments/Services there. AppProject and reviewed manifests remain essential, and this is not hostile-tenant isolation. The API server gets a separate read-only application Role and pod-log permission. Argo's own namespace Roles retain upstream permissions needed for its configuration and repository credentials; access to that namespace remains privileged.

The cache is limited to the named application namespace and the listed resource kinds; strict RBAC discovery and annotation-based resource tracking are enabled. Annotation tracking preserves existing cluster labels. The built-in default AppProject denies every source/destination. The application delivery helper supplies an explicitly scoped project and Application. Kubernetes Roles and AppProject restrictions protect different boundaries; protect write access to both.

After install, an administrator able to impersonate ServiceAccounts can verify:

```bash
SA=system:serviceaccount:argocd:argocd-application-controller
kubectl --context "$EXPECTED_CONTEXT" auth can-i create deployments -n platform-demo --as="$SA"  # yes
kubectl --context "$EXPECTED_CONTEXT" auth can-i create httproutes.gateway.networking.k8s.io -n platform-demo --as="$SA"  # yes
kubectl --context "$EXPECTED_CONTEXT" auth can-i get secrets -n platform-demo --as="$SA"  # no
kubectl --context "$EXPECTED_CONTEXT" auth can-i create gateways.gateway.networking.k8s.io -n platform-demo --as="$SA"  # no
kubectl --context "$EXPECTED_CONTEXT" auth can-i create deployments -n default --as="$SA"  # no
kubectl --context "$EXPECTED_CONTEXT" auth can-i create clusterroles --as="$SA"  # no
```

## Network and private access

Every Argo Service stays ClusterIP; no public Ingress, LoadBalancer or unauthenticated API is added. Use an authenticated private workstation and port-forward the server for evaluation. The generated initial local admin credential is a bootstrap credential: retrieve it privately, rotate it, and configure enterprise OIDC/RBAC before shared operational use. The default authenticated role grants no application permissions.

Upstream ingress NetworkPolicies remain. Added namespace egress policies permit DNS to kube-system, communication between Argo pods, and TCP443/6443. The repository server additionally receives only the configured Git transport ports; HTTPS443 is the default. For a reviewed SSH Git endpoint pass `--git-egress-port 22`. Isolated acceptance uses a temporary HTTP Git service and explicitly adds its port. Do not copy that test endpoint or permission into production.

These are port restrictions, not hostname filtering. Apply the actual Git/provider/registry destinations through Azure Firewall or a reviewed egress proxy. Resolve private API and Git DNS, allow public image registry/CDN pulls from nodes (quay.io, public.ecr.aws and, if Dex is enabled, ghcr.io), and allow the bootstrap worker to download the pinned raw.githubusercontent.com manifests. Custom DNS/NodeLocal DNS, proxies or alternate API ports need a reviewed network-policy adjustment. Kind acceptance does not establish production CNI enforcement or Azure Firewall reachability.

## Pin updates and lifecycle

Change the lock file only after checking the upstream release, manifest SHA256s and OCI manifest digests; prepare a new output directory and rerun acceptance. Installation YAML is bound by digest, and every image reference is replaced by its immutable digest before review/apply. Hash checks establish byte identity, not independent publisher trust. Argo publishes signed images and provenance for additional supply-chain verification.

CRD and controller updates are privileged platform changes. Test the inactive cluster first, preserve the prior bundle and application Git commits, and follow the version-specific upgrade notes. CRD downgrade or deletion is not an automatic rollback. Restore controllers/configuration deliberately while retaining application resources and authoritative Git desired state.

Official references: [installation](https://argo-cd.readthedocs.io/en/stable/operator-manual/installation/), [declarative cluster restrictions](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#clusters), [resource inclusion and RBAC discovery](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#resource-exclusioninclusion), [projects](https://argo-cd.readthedocs.io/en/stable/user-guide/projects/), [artifact verification](https://argo-cd.readthedocs.io/en/stable/operator-manual/security/#verification-of-argo-cd-artifacts).
