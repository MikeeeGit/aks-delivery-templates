# Image security gate and promotion

The protected build pipeline builds the selected source, pushes one unique image, obtains its immutable registry digest, and scans that exact remote image before writing a release receipt. The reference app runs its Node tests inside the Docker build stage; other consumers must retain their own application test gate. The shared build uses the digest-pinned Trivy image in build-tools.json and fails on HIGH or CRITICAL vulnerability findings.

The scanner uses a temporary registry-login directory mounted read-only. It does not receive the Docker socket. Its container has a read-only root filesystem, dropped capabilities and an isolated, mode-0700 temporary directory on the Linux runner disk. It runs as the runner UID/GID, with read-only registry credentials, and removes the cache after scanning. Provision sufficient free runner disk for the expanded database and image; a fixed 1 GiB memory filesystem is too small for the current database. The runner needs outbound access to the scanner image, vulnerability databases and the selected registry. Scanner download/authentication/time-out errors fail the build; they are not treated as a clean scan.

## Build evidence

A successful build produces:

- release.json: source commit, image repository/digest, CI producer/run and a passed security_scan record with scanner digest and report SHA256.
- image-scan.json: the separate private vulnerability report.

The release artifact contains only release.json so promotion has a small, unambiguous input. The report is a separate build artifact, including when a scanner returns a failure report. Keep artifact access and retention appropriate to the private application. The shared templates do not commit reports or registry credentials to source.

A failed scan never produces a promotable receipt. Because scanning follows an immutable push, a unique image tag may remain in ACR after failure. Apply the registry's reviewed retention/quarantine policy; do not promote that tag manually as if it passed.

## Selecting a previous build

Selected-build promotion checks the successful trusted CI producer, repository, protected branch, source commit, run ID and immutable image binding. It also requires a passed HIGH/CRITICAL scan record with an immutable scanner reference and report hash. Receipts from an older producer that did not record the gate are rejected.

A passed historical build means the image passed against the database used at build time. It is not a claim that the image remains free of newly disclosed vulnerabilities. Rebuild and scan maintained base images regularly, and define a release-age or rescan policy for sensitive environments.

A manually approved digest deployment is a separate operator path; use the selected-build caller when successful-build provenance and this receipt gate are required. This reference does not implement signed supply-chain attestations or registry-wide admission enforcement.

## Failure handling and qualification

Review the private report, remediate the dependency/base image, run app tests and create a new image/receipt. A new image has a new digest. Scanner exceptions or an alternative severity policy require an explicit reviewed extension; changing a report's result is not remediation.

Unit tests exercise failure propagation, image/report binding, temporary credential arguments and refusal to produce a receipt after failure. They do not execute Docker or qualify ACR access. Record an actual private build run before claiming that registry authentication, remote scanning and report publication have been tested together.

An actual Azure build exposed database extraction failing with `no space left on device` in the former 1 GiB cache. The build correctly withheld its release receipt. The disk-backed cache addresses that capacity limit; storage exhaustion still fails the scan and must never be bypassed.
