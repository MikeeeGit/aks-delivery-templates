# ADR 0001: Gateway API with Envoy Gateway

Date: 17 September 2026. Status: implementation selected for the public reference profile; runtime acceptance is recorded separately.

## Context

The source platform uses community ingress-nginx behind Azure Application Gateway WAF, with private endpoints in two independently deployable AKS clusters. The new public system must preserve that separation and support independently versioned platform services. Future AWS examples should reuse application routing concepts without inheriting Azure resource assumptions.

## Decision

Use the Gateway API Standard channel and Envoy Gateway for the maintained reference profile. Pin and review the controller chart, Gateway API/Envoy CRDs and generated platform manifests. Keep controller-specific configuration in the platform profile; application host/path routing uses HTTPRoute.

The currently selected Envoy 1.9 release family supports Kubernetes 1.33–1.36 and Gateway API 1.6.1 according to the [upstream compatibility matrix](https://gateway.envoyproxy.io/news/releases/matrix/). The exact package and checksums belong in the committed platform configuration. Review support dates when upgrading; a version pin is not a permanent maintenance policy.

## Alternatives considered

| Option | Assessment for this reference system |
|---|---|
| Envoy Gateway | Selected: portable controller, separate Helm lifecycle, standard application routes, explicit advanced traffic policies. We own maintenance and runtime qualification. |
| AKS application routing Gateway API | Valid Azure-managed alternative. Its Istio-based implementation supports private ingress and TLS. The managed CRDs/controller lifecycle and customization limits differ from the independently managed public profile. Current Microsoft Terraform guidance uses an AzAPI update in addition to AzureRM. |
| Application Gateway for Containers | Supports Gateway API and WAF, but its documented frontend currently lacks private IP support, which does not fit the required private ingress tier behind the retained gateway. Reassess if that capability changes. |
| Full Istio service mesh | Useful when service-mesh capabilities are required; this system does not currently need a mesh to replace ingress. |
| Another maintained Ingress controller | Can reduce initial manifest changes but retains the older annotation-oriented application contract. |
| Retired community ingress-nginx | Migration reference only. Do not make it the default of a new public platform. |

Microsoft identifies Gateway API as the AKS ingress direction and documents the managed add-on's functionality and limits in [AKS Gateway API guidance](https://learn.microsoft.com/en-us/azure/aks/app-routing-gateway-api). Its [DNS/TLS integration](https://learn.microsoft.com/en-us/azure/aks/app-routing-gateway-api-dns-tls) provides another managed approach. The private-IP limitation above is from [Application Gateway for Containers components](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/application-gateway-for-containers-components), checked on the decision date.

## Consequences

The original three tiers remain. Infrastructure owns Azure resources and identities. Platform delivery owns CRDs, the controller and listeners. Application delivery owns routes, application manifests and release receipts.

Gateway API standardizes the resource contract; it does not make every implementation feature interchangeable. Azure load-balancer annotations, Envoy traffic policies and certificate integration remain explicit profile details. AWS will require its own network, identity and load-balancer profile and its own acceptance run.

A rollback must account for CRDs and controller compatibility as well as application releases. Ingress migration has a behaviour inventory, a distinct candidate endpoint and an explicit traffic switch. See [migration and acceptance](../ingress-migration.md).
