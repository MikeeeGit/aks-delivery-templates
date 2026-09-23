# First-time platform CI access on native AKS

This optional step connects the Terraform identities to the platform pipeline. Terraform creates the dedicated platform managed identity, its CI federation and its **Cluster User** Azure assignment on selected AKS clusters. An existing Entra administrator then applies one reviewed Kubernetes binding. The platform pipeline can subsequently recreate its pinned controllers, CRDs, policies and shared resources with user credentials.

The binding grants **cluster-admin on the selected cluster**. It includes access to every namespace and Secret, controller installation and Kubernetes authorization changes. It is appropriate only for a dedicated, protected platform identity. Build and application identities do not receive it. If this authority does not meet your requirements, keep platform installation with an authorized operator and maintain a separately reviewed narrower lifecycle.

## Prerequisites

- Applied Azure AKS Foundation with `kubernetes_authorization_mode = "kubernetes_rbac"`, managed Entra authentication, private API access and local accounts disabled.
- At least one existing Entra administrator group declared by Terraform. An operator must already belong to it and be able to obtain AKS user credentials. This command does not create the group, change its membership or grant the operator Azure permissions.
- The platform identity declared with `purpose = "platform"`, no application namespaces and only the intended `clusters`. Its exact Cluster User assignments must appear in the applied Terraform output.
- A worker or operator workstation with private cluster connectivity, Azure CLI, pinned kubectl/kubelogin and the shared Python dependencies. Configure network access before identity discovery.
- A private consumer repository containing the generated delivery config and reviewed platform access configuration. Do not store these tenant-specific outputs or discovery artifacts in this public repository.

The live command requires an Azure **user** login. It checks the API server's observed operator group membership; a service-principal login cannot use this command to grant itself initial access.

## 1. Export the applied target contract

Use a protected copy of the real applied output, not a Terraform plan or a hand-authored output fixture. From the private consumer workspace:

```bash
set -euo pipefail
umask 077
mkdir -p .aks-delivery/identities
terraform -chdir=../platform-infrastructure/aks output -json \
  > .aks-delivery/applied-aks.json
```

Adjust the state directory to the AKS root used by your infrastructure pipeline. The command requires the `deployment_context` and `delivery_authorization` output wrappers with `sensitive: false`. It checks tenant, subscription, environment, region, cluster and cluster resource ID.

<a id="2-observe-the-platform-ci-identity-on-each-slot"></a>
## 2. Observe the platform CI identity on each cluster

