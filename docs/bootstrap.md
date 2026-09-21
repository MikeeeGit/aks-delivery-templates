# Existing cluster to first application deployment

For the additive managed-Entra profile with Terraform-owned Azure grants and namespace-scoped Kubernetes RoleBindings, see [native Azure authorization](native-azure-authorization.md). The existing Azure RBAC profile remains available.

Namespace/RBAC bootstrap is preparation for the second layer between Terraform infrastructure and application delivery. The full second layer also includes independently versioned [platform services](platform-services.md), including the maintained Envoy Gateway profile. Bootstrap itself runs against existing private AKS and does not provision networks, clusters, registries, controllers or external secrets.

The first layer must provide private API DNS/routing from the trusted worker, node egress, registry pull permissions on the kubelet identity, Entra/Azure RBAC, disabled local accounts, OIDC issuer and workload identity. Enable Key Vault CSI in infrastructure if the application uses it. The helper reads and validates those AKS properties before mutation and, when requested, verifies the installed SecretProviderClass CRD. It never uses `--admin` or AKS Run Command.

For the existing Azure RBAC profile, create a dedicated bootstrap identity through your normal identity/OIDC administration process. The operator initially grants it `Azure Kubernetes Service Cluster User Role`, `Azure Kubernetes Service RBAC Cluster Admin` on each intended cluster, and permission to create the two scoped role assignments below. Prefer an Azure RBAC Administrator assignment with conditions restricting assignable roles/principals and cluster scopes. Contributor alone cannot assign roles. This initial grant is an operator prerequisite; a pipeline cannot safely grant its own first privileges.

Use a separate bootstrap approval environment per slot, such as `bootstrap-pprd-uks-aks01`; restrict it to the protected branch, require review and use a dedicated private worker. Configure the actual caller's OIDC subject for each environment. The ordinary deploy identity must not inherit bootstrap permissions. See [GitHub trust](github.md) or [Azure DevOps](azure-devops.md).

Commit [bootstrap.apps.json](../examples/bootstrap.apps.json) beside `delivery.apps.json`. Each selector must exist in the application target map; namespace, tenant, subscription and cluster come only from that map. Replace `deploy_principal_object_ids` with service-principal **object IDs**, not application/client IDs. Set `pod_security_version` to each slot's actual Kubernetes minor (for example `v1.35` and `v1.36` for independently upgraded clusters). The generic example uses `v1.35` for both and must be adapted. `require_key_vault_csi` defaults true; set false for an application with no CSI resources.

In the existing Azure RBAC profile, the helper applies a single Namespace with `restricted` enforce/audit/warn Pod Security labels pinned to that minor, then creates deterministic role assignments for each declared deployment principal:

| Role | Scope | Public built-in ID |
|---|---|---|
| Azure Kubernetes Service Cluster User Role | Exact cluster | `4abbcc35-e782-43d8-92c5-2d3f1bd2253f` |
| Azure Kubernetes Service RBAC Writer | Exact cluster `/namespaces/<namespace>` | `a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb` |

These are Microsoft's documented [built-in container roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers) and [namespace assignment model](https://learn.microsoft.com/en-us/azure/aks/manage-azure-rbac). Writer is a trusted application-operator role and can access namespace Secrets and run Pods; do not share that identity with untrusted applications. Removing a principal from the bootstrap file does not revoke existing assignments automatically. Review revocation separately.

On GitHub call [bootstrap.yml](../.github/workflows/bootstrap.yml) with `template-ref`, bootstrap `client-id`, `environment`, `region`, `cluster-slots` JSON and private `runs-on` labels. Optional `config`/`bootstrap-config` override the two filenames. On Azure call [stages/bootstrap.yml](../azure-pipelines/stages/bootstrap.yml) with bootstrap `serviceConnection`, `environment`, `region`, `clusterSlots` object and private `pool`. Its approval names are `bootstrap-<environment>-<region>-<slot>` unless `bootstrapEnvironmentPrefix` is changed; the config must match exactly.

For an operator run, install the clients/dependency as in [getting started](getting-started.md), log in as the intended bootstrap identity, review the committed configuration and run:

```bash
.venv/bin/python ../aks-delivery-templates/scripts/bootstrap.py apply \
  --source . --commit FULL_REVIEWED_SOURCE_COMMIT \
  --environment pprd --region uks --slot aks01
```

The local command confirms interactively. CI uses `--yes` only after separate environment approval and a current-branch recheck. Namespace dry-run failure prevents apply and role creation. A later role-assignment failure can leave the namespace or earlier assignments in place; rerun the same reviewed bootstrap after correcting the issue. Deterministic assignments created by this helper are repeatable; independently created equivalent assignments may require operator reconciliation. Wait for Azure RBAC propagation and the default ServiceAccount before first application delivery.

The helper does not create vault secrets/certificates or application workload federation. Infrastructure and reviewed ServiceAccount/SecretProviderClass configuration own those choices. AKS RBAC Writer does not by itself guarantee custom-resource permissions. The maintained Gateway API/CSI demo needs the [conditional HTTPRoute/SPC editor and Gateway reader assignments](../examples/authorization/README.md), with their preview qualification and live positive/negative authorization checks. `require_key_vault_csi` verifies add-on/CRD availability, not those grants. Use true for the maintained and CSI compatibility profiles; the optional direct-Service profile without CSI can use false. Platform Helm installation has its own identity and approval gate.
