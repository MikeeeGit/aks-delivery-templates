# A three-tier deployment system for private AKS

The platform separates infrastructure, cluster services, and application releases because they change at different speeds and need different permissions. Terraform builds the Azure foundation. A platform pipeline manages the shared Kubernetes services. Application pipelines build one image and prepare selected-cluster releases. The application owner chooses direct pipeline apply or Argo CD reconciliation from Git, then verifies the result. A separate infrastructure change controls which healthy cluster receives user traffic.

This public implementation preserves that structure from the original design document and delivery templates. It replaces private estate configuration with a synthetic example and modernizes the ingress path to Gateway API with Envoy Gateway. The original design is the architectural source; this repository does not establish that an existing production estate has been migrated.

To run it, use the [ordered Azure pipeline run list](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/quick-runbook.md) and its linked deployment/removal procedures. The [21 September 2026 Azure record](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/qualification-2026-09-21.md) records real dual-slot application/CSI deployment and WAF cutover/rollback separately from the local Kubernetes tests.

## Three lifecycles, with explicit handoffs

| Tier | Owns | Typical change | Handoff |
| --- | --- | --- | --- |
| 1. Azure infrastructure | Remote state, hub and spokes, DNS, egress, ACR, private AKS slots, identities, Application Gateway WAF | Add a subnet, upgrade a cluster, change a traffic target | Reviewed resource IDs, cluster names, identity bindings, registry and private addresses |
| 2. Kubernetes platform | CRDs, versioned Helm releases, ingress controller and listeners, namespace policy, shared manifests and workload ServiceAccounts | Upgrade Envoy or a monitoring component; establish a namespace | Ready platform services, scoped access, private listener and certificate contract |
| 3. Application delivery | Container build and security gate, release receipt, Kustomize Deployment/Service/HTTPRoute, application verification | Promote an existing successful build to one or both slots | Exact image digest, source commit and verified slot |

A namespace bootstrap is part of the platform foundation; it is not the whole second tier. Independently versioned Helm releases and shared objects are essential to that tier. A routine app release should not recreate the network, grant itself cluster administration, or upgrade the ingress controller.

~~~mermaid
flowchart TB
  TF["Tier 1 — Terraform infrastructure"] --> NET["Hub/spokes, DNS, egress, ACR and private AKS"]
  TF --> EDGE["Application Gateway WAF and backend DNS"]
  PLATFORM["Tier 2 — platform services pipeline"] --> SHARED["Pinned CRDs and Helm releases<br/>GatewayClass, per-slot Gateway, namespaces and shared objects"]
  APP["Tier 3 — application pipeline"] --> BUILD["Build once → scan → immutable release receipt"]
  BUILD --> RELEASE["Select successful build and cluster slots"]
  NET --> SHARED
  SHARED --> RELEASE
  RELEASE --> A["aks01: route, Service and application pods"]
  RELEASE --> B["aks02: route, Service and application pods"]
  CLIENT["Client HTTPS"] --> EDGE
  EDGE --> ALIAS["Stable private backend alias"]
  ALIAS --> PROXYA["aks01 private Envoy listener"]
  ALIAS -. "separate approved traffic change" .-> PROXYB["aks02 private Envoy listener"]
  PROXYA --> A
  PROXYB --> B
~~~

## Tier 1: infrastructure establishes the boundaries

The Azure roots use reviewed, layered configuration and separate state for each component, environment and region. The shared Terraform helpers bind the chosen environment to the intended tenant, subscription and backend. A saved plan is reviewed before apply; it is not recreated silently when approved.

First-time state storage is a real dependency. It must exist and be accessible before normal remote-state operations. Network creation, reciprocal peerings, firewall/DNS readiness and private access also have an order. Terraform validation and mocked tests can verify the declared contracts, but cannot demonstrate live Azure role propagation, DNS resolution or routing.

Each AKS slot has its own cluster and subnet. Control-plane, kubelet and application workload identities have distinct responsibilities. The application identity uses federated credentials for the intended ServiceAccount on each selected cluster issuer. An image-pull identity needs access to ACR; that does not authorize a pod to read Key Vault.

Infrastructure outputs are the handoff to delivery. Export the actual outputs and review them against the private consumer configuration. Synthetic resource IDs and addresses in an example are not discovery, resource creation or proof that an address is available.

## Tier 2: independently managed platform services

The [platform-services guide](platform-services.md) and [platform example](../examples/platform-envoy/) describe the separate platform consumer. A platform release selects an environment, region and one or both cluster slots. Its configuration identifies exact chart versions and package hashes, committed values and reviewed shared manifests. Preparation produces an immutable bundle; the privileged apply uses that reviewed bundle and its receipt.

The platform owner manages cluster-scoped APIs and controller permissions. CRDs have an explicit lifecycle: a Helm release rollback does not reverse a CRD schema change. Namespace bootstrap and application role grants remain separately reviewable through the [bootstrap flow](bootstrap.md). Namespaces, ServiceAccounts, common manifests, diagnostics and third-party service upgrades are platform work, rather than hidden side effects of an application build.

