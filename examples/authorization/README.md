# Conditional AKS application custom-resource authorization (opt-in preview)

A separate [native Kubernetes authorization profile](../../docs/native-azure-authorization.md) supports managed-Entra AKS without this Azure ABAC preview recipe. Select the cluster authorization model explicitly.

This is a separately reviewed operator provisioning example for the Gateway API/CSI profile. It is not run by the application pipeline or namespace bootstrap. As checked on 2026-09-17, Microsoft documents AKS custom-resource ABAC as **preview**. Confirm current feature availability and support for the intended cluster/region before opting in; these files and local tests do not prove Azure authorization works. If preview use is unacceptable, choose and validate a different authorization model before deploying this profile. [Microsoft authorization documentation](https://learn.microsoft.com/en-us/azure/aks/entra-id-authorization#restrict-custom-resource-access-using-abac-conditions-preview).

The application identity already needs the bootstrap's Cluster User role and namespace-scoped RBAC Writer. This adds two conditional assignments at the **same namespace**:

| Role | Data actions | Allowed group/resource |
|---|---|---|
| Application CRD editor | read, write | `gateway.networking.k8s.io/httproutes`; `secrets-store.csi.x-k8s.io/secretproviderclasses` |
| Gateway status reader | read | `gateway.networking.k8s.io/gateways` |

No custom-resource delete action is needed by the current apply flow. Gateway, GatewayClass, EnvoyProxy and ClientTrafficPolicy writes are not granted. Conditions use plural API resource names; each API group is paired with its own resource name. Both roles contain only custom-resource actions, so the conditions apply to every action they grant. Microsoft's [Container Service operation list](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/containers#microsoftcontainerservice) defines the read/write/delete actions. The group/kind condition model is documented by Microsoft; write-path enforcement still requires the live gates below before treating this configuration as qualified.

## Provision the two assignments

Run from the repository root as a separate authorized identity with role-definition and assignment administration rights. Review the files and replace all synthetic inputs. Do not use the workload managed identity's client ID as the deployer's object ID. For different deployment identities per cluster, provision each intended identity/cluster pair separately.

```bash
set -euo pipefail
: "${AKS_CRD_ABAC_PREVIEW_ACK:?Set to reviewed only after accepting preview support}"
[ "$AKS_CRD_ABAC_PREVIEW_ACK" = reviewed ]
: "${SUBSCRIPTION_ID:?Set the actual AKS subscription UUID}"
: "${DEPLOY_PRINCIPAL_OBJECT_ID:?Set the application deploy service-principal object UUID}"
: "${AKS01_ID:?Set the applied aks01 Azure resource ID}"
: "${AKS02_ID:?Set the applied aks02 Azure resource ID}"
export SUBSCRIPTION_ID DEPLOY_PRINCIPAL_OBJECT_ID AKS01_ID AKS02_ID
AUTH_DIR=$(mktemp -d)
export AUTH_DIR
trap 'rm -rf -- "$AUTH_DIR"' EXIT
python3 - <<'PY'
import json, os, re, uuid
from pathlib import Path
subscription = str(uuid.UUID(os.environ['SUBSCRIPTION_ID']))
uuid.UUID(os.environ['DEPLOY_PRINCIPAL_OBJECT_ID'])
pattern = re.compile(r'/subscriptions/' + re.escape(subscription) + r'/resourceGroups/[^/]+/providers/Microsoft.ContainerService/managedClusters/[^/]+', re.I)
clusters = [os.environ['AKS01_ID'], os.environ['AKS02_ID']]
if clusters[0].lower() == clusters[1].lower() or not all(pattern.fullmatch(value) for value in clusters):
    raise SystemExit('Expected two distinct applied AKS IDs in the selected subscription')
for filename in ['app-crd-editor.role.json', 'gateway-reader.role.json']:
    role = json.loads((Path('examples/authorization') / filename).read_text())
    role['AssignableScopes'] = ['/subscriptions/' + subscription]
    (Path(os.environ['AUTH_DIR']) / filename).write_text(json.dumps(role))
PY
az account set --subscription "$SUBSCRIPTION_ID"
# Initial provisioning; existing custom roles require a separately reviewed update.
az role definition create --role-definition "@$AUTH_DIR/app-crd-editor.role.json" --output none
az role definition create --role-definition "@$AUTH_DIR/gateway-reader.role.json" --output none
for cluster in "$AKS01_ID" "$AKS02_ID"; do
  az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL_OBJECT_ID" \
    --assignee-principal-type ServicePrincipal \
    --role 'AKS Application Route and CSI Editor' \
    --scope "$cluster/namespaces/platform-demo" \
    --condition "$(cat examples/authorization/app-crd-editor.condition.txt)" \
    --condition-version 2.0 --output none
  az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL_OBJECT_ID" \
    --assignee-principal-type ServicePrincipal \
    --role 'AKS Application Gateway Status Reader' \
    --scope "$cluster/namespaces/platform-demo" \
    --condition "$(cat examples/authorization/gateway-reader.condition.txt)" \
    --condition-version 2.0 --output none
done
```

For a different namespace, change both scopes and the application/verification configuration together. The condition is mandatory: assigning either role without it grants its actions across all custom-resource types within the assignment scope. Existing grants are additive; a broader inherited or direct role is not restricted by adding these conditional assignments. Audit and remove unintended broad grants separately. Removing example files does not revoke assignments.

## Qualify using the real application deployment identity

After propagation, use the normal isolated Entra kubeconfig from the application worker for **each cluster**, not the provisioning identity and not `--admin`. The platform must have installed CRDs and the named Gateway/EnvoyProxy before the checks. Keep the resulting evidence with the selected cluster/source record.

```bash
set -euo pipefail
: "${KUBECONFIG:?Use the selected slot isolated application-identity kubeconfig}"
for resource in httproutes.gateway.networking.k8s.io secretproviderclasses.secrets-store.csi.x-k8s.io; do
  for verb in get create patch; do
    kubectl --kubeconfig "$KUBECONFIG" auth can-i "$verb" "$resource" --namespace platform-demo
  done
done
kubectl --kubeconfig "$KUBECONFIG" get gateway platform-demo-private --namespace platform-demo --output=name
for resource in gateways.gateway.networking.k8s.io envoyproxies.gateway.envoyproxy.io clienttrafficpolicies.gateway.envoyproxy.io; do
  if kubectl --kubeconfig "$KUBECONFIG" auth can-i patch "$resource" --namespace platform-demo; then
    echo 'Unexpected platform custom-resource write access; stop' >&2
    exit 1
  fi
done
# This must be rejected as Forbidden, not accepted by the API server.
# --dry-run=server prevents persistence even if an unwanted grant exists.
if denial=$(kubectl --kubeconfig "$KUBECONFIG" patch gateway platform-demo-private \
  --namespace platform-demo --type=merge \
  --patch '{"metadata":{"annotations":{"authorization-check":"must-be-denied"}}}' \
  --dry-run=server 2>&1); then
  echo 'Unexpected Gateway write permission; stop' >&2
  exit 1
fi
case "$denial" in
  *"(Forbidden)"*) echo 'Expected Gateway mutation denial confirmed' ;;
  *) printf '%s\n' "$denial" >&2; echo 'Denial test did not return Forbidden; stop' >&2; exit 1 ;;
esac
```

A nonzero result caused by an unreachable API or absent CRD is not a successful denial test; inspect the response and require `Forbidden`. Also server-dry-run the actual rendered application bundle and confirm both HTTPRoute and SecretProviderClass are admitted with the deployment identity. `can-i` alone does not prove a request carrying group/kind attributes will be authorized. Proceed with application delivery only after positive writes and negative platform-write checks behave as intended on both clusters. The normal delivery path repeats a server dry-run before apply.

## Ownership and alternatives

This setup separates custom-resource permissions. It does **not** make namespace Writer a resource-specific role. In the supplied Envoy `GatewayNamespace` mode, proxy Deployments, Services and Pods live in `platform-demo`; Writer can change those built-in resources and read namespace Secrets. It can also run a Pod using the application ServiceAccount. The application's deployer is therefore a trusted operator of this namespace, including its proxy data plane and certificate material. Logical platform ownership of the Gateway/controller does not prevent all data-plane changes. Never place the Envoy controller, its privileged ServiceAccount or controller credentials in this application namespace. [Built-in Writer permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers#azure-kubernetes-service-rbac-writer).

A stable namespace-wide custom-resource role is a broader-trust alternative: it would also permit writes to Gateway/EnvoyProxy/policy objects in that namespace and does not meet the restricted platform-object boundary above. Do not silently substitute it. Native Kubernetes Role/RoleBinding authorization is another design option only after deliberately selecting and validating that authentication/authorization model; this example does not assume an Entra binding bypasses the Azure authorizer.

For enforceable isolation from a less-trusted application deployer, redesign the proxy placement and authorization together: put generated proxy resources in a platform-only namespace, retain same-namespace certificate attachment or review explicit cross-namespace references, and verify the private HTTPS endpoint from a suitably routed trusted worker instead of granting application access to that namespace. That is a separate deployment profile, not something these conditional roles accomplish.
