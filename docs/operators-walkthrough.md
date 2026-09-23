# Operator walkthrough: from an empty subscription to a verified release

This walkthrough describes the original direct pipeline method. The additive [Argo CD method](argocd-deployment.md) shares the same infrastructure, platform and Kustomize source. [Choose one application writer](delivery-methods.md) before deployment.

Use this with the [three-tier design](three-tier-deployment-system.md), [ingress migration guide](ingress-migration.md), and each infrastructure repository's setup instructions. The sequence establishes dependencies in order and distinguishes Kubernetes release checks from actual Azure traffic qualification.

Public files use synthetic IDs, names, addresses and domains. Create private consumers, replace those values together, and pin shared templates to a reviewed full commit. A public example is not ready for a privileged production apply unchanged.

## 1. Prepare the private delivery environment

Agree the tenant, subscriptions, region, address space, DNS zones, registry, certificates, approvals and ownership. Keep infrastructure, platform and application consumers separate: shared repositories provide code; private consumers own real configuration.

~~~text
workspace/
  terraform-delivery-templates/
  aks-delivery-templates/
  network/       # private azure-network-foundation consumer
  aks/           # private azure-aks-foundation consumer
  gateway/       # private azure-application-gateway consumer
  platform/      # private platform-services consumer
  application/   # private aks-platform-demo consumer
~~~

The selected egress/firewall component and external databases are separate dependencies. The app pipeline does not create them.

Protect main branches and deployment environments. Bind federated identities to the intended repository, branch/environment and tenant. Separate build, platform/bootstrap and app permissions. Private workers need API-server, registry, DNS and Azure control-plane connectivity. Public pull requests must not run arbitrary code on these workers or receive their deployment identity.

Use the [getting-started](getting-started.md), [GitHub](github.md) and [Azure DevOps](azure-devops.md) guides. Configuration environment names must match actual approval resources. A variable naming an environment does not create reviewers or approvals.

## 2. Establish Terraform state before workload roots

Follow the shared Terraform backend bootstrap guide first. State storage cannot initially depend on the remote backend it is creating. Review the initial local-state operation, then migrate that state to the protected backend once available. Use a different key per component/environment/region and the intended state permissions. Keep state, plans and local credentials outside Git.

For an already configured consumer, the normal local cycle is:

~~~bash
az login --tenant <your-tenant-id>
source ../terraform-delivery-templates/scripts/azure/terraform-functions.sh
tf_setup network pprd uks
tf_init
tf_plan
tf_apply
~~~

Run from the layout expected by the helper; `network` is the actual consumer folder. PowerShell exposes the same function names. Read the context and saved plan before approving apply. Changing `az account set` does not make a mismatched delivery context valid.

## 3. Build the network and verify private access

Create hub and spokes first. Where reciprocal peerings need both networks, create the networks with peerings disabled, then enable both sides in subsequent reviewed plans.

Apply the selected firewall/egress configuration and use actual assigned DNS proxy addresses in spoke DNS and routes. Verify private-zone resolution and required egress from runners and clusters. A copied example IP is not an allocated endpoint.

| Synthetic PPRD purpose | Subnet or address |
| --- | --- |
| AKS cluster 1 | `10.81.0.0/22` |
| AKS cluster 2 | `10.81.4.0/22` |
| Application Gateway | `10.81.8.0/24` |
| Hub management/runner path | `10.80.2.0/24` |
| Existing direct-Service frontends | `10.81.0.20` and `10.81.4.20` |
| Envoy candidate frontends | `10.81.0.21` and `10.81.4.21` |

Keep the gateway subnet dedicated and required Azure control-plane traffic allowed. Every private frontend address has one owner. An app Service and ingress controller cannot claim the same address.

<a id="4-create-aks-slots-and-export-the-identity-handoff"></a>
## 4. Create AKS clusters and export the identity handoff

Apply the AKS root with actual subnet, registry and monitoring outputs. Check both private cluster endpoints and image-pull authorization. Synthetic cluster names are `uks-pprd-example-aks01` and `uks-pprd-example-aks02`.

