#!/usr/bin/env python3
"""Install checksum-pinned Linux amd64 clients into an explicit directory."""
import argparse, hashlib, io, json, os, platform, tarfile, urllib.request, zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ["x86_64", "AMD64"]:
        raise SystemExit(
            "Pinned automatic installation supports Linux amd64 only; provision reviewed clients on other platforms."
        )
    args.directory.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(
        (Path(__file__).resolve().parents[1] / "tools.json").read_text()
    )
    for name, tool in metadata.items():
        with urllib.request.urlopen(tool["url"], timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != tool["sha256"]:
            raise SystemExit(f"{name} checksum mismatch")
        if name == "kubelogin":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = [n for n in archive.namelist() if n.endswith("/kubelogin")]
                if len(names) != 1:
                    raise SystemExit("Unexpected kubelogin archive layout")
                data = archive.read(names[0])
        elif name == "helm":
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                member = archive.getmember("linux-amd64/helm")
                if not member.isfile():
                    raise SystemExit("Unexpected Helm archive layout")
                data = archive.extractfile(member).read()
        binary = args.directory / name
        binary.write_bytes(data)
        binary.chmod(0o755)
    if os.environ.get("GITHUB_PATH"):
        with open(os.environ["GITHUB_PATH"], "a") as handle:
            handle.write(str(args.directory.resolve()) + "\n")
    print(f"Installed verified clients to {args.directory}")


if __name__ == "__main__":
    main()
