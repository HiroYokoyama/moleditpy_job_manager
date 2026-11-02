"""Manual holds must survive dispatch, forced queues, dependencies and restarts."""

from pathlib import Path
import shlex

import pytest

from job_manager import remote_runner, remote_runner_ps
from .test_job_priority import PriorityBehavior
from .test_remote_runner import RunnerHarness as BashHarness
from .test_remote_runner_ps import RunnerHarness as PowerShellHarness


class HoldBehavior:
    command = PriorityBehavior.command
    directory = PriorityBehavior.directory
    append = PriorityBehavior.append

    def hold(self, entry, held=True):
        return self.command(self.flavour.set_hold_command(self.directory(), entry, held))

    def locations(self):
        return remote_runner.parse_listing(
            self.command(self.flavour.list_command(self.directory()))
        )

    def test_release_preserves_after_and_forced_marker_until_parent_finishes(self):
        self.set_limit("cores", 4)
        self.set_limit("memory", 0)
        self.set_limit("slots", 1)
        parent = self.enqueue("parent", self.append("parent"))
        child = self.enqueue("child", self.append("child"), after="parent", priority=100)
        self.enqueue("other", self.append("other"))
        Path(self.dir, remote_runner.FORCED_DIR, child).touch()
        assert self.hold(parent) == self.hold(child) == "updated"
        original = Path(self.dir, "held", child).read_bytes()
        assert self.hold(child, False) == "updated"
        assert Path(self.dir, "queue", child).read_bytes() == original
        self.start_runner()
        self.wait_for(lambda: self.locations().get("other") == "done", timeout=25)
        assert self.locations()["parent"] == "held"
        assert self.locations()["child"] == "queue"
        assert Path(self.dir, remote_runner.FORCED_DIR, child).exists()
        assert self.hold(parent, False) == "updated"
        self.wait_for(lambda: self.locations().get("child") == "done", timeout=25)
        assert Path(self.marker("order")).read_text().splitlines() == ["other", "parent", "child"]

    def test_held_forced_job_does_not_run_even_when_queue_is_paused(self):
        self.set_limit("cores", 4)
        self.set_limit("memory", 0)
        entry = self.enqueue("forced", self.append("forced"))
        Path(self.dir, remote_runner.FORCED_DIR, entry).touch()
        assert self.hold(entry) == "updated"
        self.command(self.flavour.pause_command(self.directory(), True))
        process = self.start_runner()
        self.wait_for(lambda: process.poll() is not None, timeout=20)
        assert self.locations()["forced"] == "held"
        assert Path(self.dir, remote_runner.FORCED_DIR, entry).exists()
        assert not Path(self.marker("order")).exists()
        assert (
            self.command(self.flavour.force_command(self.directory(), entry))
            == remote_runner.NOT_QUEUED
        )
        assert self.hold(entry, False) == "updated"
        self.start_runner()
        self.wait_for(lambda: self.locations().get("forced") == "done", timeout=25)
        assert Path(self.marker("order")).read_text().splitlines() == ["forced"]

    def test_priority_and_cancel_work_while_held(self):
        entry = self.enqueue("heldjob", self.append("heldjob"), priority=5)
        assert self.hold(entry) == "updated"
        assert (
            self.command(self.flavour.set_priority_command(self.directory(), entry, -20))
            == "updated"
        )
        detail = remote_runner.parse_queue_detail(
            self.command(self.flavour.queue_detail_command(self.directory()))
        )
        assert detail["waiting"][0]["held"] is True
        assert detail["waiting"][0]["priority"] == -20
        assert self.command(self.flavour.cancel_command(self.directory(), entry)) == "dequeued"
        assert self.locations()["heldjob"] == "done"
        assert not Path(self.marker("order")).exists()

    def test_dispatch_claim_wins_over_late_hold_without_creating_held_copy(self):
        entry = self.enqueue("claimed", self.append("claimed"))
        Path(self.dir, "queue", entry).rename(Path(self.dir, "running", entry))
        assert self.hold(entry) == remote_runner.NOT_QUEUED
        assert not Path(self.dir, "held", entry).exists()

    def test_cancel_parent_keep_dependents_does_not_remove_manual_hold(self):
        self.set_limit("cores", 4)
        self.set_limit("memory", 0)
        parent = self.enqueue("parent", self.append("parent"))
        child = self.enqueue("child", self.append("child"), after="parent")
        assert self.hold(child) == "updated"
        original = Path(self.dir, "held", child).read_bytes()
        self.command(self.flavour.cancel_command(self.directory(), parent))
        assert self.command(self.flavour.release_command(self.directory(), child)) == "released"
        assert self.locations()["child"] == "held"
        assert Path(self.dir, "held", child).read_bytes() == original
        assert self.hold(child, False) == "updated"
        self.start_runner()
        self.wait_for(lambda: self.locations().get("child") == "done", timeout=25)
        assert Path(self.marker("order")).read_text().splitlines() == ["child"]

    def test_a_live_older_helper_is_not_allowed_to_misread_a_held_parent(self):
        entry = self.enqueue("parent", self.append("parent"))
        Path(self.dir, "lock").mkdir(exist_ok=True)
        if self.flavour is remote_runner:
            prefix = f"echo $$ > {shlex.quote(self.directory() + '/lock/pid')}; "
        else:
            prefix = f"Set-Content -LiteralPath '{self.dir}\\lock\\pid' -Value $PID; "
        with pytest.raises(AssertionError, match="older helper"):
            self.command(prefix + self.flavour.set_hold_command(self.directory(), entry, True))
        assert self.locations()["parent"] == "queue"


class TestBashHold(HoldBehavior, BashHarness):
    flavour = remote_runner


class TestPowerShellHold(HoldBehavior, PowerShellHarness):
    flavour = remote_runner_ps
