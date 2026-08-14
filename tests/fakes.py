"""A transport that never touches the network.

Commands are matched against a list of (substring, result) rules, and every
command is recorded, which is what lets the submit -> poll -> fetch round trip
be asserted end to end with no cluster, no sshd and no event loop.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

from job_manager.models import HostProfile, Job, SubmitPreset
from job_manager.transport.base import CommandResult, Transport, TransportError


class FakeTransport(Transport):
    def __init__(self, host: Optional[HostProfile] = None) -> None:
        super().__init__(host or make_host())
        #: (substring, CommandResult) -- first match wins.
        self.rules: List[Tuple[str, CommandResult]] = []
        self.default = CommandResult(0, "", "")
        self.commands: List[str] = []
        self.uploads: List[Tuple[str, str]] = []
        self.downloads: List[Tuple[str, str]] = []
        self.uploaded_text: Dict[str, str] = {}
        self.closed = 0
        self.fail_downloads: Sequence[str] = ()
        #: Climbs like the counter a real host keeps, so two submissions to one
        #: fake never come away with the same dispatch number.
        self._sequence = 0

    # --- rule helpers -------------------------------------------------------

    def when(
        self, substring: str, stdout: str = "", rc: int = 0, stderr: str = ""
    ) -> "FakeTransport":
        self.rules.append((substring, CommandResult(rc, stdout, stderr)))
        return self

    def clear_rules(self) -> None:
        self.rules = []

    # --- Transport interface ------------------------------------------------

    def run(self, cmd: str, timeout: Optional[int] = None) -> CommandResult:
        self.commands.append(cmd)
        for substring, result in self.rules:
            if substring in cmd:
                return result
        # A real host always hands back a queue number, and a submission that
        # cannot get one fails on purpose. Answered after the rules, so a test
        # that wants to see that failure can still say so.
        if "sequence" in cmd:
            self._sequence += 1
            return CommandResult(0, f"{self._sequence}\n", "")
        return self.default

    def upload(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8", errors="replace") as handle:
                self.uploaded_text[remote_path] = handle.read()

    def download(self, remote_path: str, local_path: str) -> None:
        if any(pattern in remote_path for pattern in self.fail_downloads):
            raise TransportError(f"refused: {remote_path}")
        self.downloads.append((remote_path, local_path))
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as handle:
            handle.write(f"content of {remote_path}\n")

    def close(self) -> None:
        self.closed += 1

    # --- assertions ---------------------------------------------------------

    def ran(self, substring: str) -> bool:
        return any(substring in cmd for cmd in self.commands)

    def count_matching(self, substring: str) -> int:
        return sum(1 for cmd in self.commands if substring in cmd)


def make_host(**overrides) -> HostProfile:
    defaults = dict(
        id="host1",
        name="testcluster",
        hostname="login.example.org",
        username="tester",
        scheduler="slurm",
        remote_root="~/moleditpy_jobs",
    )
    defaults.update(overrides)
    return HostProfile(**defaults)


def make_preset(**overrides) -> SubmitPreset:
    defaults = dict(
        host_id="host1",
        name="test preset",
        walltime="01:00:00",
        command_template="orca {input} > {stem}.out",
    )
    defaults.update(overrides)
    return SubmitPreset(**defaults)


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job1",
        name="testjob",
        host_id="host1",
        host_name="testcluster",
        scheduler="slurm",
        remote_dir="~/moleditpy_jobs/20260101_000000_testjob",
        remote_job_id="12345",
        log_file="job.log",
    )
    defaults.update(overrides)
    return Job(**defaults)
