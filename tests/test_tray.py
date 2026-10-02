"""The tray icon's menu, its status dot, and staying alive in the tray.

Driven against a stand-in for QSystemTrayIcon: an offscreen desktop has no
tray, and what matters here is what the plugin asks of one.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtGui import QIcon  # noqa: E402
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QWidget  # noqa: E402

from job_manager import notify  # noqa: E402
from job_manager.icon import plugin_icon  # noqa: E402
from job_manager.models import STATE_FAILED, STATE_PENDING, STATE_RUNNING, Job  # noqa: E402
from job_manager.presence import Presence  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from job_manager.tray import MENU_JOB_LIMIT, TrayController, menu_label, status_icon  # noqa: E402


def make_job(**kwargs) -> Job:
    defaults = {
        "host_id": "h1",
        "scheduler": "slurm",
        "state": STATE_PENDING,
        "auto_download": False,
    }
    defaults.update(kwargs)
    return Job(**defaults)


class TrayTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="tray_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tray = MagicMock()
        patcher = patch("job_manager.notify.QSystemTrayIcon", return_value=self.tray)
        self.tray_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.tray_class.isSystemTrayAvailable.return_value = True
        self.addCleanup(notify.shutdown)
        # The real flag, put back whatever a test leaves it at: a stuck False
        # would keep this test process's QApplication from ever quitting.
        original = self.app.quitOnLastWindowClosed()
        self.addCleanup(self.app.setQuitOnLastWindowClosed, original)
        self.actions = {
            "monitor": MagicMock(),
            "submit": MagicMock(),
            "host_monitor": MagicMock(),
            "select_job": MagicMock(),
        }

    def service(self, *jobs, **prefs) -> JobService:
        store = JobStore(self.tmp)
        store.jobs = {job.id: job for job in jobs}
        store.prefs.update(prefs)
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        return service

    def presence(self, service, main_window=None, opened=True) -> Presence:
        shown = Presence(service, main_window, self.actions)
        self.addCleanup(shown.detach)
        # As the plugin records it when a window is opened; most tests are
        # about a Job Manager the user has used.
        if opened and shown.tray is not None:
            shown.tray.mark_opened()
        self._shown = shown
        return shown

    def menu_texts(self, controller) -> list:
        controller._rebuild_menu()
        return [action.text() for action in controller.menu.actions()]

    def action(self, controller, text):
        controller._rebuild_menu()
        for action in controller.menu.actions():
            if action.text() == text:
                return action
        self.fail(f"no {text!r} in {self.menu_texts(controller)}")


class TestInstall(TrayTestCase):
    def test_the_icon_gets_a_menu(self):
        shown = self.presence(self.service())
        self.tray.setContextMenu.assert_called_with(shown.tray.menu)
        self.tray.show.assert_called()

    def test_without_a_tray_nothing_is_attempted(self):
        self.tray_class.isSystemTrayAvailable.return_value = False
        controller = TrayController(self.service(), None)
        self.assertFalse(controller.install())
        self.assertIsNone(controller.menu)

    def test_notifications_reuse_the_same_icon(self):
        self.presence(self.service())
        notify.notify("t", "m")
        self.assertEqual(self.tray_class.call_count, 1)
        self.tray.showMessage.assert_called_once()

    def test_detach_takes_the_menu_away(self):
        shown = self.presence(self.service())
        controller = shown.tray
        controller.detach()
        self.tray.setContextMenu.assert_called_with(None)
        self.assertIsNone(controller.menu)


class TestMenu(TrayTestCase):
    def test_it_offers_the_everyday_actions(self):
        shown = self.presence(self.service())
        texts = self.menu_texts(shown.tray)
        for text in (
            "Open Job Monitor",
            "New Job...",
            "Host Monitor...",
            "Refresh Now",
            "Quit MoleditPy",
        ):
            self.assertIn(text, texts)

    def test_the_header_says_what_is_running(self):
        shown = self.presence(self.service(make_job(state=STATE_RUNNING)))
        self.assertEqual(self.menu_texts(shown.tray)[0], "1 running")

    def test_an_idle_header(self):
        shown = self.presence(self.service())
        self.assertEqual(self.menu_texts(shown.tray)[0], "No active jobs")

    def test_actions_reach_the_plugin(self):
        shown = self.presence(self.service())
        self.action(shown.tray, "Open Job Monitor").trigger()
        self.action(shown.tray, "New Job...").trigger()
        self.action(shown.tray, "Host Monitor...").trigger()
        self.actions["monitor"].assert_called_once()
        self.actions["submit"].assert_called_once()
        self.actions["host_monitor"].assert_called_once()

    def test_refresh_asks_the_poller(self):
        service = self.service()
        shown = self.presence(service)
        with patch.object(service.poller, "refresh_now", return_value=True) as refresh:
            self.action(shown.tray, "Refresh Now").trigger()
        refresh.assert_called_once()

    def test_a_rate_limited_refresh_says_so(self):
        service = self.service()
        shown = self.presence(service)
        said = []
        service.message.connect(said.append)
        with patch.object(service.poller, "refresh_now", return_value=False):
            self.action(shown.tray, "Refresh Now").trigger()
        self.assertTrue(any("rate limited" in text for text in said))

    def test_the_job_list_selects_a_job(self):
        job = make_job(name="opt", host_name="myhost", state=STATE_RUNNING)
        shown = self.presence(self.service(job))
        submenu = self.action(shown.tray, "Active jobs (1)").menu()
        entry = submenu.actions()[0]
        self.assertEqual(entry.text(), "opt - running on myhost")

        entry.trigger()

        self.actions["select_job"].assert_called_once_with(job.id)

    def test_running_jobs_are_listed_first(self):
        shown = self.presence(
            self.service(
                make_job(name="a", state=STATE_PENDING),
                make_job(name="b", state=STATE_RUNNING),
            )
        )
        submenu = self.action(shown.tray, "Active jobs (2)").menu()
        self.assertTrue(submenu.actions()[0].text().startswith("b "))

    def test_a_long_list_is_cut_short(self):
        jobs = [make_job(name=f"j{i:02d}", state=STATE_RUNNING) for i in range(MENU_JOB_LIMIT + 3)]
        shown = self.presence(self.service(*jobs))
        submenu = self.action(shown.tray, f"Active jobs ({len(jobs)})").menu()
        self.assertEqual(len(submenu.actions()), MENU_JOB_LIMIT + 1)
        self.assertEqual(submenu.actions()[-1].text(), "3 more...")

    def test_no_jobs_disables_the_list(self):
        shown = self.presence(self.service())
        self.assertFalse(self.action(shown.tray, "Active jobs (0)").isEnabled())

    def test_an_ampersand_in_a_name_is_shown_not_underlined(self):
        self.assertEqual(menu_label("A&B"), "A&&B")

    def test_settings_open_from_the_menu(self):
        # The switches are in the Settings window, not duplicated here.
        self.actions["settings"] = MagicMock()
        shown = self.presence(self.service())
        texts = self.menu_texts(shown.tray)
        for gone in ("Notify me when a job ends", "Flash the task bar when a job ends"):
            self.assertNotIn(gone, texts)
        self.action(shown.tray, "Settings...").trigger()
        self.actions["settings"].assert_called_once()

    def test_an_entry_with_nothing_behind_it_is_greyed(self):
        # It used to be clickable and do nothing at all.
        self.actions.pop("host_monitor")
        shown = self.presence(self.service())
        self.assertFalse(self.action(shown.tray, "Host Monitor...").isEnabled())
        self.assertTrue(self.action(shown.tray, "Open Job Monitor").isEnabled())

    def test_a_moleditpy_that_will_not_start_is_said_so(self):
        shown = self.presence(self.service())
        shown.tray.relaunch = ["/opt/missing/moleditpy"]
        with patch("job_manager.handoff.spawn_detached", return_value=False):
            shown.tray.open_moleditpy()
        self.assertIn("could not be started", self.tray.showMessage.call_args[0][1])


class TestIconAndTooltip(TrayTestCase):
    def test_the_tooltip_counts_jobs(self):
        self.presence(self.service(make_job(state=STATE_RUNNING)))
        tooltip = self.tray.setToolTip.call_args[0][0]
        self.assertIn("MoleditPy", tooltip)
        self.assertIn("1 running", tooltip)

    def test_the_tooltip_mentions_an_unseen_failure(self):
        service = self.service()
        self.presence(service)
        service.job_finished.emit("x", STATE_FAILED)
        self.assertIn("1 failed", self.tray.setToolTip.call_args[0][0])

    def test_the_icon_only_changes_when_the_state_does(self):
        service = self.service(make_job(state=STATE_RUNNING))
        self.presence(service)
        calls = self.tray.setIcon.call_count
        service.jobs_changed.emit()
        self.assertEqual(self.tray.setIcon.call_count, calls)

    def test_left_click_opens_the_monitor(self):
        shown = self.presence(self.service())
        with patch("job_manager.tray.sys.platform", "win32"):
            shown.tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.actions["monitor"].assert_called_once()

    def test_on_macos_a_click_only_opens_the_menu(self):
        shown = self.presence(self.service())
        with patch("job_manager.tray.sys.platform", "darwin"):
            shown.tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.actions["monitor"].assert_not_called()

    def test_status_icons_are_drawn_at_every_size(self):
        base = plugin_icon()
        for state in ("busy", "queued", "error"):
            icon = status_icon(base, state)
            self.assertFalse(icon.isNull())
            sizes = {size.width() for size in icon.availableSizes()}
            self.assertTrue({16, 32}.issubset(sizes), sizes)
            # A dot, so it is not the same image as the plain icon.
            self.assertNotEqual(icon.pixmap(32, 32).toImage(), base.pixmap(32, 32).toImage())

    def test_idle_is_the_plain_icon(self):
        base = plugin_icon()
        self.assertIs(status_icon(base, "idle"), base)
        self.assertTrue(status_icon(QIcon(), "busy").isNull())


class TestKeepRunning(TrayTestCase):
    """The fallback, for a MoleditPy that cannot start a separate Python."""

    def setUp(self):
        super().setUp()
        patcher = patch("job_manager.handoff.can_hand_off", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main_window(self) -> QWidget:
        window = QWidget()
        self.addCleanup(window.deleteLater)
        window.show()
        return window

    def test_off_by_default_leaves_quitting_alone(self):
        self.app.setQuitOnLastWindowClosed(True)
        self.presence(self.service())
        self.assertTrue(self.app.quitOnLastWindowClosed())

    def test_on_it_stops_the_last_window_from_quitting(self):
        self.app.setQuitOnLastWindowClosed(True)
        self.presence(self.service(keep_running_in_tray=True))
        self.assertFalse(self.app.quitOnLastWindowClosed())

    def test_not_until_the_job_manager_is_opened(self):
        # Loaded, tracking, never opened: closing MoleditPy ends everything.
        self.app.setQuitOnLastWindowClosed(True)
        shown = self.presence(self.service(keep_running_in_tray=True), opened=False)
        self.assertTrue(self.app.quitOnLastWindowClosed())
        shown.tray.mark_opened()
        self.assertFalse(self.app.quitOnLastWindowClosed())

    def test_without_a_tray_it_does_not_apply(self):
        # A process with no window and no tray icon could never be quit.
        self.tray_class.isSystemTrayAvailable.return_value = False
        self.app.setQuitOnLastWindowClosed(True)
        self.presence(self.service(keep_running_in_tray=True))
        self.assertTrue(self.app.quitOnLastWindowClosed())

    def test_the_settings_tick_applies_at_once(self):
        from job_manager.settings_dialog import SettingsDialog

        self.app.setQuitOnLastWindowClosed(True)
        service = self.service()
        self.presence(service)
        dialog = SettingsDialog(service)
        self.addCleanup(dialog.deleteLater)
        import job_manager.presence as presence_module

        with patch.object(presence_module, "current", return_value=self._shown):
            dialog.chk_keep_tracking.setChecked(True)
            self.assertTrue(service.store.get_pref("keep_running_in_tray"))
            self.assertFalse(self.app.quitOnLastWindowClosed())

            dialog.chk_keep_tracking.setChecked(False)
            self.assertTrue(self.app.quitOnLastWindowClosed())

    def test_detach_restores_quitting_and_brings_the_window_back(self):
        self.app.setQuitOnLastWindowClosed(True)
        main = self.main_window()
        shown = self.presence(self.service(keep_running_in_tray=True), main_window=main)
        main.hide()

        shown.detach()

        self.assertTrue(self.app.quitOnLastWindowClosed())
        self.assertTrue(main.isVisible())

    def test_switching_it_off_while_hidden_brings_the_window_back(self):
        main = self.main_window()
        service = self.service(keep_running_in_tray=True)
        shown = self.presence(service, main_window=main)
        main.hide()
        service.store.set_pref("keep_running_in_tray", False)

        shown.tray.apply_keep_running()

        self.assertTrue(main.isVisible())

    def test_closing_the_main_window_says_where_the_plugin_went(self):
        main = self.main_window()
        shown = self.presence(self.service(keep_running_in_tray=True), main_window=main)
        main.close()
        shown.tray._after_main_close()
        shown.tray._after_main_close()
        messages = [c for c in self.tray.showMessage.call_args_list if "Still tracking" in c[0][1]]
        self.assertEqual(len(messages), 1)

    def test_the_close_is_noticed_through_the_event_filter(self):
        main = self.main_window()
        shown = self.presence(self.service(keep_running_in_tray=True), main_window=main)
        with patch("job_manager.tray.QTimer.singleShot") as later:
            self.assertFalse(shown.tray.eventFilter(main, QEvent(QEvent.Type.Close)))
        later.assert_called_once()

    def test_a_hidden_main_window_offers_to_come_back(self):
        main = self.main_window()
        shown = self.presence(self.service(), main_window=main)
        self.assertNotIn("Show MoleditPy", self.menu_texts(shown.tray))
        main.hide()
        self.app.setProperty("moleditpy_shutting_down", True)

        self.action(shown.tray, "Show MoleditPy").trigger()

        self.assertTrue(main.isVisible())
        self.assertFalse(self.app.property("moleditpy_shutting_down"))


class TestHandOff(TrayTestCase):
    """MoleditPy quits for real, and a process of its own takes the jobs over."""

    def setUp(self):
        super().setUp()
        patcher = patch("job_manager.handoff.can_hand_off", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        spawn = patch("job_manager.handoff.spawn_detached", return_value=True)
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)

    def test_quitting_is_left_alone(self):
        # MoleditPy is meant to quit; holding its last window open would bring
        # back the hidden-window behaviour this replaces.
        self.app.setQuitOnLastWindowClosed(True)
        self.presence(self.service(keep_running_in_tray=True))
        self.assertTrue(self.app.quitOnLastWindowClosed())

    def test_quitting_with_jobs_running_starts_the_tray_process(self):
        shown = self.presence(
            self.service(make_job(state=STATE_RUNNING), keep_running_in_tray=True)
        )
        with patch("job_manager.handoff.relaunch_command", return_value=["moleditpy"]):
            shown.tray._on_about_to_quit()
        self.spawn.assert_called_once()
        command = self.spawn.call_args[0][0]
        self.assertTrue(command[1].endswith("__main__.py"))
        self.assertIn("--tray", command)
        self.assertIn('["moleditpy"]', command)

    def test_a_job_manager_never_opened_quits_with_moleditpy(self):
        shown = self.presence(
            self.service(make_job(state=STATE_RUNNING), keep_running_in_tray=True), opened=False
        )
        shown.tray._on_about_to_quit()
        self.spawn.assert_not_called()

    def test_with_only_if_opened_off_it_hands_off_unopened(self):
        service = self.service(make_job(state=STATE_RUNNING), keep_running_in_tray=True)
        service.store.set_pref("keep_running_only_if_opened", False)
        shown = self.presence(service, opened=False)
        with patch("job_manager.handoff.relaunch_command", return_value=["moleditpy"]):
            shown.tray._on_about_to_quit()
        self.spawn.assert_called_once()

    def test_it_is_wired_to_the_application_quitting(self):
        shown = self.presence(
            self.service(make_job(state=STATE_RUNNING), keep_running_in_tray=True)
        )
        self.app.aboutToQuit.emit()
        self.spawn.assert_called_once()
        shown.detach()
        self.app.aboutToQuit.emit()
        self.spawn.assert_called_once()

    def test_it_stays_with_no_job_running_too(self):
        # Asked to keep running, it does: the tray menu and the web view are
        # still worth having with the queue empty.
        shown = self.presence(self.service(keep_running_in_tray=True))
        with patch("job_manager.handoff.relaunch_command", return_value=["moleditpy"]):
            shown.tray._on_about_to_quit()
        self.spawn.assert_called_once()

    def test_the_option_off_means_nothing_is_started(self):
        shown = self.presence(self.service(make_job(state=STATE_RUNNING)))
        shown.tray._on_about_to_quit()
        self.spawn.assert_not_called()

    def test_quit_from_the_tray_means_quit_everything(self):
        main = MagicMock()
        main.isVisible.return_value = False
        shown = self.presence(
            self.service(make_job(state=STATE_RUNNING), keep_running_in_tray=True), main_window=main
        )
        with patch.object(QApplication, "quit"):
            shown.tray.quit_application()
        shown.tray._on_about_to_quit()
        self.spawn.assert_not_called()

    def test_a_cancelled_quit_does_not_disarm_the_hand_off(self):
        main = MagicMock()
        main.isVisible.return_value = True
        main.close.return_value = False
        shown = self.presence(
            self.service(make_job(state=STATE_RUNNING), keep_running_in_tray=True), main_window=main
        )
        shown.tray.quit_application()
        shown.tray._on_about_to_quit()
        self.spawn.assert_called_once()

    def test_no_still_running_note_from_a_process_that_is_quitting(self):
        main = MagicMock()
        main.isVisible.return_value = False
        shown = self.presence(self.service(keep_running_in_tray=True), main_window=main)
        shown.tray._after_main_close()
        messages = [c for c in self.tray.showMessage.call_args_list if "Still tracking" in c[0][1]]
        self.assertEqual(messages, [])

    def test_the_menu_has_no_show_moleditpy(self):
        main = MagicMock()
        main.isVisible.return_value = False
        shown = self.presence(self.service(keep_running_in_tray=True), main_window=main)
        self.assertNotIn("Show MoleditPy", self.menu_texts(shown.tray))


class TestTheStandaloneMenu(TrayTestCase):
    def standalone(self, relaunch=None):
        service = self.service()
        shown = Presence(service, None, self.actions, standalone=True, relaunch=relaunch)
        self.addCleanup(shown.detach)
        return shown

    def test_it_quits_itself_not_moleditpy(self):
        texts = self.menu_texts(self.standalone(["moleditpy"]).tray)
        self.assertIn("Quit Job Manager", texts)
        self.assertNotIn("Quit MoleditPy", texts)
        self.assertIn("Settings...", texts)

    def test_it_can_start_moleditpy_again(self):
        shown = self.standalone(["moleditpy", "--flag"])
        with patch("job_manager.handoff.spawn_detached") as spawn:
            self.action(shown.tray, "Open MoleditPy").trigger()
        spawn.assert_called_once_with(["moleditpy", "--flag"])

    def test_without_a_way_back_it_does_not_offer_one(self):
        self.assertNotIn("Open MoleditPy", self.menu_texts(self.standalone([]).tray))

    def test_it_never_hands_off_again(self):
        shown = self.standalone()
        self.assertFalse(shown.tray.hands_off())
        with patch("job_manager.handoff.spawn_detached") as spawn:
            shown.tray._on_about_to_quit()
        spawn.assert_not_called()

    def test_its_quit_quits(self):
        shown = self.standalone()
        with patch.object(QApplication, "quit") as quit_app:
            self.action(shown.tray, "Quit Job Manager").trigger()
        quit_app.assert_called_once()


class TestQuit(TrayTestCase):
    def test_quit_goes_through_the_main_windows_close(self):
        main = MagicMock()
        main.isVisible.side_effect = [True, False]
        main.close.return_value = True
        shown = self.presence(self.service(), main_window=main)
        main.isVisible.side_effect = [True, False]
        with patch.object(QApplication, "quit") as quit_app:
            shown.tray.quit_application()
        main.close.assert_called_once()
        quit_app.assert_called_once()

    def test_a_refused_close_does_not_quit(self):
        # "Cancel" on the unsaved-changes question must mean cancel.
        main = MagicMock()
        main.isVisible.return_value = True
        main.close.return_value = False
        shown = self.presence(self.service(), main_window=main)
        with patch.object(QApplication, "quit") as quit_app:
            shown.tray.quit_application()
        quit_app.assert_not_called()

    def test_with_the_window_already_closed_it_just_quits(self):
        main = MagicMock()
        main.isVisible.return_value = False
        shown = self.presence(self.service(), main_window=main)
        with patch.object(QApplication, "quit") as quit_app:
            shown.tray.quit_application()
        main.close.assert_not_called()
        quit_app.assert_called_once()


if __name__ == "__main__":
    unittest.main()
