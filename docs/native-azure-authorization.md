# Native Kubernetes permissions on managed-Entra AKS

This deployment profile keeps Azure access and Kubernetes permissions in separate, explicit layers. Terraform owns the managed identities, OIDC federation, Key Vault and ACR grants, and AKS Cluster User role assignments. Kubernetes Role and RoleBinding objects own application API access. The same direct and Argo delivery methods remain available.

Select `kubernetes_authorization_mode = "kubernetes_rbac"` in Azure AKS Foundation for a new deployment. The existing `azure_rbac` mode remains supported and unchanged. Native permissions require managed-Entra authentication with Azure RBAC for Kubernetes disabled; enabling both does not make native bindings an alternative authorization route for an Entra principal. Do not change a live cluster's authorization mode as an unreviewed migration shortcut. See [Microsoft Entra with Kubernetes RBAC](https://learn.microsoft.com/en-us/azure/aks/azure-ad-rbac).

## Identity and permission flow

| Identity | Azure rights managed by Terraform | Kubernetes access |
| --- | --- | --- |
| Build CI identity | Scoped registry publishing rights | None required |
| Platform CI identity | Cluster User on its declared clusters | Optional explicitly reviewed cluster-admin binding from an existing Entra operator; see initial platform access |
| Application CI identity | Cluster User on its declared clusters | Application namespace Role and RoleBinding from bootstrap |
| Application workload identity | Federated ServiceAccount subjects on both cluster issuers; scoped vault/resource roles | Its ServiceAccount is used by Pods, with no automatic API token mount |

A CI identity authenticates a pipeline to Azure and AKS. A workload identity authenticates a running Pod to Key Vault or another Azure service. Keep their client IDs and principal object IDs distinct. A ServiceAccount annotation is only one end of the workload trust: Terraform must also create federation for that exact namespace/name on each cluster's actual issuer and grant the required Azure resource access.

## Observe the actual CI username

Run identity discovery once for every intended CI identity and cluster pair after Terraform grants Cluster User and the worker has private API connectivity. Authenticate as that CI identity through its actual federated service connection or GitHub environment. Do not run discovery as the operator and copy the operator's username.

The shared [GitHub discovery workflow](../.github/workflows/discover-identity.yml) accepts a reviewed full `template-ref`, delivery config, environment, region, cluster, protected approval environment, client ID, tenant and subscription. The [Azure discovery stage](../azure-pipelines/stages/discover-identity.yml) accepts the equivalent selector plus a service connection and expected client ID. Both guard private, protected manual callers before scheduling private workers. Use the same approval environment and OIDC identity that the eventual operation uses; discovery does not require a new federation subject. The private artifact contains `identity.json`, with no access token or kubeconfig. Set private artifact retention according to the organization's policy.

For a local run after authenticating as the expected CI service principal:

```bash
set -euo pipefail
: "${CI_CLIENT_ID:?Use the applied CI identity client ID}"
mkdir -p .aks-delivery/identities
.venv/bin/python ../aks-delivery-templates/scripts/bootstrap.py identity \
  --source . --config delivery.gateway.apps.json \
  --environment pprd --region uks --slot aks01 \
  --client-id "$CI_CLIENT_ID" \
  --identity-output .aks-delivery/identities/app-aks01.json
```

The helper verifies the signed-in Azure account's service-principal client ID, obtains user credentials, converts the kubeconfig through `kubelogin --login azurecli`, and reads `kubectl auth whoami`. It never requests admin kubeconfig, impersonates a principal, or creates a grant. The API's SelfSubjectReview is available to authenticated users through Kubernetes' default [basic-user permissions](https://kubernetes.io/docs/reference/access-authn-authz/authentication/#self-subject-review); if that permission has been restricted, an administrator must resolve that prerequisite. Authentication, private DNS, or API failures are errors, not a usable discovery result.

Match the recorded tenant, cluster and client ID to the applied Terraform `delivery_authorization` output. Record the returned username against that principal's object ID. Do not infer the Kubernetes username from an object ID or client ID. The JSON is an operational observation from the authenticated run, not a signed authorization certificate; review its run/source provenance with the configuration change.

## Apply application permissions

Start from [bootstrap.native.apps.json](../examples/bootstrap.native.apps.json). Each target uses:

