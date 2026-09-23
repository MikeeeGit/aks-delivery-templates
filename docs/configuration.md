# Application delivery contract

For the additive managed-Entra profile with Terraform-owned Azure grants and namespace-scoped Kubernetes RoleBindings, see [native Azure authorization](native-azure-authorization.md). The existing Azure RBAC profile remains available.

Version `delivery.apps.json` with the application; see the [synthetic example](../examples/delivery.apps.json).

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `tenant_id` | Expected Entra tenant |
| `registry.name`, `.login_server`, `.repository` | ACR and image repository |
| `registry.subscription_id` | Explicit registry subscription |
| `build.context`, `.dockerfile`, `.image_name` | Relative source paths and Kustomize image placeholder |
| `targets[].environment`, `.region`, `.slot` | Unique selector; cluster `aks01` or `aks02` |
| `.subscription_id`, `.resource_group`, `.cluster_name` | Actual AKS target from infrastructure outputs |
| `.namespace`, `.deployment`, `.overlay` | Existing namespace, rollout target, committed overlay |
| `.approval_environment` | Approval-environment name; Azure template uses the same name |
| `.verification` (optional) | Selected-Service readiness/revision check: `service`, numeric `port`, `readiness_path`, `version_path`; optional `ingress` below |

Global subscription fallback is rejected. Registry and cluster targets can use different subscriptions in one tenant. No estate names are inferred. Current registry support is public Azure ACR (`<name>.azurecr.io`); other clouds/registries need a reviewed adapter.

Build receipts contain source commit, image digest, repository and full image reference. Hosted builds also record `producer.platform` and `producer.run_id`; these are checked against successful CI metadata when selecting a release. Render receipts add target/tenant and manifest SHA256. Apply requires the separate receipt checksum from the render job. Replacing both manifest and receipt fails against that independent value. A local operator controlling both bundle and checksum is the trust boundary; this is not an adversarial approval system against that operator.

Image digests identify bytes. Manual digest/source inputs do not cryptographically prove which source built those bytes. Select the pair from a successful trusted build receipt. Registry attestation/signature verification is not implemented; source ancestry and render checks protect their narrower documented boundaries.

For the demo, verification is `{"service":"platform-demo","port":80,"readiness_path":"/readyz","version_path":"/version"}`. After rollout the helper starts an authenticated loopback-only port-forward to that cluster's Service, requires readiness HTTP200 and a version JSON object whose `slot` and `revision` exactly match the receipt. It rejects redirects and ignores proxy environment variables. This proves the selected application endpoint's declared identity, not the external gateway route or every replica's behavior. The selected application authorization role must permit namespace pod port-forward operations. Applications with another response shape need a reviewed adapter or omit this optional check and run their own cluster-specific acceptance tests.

Privileged first-time settings are in the separate [bootstrap.apps.json contract](bootstrap.md); adding bootstrap fields to application overlays cannot obtain cluster-scoped permissions.

Allowed APIs: Deployment apps/v1; Service, ConfigMap, ServiceAccount v1; SecretProviderClass secrets-store.csi.x-k8s.io/v1; NetworkPolicy and Ingress networking.k8s.io/v1; HTTPRoute gateway.networking.k8s.io/v1; HPA autoscaling/v2; PDB policy/v1. Namespace, cluster administration, Gateway/EnvoyProxy and Secret objects are excluded. All Deployment/init-container images require digests. Vendor resources/components/patches/schema/generator file references into source. Local ConfigMap generation and inline patches are supported; plugins, Helm inflation and Secret generation are rejected.

For maintained ingress, add `verification.ingress` with `gateway`, `http_routes` (resource names), `hosts` (actual TLS hostnames), and optional `ca_file` (committed relative public trust-chain file). For example: `{"gateway":"platform-demo-private","http_routes":["platform-demo"],"hosts":["web.example.test","api.example.test"]}`. Private CA bytes are copied into and bound by the render receipt; only public certificates belong here, never private keys. Omit `ca_file` to use system trust. A `.pem` is ignored by the default repository policy: explicitly review/allow a public CA file before committing it.

The verifier requires Gateway `Programmed` and every declared matching HTTPRoute parent/listener's `Accepted`/`ResolvedRefs` at the current object generation. It discovers the unique same-namespace Envoy Service by owning-Gateway labels and port-forwards its HTTPS port, with the configured Host and TLS SNI, normal trust validation and exact cluster/revision checks. It rejects redirects. This exercises the selected Envoy route; it does not establish actual ILB allocation, Application Gateway configuration, upstream forwarding-header behavior or end-user network reachability. Those need the separate cutover acceptance test.

An allowed renderer kind does not grant cluster API permissions. HTTPRoute/SecretProviderClass need the native bootstrap Role or separately provisioned Azure custom-resource authorization; CSI also needs workload identity/vault permissions and the add-on. See [bootstrap boundaries](bootstrap.md), [conditional authorization examples](../examples/authorization/README.md) and the independent [platform contract](platform-services.md).
