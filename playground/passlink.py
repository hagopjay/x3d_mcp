"""Print the sign-in path for a pass id, signed with the session key.

The key is read from PLAYGROUND_SESSION_KEY or, better, from a file, so it
never appears in a shell history or a process list:

    python -m playground.passlink <pass-id> --key-file key.txt
    -> /p/<pass-id>.<signature>      (append it to the service's address)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from playground.auth import Signer


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pass_id")
    ap.add_argument("--key-file", type=Path)
    a = ap.parse_args()
    key = a.key_file.read_text().strip() if a.key_file else os.environ.get("PLAYGROUND_SESSION_KEY", "")
    if not key:
        raise SystemExit("no session key: pass --key-file or set PLAYGROUND_SESSION_KEY")
    print("/p/" + Signer(key).pass_token(a.pass_id))


if __name__ == "__main__":
    main()