For the maintained ingress profile:

- The Envoy Gateway controller runs in `envoy-gateway-system`.
- `GatewayClass/envoy-gateway` identifies that controller.
- Platform-owned `Gateway/platform-demo-private` lives in `platform-demo` and references the same-namespace TLS Secret `platform-demo-tls`.
- GatewayNamespace mode places that Gateway's proxy infrastructure in its namespace. The application owns `HTTPRoute/platform-demo` and the ClusterIP Service it references.
- Each slot has its own private frontend. The migration example reserves candidate addresses `10.81.0.21` and `10.81.4.21` so they do not contend with the older direct-Service or compatibility frontends at `.20`.

Gateway API is the configuration API, Envoy Gateway reconciles it, and Envoy Proxy handles requests. These roles are separate. The [migration guide](ingress-migration.md) and [decision record](decisions/0001-gateway-api.md) explain the controller choice, source behavior mapping and acceptance requirements. GatewayNamespace mode is described in the [Envoy documentation](https://gateway.envoyproxy.io/docs/tasks/operations/gateway-namespace-mode/).

The public platform demo is not a preinstalled production monitoring stack. Monitoring, certificate automation and other services need their own pinned releases, permissions, capacity and operational checks. The original document described those responsibilities; it also identified some future ideas. A future idea is not evidence that a component was deployed.

## Tier 3: build once and promote the selected release

The synthetic [aks-platform-demo](https://github.com/MikeeeGit/aks-platform-demo) follows the source delivery pattern without copying business application code. It uses Node.js for a small independently testable HTTP application. Kustomize remains the shared application configuration renderer. The original direct pipeline applies its reviewed bundle; the additive Argo CD method commits the same rendered YAML to Git for Argo to reconcile. Kustomize and Argo have different responsibilities and can be used together. See [delivery methods](delivery-methods.md). Helm is used for pinned platform services in this reference.

The build records the full source commit in the image and produces an immutable registry digest. A mandatory remote-image security scan blocks a promotable release receipt when HIGH or CRITICAL findings violate the gate. A pushed image whose scan failed is not an approved release. Keep the scan report with the private build evidence; a test of scanner invocation is not a successful container scan.

There are two useful paths:

1. Build the current reviewed source, then pass its receipt to the selected cluster slots.
2. Select a previous successful build run, verify its producer and receipt, and promote that same digest without rebuilding.

The [promotion guide](promotion.md) explains receipt selection and checks. The [GitHub](github.md) and [Azure DevOps](azure-devops.md) entrypoints support selected slots and sequential deployment. Sequential deployment stops before the next slot when the first fails. Parallel deployment is an explicit operator choice, not a traffic failover mechanism.

An application release has both immutable and target-specific data. The image digest and source revision remain the same across slots. Runtime `APP_SLOT`, target cluster and reviewed environment configuration differ. The release bundle binds those choices together and verifies the exact target before applying.

~~~mermaid
sequenceDiagram
  participant Dev as Reviewed source
  participant Build as Build and scan
  participant Store as Registry and release receipt
  participant Operator as Release operator
  participant Pipeline as App delivery
  participant Candidate as Selected AKS slot
  participant Infra as Traffic owner
  Dev->>Build: Full source commit
  Build->>Store: Push immutable image
  Build->>Build: Scan selected remote digest
  Build->>Store: Publish receipt only after success
  Operator->>Pipeline: Successful build run + selected slot
  Pipeline->>Store: Verify producer, commit and digest receipt
  Pipeline->>Candidate: Apply reviewed Kustomize bundle
  Candidate-->>Pipeline: Rollout and exact slot/revision
  Pipeline->>Candidate: Gateway and route status + verified HTTPS
  Operator->>Infra: Candidate acceptance evidence
  Infra->>Infra: Review separate saved traffic plan
  Infra->>Candidate: Switch stable backend alias
~~~

## Optional Argo CD application delivery

The infrastructure and platform tiers stay independently operated. Each selected slot gains a namespace-scoped Argo installation. CI still builds/scans once and renders the same Kustomize source, but opens a one-slot GitOps PR instead of applying workloads. Argo reads only the committed `manifest.yaml` using directory mode. It does not need an extra Kustomize render.

An approved merge establishes desired state; explicit sync and HTTPS verification qualify that release. Git changes and application image changes have distinct revisions. The default is manual sync, no automatic pruning and no cascading-deletion finalizer. Continuous comparison shows drift; deliberate sync repairs it. Direct pipelines and Argo must never write the same application resources concurrently.

Read the [design](argocd-design.md), [deployment](argocd-deployment.md), [operations](argocd-operations.md) and [troubleshooting](argocd-troubleshooting.md) guides. The direct sequence above remains a fully supported choice.

## TLS, identity and network policy are connected

The full profile retains TLS between Application Gateway and the cluster listener. Application Gateway verifies the backend certificate and sends the correct Host and SNI. Envoy terminates that connection and forwards HTTP to the application's Service. This last hop is constrained by NetworkPolicy; the sample does not implement pod-to-pod mTLS.

Key Vault CSI uses the dedicated workload identity to mount certificate material and synchronize the Kubernetes TLS Secret. An application pod must mount the CSI volume before that synchronization occurs. The listener can therefore remain pending during first deployment until the app pod has mounted the certificate. A Terraform dependency only orders API operations; it does not prove Entra, Key Vault or network propagation. See the [AKS CSI TLS example](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-nginx-tls) for the Secret synchronization mechanism; its legacy controller choice is not the maintained controller selected here.

The application policy permits its Gateway proxy pods to reach port 8080 and permits DNS egress. It does not grant arbitrary Internet or database access. The CSI node/provider components have their own network and authorization requirements. Real applications need explicit additional dependencies and tests using their production network-policy implementation.

| Identity | Necessary responsibility |
| --- | --- |
| Infrastructure operator | Approved Azure resource and state operations |
| Platform operator | Reviewed controller/CRD/shared-object lifecycle |
| Namespace bootstrap operator | Namespace admission settings and scoped application role assignments |
| Image builder | Push and scan the intended registry repository |
| Application deployer | Selected cluster access and required namespace resource operations |
| AKS control plane | Manage the cluster's Azure resources |
| Kubelet | Pull application images |
| Application workload | Access only the intended Key Vault objects or other declared services |
| Application Gateway | Retrieve its frontend certificate and serve the edge |

Namespace-level Azure RBAC Writer alone must not be assumed to cover every custom resource. The platform owner must establish the necessary HTTPRoute and SecretProviderClass authorization without giving the app deployer controller or cluster-admin ownership.

## Slots, upgrades and traffic switches

Two slots allow a cluster or platform upgrade to be qualified away from active traffic. They are not automatically disaster recovery: shared DNS, registry, networking, identity and application data can still be common dependencies.

Deploying an image, upgrading a controller and changing the active DNS/backend target are separate operations. Validate the inactive slot first, including HTTPS routing and the expected application revision. Only then approve the infrastructure-owned traffic change. Keep the previous healthy slot available for the rollback window.

DNS TTL, resolver caches, connection reuse and application sessions prevent a universal instantaneous or zero-downtime promise. Traffic rollback restores the previously healthy target; application rollback reapplies a previous receipt. Database and external state compatibility require their own recovery procedure.

## Repository and file map

| Repository or file | Role |
| --- | --- |
| [terraform-delivery-templates](https://github.com/MikeeeGit/terraform-delivery-templates) | Azure Terraform helpers, reusable infrastructure CI and output handoff |
| [azure-network-foundation](https://github.com/MikeeeGit/azure-network-foundation) | Hub/spoke networks, subnets, DNS and network configuration |
| [azure-aks-foundation](https://github.com/MikeeeGit/azure-aks-foundation) | Private cluster slots and identity foundations |
| [azure-application-gateway](https://github.com/MikeeeGit/azure-application-gateway) | WAF, frontend/backend TLS and independently reviewed traffic selection |
| [aks-delivery-templates scripts](../scripts/) | Platform, bootstrap, application and selected-release helpers |
| [GitHub reusable workflows](../.github/workflows/) / [Azure stages](../azure-pipelines/stages/) | The same delivery contracts on both hosts |
| [Platform examples](../examples/platform-envoy/) | Independent platform-service consumer configuration |
| [aks-platform-demo](https://github.com/MikeeeGit/aks-platform-demo) | Synthetic application, slot overlays, inactive caller recipes and acceptance harness |

Documentation links may follow a repository's main branch. Executable consumers must use the reviewed immutable template commit and the selected immutable image/chart references.

## What is preserved and what is deliberately changed

The preserved design is three independent lifecycles, dual private clusters, platform-owned ingress, Key Vault-backed TLS, Kustomize application deployment, selected-build promotion and a separately controlled stable backend alias.

The public changes include synthetic configuration; a small replacement application; scoped identities and federated access; image-digest receipts and build security gating; explicit namespace admission and network policies; and Gateway API with Envoy instead of retired community ingress-nginx. The archived NGINX profile is a migration comparison, not the default. The direct-Service sample remains useful for learning and isolated testing, but is not full parity with the original ingress-backed system.

Use the [operator walkthrough](operators-walkthrough.md) for the complete order. Describe evidence precisely: source and render tests demonstrate contracts; a retained two-cluster acceptance report demonstrates only that Kubernetes test environment; Azure qualification must separately prove private connectivity, image pulls, workload identity, CSI access, actual ILB assignment, WAF health and cutover. No guide, pipeline file or mocked test is itself evidence of a successful cloud deployment.


The [custom-resource authorization example](../examples/authorization/README.md) is an opt-in Azure ABAC preview recipe. Review its limitations: namespace Writer retains broad access to ordinary same-namespace objects, including proxy and TLS Secret resources. The reference ownership split does not by itself establish hostile-tenant isolation.
