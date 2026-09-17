# Argo CD operational manual

This manual assumes [deployment](argocd-deployment.md) is complete for each selected cluster. Use [troubleshooting](argocd-troubleshooting.md) for a failed operation. The reference workflow keeps manual application sync and separate traffic approval.

## Keep the release record unambiguous

For every slot retain the template/bootstrap versions, kubecontext/API endpoint, Application name, GitOps commit, application source commit, image digest, release-receipt checksum, approved PR/build run and verification result. Record which slot currently receives live traffic separately.

**GitOps revision and application source revision are different.** The former identifies desired-state Git; the latter is baked into the image and returned by `/version`. An aks02-only PR advances the common protected branch while aks01's release files and image remain unchanged. Once Argo refreshes that branch, an aks01 verification uses the new GitOps head and the original aks01 source/digest receipt. That is not a rebuild or an app update.

Keep the branch stable while explicitly syncing/verifying an exact SHA. If it moves, review the new head and select it again. Do not replace the equality check with “whatever is latest.” Serialise proposal merges and releases according to the team's change window; one-slot proposal jobs alone do not prevent unrelated repository merges.

## Routine health and deployment

An operator should be able to answer: can both Argo instances reach Git and the API; is the selected Application Synced/Healthy at the reviewed revision; is the actual app digest correct; do private Envoy HTTPS and the external preview/live path return the expected slot/source revision?

Use the deployment guide's `gitops.py verify` command for full application verification, including `--gitops-config` and `--gitops-source`. The reviewed clone must contain the selected full GitOps commit and its exact slot files; a local directory with matching-looking YAML is insufficient. To inspect status without printing configuration or Secrets:

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  get applications -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision'
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n argocd \
  get deployments,statefulsets,pods
kubectl --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" -n platform-demo \
  get deployments,pods,services,hpa,pdb,httproutes
```

The normal sequence is select a successful scanned build → propose inactive slot → review/merge → sync exact GitOps SHA → verify app/Envoy → verify Azure preview → approve separate traffic cutover. Promote the other slot only after the first passes. No automatic cross-cluster transaction or fallback is implied.

The CI proposal identity can open branches/PRs but cannot merge them or reach Kubernetes. Argo has read-only Git access. The release operator controls manual sync. Keep the build identity, proposal identity, platform administrator and traffic approver distinct where the team's governance requires it.

## Drift and HPA ownership

With manual sync, Argo reports meaningful divergence as OutOfSync. Determine whether it is an incident fix, an unexpected writer, a changed admission default or a new reviewed Git revision before acting. Preserve evidence and reconcile the intended state through a reviewed change or a deliberate sync.

Do not use live `kubectl set image` or direct pipeline deploy as the normal update mechanism for an Argo-owned slot. It bypasses the desired-state review and leaves two competing records.

The generated Application excludes only the selected Deployment's `/spec/replicas` and enables `RespectIgnoreDifferences=true`; the HPA remains authoritative for replicas. This does not hide image, environment, resources, route or NetworkPolicy drift. Healthy HPA behavior still depends on the cluster metrics API and realistic requests/limits. The [Argo diff guide](https://argo-cd.readthedocs.io/en/stable/user-guide/diffing/) explains field exclusions.

If the team later enables automatic self-heal, treat it as a design change: evaluate emergency edits, sync windows, monitoring and rollback semantics first. The supplied operator path requires manual sync. Argo's imperative history rollback is unavailable while automated sync is enabled; a Git revert remains the authoritative rollback. [Automated-sync behavior](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).

## Application rollback and traffic rollback

Restore a previously approved slot directory in a new review branch, including its manifest, optional public CA and both receipts. Restoring the directory is safer than indiscriminately reverting a commit that also changed unrelated source or another slot:

```bash
git -C "$APP_DIR" switch -c rollback/aks02-approved-release
git -C "$APP_DIR" restore --source="$PREVIOUS_GOOD_GITOPS_COMMIT" \
  -- gitops/releases/pprd/uks/aks02
