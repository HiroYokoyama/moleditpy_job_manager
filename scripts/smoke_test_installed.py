#!/usr/bin/env python3
"""Smoke-test the *installed* ``moleditpy-job-manager`` package (e.g. from PyPI).

Run this from a directory that does NOT contain the repository's
``job_manager/`` folder, so ``import job_manager`` resolves to the installed
distribution rather than the source tree. The workflows copy this file into a
temporary directory before running it for exactly that reason.

    python smoke_test_installed.py                    # the default install
    python smoke_test_installed.py --extras all       # also paramiko, cryptography
    python smoke_test_installed.py --version 3.3.4    # and insist on a version

Nothing here touches the user's job list: the window runs against a temporary
data directory, and the client talks to a stand-in API on a free port.

Set ``JOB_MANAGER_SMOKE_ALLOW_LOCAL=1`` to skip the "must be an installed copy"
guard when trying the script out from the source tree.
"""

from __future__ import annotations

import argparse
import http.server
import importlib
import importlib.metadata
import importlib.resources
import json
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import job_manager

DISTRIBUTION = "moleditpy-job-manager"
COMMANDS = ("moleditpy-job-manager", "moleditpy-job")
OPTIONAL_MODULES = ("paramiko", "cryptography")


def _check_installed_origin() -> None:
    path = os.path.abspath(job_manager.__file__)
    if os.environ.get("JOB_MANAGER_SMOKE_ALLOW_LOCAL") == "1":
        print(f"[info] importing job_manager from {path} (local allowed)")
        return
    if "site-packages" not in path.replace(os.sep, "/"):
        raise SystemExit(
            f"job_manager was imported from {path}, which is not an installed "
            "copy -- run this from a clean directory so the installed package "
            "is under test (or set JOB_MANAGER_SMOKE_ALLOW_LOCAL=1)."
        )
    print(f"[ok] installed job_manager at {path}")


def _check_version(expected: str) -> None:
    installed = importlib.metadata.version(DISTRIBUTION)
    assert installed == job_manager.PLUGIN_VERSION, (
        f"distribution says {installed}, PLUGIN_VERSION says {job_manager.PLUGIN_VERSION}"
    )
    if expected:
        assert installed == expected, f"installed {installed}, expected {expected}"
    print(f"[ok] version {installed}")


def _check_every_module_imports(extras: str) -> None:
    # A subpackage left out of the wheel only shows up when something imports
    # it, which for most of them is a dialog nobody opens in a smoke test.
    names = [job_manager.__name__]
    for info in pkgutil.walk_packages(job_manager.__path__, job_manager.__name__ + "."):
        names.append(info.name)
    failures = []
    for name in names:
        if name.endswith(".__main__"):
            continue
        try:
            importlib.import_module(name)
        except ImportError as exc:
            # Job Server needs cryptography, which only the extras bring.
            if extras != "all" and exc.name in OPTIONAL_MODULES:
                print(f"[info] {name} needs {exc.name}, not in the default install")
                continue
            failures.append(f"{name}: {exc}")
    assert not failures, "modules that did not import:\n  " + "\n  ".join(failures)
    print(f"[ok] {len(names)} modules import")


def _check_package_data() -> None:
    folder = importlib.resources.files("job_manager.job_server_client")
    for name in ("LICENSE", "COPYING", "LICENSING.md", "PROVENANCE.json"):
        assert folder.joinpath(name).is_file(), f"job_server_client/{name} is missing"
    json.loads(folder.joinpath("PROVENANCE.json").read_text(encoding="utf-8"))
    print("[ok] job_server_client notices and provenance are packaged")


def _check_extras(extras: str) -> None:
    import PySide6  # noqa: F401 -- the default install must bring Qt

    if extras == "all":
        import cryptography  # noqa: F401
        import paramiko  # noqa: F401
    print(f"[ok] dependencies for '{extras}' import")


def _command(name: str) -> str:
    # Beside this interpreter first: an environment that is not activated has
    # its commands there and not on PATH.
    found = shutil.which(name, path=os.path.dirname(sys.executable)) or shutil.which(name)
    assert found, f"the {name} command is not installed"
    return found


def _check_client_command() -> None:
    """``moleditpy-job ping`` against a stand-in API that checks the token."""
    token = "smoke-test-token"
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- the http.server hook name
            seen.append((self.path, self.headers.get("Authorization")))
            authorised = self.headers.get("Authorization") == f"Bearer {token}"
            body = json.dumps({"ok": True, "api_version": 1} if authorised else {"error": "token"})
            self.send_response(200 if authorised else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env["MOLEDITPY_JOB_API_URL"] = f"http://127.0.0.1:{server.server_port}/api/v1"
        env["MOLEDITPY_JOB_API_TOKEN"] = token
        result = subprocess.run(
            [_command("moleditpy-job"), "ping"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        server.shutdown()
    assert result.returncode == 0, f"ping failed: {result.stderr}"
    assert json.loads(result.stdout).get("ok") is True, result.stdout
    assert seen == [("/api/v1/ping", f"Bearer {token}")], seen
    print("[ok] moleditpy-job ping reached the API with the token")


def _check_window_command(seconds: float) -> None:
    """``moleditpy-job-manager`` starts and stays up, on a throwaway data dir."""
    data_dir = tempfile.mkdtemp(prefix="job-manager-smoke-")
    env = dict(os.environ)
    env["MOLEDITPY_JOB_MANAGER_DIR"] = data_dir
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    log_path = os.path.join(data_dir, "window.log")
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [_command("moleditpy-job-manager")], env=env, stdout=log, stderr=log
        )
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.5)
            code = process.poll()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
    with open(log_path, encoding="utf-8", errors="replace") as log:
        output = log.read()
    assert code is None, f"the window exited with {code} within {seconds:.0f}s:\n{output}"
    # The instance registry is the first thing the standalone app writes, and
    # it must have gone to the directory it was given.
    assert os.path.isdir(os.path.join(data_dir, "instances")), (
        f"no instance registry in {data_dir}:\n{output}"
    )
    shutil.rmtree(data_dir, ignore_errors=True)
    print(f"[ok] moleditpy-job-manager ran for {seconds:.0f}s on a temporary data dir")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--extras", choices=("default", "all"), default="default")
    parser.add_argument("--version", default="", help="Fail unless this version is installed")
    parser.add_argument("--window-seconds", type=float, default=10.0)
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _check_installed_origin()
    _check_version(args.version)
    _check_extras(args.extras)
    _check_package_data()
    _check_every_module_imports(args.extras)
    for name in COMMANDS:
        _command(name)
    print("[ok] both commands are installed")
    _check_client_command()
    _check_window_command(args.window_seconds)
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