```json
{
  "authorization_mode": "kubernetes_rbac",
  "deploy_principal_object_ids": ["00000000-0000-0000-0000-000000000101"],
  "deploy_kubernetes_usernames": {
    "00000000-0000-0000-0000-000000000101": "observed-application-username"
  }
}
```

The example username is a placeholder. Replace it with the corresponding discovery result on each cluster. Keep namespace, tenant, target cluster and Pod Security version aligned with the applied infrastructure and normal delivery config. Commit the reviewed configuration.

Run the existing [bootstrap workflow or stage](bootstrap.md) with this config and the separately authorized bootstrap identity. The local equivalent, authenticated as an authorized operator, is:

```bash
set -euo pipefail
SOURCE_COMMIT=$(git rev-parse HEAD)
.venv/bin/python ../aks-delivery-templates/scripts/bootstrap.py apply \
  --source . --config delivery.gateway.apps.json \
  --bootstrap-config bootstrap.native.apps.json --commit "$SOURCE_COMMIT" \
  --environment pprd --region uks --slot aks01
```

Repeat for aks02. Native mode validates the actual cluster's authorization mode, creates the restricted namespace, and applies `Role/aks-delivery-application` plus one consolidated `RoleBinding/aks-delivery-application` in that namespace. It creates no Azure role assignments. The role supports the supplied deployment, service, config, ServiceAccount, HPA/PDB, network policy, HTTPRoute and CSI resources, read-only CSI pod status, logs and Service port-forward verification. Gateway access is read-only. It does not grant Role/RoleBinding writes, cluster resources, Secret reads, Pod exec or platform custom-resource writes.

The application's deployer remains a trusted namespace operator. It can deploy Pods using the workload ServiceAccount and change ordinary proxy resources placed in that namespace. This profile is not hostile tenant isolation. Argo's controller uses its separately scoped ServiceAccount authorization; the direct CI user's RoleBinding does not replace Argo's controller permissions.

## Initial platform access

The first namespace/platform operation requires an already authorized operator. The native AKS Terraform profile declares the Entra administrator group; group membership and the operator's initial Azure permissions must already be administered. The operator uses user credentials to run bootstrap and the existing platform service lifecycle.

The optional [platform CI bootstrap](platform-ci-bootstrap.md) now closes the coded handoff for a dedicated platform identity: an existing Entra operator validates applied Terraform outputs and observed CI usernames, then explicitly opts in to a consolidated cluster-admin binding on each selected cluster. Its authority is broad because platform installation owns controllers, CRDs and authorization resources. Build/application identities cannot be selected. An explicit empty selection revokes this managed binding's subjects. The command never creates Azure grants or uses admin kubeconfig. If this authority is unsuitable, retain an operator-owned platform lifecycle and separately review narrower permissions. Cluster User alone still permits only user-credential retrieval.

## Reconciliation and migration

The native Role and RoleBinding use server-side apply with field manager `aks-delivery-authorization`. Conflicting ownership fails; the helper does not force ownership. One reviewed config owns the binding for each application namespace. Multiple applications sharing that namespace must consolidate their authorized deployers into that config.

Removing a subject from the map and principal list removes it from this managed binding on the next successful apply. Explicit empty values (`deploy_principal_object_ids: []` and `deploy_kubernetes_usernames: {}`) clear all its subjects. Other RoleBindings, ClusterRoleBindings, Entra group membership and Azure assignments are independent and remain additive. Removing an entire target from config does not delete that target's binding.

The legacy Azure mode still creates its original deterministic Cluster User and namespace Writer assignments. Switching a config to native mode does not revoke those assignments or change the cluster. Inventory and deliberately migrate old grants/state before an authorization-mode transition. Do not let two tools manage the same grant.

## Qualification

Offline tests check intended application operations, denied platform/cluster operations, identity mismatch failures, exact observed subjects, empty/removed subject reconciliation inputs, mode mismatches and zero Azure grant mutations in native mode. They do not establish Entra federation or Azure authorization in a deployed tenant.

On each real cluster, run discovery, bootstrap and application delivery through the actual identities. Verify HTTPRoute and SecretProviderClass server dry-run succeeds; Gateway mutation and RoleBinding creation must return Forbidden. Confirm CSI status shows the expected mounting Pods, the app is ready, and the private HTTPS route serves the selected revision. Keep the actual source/run/cluster evidence. The existing kind suites remain useful application/controller tests and do not replace these Azure checks.
