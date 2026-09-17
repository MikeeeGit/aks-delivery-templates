# Migrating ingress-nginx to Gateway API

Decision date: 17 September 2026. This guide describes the public reference implementation and the acceptance work required to use it in an existing estate. It does not claim that an employer's production clusters have been migrated.

## Decision and scope

Use Kubernetes Gateway API with Envoy Gateway as the maintained ingress profile. Keep Application Gateway WAF at the Azure edge, an independent private ingress endpoint in each AKS slot, and a separately approved traffic switch. The application still builds once and promotes the same immutable digest to one or both clusters.

Gateway API is the configuration API; Envoy Gateway is its controller, and Envoy Proxy handles traffic. These are different responsibilities. The Kubernetes project recommends moving from the retired community ingress-nginx controller to a maintained implementation. This retirement is specific to community ingress-nginx; it does not mean all NGINX products or the Kubernetes Ingress API have disappeared. See the [Kubernetes retirement notice](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/) and [Gateway API resource model](https://gateway-api.sigs.k8s.io/docs/concepts/api-overview/).

The Azure-specific choice and alternatives are recorded in [the decision record](decisions/0001-gateway-api.md). Original ingress-nginx examples are compatibility references for migration review, not the default for a new deployment.

## Preserve the three layers

~~~mermaid
flowchart TB
  TF["Tier 1: Terraform — hub/spokes, firewall, AKS slots, ACR, Application Gateway WAF"]
  PLATFORM["Tier 2: platform pipeline — Gateway API CRDs, Envoy controller, private Gateway, namespaces/RBAC"]
  APP["Tier 3: app pipeline — build/scan once, digest receipt, HTTPRoute, ClusterIP Service, app pods"]
  TF --> PLATFORM --> APP
  CLIENT["Client"] --> WAF["Application Gateway WAF"]
  WAF -->|"HTTPS, validated backend certificate and Host/SNI"| ACTIVE["Private DNS backend alias"]
  ACTIVE --> SLOT1["aks01: private Envoy HTTPS listener"]
  ACTIVE -. "separate reviewed cutover" .-> SLOT2["aks02: private Envoy HTTPS listener"]
  SLOT1 --> ROUTE1["HTTPRoute → Service → pods"]
  SLOT2 --> ROUTE2["HTTPRoute → Service → pods"]
~~~

TLS terminates at the outer gateway and is re-established to the cluster listener. The reference application's final proxy-to-Service hop is HTTP within the cluster, constrained by NetworkPolicy. Do not call that workload mTLS or end-to-end pod encryption.

The platform team owns the controller, GatewayClass, listener, private-address configuration and TLS attachment. This is also a trust decision: in GatewayNamespace mode an application principal with namespace Writer can modify the generated proxy Deployment/Service and access namespace Secrets. The sample is a trusted single-application namespace; stronger tenant isolation requires different namespace/authorization boundaries. See the [explicit authorization profile](../examples/authorization/README.md). The application team owns routes, the application Service, workload configuration and its release. A same-namespace route and certificate reference avoid unnecessary cross-namespace grants. A separate workload identity reads the intended Key Vault material; it is not the AKS control-plane identity.

## Behaviour inventory and translation

Inspect the rendered source configuration and observed behaviour before moving an application. The supplied private snapshot contains the following families of settings; that does not establish that every application used every setting.

| Existing behaviour | Public Gateway API approach | Acceptance requirement |
|---|---|---|
| ingress class and controller installation | Versioned Envoy controller; explicit GatewayClass and Gateway attachment | Controller available; class accepted; Gateway programmed |
| host/path routing | HTTPRoute hostnames, PathPrefix/Exact matches and backend references | Positive and negative host/path tests, including /api and trailing slashes |
| private load balancer per cluster | Per-slot Envoy proxy Service with Azure internal-LB configuration | Actual assigned IP, subnet and source restrictions match the reviewed target |
| TLS Secret from Key Vault CSI | Same-namespace TLS Secret referenced by the HTTPS listener | Secret exists, valid chain/SAN/expiry, rotation and backend health verified |
| HTTP backend protocol | ClusterIP Service behind Envoy | Scheme and app redirects correct; only the intended proxy pods can reach it |
| SSL redirect annotations | Explicit listener/routing policy or edge redirect | HTTPS redirect behaviour is tested once at the intended boundary |
| read/send/connect timeouts | Explicit HTTPRoute timeouts and, where needed, Envoy traffic policy | NGINX inactivity timeouts are not equivalent to whole-request timeouts; test slow responses and uploads |
| cookie affinity and balancing | Deliberate Envoy session/load-balancing policy when the app needs it | The stateless demo does not need affinity; stateful applications must test sessions, failover and cookie attributes |
| forwarded headers and real client IP | Deliberate proxy trust policy and restricted network path | Spoofed incoming headers must not become trusted identity; verify the actual proxy hop chain |
| body size, buffering and keep-alive tuning | Per-application Envoy policy after measuring the requirement | Test uploads, large headers, streaming and WebSockets; do not copy numeric settings blindly |
| default backend | Explicit fallback route if required, otherwise unmatched traffic remains rejected | Unknown hosts and paths never expose an unintended application |
| namespace isolation | Reviewed NetworkPolicy for Envoy-to-app traffic, DNS and explicit egress | Test both allowed and denied traffic with the production CNI |

The sample preserves its /api prefix: it does not need an NGINX regex rewrite. An application's annotation-specific behaviour is not proven merely because its HTTPRoute is accepted.

Envoy's optional request-buffer policy buffers the entire request and can reject an oversized body. It is unsuitable as a blanket replacement for streaming or WebSocket routes. Keep it an explicit application decision. See [Envoy request buffering](https://gateway.envoyproxy.io/docs/tasks/traffic/request-buffering/) and [traffic policy API](https://gateway.envoyproxy.io/docs/api/extension_types/).

## Migration sequence

1. Capture the current rendered ingress configuration, certificates, private IPs, health probes, host headers, DNS TTL and a baseline set of application checks. Record the active slot and rollback target.
2. Reserve a distinct candidate ingress address. Never let an existing application LoadBalancer Service, legacy NGINX controller and Envoy controller claim the same private IP.
3. On the inactive slot, run the separately approved platform pipeline. Review the pinned CRD/chart/values bundle. Install or update CRDs deliberately, wait for establishment, install the controller, then create the private Gateway configuration. A Helm rollback does not roll back CRD schemas.
4. Deploy the chosen application release by its existing image digest. Apply workload identity/CSI resources and start pods so CSI can synchronize the TLS Secret. The Gateway may be unready until that Secret exists.
5. Check current-generation Gateway and HTTPRoute status, backend endpoints, rollout health and the expected certificate. Test the candidate HTTPS endpoint using the real hostname and certificate verification. A Service port-forward alone bypasses ingress and is insufficient.
6. Check Application Gateway's candidate backend health and perform a preview route test through WAF. Validate /api, redirects, headers, long requests and any application-specific session or upload requirements.
7. Review and apply the separate DNS/backend routing change. Observe requests, errors, latency and old/new traffic during the drain window. DNS caches, keep-alive connections and sessions mean the change is not instantaneous.
8. Keep the previous healthy slot available for the agreed rollback window. Repeat the platform/application validation on the other slot before declaring the migration complete. Remove legacy ingress resources only after acceptance and ownership review.

Rollback normally restores the previous healthy traffic target first. Reapplying an older application receipt is a separate operation. Controller, CRD, application, database and certificate rollback each have their own compatibility constraints; a stateless demo cannot qualify a stateful application's recovery.

## Required evidence

| Evidence level | What it establishes | What it cannot establish |
|---|---|---|
| Static tests and real Helm/Kustomize rendering | Configuration contracts, pinning, valid generated inputs and guarded failure paths | A running proxy or reachable AKS endpoint |
| Two-cluster Kubernetes acceptance | Controller reconciliation, HTTPS host/path routing, selected-slot deployment, inactive-slot update and rollback in that test environment | Azure identity, private DNS, cloud load balancers, WAF or production-CNI enforcement |
| Private Azure qualification | Actual service connections, role propagation, ACR pulls, CSI certificate access, ILB allocation, WAF/backend health and cutover | Production behaviour at untested load or application-specific data recovery |
| Application acceptance | Required sessions, uploads, redirects, API behaviour, streaming and data compatibility | Features not included in the acceptance cases |

Do not describe CI files as a successful CI run. Record the run URL, source commits, controller/chart version, test report and acceptance result before claiming that level passed. The local two-cluster runner requires Docker; absence of a runtime is a qualification gap, not a pass.

## Interview explanation

Use the architecture and the actual evidence together:

> “My original platform separated Terraform infrastructure, cluster platform services and application delivery. During the public modernization I identified community ingress-nginx retirement and moved the reference ingress design to Gateway API with Envoy Gateway. I kept the WAF, private cluster endpoints, TLS and immutable dual-cluster promotion. I separated platform-owned listeners from app-owned routes, then designed the migration around validating the inactive cluster before a separately approved traffic switch.”

Follow that with the precise current result: code and render tests completed, Kubernetes acceptance completed, or Azure acceptance completed. Only use past-tense deployment claims for the environments whose run evidence exists. Explain the tradeoff: we own the Envoy upgrade lifecycle in exchange for portable configuration and independent platform releases.
