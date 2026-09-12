#!/usr/bin/env python3
"""Adapt systemd's protected credentials to the application's owner-only files."""
import os
import sys
import tempfile
from pathlib import Path


def copy_credential(source, directory, role):
    # systemd may expose credentials with group-read permission inside its
    # inaccessible mount. Keep the application's portable 0600 rule unchanged.
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".credential-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(Path(source).read_bytes())
            stream.flush()
            temporary.replace(Path(directory) / (role + ".token"))
        finally:
            temporary.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    source = Path(os.environ["CREDENTIALS_DIRECTORY"])
    for role in ("submit", "worker"):
        copy_credential(source / (role + ".token"), Path("/run/macqueue"), role)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from macqueue.cli import main as cli
    cli()


if __name__ == "__main__":
    main()
