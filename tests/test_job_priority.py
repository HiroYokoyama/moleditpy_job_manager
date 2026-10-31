"""Both real helper shells must schedule by priority and preserve FIFO ties."""

import os
import shlex
import subprocess

import pytest

from job_manager import remote_runner, remote_runner_ps
from job_manager.models import Job, SubmitPreset, validate_priority

from .bash_support import bash_path
from .test_remote_runner import BASH, RunnerHarness as BashHarness
from .test_remote_runner_ps import POWERSHELL, RunnerHarness as PowerShellHarness


@pytest.mark.parametrize("value", [True, False, -1001, 1001, 1.5, "3", None])
def test_priority_rejects_invalid_numbers(value):
    with pytest.raises(ValueError, match="Priority"):
        validate_priority(value)


def test_saved_priorities_and_legacy_defaults():
    assert Job.from_dict({}).priority == SubmitPreset.from_dict({}).priority == 0
    for model in (Job, SubmitPreset):
        for value in (-1000, -1, 0, 10, 1000):
            assert model.from_dict(model(priority=value).to_dict()).priority == value


def test_queue_details_keep_legacy_entries_and_priority_order():
    result = remote_runner.parse_queue_detail(
        "entry queue job_0001_a.sh 1 100\n"
        "entry queue job_10000_b.ps1 1 100 8\n"
        "entry queue job_9999_c.sh 1 100 8\n"
        "entry queue job_0003_d.sh 1 100 invalid\n"
        "entry queue job_0004_e.sh 1 100 ²\n"
    )
    assert [item["job_id"] for item in result["waiting"]] == ["c", "b", "a", "d", "e"]
    assert [item["priority"] for item in result["waiting"]] == [8, 8, 0, 0, 0]


class PriorityBehavior:
    def append(self, name):
        path = self.marker("order")
        if self.flavour is remote_runner:
            return f"echo {name} >> {shlex.quote(bash_path(path))}"
        return f"[IO.File]::AppendAllText('{path}', '{name}' + [Environment]::NewLine)"

    def command(self, command):
        shell = (
            [BASH, "-lc", command]
            if self.flavour is remote_runner
            else [POWERSHELL, "-NoProfile", "-Command", command]
        )
        result = subprocess.run(shell, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def directory(self):
        return bash_path(self.dir) if self.flavour is remote_runner else self.dir

    def finish_order(self, expected):
        self.start_runner()
        self.wait_for(
            lambda: sum(v == "done" for v in self.listing().values()) == len(expected),
            timeout=30,
            what="priority jobs completing",
        )
        with open(self.marker("order"), encoding="utf-8") as handle:
            assert handle.read().splitlines() == expected

    def test_negative_priorities_sort_below_default_and_overrides_are_reported(self):
        self.set_limit("slots", 1)
        self.enqueue("lowest", self.append("lowest"), priority=-1000)
        self.enqueue("low", self.append("low"), priority=-10)
        self.enqueue("normal", self.append("normal"))
        entry = self.enqueue("changed", self.append("changed"), priority=10)
        assert (
            self.command(self.flavour.set_priority_command(self.directory(), entry, -5))
            == "updated"
        )
        detail = remote_runner.parse_queue_detail(
            self.command(self.flavour.queue_detail_command(self.directory()))
        )
        assert [item["priority"] for item in detail["waiting"]] == [0, -5, -10, -1000]
        self.finish_order(["normal", "changed", "low", "lowest"])

    def test_higher_priority_first_with_fifo_ties(self):
        self.set_limit("slots", 1)
        self.enqueue("low", self.append("low"), priority=0)
        self.enqueue("first", self.append("first"), priority=10)
        self.enqueue("second", self.append("second"), priority=10)
        self.finish_order(["first", "second", "low"])

    def test_priority_does_not_bypass_dependencies(self):
        self.set_limit("slots", 1)
        self.enqueue("parent", self.append("parent"), priority=0)
        self.enqueue("child", self.append("child"), after="parent", priority=100)
        self.enqueue("other", self.append("other"), priority=1)
        self.finish_order(["other", "parent", "child"])

    def test_fifo_ties_use_numeric_sequences_beyond_9999(self):
        self.set_limit("slots", 1)
        seed = os.path.join(self.dir, "done", "job_9998_seed" + self.flavour.ENTRY_SUFFIX)
        with open(seed, "w", encoding="ascii"):
            pass
        self.enqueue("first", self.append("first"), priority=10)
        self.enqueue("second", self.append("second"), priority=10)
        os.unlink(seed)
        self.finish_order(["first", "second"])

    def test_priority_change_cannot_modify_an_already_claimed_job(self):
        entry = self.enqueue("claimed", self.append("claimed"), priority=3)
        os.replace(os.path.join(self.dir, "queue", entry), os.path.join(self.dir, "running", entry))
        assert (
            self.command(self.flavour.set_priority_command(self.directory(), entry, 100))
            == remote_runner.NOT_QUEUED
        )
        assert not os.path.exists(os.path.join(self.dir, remote_runner.PRIORITY_DIR, entry))

    def test_reprioritizing_updates_report_and_dispatch_without_rewriting_job(self):
        self.set_limit("slots", 1)
        self.enqueue("first", self.append("first"), priority=1)
        entry = self.enqueue("later", self.append("later"), priority=0)
        with open(os.path.join(self.dir, "queue", entry), "rb") as handle:
            original = handle.read()
        result = self.command(self.flavour.set_priority_command(self.directory(), entry, 20))
        assert result == "updated"
        with open(os.path.join(self.dir, "queue", entry), "rb") as handle:
            assert handle.read() == original
        detail = remote_runner.parse_queue_detail(
            self.command(self.flavour.queue_detail_command(self.directory()))
        )
        assert detail["waiting"][0]["job_id"] == "later"
        assert detail["waiting"][0]["priority"] == 20
        self.finish_order(["later", "first"])
        assert (
            self.command(self.flavour.set_priority_command(self.directory(), entry, 3))
            == remote_runner.NOT_QUEUED
        )

    def test_legacy_entries_without_priority_still_run(self):
        self.set_limit("slots", 1)
        entry = self.enqueue("legacy", self.append("legacy"))
        path = os.path.join(self.dir, "queue", entry)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "\n".join(
                    line
                    for line in text.splitlines()
                    if not line.startswith(remote_runner.PRIORITY_TAG)
                )
                + "\n"
            )
        self.enqueue("high", self.append("high"), priority=1)
        self.finish_order(["high", "legacy"])


class TestBashPriority(PriorityBehavior, BashHarness):
    flavour = remote_runner


class TestPowerShellPriority(PriorityBehavior, PowerShellHarness):
    flavour = remote_runner_ps

    def test_priority_override_uses_helper_directory_and_replaces_existing_value(self):
        entry = self.enqueue("queued", self.append("queued"))
        os.rmdir(os.path.join(self.dir, remote_runner.PRIORITY_DIR))
        unrelated = self.marker("dotnet-cwd")
        os.mkdir(unrelated)
        for value in (3, 7):
            command = (
                f"[Environment]::CurrentDirectory = '{unrelated}'; "
                + self.flavour.set_priority_command(self.directory(), entry, value)
            )
            assert self.command(command) == "updated"
            with open(os.path.join(self.dir, remote_runner.PRIORITY_DIR, entry)) as handle:
                assert handle.read() == str(value)
        assert not os.path.exists(os.path.join(unrelated, remote_runner.PRIORITY_DIR))
