"""Opt-in real-daemon checks. Never discovers or reads a user's JobStore."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from job_manager.job_dependencies import DependencyGraph  # noqa: E402
from job_manager.models import (  # noqa: E402
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    STATE_RUNNING,
    HostProfile,
    Job,
    SubmitPreset,
)
from job_manager.runner import cancel_job, fetch_results, poll_host, submit_job  # noqa: E402
from job_manager.schedulers import get_scheduler  # noqa: E402
from job_manager.transport.openssh import OpenSSHTransport  # noqa: E402


def main():
    scheduler = sys.argv[1]
    if scheduler not in {"slurm", "pbs", "sge"}:
        raise ValueError("Unknown scheduler")
    lab = Path(os.environ["LAB"]).resolve()
    host = HostProfile(
        name="disposable scheduler lab",
        hostname="127.0.0.1",
        port=2222,
        username="schedulerlab",
        scheduler=scheduler,
        key_path=str(lab / "id_ed25519"),
        remote_root="/home/schedulerlab/job results",
        load_profile=False,
        login_commands=[
            "export PATH=/opt/pbs/bin:/opt/pbs/sbin:$PATH",
            "export TZ=UTC",
        ],
        ssh_options=[
            "StrictHostKeyChecking=yes",
            "UserKnownHostsFile=" + str(lab / "known_hosts"),
            "GlobalKnownHostsFile=/dev/null",
        ],
    )
    transport = OpenSSHTransport(host)
    jobs = []
    report = {"scheduler": scheduler, "cases": []}
    source = lab / "source files" / "input.inp"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("synthetic scheduler integration input\n", encoding="utf-8")

    def submit(command, *, after=None, any_outcome=False, cores=1, start=0.0):
        job = Job(
            host_id=host.id,
            scheduler=scheduler,
            name="integration_" + str(len(jobs)),
            input_files=[str(source)],
            after_job_id=after.id if after else "",
            chain_any=any_outcome,
        )
        preset = SubmitPreset(
            command_template=command,
            cpus_per_task=cores,
            memory="64M",
            walltime="00:02:00",
            extra_directives=["#$ -pe smp 2"] if scheduler == "sge" and cores == 2 else [],
        )
        jobs.append(job)
        submit_job(
            transport,
            host,
            preset,
            job,
            [str(source)],
            run_after=after.remote_job_id if after else "",
            run_after_any=any_outcome,
            start_after=start,
        )
        print("SUBMITTED", job.name, job.remote_job_id, flush=True)
        return job

    def refresh(job):
        updates = poll_host(transport, host, [job])
        if job.id in updates:
            job.touch(updates[job.id])
        return job.state

    def wait(job, acceptable, timeout=70):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = refresh(job)
            if state in acceptable:
                return state
            if job.is_terminal:
                raise AssertionError(f"Unexpected terminal state {state}, rc={job.rc}")
            time.sleep(0.3)
        raw = transport.run(get_scheduler(scheduler).status_command(host.username, []))
        raise AssertionError(
            f"Timed out: {job.remote_job_id} state={job.state}\n{raw.stdout}\n{raw.stderr}"
        )

    def fetch(job, name):
        paths = fetch_results(transport, job, str(lab / "downloads" / job.id), globs=[name])
        assert len(paths) == 1, (name, paths)
        return Path(paths[0]).read_text(encoding="utf-8")

    def passed(name):
        report["cases"].append({"name": name, "passed": True})
        print("PASS", name, flush=True)

    try:
        transport.test_connection()
        normal = submit("cat {input} > result.out; sleep 1")
        wait(normal, {STATE_DONE})
        assert normal.rc == 0
        assert fetch(normal, "result.out") == source.read_text(encoding="utf-8")
        passed("SSH upload, real submit, completion sentinel, result download, paths with spaces")

        failed = submit("exit 7")
        wait(failed, {STATE_FAILED})
        assert failed.rc == 7
        passed("nonzero payload exit is FAILED with rc=7")

        variables = {
            "slurm": 'test "$SLURM_CPUS_PER_TASK" -eq 2 || exit 9; echo "$SLURM_CPUS_PER_TASK" > resources.out',
            "pbs": 'test "$(wc -l < "$PBS_NODEFILE")" -ge 2 || exit 9; wc -l < "$PBS_NODEFILE" > resources.out',
            "sge": 'test "$NSLOTS" -eq 2 || exit 9; echo "$NSLOTS" > resources.out',
        }
        resources = submit(variables[scheduler], cores=2)
        wait(resources, {STATE_DONE})
        assert int(fetch(resources, "resources.out").strip()) >= 2
        passed("requested CPU allocation (site-configured smp PE for SGE)")

        running = submit("sleep 45")
        wait(running, {STATE_RUNNING})
        cancel_job(transport, host, running)
        deadline = time.monotonic() + 25
        while not running.is_terminal and time.monotonic() < deadline:
            refresh(running)
            time.sleep(0.3)
        assert running.is_terminal
        passed("RUNNING detection and scheduler cancellation")

        predecessor = submit("sleep 4; exit 0")
        dependent = submit("echo dependency > result.out", after=predecessor)
        assert refresh(dependent) == STATE_PENDING
        wait(predecessor, {STATE_DONE})
        wait(dependent, {STATE_DONE})
        assert fetch(dependent, "result.out").strip() == "dependency"
        passed("queued dependency runs after predecessor completion")

        predecessor = submit("sleep 4; exit 7")
        dependent = submit("echo after_failure > result.out", after=predecessor)
        wait(predecessor, {STATE_FAILED})
        if scheduler == "sge":
            wait(dependent, {STATE_DONE})
        else:
            refresh(dependent)
            assert DependencyGraph({j.id: j for j in jobs}).chain_blocker(dependent) is predecessor
            cancel_job(transport, host, dependent)
        passed("failure dependency respects scheduler afterok/hold_jid semantics")

        predecessor = submit("sleep 4; exit 7")
        dependent = submit("echo after_any > result.out", after=predecessor, any_outcome=True)
        wait(predecessor, {STATE_FAILED})
        wait(dependent, {STATE_DONE})
        passed("after-any dependency runs after a failed predecessor")

        # The client and the SSH host need not have the same time zone.
        previous_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        try:
            target = time.time() + 8
            scheduled = submit("date +%s > began.out", start=target)
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()
        wait(scheduled, {STATE_DONE}, timeout=65)
        assert int(fetch(scheduled, "began.out").strip()) >= int(target)
        passed("scheduled start with different client and host time zones")
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["error"] = str(exc)
        raise
    finally:
        for job in jobs:
            if job.remote_job_id and not job.is_terminal:
                try:
                    cancel_job(transport, host, job)
                except Exception as exc:
                    print("CLEANUP", job.remote_job_id, str(exc), flush=True)
        transport.close()
        report["jobs"] = [
            {"id": job.remote_job_id, "state": job.state, "rc": job.rc} for job in jobs
        ]
        (lab / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
