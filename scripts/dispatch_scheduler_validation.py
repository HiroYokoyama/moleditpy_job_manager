"""Dispatch the opt-in real-scheduler workflow using the signed-in GitHub CLI."""

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "real-schedulers.yml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduler", choices=["all", "slurm", "pbs", "sge"], default="all")
    parser.add_argument("--repo", default="HiroYokoyama/moleditpy_job_manager")
    parser.add_argument("--ref", help="Branch or tag; defaults to the current local branch")
    parser.add_argument("--wait", action="store_true", help="Wait and return the CI exit status")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the command without dispatching"
    )
    args = parser.parse_args()
    ref = (
        args.ref
        or subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip()
    )
    if not ref:
        parser.error("Use --ref when the checkout has a detached HEAD")
    marker = "dispatch-" + uuid.uuid4().hex[:12]
    command = [
        "gh",
        "workflow",
        "run",
        WORKFLOW,
        "--repo=" + args.repo,
        "--ref=" + ref,
        "-f",
        "scheduler=" + args.scheduler,
        "-f",
        "dispatch_id=" + marker,
    ]
    print(json.dumps(command), flush=True)
    if args.dry_run:
        return
    subprocess.run(command, cwd=ROOT, check=True)
    print(f"Dispatched {args.scheduler} at {ref}; correlation ID: {marker}", flush=True)
    if not args.wait:
        print(f"Runs: https://github.com/{args.repo}/actions/workflows/{WORKFLOW}")
        return
    # Match this dispatch, never a different user's latest run on the branch.
    endpoint = (
        f"repos/{args.repo}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=20"
    )
    for _ in range(20):
        result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
        if result.returncode == 0:
            for run in json.loads(result.stdout).get("workflow_runs", []):
                if marker in run.get("display_title", ""):
                    print(run["html_url"], flush=True)
                    result = subprocess.run(
                        [
                            "gh",
                            "run",
                            "watch",
                            str(run["id"]),
                            "--repo=" + args.repo,
                            "--exit-status",
                        ],
                        cwd=ROOT,
                    )
                    raise SystemExit(result.returncode)
        time.sleep(2)
    raise SystemExit(
        "Dispatch was accepted, but its run could not be located; inspect the workflow page."
    )


if __name__ == "__main__":
    main()
