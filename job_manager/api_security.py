"""Local API credentials and endpoint discovery; no request routing."""

from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
import time
from typing import Any, Dict, Optional

from .api_types import API_PREFIX, API_VERSION, BIND_HOST, ENDPOINT_FILENAME, TOKEN_FILENAME


def token_path(directory: str) -> str:
    return os.path.join(directory, TOKEN_FILENAME)


def endpoint_path(directory: str) -> str:
    return os.path.join(directory, ENDPOINT_FILENAME)


def write_private_file(path: str, text: str) -> None:
    """Write a file only this user can read.

    The mode is applied when the temp file is *created*, before any content is
    written, so the secret is never on disk world-readable even for an instant.
    It is a no-op on Windows, where the file inherits the directory's ACL --
    said plainly in docs/API.md rather than pretended otherwise.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handle, temp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".tmp-", dir=os.path.dirname(path) or "."
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def read_token(directory: str) -> str:
    try:
        with open(token_path(directory), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def new_token(length: int = 32) -> str:
    """A secret that is safe to hand to a command line.

    ``token_urlsafe`` draws from the base64url alphabet, so about one token in
    sixty-four begins with "-". Every one of those breaks
    ``--token <value>``: argparse reads the leading hyphen as an option name
    and refuses with "expected one argument", which says nothing about the
    real problem and cannot be worked around without knowing to write
    ``--token=<value>`` instead. Rerolling costs nothing and the entropy is
    unchanged -- the first character is simply drawn from a smaller set.
    """
    while True:
        token = secrets.token_urlsafe(length)
        if not token.startswith("-"):
            return token


def ensure_token(directory: str, renew: bool = False) -> str:
    """The shared secret, generating and storing one on first use."""
    existing = "" if renew else read_token(directory)
    if existing:
        return existing
    token = new_token(32)
    write_private_file(token_path(directory), token + "\n")
    return token


def write_endpoint_file(directory: str, port: int, token: str) -> str:
    """Publish where the server is listening, for a client to discover."""
    path = endpoint_path(directory)
    write_private_file(
        path,
        json.dumps(
            {
                "url": f"http://{BIND_HOST}:{int(port)}{API_PREFIX}",
                "host": BIND_HOST,
                "port": int(port),
                "token": token,
                "api_version": API_VERSION,
                "pid": os.getpid(),
                "started_at": time.time(),
            },
            indent=2,
        )
        + "\n",
    )
    return path


def live_endpoint(directory: str, timeout: float = 0.5) -> Optional[Dict[str, Any]]:
    """The endpoint another running instance published, or None.

    Instances share one state directory, so a second MoleditPy finds the first
    one's ``api.json``. It counts only if it names another process *and* that
    port still accepts a connection: a crash leaves the file behind, and a
    stale one must not stop this instance from starting its own server.
    """
    try:
        with open(endpoint_path(directory), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        port = int(data["port"])
        pid = int(data.get("pid", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid == os.getpid() or not 0 < port < 65536:
        return None
    try:
        with socket.create_connection((BIND_HOST, port), timeout=timeout):
            return data
    except OSError:
        return None


def remove_endpoint_file(directory: str) -> None:
    try:
        os.unlink(endpoint_path(directory))
    except OSError:
        pass


def tokens_match(presented: str, expected: str) -> bool:
    """Constant-time comparison; a token is a secret like any other."""
    if not presented or not expected:
        return False
    # As bytes: on str, compare_digest raises for any non-ASCII character.
    return secrets.compare_digest(str(presented).encode("utf-8"), str(expected).encode("utf-8"))