git -C "$APP_DIR" diff -- gitops/releases/pprd/uks/aks02
```

Validate the restored bundle with the shared helper/tests, confirm the old immutable image still exists and is permitted by current security policy, then commit/open the normal PR. Review and merge it; sync the **new rollback merge SHA**. The restored receipt's source revision and digest remain the original approved pair. Repeat Service/HTTPS and Azure preview checks.

If production traffic already points to the unhealthy slot, apply the separately reviewed traffic rollback promptly according to the service's incident procedure. App rollback and DNS/Application Gateway rollback are separate actions. Neither automatically reverses database changes or restores data. Do not remove the other healthy slot during investigation.

## Switching between direct and Argo delivery

For direct → Argo adoption:

1. Freeze direct application deployments to that slot and ensure no operation is running. Keep platform and traffic pipelines available.
2. Propose the exact currently approved source/digest through GitOps. Compare the generated objects with the live app, including slot labels and Service type.
3. Install Argo/bootstrap permissions, create the restricted Project/Application and inspect its diff. Resolve unexpected ownership changes before sync.
4. Perform one manual sync and verify the same Service/HTTPS path. Record Argo as the application owner and keep direct apply disabled for that slot.

`FailOnSharedResource=true` detects another Argo Application's tracking; it does not detect or prevent an unrelated direct pipeline from applying the same objects. The operational freeze is therefore essential.

For Argo → direct handoff, disable any automation and stop an outstanding Argo operation through the approved Argo UI/CLI first. Record the current release and verify the Application has no deletion finalizer. The provided Application has none; preserve that behavior. After reviewing ownership, remove only the Application without cascading application-resource deletion, then confirm Deployments/Services/routes remain. Re-enable direct deployment with the same approved receipt and verify. Do not delete the Argo namespace or CRDs to perform an app handoff.

A profile change can leave old resources because neither default path silently prunes. Explicitly review old Ingress/Service/route removal only after traffic no longer depends on it.

## Access, Git credentials and SSO

Maintain the committed `cluster_api_servers` mapping from applied infrastructure for both slots. The operator helper checks the actual selected kubeconfig API endpoint and TLS policy before use. Review endpoint changes explicitly during cluster replacement; never change the expected URL merely to make an accidental context pass.

Treat `argocd` namespace write access as platform administration. The shared sync helper patches an Application through Kubernetes, so it uses Kubernetes authorization; it does not inherit the Argo web UI's SSO/RBAC policy. Give that capability only to trusted release operators and scope resource access to the intended Application where practical. Read-only verification additionally needs app status/read/watch and the port-forward permissions used by the existing verifier.

For shared human access, configure a private Argo hostname/TLS and Entra OIDC using the organization's reviewed registration/federation method. Direct OIDC does not require enabling Dex; Dex is a separate integration choice. Map actual group IDs to explicit viewer, release-operator and platform-admin roles. A release operator normally needs application get/sync for its project, without project/repository/cluster administration, exec, arbitrary overrides or deletion. Validate both positive and denied cases using real group members. The initial local admin should be rotated and retained only under the team's break-glass policy, then disabled when the chosen recovery model permits it. SSO is an integration requirement, not preconfigured by this repository. [Entra setup](https://argo-cd.readthedocs.io/en/stable/operator-manual/user-management/microsoft/) and [Argo RBAC](https://argo-cd.readthedocs.io/en/stable/operator-manual/rbac/).

Rotate Git credentials before expiry using the approved secret store. Update one Argo instance, confirm a fresh fetch/refresh, then update the other. Revoke the old credential only after both prove access. Repository connection failure does not instantly stop running workloads but prevents safe deployment/recovery. Git write credentials belong to the proposal job, never the repo-server.

## Backup and cluster recovery

Retain protected Git history, immutable image digests, scan/build receipts for the rollback period, bootstrap bundles/checksums, Terraform state/recovery arrangements and the environment configuration. Keep Argo repository/SSO credentials and TLS material in the organization's secret backup system.

Argo configuration exports can contain Secrets. Install the compatible Argo CLI from the version-specific release asset and verify its SHA256 against [argocd-lock.json](../argocd-lock.json) before execution; the ordinary shared kubectl/Helm installer does not install this optional CLI. Use an explicit context/namespace and a protected path outside source/CI artifacts:

```bash
umask 077
argocd admin export --kubeconfig "$KUBECONFIG" --context "$EXPECTED_CONTEXT" \
  -n argocd --out "$SECURE_BACKUP_FILE"
```

Encrypt/restrict the output under the organization's backup procedure and verify restore in an isolated environment. An export from the wrong namespace may not fail, so confirm its context/namespace and expected object inventory without publishing contents. [Upstream recovery guidance](https://argo-cd.readthedocs.io/en/stable/operator-manual/disaster_recovery/).

To replace a failed cluster, first restore/recreate the Azure infrastructure and tier2 platform, including that new cluster's workload identity issuer federation. Reapply the reviewed Argo bootstrap, restore credentials securely, recreate the correctly scoped Project/Application and explicitly sync the approved slot Git state. Verify app, TLS, Azure private frontend and traffic readiness before routing users to it. Reusing an old cluster's OIDC assumption is not sufficient.

Use `argocd admin import` only for a reviewed backup with the correct version/target. Dry-run and inspect the intended objects; do not default to pruning or conflict override. A config restore is not a backup of persistent application data.

## Upgrades, capacity and observability

Update the Argo lock only after reviewing release/upgrade notes, manifest hashes, runtime image digests, Kubernetes compatibility and RBAC changes. Prepare a new bundle, run unit/schema/hosted acceptance, then install on the inactive cluster under the platform approval process. Verify Argo, Git fetch, app reconciliation, rollback and negative permissions before proceeding to the other cluster. CRD downgrade/removal requires a separate recovery decision.

The evaluation profile has a single replica of each required component. Its loss can pause reconciliation or UI access. The HA profile uses upstream HA resources; test node loss, Redis quorum, disruption/upgrade behavior and resource pressure with the real node topology. A rendered HA manifest is not proof of failure recovery.

Add monitoring for persistent OutOfSync/Degraded/Unknown state, failed/stalled operations, repo-server fetch/render errors, controller queue/reconcile duration, pod restarts/OOM, Redis availability, certificate expiry, Git-credential expiry and application SLOs. Keep WAF/backend health and externally observed requests alongside Argo state. Notifications, dashboards and paging integrations are environment-specific and not activated by the example.
