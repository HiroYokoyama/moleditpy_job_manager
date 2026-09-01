"""A client for the local job API -- and a command line built on it.

Standard library only, and it imports nothing from the rest of this package on
purpose: the intended use is a *different* program submitting work, so this
file has to be usable by copying it next to that program. Nothing here needs
MoleditPy installed, or Qt, or the plugin itself.

    from job_manager.api_client import JobManagerClient

    client = JobManagerClient()           # finds the running MoleditPy itself
    job = client.submit(host="mycluster", files=["h2o.inp"],
                        command="/opt/orca/orca {input} > {stem}.out")
    final = client.wait(job["id"])        # blocks until the queue is done
    client.download(final["id"], wait=True)

From a shell:

    python -m job_manager.api_client submit --host mycluster \\
        --command "/opt/orca/orca {input} > {stem}.out" h2o.inp
    python -m job_manager.api_client wait <job id> --download

See docs/API.md for every field and every route.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

#: Same override the plugin honours, so a test run or a second profile is
#: found by pointing both at one directory.
DATA_DIR_ENV = "MOLEDITPY_JOB_MANAGER_DIR"
#: Set either of these and no discovery happens at all -- for a caller that
#: knows where the API is and does not want a file lookup.
URL_ENV = "MOLEDITPY_JOB_API_URL"
TOKEN_ENV = "MOLEDITPY_JOB_API_TOKEN"

ENDPOINT_FILENAME = "api.json"
TOKEN_FILENAME = "api_token"

DEFAULT_TIMEOUT = 130.0
#: Terminal states, repeated here rather than imported: this file is meant to
#: be copyable, and a client that has to import the plugin to know a job has
#: finished is not.
TERMINAL_STATES = frozenset({"DONE", "FAILED", "CANCELLED", "LOST"})


class JobApiError(RuntimeError):
    """The API refused the request, or could not be reached."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status)


def default_data_dir() -> str:
    return os.environ.get(DATA_DIR_ENV) or os.path.join(
        os.path.expanduser("~"), ".moleditpy", "job_manager"
    )


