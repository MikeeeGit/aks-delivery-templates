# Security

Do not post credentials, Terraform state, plan files, private configuration or vulnerability exploit details in a public issue.

Report a vulnerability using GitHub private vulnerability reporting if the repository offers it. Otherwise open an issue containing only a request for a private contact channel; do not include sensitive details.

Public CI validates synthetic examples and offline renders without deployment credentials. Real builds/deployments must run in a private trusted repository/project with scoped workload identities, protected branches/environments, restricted worker pools and private artifacts. Tests do not prove cloud IAM, private networking or application health.

Namespace Writer permissions remain powerful. Review application code, Dockerfiles and manifests as trusted executable inputs. Render checks restrict target scope and resource types; they are not a sandbox for hostile code in an authenticated private consumer. Keep registry credentials, kubeconfigs and Azure CLI profiles isolated and prefer disposable workers.

Never commit state, saved plans, backend credentials, account identifiers copied from another environment, service-principal secrets, PATs or private keys. Generated plan/state output can disclose secrets even when an input is marked sensitive.
