# AKS Delivery Templates

Reusable Azure DevOps and GitHub delivery for a three-tier AKS platform: Terraform infrastructure, independently versioned cluster/platform services, and application releases. Shared scripts preserve the archived environment/region/cluster-slot and Kustomize application pattern. Build an image once, scan its immutable digest, then promote the same successful build to `aks01`, `aks02` or both.

The maintained platform profile installs pinned **Envoy Gateway / Gateway API**, private per-slot frontends, workload ServiceAccounts and reviewed common manifests. A separate retired NGINX compatibility profile records the original controller settings for migration. Application configuration remains shared Kustomize: configuration/CSI and immutable image rendering feed either approved direct deployment or Argo CD reconciliation of reviewed YAML in Git. Both methods verify rollout and selected-slot Service plus HTTPS Gateway behavior. Application deployment and traffic cutover have separate approvals.

Choose the [application delivery method](docs/delivery-methods.md): the original direct pipelines or the additive Argo CD option. Argo guides cover [design](docs/argocd-design.md), [deployment](docs/argocd-deployment.md), [operations](docs/argocd-operations.md) and [troubleshooting](docs/argocd-troubleshooting.md).

Start with the [three-tier guide](docs/three-tier-deployment-system.md), [operator walkthrough](docs/operators-walkthrough.md), [first deployment](docs/getting-started.md) and [platform lifecycle](docs/platform-services.md). [Migration](docs/ingress-migration.md) explains the maintained-controller decision and compatibility limits. [Source provenance](docs/source-provenance.md) accounts for all seven archived template families.

| Entry point | Purpose |
|---|---|
| [GitHub build](.github/workflows/build.yml) / [Azure build](azure-pipelines/stages/build.yml) | Build/push once, HIGH/CRITICAL scan, retained release receipt |
| [GitHub bootstrap](.github/workflows/bootstrap.yml) / [Azure bootstrap](azure-pipelines/stages/bootstrap.yml) | Separately approved namespace security and scoped deployment permissions |
| [GitHub platform](.github/workflows/platform-services.yml) / [Azure platform](azure-pipelines/stages/platform-services.yml) | Independently upgrade pinned charts/CRDs/common resources on selected slots |
| [GitHub deployment](.github/workflows/deploy-selected.yml) / [Azure build/deploy](azure-pipelines/stages/build-deploy.yml) | One/both slots, sequential by default; explicit parallel option |
| [GitHub promotion](.github/workflows/promote.yml) / [Azure promotion](azure-pipelines/stages/promote.yml) | Select a successful build receipt and deploy without rebuilding |
| [GitHub GitOps proposal](.github/workflows/gitops-propose.yml) / [Azure proposal](azure-pipelines/stages/gitops-propose.yml) | Review-only one-slot PR from a successful build; Argo owns application reconciliation |
| [delivery.py](scripts/delivery.py) / [platform_services.py](scripts/platform_services.py) | Shared local and CI command contracts |

Authenticated workflows require a trusted **private consumer repository**, protected source and external approval settings. Hosted guards run before private workers or cloud authentication. Public PR validation stays credential-free on isolated hosted workers. Exact source snapshots, local manifest inputs, separate artifact checksums, explicit tenant/subscription selection and temporary credentials prevent accidental context drift; they do not replace operator authorization.

[AKS Platform Demo](https://github.com/MikeeeGit/aks-platform-demo) includes maintained Gateway API/CSI TLS, retired Ingress compatibility and direct-Service examples. The maintained profile uses separate `.21` candidate frontend addresses while old `.20` frontends can remain during migration. It demonstrates application/controller integration, not a production monitoring stack.

See [GitHub](docs/github.md), [Azure DevOps](docs/azure-devops.md), [configuration](docs/configuration.md), [image security](docs/image-security.md), [promotion](docs/promotion.md) and [testing](docs/testing.md). Python 3.10+, hashed PyYAML, checksum-pinned kubectl/kubelogin/Helm and digest-pinned BuildKit/Trivy are used. Local tests exercise real Helm/Kustomize rendering and real loopback TLS; Azure OIDC, private network, RBAC/CSI and live cloud deployment remain separate qualification gates. [Apache-2.0](LICENSE).
