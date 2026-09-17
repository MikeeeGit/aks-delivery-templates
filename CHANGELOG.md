# Changelog

## Unreleased

- September-source reconciliation: explicit one/both cluster selection, sequential default, runnable selected-build promotion and first-time namespace/RBAC bootstrap.
- Exact selected-Service revision/slot verification replaces permissive content probes and automatic rollout undo.
- Successful CI receipt selection validates trusted producer, protected source branch, run ID and commit; image promotion never rebuilds.
- Public Kustomize delivery derived from reviewed archived template behavior.
- Immutable image build, explicit environment/region/slot selection and per-subscription context.
- Private-only GitHub/Azure delivery, OIDC and external approval contracts.
- Offline rendering, independently bound receipts, server validation and rollout waiting.
- Credential-free tests and first-use/promotion documentation.
