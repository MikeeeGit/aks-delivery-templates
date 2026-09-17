#!/usr/bin/env python3
"""Reproduce the committed, repository-owned demonstration chart package."""
import gzip
import hashlib
import io
from pathlib import Path
import tarfile


def main():
    root = Path(__file__).resolve().parents[1] / "examples/platform/charts"
    chart = root / "platform-demo"
    destination = root / "packages/platform-demo-0.1.0.tgz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(chart.rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            entry = tarfile.TarInfo(
                "platform-demo/" + path.relative_to(chart).as_posix()
            )
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    print(hashlib.sha256(destination.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
