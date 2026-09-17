# Contributing

Open an issue or pull request describing the concrete change and its effect on consumers. Keep examples synthetic and avoid identifiers/configuration from a real estate.

Run the documented unit, real offline Kustomize render and workflow syntax checks before submitting. Changed contracts need meaningful positive and rejection tests. Tests must not require Azure or Kubernetes credentials. Never run cloud operations on untrusted pull-request code.

Use explicit typed JSON inputs, validated subprocess argument lists, stable target identifiers and descriptive errors. Keep dependency updates reviewable and preserve exact client, action, package and BuildKit pins. Document changed worker prerequisites and any real-deployment qualification limits.

Releases use semantic versions. Breaking configuration/receipt/workflow changes require migration notes and a major release once stable; initial 0.x releases may evolve. A maintainer verifies CI, checks for accidental secrets/private data and reviews the diff before tagging. Pin consumers to a reviewed commit and upgrade through a pull request.

See SECURITY.md for disclosure and deployment boundaries.
