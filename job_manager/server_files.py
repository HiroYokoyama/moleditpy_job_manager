"""Job Server's virtual folders adapted to the host file browser contract."""

import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .job_server_client.files import contained, validate_path


def virtual_path(path):
    if path in ("", "/"):
        return ""
    value = path[1:] if path.startswith("/") else path
    head, _, relative = value.partition("/")
    try:
        if str(uuid.UUID(head)) != head:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Choose a job UUID in the server's file browser") from None
    if "/" in value and not relative:
        raise ValueError("Job paths cannot end with a slash")
    if relative:
        validate_path(relative)
    return value


def entries(client, path):
    path = virtual_path(path)
    offset, seen = 0, set()
    while True:
        page = client.request("GET", "/browse?" + urlencode({"path": path, "offset": offset}))
        for entry in page["entries"]:
            child = virtual_path(entry["path"])
            name = child.rsplit("/", 1)[-1]
            if child != (path + "/" if path else "") + name or name != entry["name"]:
                raise ValueError("Server returned a path outside the requested folder")
            if child in seen or len(seen) >= 50000:
                raise ValueError("Server folder listing is duplicated or too large")
            seen.add(child)
            if entry["type"] not in {"file", "directory"}:
                raise ValueError("Unsupported server file type")
            yield entry
        following = page.get("next_offset")
        if following is None:
            return
        if (
            type(following) is not int
            or following <= offset
            or following > 50000
            or not page["entries"]
        ):
            raise ValueError("Server returned invalid folder pagination")
        offset = following


def list_path(client, path, depth=1):
    path = virtual_path(path)
    result, pending, visited = [], [(path, "", 1)], 0
    depth = max(1, min(int(depth or 1), 20))
    while pending:
        folder, prefix, level = pending.pop()
        for entry in entries(client, folder):
            visited += 1
            if visited > 50000:
                raise ValueError("Server folder tree is too large")
            name = prefix + entry["name"]
            if depth == 1:
                result.append(name + ("/" if entry["type"] == "directory" else ""))
            elif entry["type"] == "file":
                result.append(name)
            elif level < depth:
                pending.append((entry["path"], name + "/", level + 1))
            if len(result) + len(pending) > 50000:
                raise ValueError("Server folder tree is too large")
    return result


def stat_path(client, path, digest=True):
    path = virtual_path(path)
    return client.request(
        "GET", "/browse/stat?" + urlencode({"path": path, "digest": str(digest).lower()})
    )


def download_paths(client, paths, local_dir, overwrite=False):
    root = Path(local_dir)
    root.mkdir(parents=True, exist_ok=True)
    downloaded, skipped, taken = [], [], set()
    count = 0
    for path in paths:
        path = virtual_path(path)
        if not path:
            raise ValueError("Select a job or file, not the entire server")
        info = stat_path(client, path, False)
        pending = [(path, path.rsplit("/", 1)[-1], info["type"])]
        while pending:
            remote, relative, kind = pending.pop()
            count += 1
            if count > 50000:
                raise ValueError("Server download contains too many entries")
            target = contained(root, relative, create=True)
            if kind == "directory":
                if target.exists() and not target.is_dir():
                    skipped.append((remote, f"{target} is not a directory"))
                    continue
                target.mkdir(exist_ok=True, mode=0o700)
                pending.extend(
                    (entry["path"], relative + "/" + entry["name"], entry["type"])
                    for entry in entries(client, remote)
                )
                continue
            identity = os.path.normcase(str(target.resolve()))
            if identity in taken or (target.exists() and not overwrite):
                skipped.append((remote, f"{target} already exists or is selected twice"))
                continue
            taken.add(identity)
            job_id, _, filename = remote.partition("/")
            if not filename or kind != "file":
                raise ValueError("Server returned an invalid file path")
            # Fetch privately, then install without replacing an existing file unless asked.
            with tempfile.TemporaryDirectory(prefix=".job-server-", dir=target.parent) as staging:
                verified = client.download(job_id, filename, Path(staging) / "download")
                target = contained(root, relative)
                try:
                    if overwrite:
                        os.replace(verified, target)
                    else:
                        os.link(verified, target)
                except FileExistsError:
                    skipped.append((remote, f"{target} already exists"))
                    continue
            downloaded.append(str(target))
    return downloaded, skipped
