"""Details show saved scheduling, files, results and submission metadata."""

import re

import pytest

pytest.importorskip("PySide6.QtWidgets")

from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import Job, SubmitPreset  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TestCompleteJobRecord(DialogTestCase):
    def describe(self, job):
        self.store.add_job(job)
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        text = dialog._describe(job)
        rows = {}
        for line in text.split("--- script ---")[0].splitlines():
            match = re.match(r"^(.*?)\s{2,}(.*)$", line)
            if match:
                rows[match[1]] = match[2]
        return text, rows

    def test_priority_is_only_a_number_and_force_does_not_claim_a_start(self):
        text, rows = self.describe(
            Job(id="child", host_id=self.host.id, state="PENDING", priority=-10, force_run=True)
        )
        assert rows["Priority"] == "-10"
        assert "higher runs first" not in text
        assert rows["Force run"] == "yes"
        assert rows["Force chain"] == "no"
        assert "started ahead of the queue" not in text

    def test_forced_chain_names_its_predecessor_and_completion_policy(self):
        parent = Job(
            id="parent",
            name="Previous forced check",
            host_id=self.host.id,
            state="RUNNING",
            remote_job_id="job_0001_parent.sh",
            force_run=True,
        )
        self.store.add_job(parent)
        _, rows = self.describe(
            Job(
                id="child",
                host_id=self.host.id,
                state="PENDING",
                force_run=True,
                after_job_id=parent.id,
                chain_any=True,
            )
        )
        assert rows["Force chain"] == "yes"
        assert rows["After job"] == "Previous forced check (parent)"
        assert rows["After queue id"] == parent.remote_job_id
        assert rows["After state"] == "RUNNING"
        assert rows["Chain on any result"] == "yes"
        assert rows["Wait condition"] == "completion (success or failure)"
        assert rows["Recorded state"] == "PENDING"

    def test_normal_chain_requires_success(self):
        _, rows = self.describe(
            Job(
                id="child",
                host_id=self.host.id,
                state="PENDING",
                after_job_id="parent",
                chain_any=False,
            )
        )
        assert rows["Force run"] == rows["Force chain"] == "no"
        assert rows["Chain on any result"] == "no"
        assert rows["Wait condition"] == "successful completion"

    def test_removed_predecessor_keeps_the_recorded_id(self):
        _, rows = self.describe(
            Job(
                id="child",
                host_id=self.host.id,
                after_job_id="removed-parent",
                force_run=True,
                chain_any=True,
            )
        )
        assert rows["After job"] == "removed-parent"
        assert rows["After state"] == "not in current job list"
        assert rows["Force chain"] == "yes"

    def test_saved_files_downloads_ids_and_timing_are_present(self):
        job = Job(
            id="record-id",
            name="example",
            host_id=self.host.id,
            state="DONE",
            rc=0,
            remote_dir="/work/example",
            remote_dir_provided=True,
            remote_input="input.inp",
            script_name="run_example.sh",
            sentinel_name=".example.exit",
            log_file="example.log",
            input_files=["/inputs/input.inp"],
            local_dir="/results/example",
            fetch_globs=["*.out", "*.xyz"],
            auto_download=False,
            downloaded=True,
            downloaded_files=["/results/example/result.out"],
            submitted_at=1000,
            started_at=1100,
            updated_at=1300,
            finished_at=1200,
            start_after=1050,
            command="run input.inp",
            server_job_id="remote-job",
            server_id="server-id",
            owner_user_id="owner-id",
            execution_os="linux",
            last_server_state="finished",
        )
        text, rows = self.describe(job)
        for key, expected in {
            "Job id": "record-id",
            "Host id": self.host.id,
            "Exit code": "0",
            "Provided directory": "yes",
            "Remote input": "input.inp",
            "Script file": "run_example.sh",
            "Completion sentinel": ".example.exit",
            "Input files": "/inputs/input.inp",
            "Results folder": "/results/example",
            "Fetch patterns": "*.out, *.xyz",
            "Auto download": "no",
            "Downloaded": "yes",
            "Downloaded files": "/results/example/result.out",
            "Server job id": "remote-job",
            "Server id": "server-id",
            "Owner user id": "owner-id",
            "Execution OS": "linux",
            "Last server state": "finished",
        }.items():
            assert rows[key] == expected, key
        for key in ("Submitted", "Started", "Updated", "Finished", "Scheduled start"):
            assert rows[key] != "-", key
        assert rows["Queue wait"] == rows["Run time"] == "1m 40s"
        assert text.endswith("run input.inp")

    def test_snapshot_is_complete_and_separate_from_current_download_settings(self):
        preset = SubmitPreset(
            id="preset-id",
            host_id=self.host.id,
            name="Saved template",
            nodes=0,
            ntasks=3,
            cpus_per_task=4,
            priority=0,
            fetch_globs=["*.saved"],
            auto_download=True,
        )
        snapshot = preset.to_dict()
        snapshot["custom_setting"] = {"mode": "example"}
        _, rows = self.describe(
            Job(
                id="child",
                host_id=self.host.id,
                priority=-5,
                auto_download=False,
                fetch_globs=["*.current"],
                preset=snapshot,
            )
        )
        assert rows["Preset id"] == "preset-id"
        assert rows["Preset name"] == "Saved template"
        assert rows["Preset host id"] == self.host.id
        assert rows["Nodes"] == rows["Preset priority"] == "0"
        assert rows["Priority"] == "-5"
        assert rows["Fetch patterns"] == "*.current"
        assert rows["Preset fetch patterns"] == "*.saved"
        assert rows["Auto download"] == "no"
        assert rows["Preset auto download"] == "yes"
        assert rows["Preset custom_setting"] == '{"mode": "example"}'

    def test_minimal_record_works_without_a_host_or_preset(self):
        text, rows = self.describe(Job(id="legacy", host_id="removed-host", host_name="Old host"))
        assert rows["Host"] == "Old host"
        assert rows["Priority"] == "0"
        assert rows["After job"] == rows["Wait condition"] == "-"
        assert rows["Script file"] == "(scheduler default)"
        assert rows["Completion sentinel"] == "(default)"
        assert "current host profile" not in text
        assert "submission preset" not in text
