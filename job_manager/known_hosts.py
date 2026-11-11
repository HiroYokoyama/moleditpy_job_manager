"""Reading ``~/.ssh/known_hosts`` line by line, and adding one line to it.

paramiko's ``HostKeys`` is not used for either. Its ``load`` raises on a
``@cert-authority`` or ``@revoked`` line and stops there, and its ``save``
writes back only what it parsed: comments, FIDO keys and every key type it does
not know were removed from the user's file the first time a key was accepted
here. A file the user and ``ssh`` both depend on is appended to, never rewritten.

Pure stdlib, so the pytest-only CI job covers it.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import List, Set, Tuple

MARKER_REVOKED = "@revoked"
MARKER_CERT_AUTHORITY = "@cert-authority"


class KnownHostConflict(ValueError):
    """The file already holds a different key for this host, or revokes this one."""


@dataclass(frozen=True)
class Entry:
    marker: str
    hosts: str
    keytype: str
    blob: str
    #: The line without its marker, as ``ssh`` wrote it.
    line: str

    def matches(self, name: str) -> bool:
        return _hosts_match(self.hosts, name)


def host_name(hostname: str, port: int = 22) -> str:
    """How ``ssh`` spells a host in this file: bracketed off the default port."""
    return hostname if int(port or 22) == 22 else f"[{hostname}]:{int(port)}"


def _hashed_match(pattern: str, name: str) -> bool:
    try:
        _, _, salt, digest = pattern.split("|", 3)
        expected = base64.b64decode(digest)
        actual = hmac.new(base64.b64decode(salt), name.encode("utf-8"), hashlib.sha1).digest()
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


def _hosts_match(hosts: str, name: str) -> bool:
    if hosts.startswith("|1|"):
        return _hashed_match(hosts, name)
    matched = False
    lowered = name.lower()
    for pattern in hosts.split(","):
        negated = pattern.startswith("!")
        if fnmatch.fnmatchcase(lowered, pattern.lstrip("!").lower()):
            if negated:
                return False
            matched = True
    return matched


def parse(path: str) -> List[Entry]:
    """Every key line that can be read; one that cannot is skipped, not fatal."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    entries = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        marker = ""
        if line.startswith("@"):
            marker, _, line = line.partition(" ")
            line = line.strip()
        fields = line.split()
        if len(fields) < 3:
            continue
        entries.append(Entry(marker.lower(), fields[0], fields[1], fields[2], line))
    return entries


def revoked_blobs(entries: List[Entry]) -> Set[str]:
    """Keys the user has revoked. Honoured for every host, whatever the pattern:
    refusing a revoked key somewhere it was not listed for costs a prompt."""
    return {entry.blob for entry in entries if entry.marker == MARKER_REVOKED}


def usable(entries: List[Entry]) -> List[Entry]:
    """Plain host keys that are not revoked."""
    revoked = revoked_blobs(entries)
    return [entry for entry in entries if not entry.marker and entry.blob not in revoked]


def keys_for(path: str, hostname: str, port: int = 22) -> List[Tuple[str, str]]:
    """``(keytype, blob)`` of every plain key on file for this host."""
    name = host_name(hostname, port)
    return [
        (entry.keytype, entry.blob)
        for entry in parse(path)
        if not entry.marker and entry.matches(name)
    ]


def append(path: str, hostname: str, port: int, keytype: str, blob: str) -> bool:
    """Add one line for this host. False when that exact key is already there.

    Refuses rather than replaces: a host that already has a different key of
    this type is a host whose key *changed*, and filing the new one over it is
    how an intercepted connection becomes a trusted one.
    """
    entries = parse(path)
    if blob in revoked_blobs(entries):
        raise KnownHostConflict(f"This key is marked @revoked in {path}. Nothing was added.")
    name = host_name(hostname, port)
    for entry in entries:
        if entry.marker or entry.keytype != keytype or not entry.matches(name):
            continue
        if entry.blob == blob:
            return False
        raise KnownHostConflict(
            f"{path} already holds a different {keytype} key for {name}. Nothing was changed."
        )
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, mode=0o700, exist_ok=True)
    needs_newline = False
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                needs_newline = handle.read(1) != b"\n"
    except OSError:
        pass
    # The mode applies only when this call creates the file.
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(("\n" if needs_newline else "") + f"{name} {keytype} {blob}\n")
    return True


__all__ = [
    "Entry",
    "KnownHostConflict",
    "MARKER_CERT_AUTHORITY",
    "MARKER_REVOKED",
    "append",
    "host_name",
    "keys_for",
    "parse",
    "revoked_blobs",
    "usable",
]
