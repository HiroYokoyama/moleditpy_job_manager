"""Render the real Qt windows with isolated, entirely fictional example data.

Run from the repository root with PySide6 installed. No user settings are read,
no host connections are made, and no calculations are submitted.
"""

import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = "2"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(
        prefix="job-manager-website-", ignore_cleanup_errors=True
    ) as directory:
        os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = directory
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
        from PySide6.QtWidgets import QApplication

        from job_manager.host_monitor import HostMonitorDialog
        from job_manager.host_sampler import HostSampler
        from job_manager.host_stats import HostStats
        from job_manager.jobs_dialog import JobsDialog
        from job_manager.models import HostProfile, Job
        from job_manager.poller import JobPoller
        from job_manager.service import JobService
        from job_manager.store import JobStore

        app = QApplication([])
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
        app.setStyle("Fusion")
        app.setFont(QFont("Segoe UI", 10))
        palette = QPalette()
        for role, color in (
            (QPalette.ColorRole.Window, "#f4f7f9"),
            (QPalette.ColorRole.WindowText, "#152433"),
            (QPalette.ColorRole.Base, "#ffffff"),
            (QPalette.ColorRole.AlternateBase, "#f4f7f9"),
            (QPalette.ColorRole.Text, "#152433"),
            (QPalette.ColorRole.Button, "#f4f7f9"),
            (QPalette.ColorRole.ButtonText, "#152433"),
            (QPalette.ColorRole.Highlight, "#deebff"),
            (QPalette.ColorRole.HighlightedText, "#152433"),
        ):
            palette.setColor(role, QColor(color))
        app.setPalette(palette)
        store = JobStore(directory)
        hosts = [
            HostProfile(
                id="demo-workstation", name="Workstation", hostname="workstation.example.invalid"
            ),
            HostProfile(
                id="demo-cluster",
                name="Research cluster",
                hostname="cluster.example.invalid",
                scheduler="slurm",
            ),
        ]
        store.hosts = {host.id: host for host in hosts}
        # Fixed example timestamps keep the images reproducible.
        now = 1793239200.0
        examples = [
            ("Conformer search", 0, "RUNNING", 1680),
            ("Geometry optimization", 0, "RUNNING", 960),
            ("Frequency calculation", 0, "PENDING", 0),
            ("Single-point energy", 1, "RUNNING", 3240),
            ("Solvent screening", 1, "QUEUED", 0),
            ("Reference calculation", 1, "DONE", 420),
            ("Basis-set comparison", 0, "DONE", 780),
        ]
        for index, (name, host_index, state, elapsed) in enumerate(examples):
            host = hosts[host_index]
            job = Job(
                id=f"demo-job-{index + 1}",
                name=name,
                host_id=host.id,
                host_name=host.name,
                scheduler=host.scheduler,
                state=state,
                remote_job_id=str(4201 + index),
                submitted_at=now - 3600,
                started_at=now - elapsed if elapsed else 0,
                updated_at=now,
                finished_at=now if state == "DONE" else 0,
                auto_download=False,
                downloaded=state == "DONE",
                number=index + 1,
            )
            store.jobs[job.id] = job

        output = ROOT / "img" / "website"
        output.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(JobPoller, "start"),
            patch.object(HostSampler, "acquire"),
            patch.object(HostSampler, "sample_all"),
            patch.object(JobStore, "save_settings"),
            patch("time.time", return_value=now),
        ):
            service = JobService(store)
            monitor = JobsDialog(service)
            monitor.resize(1180, 640)
            monitor.txt_log.setPlainText(
                "Example session — fictional jobs and hosts\n\n"
                "Two workstation calculations running; frequency calculation waiting in the helper queue.\n"
                "Reference calculation completed. Results ready to open."
            )
            monitor.show()
            app.processEvents()
            monitor.table.horizontalHeader().setSectionResizeMode(
                1, monitor.table.horizontalHeader().ResizeMode.Stretch
            )
            monitor.table.setColumnWidth(2, 155)
            monitor.table.setColumnWidth(6, 110)
            monitor.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
            app.processEvents()
            if not monitor.grab().save(str(output / "job-monitor.png")):
                raise RuntimeError("Could not save Job Monitor screenshot")

            print("Job Monitor rendered.", flush=True)
            hosts_window = HostMonitorDialog(service)
            print("Host Monitor constructed.", flush=True)
            hosts_window.resize(850, 480)
            hosts_window.show()
            app.processEvents()
            for host, cores, threads, load, total, free in (
                (hosts[0], 8, 16, 10.2, 32768, 16384),
                (hosts[1], 32, 64, 25.6, 131072, 98304),
            ):
                hosts_window.cards[host.id].show_stats(
                    HostStats(
                        cores=cores,
                        threads=threads,
                        load=(load,),
                        loadavg=(load / 2, load / 2 - 0.3, load / 2 - 0.5),
                        mem_total_mb=total,
                        mem_free_mb=free,
                    )
                )
            app.processEvents()
            if not hosts_window.grab().save(str(output / "host-monitor.png")):
                raise RuntimeError("Could not save Host Monitor screenshot")
            hosts_window.close()
            monitor.close()
            service.poller.stop()
        print("Rendered website screenshots with fictional data only.")


if __name__ == "__main__":
    main()
