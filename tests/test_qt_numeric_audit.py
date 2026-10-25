"""Actual service callbacks with synthetic jobs and no network."""

from unittest.mock import patch


from .test_service import ServiceTestCase


class TestAudit(ServiceTestCase):
    def test_huge_saved_host_integers_do_not_overflow_qt(self):
        from job_manager.hosts_dialog import HostsDialog

        self.host.port = 10**40
        dialog = HostsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog._load_selected()
        assert dialog.spin_port.value() == dialog.spin_port.maximum()

    def test_large_input_cpu_request_is_not_silently_clamped(self):
        from job_manager.submit_dialog import SubmitDialog

        with open(self.input_path, "w", encoding="utf-8") as handle:
            handle.write("! HF\n%pal nprocs 2048 end\n")
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog._apply_scanned_resources(self.input_path)
        assert dialog.spin_cpus.value() == 2048

    def test_scanned_huge_integer_does_not_overflow_qt(self):
        from job_manager.submit_dialog import SubmitDialog

        with open(self.input_path, "w", encoding="utf-8") as handle:
            handle.write("! HF\n%pal nprocs " + str(10**40) + " end\n")
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog._apply_scanned_resources(self.input_path)
        assert "range" in dialog.lbl_scanned.text().lower()

    def test_out_of_range_cpu_request_cannot_be_submitted(self):
        from job_manager.submit_dialog import SubmitDialog

        with open(self.input_path, "w", encoding="utf-8") as handle:
            handle.write("! HF\n%pal nprocs " + str(10**40) + " end\n")
        dialog = SubmitDialog(self.service)
        dialog.add_files([self.input_path])
        self.addCleanup(dialog.deleteLater)
        with (
            patch("job_manager.submit_dialog.QMessageBox.warning") as warning,
            patch.object(self.service, "submit") as submit,
        ):
            dialog._submit()
        warning.assert_called_once()
        submit.assert_not_called()

    def test_monitor_interval_cannot_overflow_qt_timer(self):
        from job_manager.host_sampler import HostSampler

        sampler = HostSampler(self.service)
        self.addCleanup(sampler.deleteLater)
        self.store.set_pref("host_monitor_interval", 10**40)
        self.host.monitor_interval = 10**40
        assert sampler.interval_seconds() == 3600
        assert sampler.tick_seconds() == 3600
