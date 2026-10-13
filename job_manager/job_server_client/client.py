import base64
import json
import ssl
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .auth import signing_message
from .credentials import load_key, save_key


class ClientError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(body.get("message", "Server request failed"))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError(code, {"message": "Server redirects are forbidden"})


class JobServerClient:
    def __init__(self, url, key_path=None, ca_file=None, server_id=None, timeout=30):
        parts = urlsplit(url)
        if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
            raise ValueError("Invalid server URL")
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("HTTPS is required except for same-machine loopback")
        self.url = url.rstrip("/")
        self.key_path = Path(key_path) if key_path else None
        self.server_id, self.timeout = server_id, timeout
        handlers = [NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=ca_file))]
        if parts.hostname in {"127.0.0.1", "localhost", "::1"}:
            handlers.append(ProxyHandler({}))
        self.opener = build_opener(*handlers)
        self.token, self.expires = None, 0
        self.authenticated_at = 0

    def request(self, method, path, body=None, headers=None, authenticated=True, binary=False):
        headers = dict(headers or {})
        if authenticated:
            if (
                not self.token
                or self.expires <= time.time() + 10
                or (path.startswith("/admin/") and time.time() - self.authenticated_at > 240)
            ):
                self.authenticate()
            headers["Authorization"] = "Bearer " + self.token
        data = (
            body
            if isinstance(body, bytes)
            else json.dumps(body or {}).encode()
            if method in {"POST", "PUT"}
            else None
        )
        if not isinstance(body, bytes):
            headers["Content-Type"] = "application/json"
        request = Request(self.url + "/api/v1" + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.headers.get("X-API-Version", "1") != "1":
                    raise ValueError("Incompatible server API version")
                data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("Use streamed downloads for large files")
                return data if binary else json.loads(data)
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(65536))
            except (ValueError, UnicodeDecodeError):
                error = {"message": "Server returned an invalid error response"}
            raise ClientError(exc.code, error) from None

    def enroll(self, username, password):
        if not self.key_path:
            raise ValueError("A credential file path is required")
        if self.key_path.exists():
            raise ValueError("Credential file already exists")
        session = self.request(
            "POST", "/auth/login", {"username": username, "password": password}, authenticated=False
        )
        key = Ed25519PrivateKey.generate()
        device_id = str(uuid.uuid4())
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.request(
            "POST",
            "/auth/devices",
            {"device_id": device_id, "public_key": base64.b64encode(public).decode()},
            headers={"Authorization": "Bearer " + session["token"]},
            authenticated=False,
        )
        challenge = self.request(
            "POST", "/auth/challenge", {"device_id": device_id}, authenticated=False
        )
        if self.server_id and self.server_id != challenge["server_id"]:
            raise ValueError("Server identity differs from configured identity")
        save_key(self.key_path, device_id, challenge["server_id"], key)
        self.authenticate(challenge)

    def authenticate(self, challenge=None):
        if not self.key_path:
            raise ValueError("Sign in and register this device first")
        credential = load_key(self.key_path)
        device = credential["device_id"]
        challenge = challenge or self.request(
            "POST", "/auth/challenge", {"device_id": device}, authenticated=False
        )
        if (
            challenge["server_id"] != credential["server_id"]
            or (self.server_id and self.server_id != challenge["server_id"])
            or challenge["protocol_version"] != "1"
        ):
            raise ValueError("Server identity or protocol changed")
        signature = credential["private_key"].sign(
            signing_message(challenge["server_id"], device, challenge["nonce"])
        )
        token = self.request(
            "POST",
            "/auth/token",
            {
                "device_id": device,
                "nonce": challenge["nonce"],
                "signature": base64.b64encode(signature).decode(),
            },
            authenticated=False,
        )
        self.token, self.expires = token["token"], token["expires_at"]
        self.authenticated_at = time.time()
        self.server_id = challenge["server_id"]

    def submit(self, spec, files=None, idempotency_key=None):
        files = {name: Path(path) for name, path in (files or {}).items()}
        manifest = []
        for name, path in files.items():
            from .files import sha256

            manifest.append({"path": name, "size": path.stat().st_size, "sha256": sha256(path)})
        spec = {**spec, "files": manifest}
        reservation = self.request(
            "POST", "/jobs", spec, headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        )
        job_id = reservation["id"]
        status = self.request("GET", f"/jobs/{job_id}/uploads")
        capabilities = self.request("GET", "/capabilities")
        chunk_size = min(capabilities["max_chunk_bytes"], 8 * 1024 * 1024)
        for entry in status:
            if entry["complete"]:
                continue
            path = files[entry["path"]]
            offset, total = entry["offset"], entry["size"]
            with path.open("rb") as stream:
                stream.seek(offset)
                while data := stream.read(chunk_size):
                    result = self.request(
                        "PUT",
                        f"/jobs/{job_id}/files/" + quote(entry["path"], safe="/"),
                        data,
                        headers={
                            "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{total}"
                        },
                    )
                    offset = result["offset"]
                if total == 0:
                    self.request(
                        "PUT", f"/jobs/{job_id}/files/" + quote(entry["path"], safe="/"), b""
                    )
        return self.request("POST", f"/jobs/{job_id}/commit")

    def download(self, job_id, relative, destination):
        from .files import open_regular, validate_path

        validate_path(relative)
        entry = next(
            (
                item
                for item in self.request("GET", f"/jobs/{job_id}/files")
                if item["path"] == relative
            ),
            None,
        )
        if not entry:
            raise ValueError("File is not in the server manifest")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > entry["size"]:
            partial.unlink()
            offset = 0
        if not self.token or self.expires <= time.time() + 10:
            self.authenticate()
        if offset < entry["size"] or not partial.exists():
            headers = {"Authorization": "Bearer " + self.token}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(
                self.url + f"/api/v1/jobs/{job_id}/files/" + quote(relative, safe="/"),
                headers=headers,
            )
            with self.opener.open(request, timeout=self.timeout) as response:
                if offset and (
                    response.status != 206
                    or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")
                ):
                    raise ValueError("Server did not honor resume range")
                with open_regular(partial, "r+b") as stream:
                    if not offset:
                        stream.truncate(0)
                    stream.seek(offset)
                    while data := response.read(1024 * 1024):
                        stream.write(data)
                        if stream.tell() > entry["size"]:
                            raise ValueError("Download exceeded expected file size")
        from .files import sha256

        if partial.stat().st_size != entry["size"] or sha256(partial) != entry["sha256"]:
            partial.unlink()
            raise ValueError("Downloaded file checksum differs; retry from zero")
        partial.replace(destination)
        return destination
