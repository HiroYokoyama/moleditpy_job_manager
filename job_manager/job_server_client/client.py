import base64
import hashlib
import json
import ssl
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .auth import signing_message
from .credentials import load_key, save_key


# Only immutable authentication snapshots are shared. Each client owns its opener.
_AUTH_STATES = OrderedDict()
_AUTH_LOCK = threading.RLock()
_AUTH_FLIGHTS = weakref.WeakValueDictionary()
_MAX_AUTH_STATES = 64


def _auth_flight(key):
    with _AUTH_LOCK:
        lock = _AUTH_FLIGHTS.get(key)
        if lock is None:
            lock = threading.RLock()
            _AUTH_FLIGHTS[key] = lock
        return lock


class ClientError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(body.get("message", "Server request failed"))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError(code, {"message": "Server redirects are forbidden"})


def job_path(identifier):
    """Keep imported job IDs inside one UUID path segment."""
    try:
        identifier = str(uuid.UUID(str(identifier)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Job ID must be a UUID") from None
    return "/jobs/" + identifier


class JobServerClient:
    def __init__(
        self, url, key_path=None, ca_file=None, server_id=None, timeout=30, *, reuse_auth=False
    ):
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
        self._expected_server_id = server_id
        self._reuse_auth = reuse_auth
        self._ca_file = Path(ca_file) if ca_file else None
        self._loopback = parts.hostname in {"127.0.0.1", "localhost", "::1"}
        self._configure_transport()
        self.token, self.expires = None, 0
        self.authenticated_at = 0
        self._active_auth_key = None

    def _configure_transport(self):
        tls_context = ssl.create_default_context(cafile=self._ca_file)
        self._ca_digest = self._file_digest(self._ca_file)
        self._trust_identity = (
            str(self._ca_file.resolve()) if self._ca_file else None,
            self._ca_digest,
            hashlib.sha256(b"".join(sorted(tls_context.get_ca_certs(binary_form=True)))).digest(),
            ssl.get_default_verify_paths() if not self._ca_file else None,
            tls_context.verify_mode,
            tls_context.verify_flags,
            tls_context.check_hostname,
            tls_context.minimum_version,
            tls_context.maximum_version,
            tls_context.options,
        )
        handlers = [NoRedirect(), HTTPSHandler(context=tls_context)]
        if self._loopback:
            handlers.append(ProxyHandler({}))
        self.opener = build_opener(*handlers)

    @staticmethod
    def _file_digest(path):
        return hashlib.sha256(path.read_bytes()).digest() if path else None

    def _auth_identity(self):
        if self._file_digest(self._ca_file) != self._ca_digest:
            self._configure_transport()
        if not self.key_path or not self.key_path.is_file():
            raise ValueError("Sign in and register this device first: credential file is missing")
        credential = load_key(self.key_path)
        public = (
            credential["private_key"]
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        return (
            self.url,
            self._expected_server_id,
            str(self.key_path.resolve()),
            credential["device_id"],
            credential["server_id"],
            public,
            self._trust_identity,
        ), credential

    def _remember_auth(self, key):
        with _AUTH_LOCK:
            _AUTH_STATES[key] = (self.token, self.expires, self.authenticated_at, self.server_id)
            _AUTH_STATES.move_to_end(key)
            while len(_AUTH_STATES) > _MAX_AUTH_STATES:
                _AUTH_STATES.popitem(last=False)
        self._active_auth_key = key

    def _ensure_auth(self, path):
        if not self._reuse_auth:
            if (
                not self.token
                or self.expires <= time.time() + 10
                or (path.startswith("/admin/") and time.time() - self.authenticated_at > 240)
            ):
                self.authenticate()
            return
        key, credential = self._auth_identity()
        # Serialize only this identity, never unrelated or unreachable hosts.
        with _auth_flight(key):
            if self._auth_identity()[0] != key:
                raise ValueError("Credential or trust settings changed while waiting; retry")
            with _AUTH_LOCK:
                state = _AUTH_STATES.get(key)
                if state:
                    _AUTH_STATES.move_to_end(key)
            if (
                state
                and state[1] > time.time() + 10
                and not (path.startswith("/admin/") and time.time() - state[2] > 240)
            ):
                self.token, self.expires, self.authenticated_at, self.server_id = state
                self._active_auth_key = key
                return
            self._authenticate(credential=credential)
            if self._auth_identity()[0] != key:
                self.token, self.expires = None, 0
                raise ValueError(
                    "Credential or trust settings changed during authentication; retry"
                )
            self._remember_auth(key)

    def _forget_auth(self):
        with _AUTH_LOCK:
            state = _AUTH_STATES.get(self._active_auth_key)
            if state and state[0] == self.token:
                del _AUTH_STATES[self._active_auth_key]
        self.token, self.expires = None, 0

    def _open(self, request):
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            if exc.code == 401:
                self._forget_auth()
            raise

    def request(self, method, path, body=None, headers=None, authenticated=True, binary=False):
        headers = dict(headers or {})
        if authenticated:
            self._ensure_auth(path)
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
            with self._open(request) as response:
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
        if not self._reuse_auth:
            return self._authenticate(challenge)
        key, credential = self._auth_identity()
        with _auth_flight(key):
            self._authenticate(challenge, credential)
            if self._auth_identity()[0] != key:
                self.token, self.expires = None, 0
                raise ValueError(
                    "Credential or trust settings changed during authentication; retry"
                )
            self._remember_auth(key)

    def _authenticate(self, challenge=None, credential=None):
        if not self.key_path or not self.key_path.is_file():
            raise ValueError(
                "This application's device credential is missing. Use Sign in / register device "
                "with an existing Job Server account first. Each application or browser "
                "registers its own device."
            )
        credential = credential or load_key(self.key_path)
        device = credential["device_id"]
        challenge = challenge or self.request(
            "POST", "/auth/challenge", {"device_id": device}, authenticated=False
        )
        if (
            challenge["server_id"] != credential["server_id"]
            or (self._expected_server_id and self._expected_server_id != challenge["server_id"])
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
        route = job_path(reservation["id"])
        status = self.request("GET", route + "/uploads")
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
                        route + "/files/" + quote(entry["path"], safe="/"),
                        data,
                        headers={
                            "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{total}"
                        },
                    )
                    offset = result["offset"]
                if total == 0:
                    self.request("PUT", route + "/files/" + quote(entry["path"], safe="/"), b"")
        return self.request("POST", route + "/commit")

    def download(self, job_id, relative, destination):
        from .files import open_regular, validate_path

        validate_path(relative)
        route = job_path(job_id)
        entry = next(
            (item for item in self.request("GET", route + "/files") if item["path"] == relative),
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
        self._ensure_auth(route + "/files")
        if offset < entry["size"] or not partial.exists():
            headers = {"Authorization": "Bearer " + self.token}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(
                self.url + "/api/v1" + route + "/files/" + quote(relative, safe="/"),
                headers=headers,
            )
            with self._open(request) as response:
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