Run the existing [identity discovery workflow or stage](native-azure-authorization.md#observe-the-actual-ci-username) using the **actual platform identity** and its ordinary protected platform environment/service connection. Terraform's Cluster User assignment allows it to obtain user credentials. The discovery operation only asks the API server for its own username; it does not need this new platform binding.

Download each private `identity.json` artifact to a distinct file:

```text
.aks-delivery/identities/platform-aks01.json
.aks-delivery/identities/platform-aks02.json
```

Review the source commit, CI run, selected environment and authenticated client ID. These JSON observations are not signed certificates: the helper validates their fields against applied Terraform, while the operator must establish the provenance of the files. Never guess a Kubernetes username from a client ID or principal ID.

Combine the records without changing their contents:

```bash
python3 - <<'PY'
import json
from pathlib import Path
paths = [
    Path(".aks-delivery/identities/platform-aks01.json"),
    Path(".aks-delivery/identities/platform-aks02.json"),
]
output = Path(".aks-delivery/platform-identities.json")
with output.open("x") as stream:
    json.dump([json.loads(path.read_text()) for path in paths], stream, indent=2)
    stream.write("\n")
output.chmod(0o600)
PY
```

More than one record for the same selected client/cluster is rejected. An identity observed on aks01 cannot supply the subject for aks02.

## 3. Review the selected principals

Copy [platform.access.json](../examples/platform.access.json) into the private consumer. Each target contains a list of **keys from the applied `delivery_authorization.principals` map**:

```json
{
  "schema_version": 1,
  "targets": [
    {
      "environment": "pprd",
      "region": "uks",
      "slot": "aks01",
      "platform_principal_keys": ["platform"]
    },
    {
      "environment": "pprd",
      "region": "uks",
      "slot": "aks02",
      "platform_principal_keys": ["platform"]
    }
  ]
}
```

The key `platform` is an example; use the actual Terraform key. No build/application principal, unknown key, shared client/object ID, undeclared cluster or missing applied Cluster User assignment is accepted.

## 4. Apply as the existing operator

Use the existing approved operator login in the correct tenant, or authenticate interactively if no valid user session exists. From the private consumer:

```bash
set -euo pipefail
# az login --tenant <your-tenant-UUID>  # use the existing approved operator account
for SLOT in aks01 aks02; do
  .venv/bin/python ../aks-delivery-templates/scripts/bootstrap.py platform-access \
    --source . --config delivery.azure-workload.apps.json \
    --platform-access-config platform.access.json \
    --aks-outputs .aks-delivery/applied-aks.json \
    --identity-records .aks-delivery/platform-identities.json \
    --environment pprd --region uks --slot "$SLOT" \
    --allow-platform-admin
done
```

Each cluster prompts for confirmation. `--yes` suppresses that prompt for a previously reviewed operator-run script; it does not bypass the user-login, group-membership, target or opt-in checks. Run from the appropriate private network. No local/admin kubeconfig, impersonation, Azure role mutation or forced field ownership is used.

The command rechecks the live AKS ID, provisioning, authorization mode, tenant, exact administrator group set, private API and disabled local accounts. It obtains temporary user credentials, verifies the operator's observed Entra group membership and permission to bind cluster-admin, performs a server dry-run, then reconciles `ClusterRoleBinding/aks-delivery-platform` using server-side apply.

## Optional: keep the operator login local through an SSH tunnel

A workstation without direct private API connectivity can use a reviewed SSH worker as a SOCKS5 transport. The worker must resolve and reach the private AKS API. Verify its SSH host key through an authenticated inventory or Azure VM control-plane read before connecting; store that verified key in the selected known-hosts file.

Start a loopback-only forwarding session in a separate terminal, substituting your own approved worker and dedicated SSH key:

```bash
ssh -N -D 127.0.0.1:1080 \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=yes \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile="$VERIFIED_KNOWN_HOSTS" \
  -i "$WORKER_SSH_KEY" "$WORKER_USER@$WORKER_HOST"
```

Run the same operator bootstrap from the local private consumer, adding the explicit proxy option:

```bash
.venv/bin/python ../aks-delivery-templates/scripts/bootstrap.py platform-access \
  --source . --config delivery.azure-workload.apps.json \
  --platform-access-config platform.access.json \
  --aks-outputs .aks-delivery/applied-aks.json \
  --identity-records .aks-delivery/platform-identities.json \
  --environment pprd --region uks --slot aks01 \
  --allow-platform-admin \
  --kubernetes-proxy-url socks5://127.0.0.1:1080
```

Repeat for the other selected cluster only after reviewing its contract. The optional URL accepts only `socks5://127.0.0.1:PORT` or `socks5://[::1]:PORT`, with ports 1–65535. It adds `proxy-url` to the requested cluster in the fresh temporary kubeconfig, after confirming the current context and HTTPS server match the selected private AKS endpoint. It preserves the API hostname, certificate authority, TLS verification and user authentication. Other kubeconfig entries and the workstation's ordinary kubeconfig remain unchanged.

Azure CLI and kubelogin run locally with the operator's existing authorized session. The command does not copy an Azure token cache, private key or kubeconfig to the worker, and does not set a global `HTTPS_PROXY`. All user-login, observed administrator-group, explicit privilege selection and server dry-run checks still apply. Without the option, the existing direct-connect behavior is unchanged.

Kubernetes documents [per-cluster SOCKS proxy configuration](https://kubernetes.io/docs/tasks/extend-kubernetes/socks5-proxy-access-api/). This bootstrap uses REST operations for identity checks and binding reconciliation. The [kubeconfig reference](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/#cluster) notes that SOCKS5 does not support SPDY streaming endpoints; do not treat this bootstrap transport as qualification for exec, attach or port-forward. Close the forwarding session with Ctrl+C after bootstrap. An unavailable tunnel causes API access to fail; it does not enable another authentication or TLS mode.

## 5. Run platform and application pipelines

Run the [platform services lifecycle](platform-services.md) through the dedicated platform identity on aks01 and aks02, installing the pinned Gateway API/Envoy platform profile. Run [native application bootstrap](native-azure-authorization.md#apply-application-permissions) to create the application namespace and its narrower application Role/RoleBinding. That operation can now run through the authorized platform identity.

Run the application's Azure build/deploy and promotion callers with the application identity. Its workload ServiceAccount uses the separate Terraform-managed workload identity to reach Key Vault. Platform cluster-admin does not supply a Pod's Azure access and does not replace workload federation.

Record both real cluster runs, private HTTPS checks, image revisions and the Azure workload qualifier output. The disposable kind tests prove Kubernetes binding and reconciliation behavior; they cannot prove Entra login, private Azure networking, Key Vault CSI or real AKS deployment.

## Removal and reconciliation

One reviewed configuration owns this binding per cluster, using field manager `aks-delivery-platform-access`. Reapply the same configuration for idempotent reconciliation. Ownership conflicts fail; investigate them instead of adding `--force-conflicts`.

Remove a key to remove its subject from this binding. Use an explicit `"platform_principal_keys": []` and run the same operator command to clear every subject. Discovery records can be an empty list for this revocation. Keep the target in the configuration until revocation has succeeded; removing the target/file alone does not change the cluster.

Revocation affects this binding only. Other ClusterRoleBindings, RoleBindings, Entra groups and Azure grants remain additive. A cluster-admin identity can create other authorization objects while it is authorized. After an incident or deliberate privilege reduction, inventory those objects, revoke CI federation and Azure rights as appropriate, and rotate affected credentials. Clearing one binding is not proof that all access has been removed.