def discover(directory: str = "") -> Dict[str, Any]:
    """Where the API is listening, from the file the server writes.

    Absent means the API is not running (the server deletes it when it stops),
    which is a different problem from a refused connection and is worth saying
    so plainly.
    """
    directory = directory or default_data_dir()
    path = os.path.join(directory, ENDPOINT_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise JobApiError(
            f"The Job Manager API is not running: no {path}. Enable it in "
            f"MoleditPy under Extensions > Job Manager > Local API..."
        ) from exc
    except (OSError, ValueError) as exc:
        raise JobApiError(f"Could not read {path}: {exc}") from exc
    if not data.get("url"):
        raise JobApiError(f"{path} names no url")
    return data


class JobManagerClient:
    """Talks to one running Job Manager."""

    def __init__(
        self,
        url: str = "",
        token: str = "",
        directory: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        url = url or os.environ.get(URL_ENV, "")
        token = token or os.environ.get(TOKEN_ENV, "")
        if not url or not token:
            found = discover(directory)
            url = url or str(found.get("url", ""))
            token = token or str(found.get("token", ""))
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = float(timeout)

    # --- transport ----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = f"{self.url}/{path.lstrip('/')}"
        if query:
            from urllib.parse import urlencode

            pairs = {k: v for k, v in query.items() if v not in (None, "")}
            if pairs:
                url = f"{url}?{urlencode(pairs)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method.upper())
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as reply:
                return self._decode(reply.read())
        except urllib.error.HTTPError as exc:
            payload = self._decode(exc.read(), quiet=True)
            raise JobApiError(payload.get("error") or exc.reason, exc.code) from exc
        except urllib.error.URLError as exc:
            raise JobApiError(
                f"Could not reach the Job Manager API at {self.url}: {exc.reason}. "
                f"Is MoleditPy still running?"
            ) from exc

    @staticmethod
    def _decode(raw: bytes, quiet: bool = False) -> Dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            if quiet:
                return {}
            raise JobApiError(f"The API answered with something that is not JSON: {exc}") from exc
        return value if isinstance(value, dict) else {"result": value}

    # --- routes -------------------------------------------------------------

    def ping(self) -> Dict[str, Any]:
        return self.request("GET", "ping")

    def hosts(self) -> List[Dict[str, Any]]:
        return list(self.request("GET", "hosts").get("hosts", []))

    def presets(self, host: str = "") -> List[Dict[str, Any]]:
        return list(self.request("GET", "presets", query={"host": host}).get("presets", []))

    def jobs(
        self,
        state: str = "",
        host: str = "",
        name: str = "",
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        query = {"state": state, "host": host, "name": name, "limit": limit or ""}
        return list(self.request("GET", "jobs", query=query).get("jobs", []))

    def job(self, job_id: str) -> Dict[str, Any]:
        return self.request("GET", f"jobs/{job_id}").get("job", {})

    def submit(self, **fields: Any) -> Dict[str, Any]:
        """Submit a job. ``host`` plus ``command`` or ``preset`` are required.

        Returns the job record straight away: submission itself runs in the
        background, so the record comes back UPLOADING and reaches the queue a
        moment later. :meth:`wait` is how a script waits for the rest.
        """
        files = fields.get("files")
        if isinstance(files, str):
            fields["files"] = [files]
        if fields.get("files"):
            # The server reads them off this machine's disk; a relative path
            # would be resolved against MoleditPy's working directory.
            fields["files"] = [os.path.abspath(os.path.expanduser(p)) for p in fields["files"]]
        return self.request("POST", "jobs", body=fields).get("job", {})

    def cancel(self, job_id: str, release_dependents: bool = True) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"jobs/{job_id}/cancel",
            body={"release_dependents": bool(release_dependents)},
        )

    def download(
        self,
        job_id: str,
        into: str = "",
        names: Optional[Sequence[str]] = None,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"wait": bool(wait)}
        if into:
            body["into"] = os.path.abspath(os.path.expanduser(into))
        if names:
            body["names"] = list(names)
        return self.request("POST", f"jobs/{job_id}/download", body=body, timeout=timeout)

    def log(self, job_id: str, lines: int = 200, file: str = "") -> str:
        reply = self.request("GET", f"jobs/{job_id}/log", query={"lines": lines, "file": file})
        return str(reply.get("text", ""))

    def files(self, job_id: str) -> List[str]:
        return list(self.request("GET", f"jobs/{job_id}/files").get("files", []))

    def forget(self, job_id: str) -> Dict[str, Any]:
        return self.request("DELETE", f"jobs/{job_id}")

    # --- convenience --------------------------------------------------------

    def wait(
        self,
        job_id: str,
        interval: float = 10.0,
        timeout: float = 0.0,
        on_state: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Block until the job reaches a terminal state; returns the record.

        ``interval`` is how often *this* asks MoleditPy, which is a local
        question -- it does not make the plugin poll the cluster any faster
        than its own poll interval, so a small number here costs nothing on the
        cluster. ``timeout`` of 0 waits indefinitely.
        """
        deadline = time.time() + timeout if timeout else 0.0
        previous = ""
        while True:
            job = self.job(job_id)
            state = str(job.get("state", ""))
            if on_state is not None and state != previous:
                on_state(job)
            previous = state
            if state in TERMINAL_STATES:
                return job
            if deadline and time.time() > deadline:
                raise JobApiError(f"Timed out waiting for job {job_id} (still {state})")
            time.sleep(max(0.5, float(interval)))


# --- command line -----------------------------------------------------------


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _job_line(job: Dict[str, Any]) -> str:
    return "{id}  {state:<11} {host_name:<16} {name}".format(
        id=job.get("id", ""),
        state=job.get("state", ""),
        host_name=job.get("host_name", ""),
        name=job.get("name", ""),
    )


def _key_values(pairs: Sequence[str], what: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for pair in pairs or ():
        if "=" not in pair:
            raise SystemExit(f"--{what} wants NAME=VALUE, not {pair!r}")
        key, value = pair.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-manager",
        description="Submit to, and read, a running MoleditPy Job Manager.",
    )
    parser.add_argument("--url", default="", help="API base URL (default: discovered)")
    parser.add_argument("--token", default="", help="API token (default: discovered)")
    parser.add_argument("--json", action="store_true", help="Print raw JSON, not a summary")
    # Not dest="command": `submit --command` would overwrite it, and the
    # dispatch below then matched nothing while still reporting success.
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("ping", help="Check that the API is answering")
    sub.add_parser("hosts", help="List configured hosts")

    presets = sub.add_parser("presets", help="List saved submit presets")
    presets.add_argument("--host", default="", help="Only this host's presets")

    jobs = sub.add_parser("jobs", help="List tracked jobs")
    jobs.add_argument("--state", default="", help="A state, or ACTIVE / TERMINAL")
    jobs.add_argument("--host", default="")
    jobs.add_argument("--name", default="", help="Substring of the job name")
    jobs.add_argument("--limit", type=int, default=0)

    show = sub.add_parser("job", help="Show one job")
    show.add_argument("job_id")

    submit = sub.add_parser("submit", help="Submit a job")
    submit.add_argument("files", nargs="*", help="Input files to upload")
    submit.add_argument("--host", required=True, help="Host name or id")
    submit.add_argument("--command", default="", help="Command line, e.g. 'prog {input}'")
    submit.add_argument("--preset", default="", help="A saved preset on that host")
    submit.add_argument("--name", default="", help="Job name (default: the input's)")
    submit.add_argument("--queue", default="")
    submit.add_argument("--account", default="")
    submit.add_argument("--walltime", default="")
    submit.add_argument("--nodes", type=int)
    submit.add_argument("--ntasks", type=int)
    submit.add_argument("--cpus", type=int, dest="cpus_per_task")
    submit.add_argument("--memory", default="", help="e.g. 16GB")
    submit.add_argument("--module", action="append", default=[], dest="modules")
    submit.add_argument("--pre", action="append", default=[], dest="pre_commands")
    submit.add_argument("--fetch", action="append", default=[], dest="fetch_globs")
    submit.add_argument("--remote-dir", default="", help="Run in a directory already on the host")
    submit.add_argument("--remote-input", default="", help="Input inside --remote-dir")
    submit.add_argument("--after", default="", dest="after_job", help="Chain behind this job id")
    submit.add_argument("--chain-any", action="store_true", help="Chain on ending, not succeeding")
    submit.add_argument("--start-after", default="", help="Epoch second or 2026-01-31T18:30")
    submit.add_argument("--no-auto-download", action="store_true")
    submit.add_argument("--wait", action="store_true", help="Block until the job finishes")

    cancel = sub.add_parser("cancel", help="Cancel a running job")
    cancel.add_argument("job_id")
    cancel.add_argument("--strand-dependents", action="store_true")

    download = sub.add_parser("download", help="Fetch a job's results")
    download.add_argument("job_id")
    download.add_argument("--into", default="")
    download.add_argument("--name", action="append", default=[], dest="names")
    download.add_argument("--no-wait", action="store_true")

    log = sub.add_parser("log", help="Tail a job's log")
    log.add_argument("job_id")
    log.add_argument("--lines", type=int, default=200)
    log.add_argument("--file", default="", help="Another file in the job directory")

    listing = sub.add_parser("files", help="List a job's remote files")
    listing.add_argument("job_id")

    wait = sub.add_parser("wait", help="Block until a job finishes")
    wait.add_argument("job_id")
    wait.add_argument("--interval", type=float, default=10.0)
    wait.add_argument("--timeout", type=float, default=0.0)
    wait.add_argument("--download", action="store_true", help="Fetch the results afterwards")

    forget = sub.add_parser("forget", help="Stop tracking a finished job")
    forget.add_argument("job_id")

    return parser


def _submit_fields(args: argparse.Namespace) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"host": args.host, "files": list(args.files)}
    for key in ("command", "preset", "name", "queue", "account", "walltime", "memory"):
        if getattr(args, key, ""):
            fields[key] = getattr(args, key)
    for key in ("nodes", "ntasks", "cpus_per_task"):
        if getattr(args, key, None) is not None:
            fields[key] = getattr(args, key)
    for key in ("modules", "pre_commands", "fetch_globs"):
        if getattr(args, key, None):
            fields[key] = list(getattr(args, key))
    if args.remote_dir:
        fields["remote_dir"] = args.remote_dir
    if args.remote_input:
        fields["remote_input"] = args.remote_input
    if args.after_job:
        fields["after_job"] = args.after_job
    if args.chain_any:
        fields["chain_any"] = True
    if args.start_after:
        # A bare number is an epoch second; anything else is a local time the
        # server parses. Sending the string either way would make "1800" a date.
        try:
            fields["start_after"] = float(args.start_after)
        except ValueError:
            fields["start_after"] = args.start_after
    if args.no_auto_download:
        fields["auto_download"] = False
    return fields


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = JobManagerClient(url=args.url, token=args.token)
        return _run(client, args)
    except JobApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130


def _run(client: JobManagerClient, args: argparse.Namespace) -> int:
    raw = bool(args.json)
    if args.subcommand == "ping":
        _print(client.ping())
    elif args.subcommand == "hosts":
        found = client.hosts()
        if raw:
            _print(found)
        else:
            for host in found:
                mark = "" if host["enabled"] else "  (disabled)"
                print(
                    f"{host['id']}  {host['name']:<20} {host['target']:<28} "
                    f"{host['scheduler']}{mark}"
                )
    elif args.subcommand == "presets":
        found = client.presets(args.host)
        if raw:
            _print(found)
        else:
            for preset in found:
                print(f"{preset['id']}  {preset['name']:<20} {preset['command']}")
    elif args.subcommand == "jobs":
        found = client.jobs(args.state, args.host, args.name, args.limit)
        _print(found) if raw else [print(_job_line(job)) for job in found]
    elif args.subcommand == "job":
        _print(client.job(args.job_id))
    elif args.subcommand == "submit":
        job = client.submit(**_submit_fields(args))
        if raw:
            _print(job)
        else:
            print(_job_line(job))
        if args.wait:
            return _wait(client, job.get("id", ""), 10.0, 0.0, download=True, raw=raw)
    elif args.subcommand == "cancel":
        _print(client.cancel(args.job_id, release_dependents=not args.strand_dependents))
    elif args.subcommand == "download":
        reply = client.download(
            args.job_id, into=args.into, names=args.names, wait=not args.no_wait
        )
        _print(reply) if raw else [print(path) for path in reply.get("files", [])]
    elif args.subcommand == "log":
        print(client.log(args.job_id, args.lines, args.file))
    elif args.subcommand == "files":
        found = client.files(args.job_id)
        _print(found) if raw else [print(name) for name in found]
    elif args.subcommand == "wait":
        return _wait(client, args.job_id, args.interval, args.timeout, args.download, raw)
    elif args.subcommand == "forget":
        _print(client.forget(args.job_id))
    return 0


def _wait(
    client: JobManagerClient,
    job_id: str,
    interval: float,
    timeout: float,
    download: bool,
    raw: bool,
) -> int:
    def announce(job: Dict[str, Any]) -> None:
        if not raw:
            print(f"{job.get('state', '')}: {job.get('name', '')}", file=sys.stderr)

    job = client.wait(job_id, interval=interval, timeout=timeout, on_state=announce)
    if download and not job.get("downloaded"):
        try:
            reply = client.download(job_id, wait=True)
            job = reply.get("job", job)
        except JobApiError as exc:
            print(f"warning: the results were not downloaded: {exc}", file=sys.stderr)
    if raw:
        _print(job)
    else:
        for path in job.get("downloaded_files", []):
            print(path)
    # A job that failed on the cluster is a failed command here too, so a shell
    # script can chain on it without parsing the JSON.
    return 0 if job.get("state") == "DONE" else 2


if __name__ == "__main__":  # pragma: no cover - the entry point
    sys.exit(main())
