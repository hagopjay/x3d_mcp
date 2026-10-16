"""Fetch the pinned X_ITE release into playground/vendor/x_ite.

The viewer serves X_ITE from this service, never from a CDN, so every verdict
names the exact renderer that produced it. The tarball is checked against a
pinned SHA-256 before anything is unpacked.

    python -m playground.fetch_xite            # into playground/vendor/x_ite
    python -m playground.fetch_xite /xite      # into another folder
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = os.environ.get("PLAYGROUND_XITE_VERSION", "16.4.3")
SHA256 = {"16.4.3": "f3a2814098624fda1a635139d0659da409fd17502aa92d84a704b7581ccd4981"}
KEEP = ("x_ite.min.js", "x_ite.css", "LICENSE.md", "assets/")


def fetch(dest: Path, version: str = VERSION) -> Path:
    url = f"https://registry.npmjs.org/x_ite/-/x_ite-{version}.tgz"
    data = urllib.request.urlopen(url, timeout=120).read()
    digest = hashlib.sha256(data).hexdigest()
    want = SHA256.get(version)
    if want is None or digest != want:
        raise SystemExit(f"X_ITE {version}: sha256 {digest} does not match the pin {want}")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.name.startswith("package/dist/"):
                continue
            rel = m.name[len("package/dist/"):]
            if not rel or not any(rel == k or rel.startswith(k) for k in KEEP):
                continue
            target = (root / rel).resolve()
            if root not in target.parents or not (m.isfile() or m.isdir()):
                continue          # no links, nothing outside dest
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
    return dest


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "vendor" / "x_ite"
    print(f"X_ITE {VERSION} -> {fetch(out)}")
