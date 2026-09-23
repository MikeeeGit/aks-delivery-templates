# Changelog

## Unreleased

- Rename the public pipeline selectors to targetClusters/target-clusters and
  targetCluster/target-cluster. Update private callers and immutable template
  pins together; callers using an older pinned revision retain its old interface.
- Describe aks01/aks02 as target AKS clusters and link the full Azure Argo
  deployment, identity, verification and retirement run list.


## Unreleased

- September-source reconciliation: explicit one/both cluster selection, sequential default, runnable selected-build promotion and first-time namespace/RBAC bootstrap.
- Exact selected-Service revision/cluster verification replaces permissive content probes and automatic rollout undo.
- Successful CI receipt selection validates trusted producer, protected source branch, run ID and commit; image promotion never rebuilds.
- Public Kustomize delivery derived from reviewed archived template behavior.
- Immutable image build, explicit environment/region/cluster selection and per-subscription context.
- Private-only GitHub/Azure delivery, OIDC and external approval contracts.
- Offline rendering, independently bound receipts, server validation and rollout waiting.
- Credential-free tests and first-use/promotion documentation.