Prepare the dedicated workload identity and one federated credential per selected cluster issuer for the exact subject `system:serviceaccount:platform-demo:platform-demo`. Keep it separate from control-plane and kubelet identities.

Export actual Terraform outputs through the infrastructure handoff helper and review its generated context and optional ServiceAccount binding. The [AKS TLS example](https://github.com/MikeeeGit/azure-aks-foundation/tree/main/examples/ingress-tls) explains the issuer, subject and Key Vault prerequisites. Exporting configuration does not create a load balancer, allocate its IP, upload a certificate or grant every custom-resource permission.

## 5. Prepare certificates and independent platform services

Start with the [platform example](../examples/platform-envoy/) and [platform-services guide](platform-services.md). This consumer has its own `platform.services.json`, immutable chart packages, committed values, common manifests, source revision and privileged approval. It is independent of the application delivery configuration.

Install the pinned Gateway API and Envoy CRDs through the explicit platform lifecycle and wait for establishment. Then install the controller and platform-owned GatewayClass, per-cluster proxy configuration and Gateway. Review schema/privilege changes separately; Helm rollback cannot reverse CRD schemas.

~~~text
Controller namespace: envoy-gateway-system
GatewayClass:         envoy-gateway
Gateway:              platform-demo/platform-demo-private
HTTPS listeners:      https-web, https-api
TLS Secret:           platform-demo/platform-demo-tls
HTTPRoute:            platform-demo/platform-demo (app-owned)
~~~

Use the pinned profile's values rather than guessing generated Service names or copying old NGINX annotations. Configure each candidate IP/subnet and source restrictions. GatewayNamespace mode places the proxy and Gateway in the same namespace as the certificate Secret.

The certificate must cover the real replacements of `web.example.test` and `api.example.test`. The synthetic Key Vault object is `platform-demo-ingress` in `example-platform-app`. It needs an exportable private key, correct workload-identity access and reachable vault endpoint. Upload and renewal are explicit external operations; never commit private keys or PFX files.

The application's CSI mount synchronizes the Kubernetes TLS Secret. A new Gateway can remain pending until the app pod mounts that certificate, but must pass readiness before traffic cutover. A private CA requires a reviewed public trust bundle in delivery configuration and consistent Application Gateway backend trust.

## 6. Bootstrap application access

Run the separately approved [namespace bootstrap](bootstrap.md) for the selected clusters. The maintained app uses `bootstrap.gateway.apps.json`, which requires Key Vault CSI. Bootstrap establishes namespace admission settings and scoped application role assignments.

The platform operator also establishes the exact HTTPRoute and SecretProviderClass permissions. Namespace Azure RBAC Writer is not proof that every CRD is writable. Use an Entra non-admin kubeconfig and preserve the boundary between application operations and controller/cluster administration.

The ServiceAccount is `platform-demo`. Its client-ID annotation and the SecretProviderClass client/tenant IDs must match the exported identity. Pods need the workload-identity label and CSI volume. Terraform identity resources alone do not complete the Kubernetes binding.

## 7. Build and deploy a selected application release

Use the demo's [private caller examples](https://github.com/MikeeeGit/aks-platform-demo/tree/main/examples/delivery), reviewing the supplied immutable shared-template pins and updating them together when selecting another reviewed release. They select `delivery.gateway.apps.json` for the maintained profile. The direct-Service configuration remains an explicit simpler alternative.

The build tests the app, builds and pushes one image, scans the selected remote digest, and publishes a promotable receipt only after the required security gate succeeds. Retain the private scan report and producer run identity. An image present in a registry is not sufficient evidence of a successful release build.

Use the combined build/deploy caller or selected-build promotion caller. Promotion selects the actual successful producer run and expected artifact, without rebuilding or substituting a mutable image tag. Start with `aks02` when `aks01` is active. After acceptance the same digest can reach `aks01`. Sequential deployment is the default for both clusters.

The app profile creates a Deployment, ClusterIP Service, HTTPRoute, workload identity/CSI objects, HPA and NetworkPolicy. It does not install the controller, directly allocate an Azure load balancer or change the active backend alias. The private helper applies the approved immutable bundle and checks the selected cluster and full source revision.

## 8. Prove each part of the request path

| Check | Required result |
| --- | --- |
| App rollout | Expected immutable digest, available replicas and exact cluster/revision |
| Platform reconciliation | Current-generation Gateway `Programmed` and HTTPRoute `Accepted`/`ResolvedRefs` |
| Controller HTTPS | Trusted chain/SNI/Host; web and API paths return the selected release |
| Azure frontend | Envoy Service `status.loadBalancer.ingress` has the reserved candidate IP |
| Private network | Authorized runner/gateway reaches that IP; source restrictions and DNS work |
| Gateway preview | Healthy HTTPS backend, correct certificate/probe and WAF route returning the expected release |

Reusable ingress verification discovers the selected Gateway's proxy Service and checks HTTPS through port-forwarding. This exercises the controller and route, but bypasses the Azure load balancer. Independently inspect actual IP assignment and test the private network path.

Use real hostname verification for direct-IP checks, such as an approved `curl --resolve` mapping or temporary reviewed DNS name. A Host header with TLS validation disabled is not equivalent. Test the API prefix, unsupported hosts, redirects and app-specific requirements in the [migration guide](ingress-migration.md).

The final Envoy-to-app hop uses HTTP with NetworkPolicy. It is not workload mTLS. Prove allowed and denied traffic using the real AKS CNI; a kind render or successful HTTP response cannot establish Azure policy enforcement.

## 9. Preview, cut over and retain rollback

The gateway's [ingress TLS profile](https://github.com/MikeeeGit/azure-application-gateway/tree/main/examples/ingress-tls) previews the candidate independently of live traffic. Its candidate profile sends only the preview route to `10.81.4.21` over HTTPS while the original direct endpoint can remain `10.81.0.20` over HTTP.

After acceptance, plan the separate infrastructure-owned change for the stable alias and HTTPS backend settings. Review destination, Host/SNI, certificate trust and probe, then apply the saved plan. Observe errors and latency and retain the previous healthy target for the agreed rollback window.

Moving from old HTTP/direct service to HTTPS/Envoy changes both address and protocol. Rollback to the old path must restore both; changing DNS alone is insufficient. Reapplying a previous app receipt is separate from moving traffic, and database compatibility/recovery is separate again.

A sample TTL does not promise a fixed zero-downtime deadline. Existing connections, resolver caches and application sessions affect transition.

## 10. Record qualification and maintain each layer

Retain source/template commits, chart/image digests, producer/deployment run URLs, scan results, bundle receipts, both-cluster checks and the traffic plan. Record failures honestly; a workflow definition is not a successful workflow run.

| Evidence | Qualification boundary |
| --- | --- |
| Local app tests and Helm/Kustomize rendering | No running cluster or Azure deployment demonstrated |
| Docker build/container test | No Kubernetes or private network demonstrated |
| Two-cluster acceptance report | Only its tested controller/TLS/update/rollback environment |
| Private pipeline run | Actual identity, approval and selected-cluster execution |
| Azure endpoint and preview checks | Actual ILB, DNS, CSI, gateway trust and WAF path |
| Observed cutover and rollback rehearsal | Only tested routing and application recovery behavior |

The acceptance harness requires Docker. Its absence is an outstanding qualification, not a pass. Report hosted or Azure success only when retained run evidence exists.

Upgrade infrastructure, platform services and applications independently. For teardown, remove traffic dependencies first, then apps/platform resources before clusters/network. Keep state until resource cleanup is verified. Removing configuration from a role list, manifest or Helm profile is not a general deletion or access-revocation procedure; review those operations explicitly.


The [custom-resource authorization example](../examples/authorization/README.md) is an opt-in Azure ABAC preview recipe. Review its limitations: namespace Writer retains broad access to ordinary same-namespace objects, including proxy and TLS Secret resources. The reference ownership split does not by itself establish hostile-tenant isolation.
