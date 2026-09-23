# Offline platform-engine demonstration

This tiny repository-owned Helm chart creates a ConfigMap describing the selected cluster. It exercises exact package/version/SHA binding, local values, reviewed common resources and repeatable Helm upgrade semantics without a vendor account. It is **not** an ingress controller or production monitoring installation.

Copy these contents into a private platform consumer, replace synthetic targets, commit and follow [platform services](../../docs/platform-services.md). Rebuild the deterministic package from the shared repository with `python3 scripts/package_demo_chart.py`; update its reviewed config SHA only when chart content changes. The maintained real controller example is [Envoy Gateway](../platform-envoy/README.md).
