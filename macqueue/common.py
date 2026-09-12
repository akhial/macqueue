import hashlib
import json
import os
import re
import stat
from pathlib import Path


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def keys(value, required=(), optional=()):
    require(isinstance(value, dict), "expected an object")
    require(set(required) <= value.keys(), f"missing keys: {set(required) - value.keys()}")
    require(value.keys() <= set(required) | set(optional),
            f"unknown keys: {value.keys() - set(required) - set(optional)}")


def integer(value, low, high, name):
    require(type(value) is int and low <= value <= high, f"{name} must be {low}..{high}")
    return value


def name(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value),
            "invalid name")
    return value


def relative(value):
    require(isinstance(value, str) and 0 < len(value) <= 512, "invalid relative path")
    require(not value.startswith("/") and all(p not in ("", ".", "..") for p in value.split("/")),
            "path must be relative, without dot or empty components")
    require(not any(c in value for c in ("\\", "\x00", "\n", "\r", "${")), "invalid path characters")
    return value


def safe_path(root, value, *, exists=False):
    """Reject symlinks at every component, including dangling links."""
    relative(value)
    root = Path(root).resolve()
    path = root
    for component in value.split("/"):
        path = path / component
        require(not path.is_symlink(), f"symlink is not allowed: {value}")
    require(path.resolve().is_relative_to(root), "path escapes job")
    if exists:
        require(path.exists(), f"missing path: {value}")
    return path


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_bytes(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def read_secret(path):
    path = Path(path)
    require(not path.is_symlink(), "token file must not be a symlink")
    require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "token file must have mode 0600")
    value = path.read_text().strip()
    require(len(value) >= 32, "tokens must contain at least 32 characters")
    return value


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)

