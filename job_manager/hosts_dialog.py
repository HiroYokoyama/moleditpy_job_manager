"""Host profile editor with a Test Connection button."""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION
from .credentials import ensure_password, needs_password
from .host_profile_form import (
    EQUAL_PATH_TIP as EQUAL_PATH_TIP,
)
from .host_profile_form import (
    KEY_TIP as KEY_TIP,
)
from .host_profile_form import HostProfileForm
from .models import (
    BACKEND_LOCAL,
    BACKEND_PARAMIKO,
    BACKEND_WSL,
    MODE_LANES,
    MODE_RUNNER,
    SCHEDULER_SHELL,
    SCHEDULER_WINDOWS,
    HostProfile,
)
from .runner import apply_queue_limits, probe_resources, queue_paused, set_queue_paused
from .service import JobService
from .tasks import run_async
from .theme import apply_theme
from .transport import local_shell_available, paramiko_available, wsl_available
from .transport.base import HostKeyRejected
from .transport.local import INSTALL_HINT as LOCAL_INSTALL_HINT
from .transport.local import POWERSHELL_HINT, SHELL_POSIX, SHELL_POWERSHELL
from .window_utils import make_independent


def _running_on_windows() -> bool:
    """Indirection so a test can pretend otherwise without touching ``sys``.

    ``sys`` here is the one real, process-wide module: patching its
    ``platform`` attribute changes it for Qt and for every test sharing the
    interpreter, which under xdist crashed whole workers rather than failing
    one assertion.
    """
    return sys.platform == "win32"


class HostsDialog(QDialog):
    """Create, edit and remove host profiles."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.store = service.store
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Hosts")
        make_independent(self)
        apply_theme(self)
        # Wide enough for the list and a form that is not squeezed, tall enough
        # that Connection and Advanced are both on screen -- but never taller
        # than the screen, since the column scrolls anyway.
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        self.resize(
            min(900, available.width() - 80) if available else 900,
            min(720, int(available.height() * 0.9)) if available else 720,
        )
        self._current: Optional[HostProfile] = None
        #: True while the pause box is being set to match the host, so that
        #: showing a state does not ask the host to change to it.
        self._syncing_pause = False
        #: The host whose queue state has already been read, so that a save --
        #: which reloads and re-selects -- does not ask the host again.
        self._queue_state_for = ""
        #: The selected profile as it was when it was loaded or last saved.
        #: Anything else in the form is an unsaved edit.
        self._loaded: dict = {}
        #: True while the list is being rebuilt. Saving reloads the list, which
        #: re-selects, which lands back in _load_selected -- so without this the
        #: answer to "save your changes?" asked the question again, and again.
        self._reloading = False
        self._build_ui()
        self._reload_list()

    # --- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)

        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.currentItemChanged.connect(lambda *_: self._load_selected())
        left.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._add_host)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_host)
        buttons.addWidget(add)
        buttons.addWidget(self.btn_remove)
        left.addLayout(buttons)
        outer.addLayout(left, 1)

        # The editing column scrolls: Connection, Advanced and the queue row
        # together are taller than a laptop screen, and Save must stay reachable.
        callbacks = {
            "_apply_queue_limits": self._apply_queue_limits,
            "_detect_resources": self._detect_resources,
            "_on_detect_toggled": self._on_detect_toggled,
            "_on_pause_toggled": self._on_pause_toggled,
            "_suggest_local_scheduler": self._suggest_local_scheduler,
            "_update_backend_hint": self._update_backend_hint,
            "_update_concurrency_row": self._update_concurrency_row,
        }
        self.profile_form = HostProfileForm(callbacks, self)
        right_panel = self.profile_form
        right = self.profile_form.layout()
        self.adv_box = self.profile_form.adv_box
        self.btn_apply_limits = self.profile_form.btn_apply_limits
        self.btn_detect = self.profile_form.btn_detect
        self.chk_ask_password = self.profile_form.chk_ask_password
        self.chk_detect_resources = self.profile_form.chk_detect_resources
        self.chk_enabled = self.profile_form.chk_enabled
        self.chk_load_profile = self.profile_form.chk_load_profile
        self.chk_monitor_usage = self.profile_form.chk_monitor_usage
        self.chk_pause = self.profile_form.chk_pause
        self.cmb_backend = self.profile_form.cmb_backend
        self.cmb_concurrency = self.profile_form.cmb_concurrency
        self.cmb_distro = self.profile_form.cmb_distro
        self.cmb_scheduler = self.profile_form.cmb_scheduler
        self.form_box = self.profile_form.form_box
        self.lbl_backend_hint = self.profile_form.lbl_backend_hint
        self.lbl_key_tip = self.profile_form.lbl_key_tip
        self.lbl_queue = self.profile_form.lbl_queue
        self.monitor_box = self.profile_form.monitor_box
        self.queue_box = self.profile_form.queue_box
        self.row_distro_label = self.profile_form.row_distro_label
        self.spin_command_timeout = self.profile_form.spin_command_timeout
        self.spin_connect_timeout = self.profile_form.spin_connect_timeout
        self.spin_max_concurrent = self.profile_form.spin_max_concurrent
        self.spin_monitor_interval = self.profile_form.spin_monitor_interval
        self.spin_poll_interval = self.profile_form.spin_poll_interval
        self.spin_port = self.profile_form.spin_port
        self.spin_runner_cores = self.profile_form.spin_runner_cores
        self.spin_runner_memory = self.profile_form.spin_runner_memory
        self.txt_equal_path = self.profile_form.txt_equal_path
        self.txt_hostname = self.profile_form.txt_hostname
        self.txt_jump = self.profile_form.txt_jump
        self.txt_key = self.profile_form.txt_key
        self.txt_login = self.profile_form.txt_login
        self.txt_name = self.profile_form.txt_name
        self.txt_options = self.profile_form.txt_options
        self.txt_remote_root = self.profile_form.txt_remote_root
        self.txt_submit_options = self.profile_form.txt_submit_options
        self.txt_username = self.profile_form.txt_username

        action_row = QHBoxLayout()
        self.btn_test = QPushButton("Test Connection")
        self.btn_test.clicked.connect(self._test_connection)
        self.lbl_test = QLabel("")
        self.lbl_test.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_test.setWordWrap(True)
        action_row.addWidget(self.btn_test)
        action_row.addWidget(self.lbl_test, 1)
        right.addLayout(action_row)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        self.btn_save = box.button(QDialogButtonBox.StandardButton.Save)
        # Through a lambda: clicked carries a `checked` bool, which would
        # arrive as the first positional argument -- reload=False -- and the
        # list would silently stop refreshing after a save.
        self.btn_save.clicked.connect(lambda: self._save_current())
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)

        column = QVBoxLayout()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(right_panel)
        column.addWidget(scroll, 1)
        # Outside the scroll area: Save and Close stay put however far it scrolls.
        column.addWidget(box)
        outer.addLayout(column, 2)
        # After every widget exists: this one now also shows or hides the queue
        # controls, which are built further down than the rows that drive it.
        self._update_concurrency_row()
        self._update_backend_hint()

    # --- list handling ------------------------------------------------------

    def _reload_list(self, select_id: str = "") -> None:
        self._reloading = True
        try:
            self._reload_list_now(select_id)
        finally:
            self._reloading = False

    def _reload_list_now(self, select_id: str = "") -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for host in self.store.host_list():
            label = f"{host.name}  ({host.target})"
            if not host.enabled:
                label += "  [disabled]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, host.id)
            if not host.enabled:
                # A plain grey foreground, not setEnabled(False): the item
                # must stay selectable so a disabled host can still be
                # re-enabled from here.
                from PyQt6.QtGui import QColor

                item.setForeground(QColor("#8b949e"))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.list.count():
            target_row = 0
            if select_id:
                for row in range(self.list.count()):
                    if self.list.item(row).data(Qt.ItemDataRole.UserRole) == select_id:
                        target_row = row
                        break
            self.list.setCurrentRow(target_row)
        else:
            self._current = None
            self._clear_form()

    def _selected_host(self) -> Optional[HostProfile]:
        item = self.list.currentItem()
        if item is None:
            return None
        return self.store.hosts.get(item.data(Qt.ItemDataRole.UserRole))

    def _set_editor_enabled(self, enabled: bool) -> None:
        """Nothing is editable until a host is selected.

        Everything the form collects belongs to the selected profile, so with
        no selection there is nowhere for a keystroke to go: Save and Test
        Connection both found no host and returned in silence, which reads as
        the buttons being broken rather than as nothing being selected.
        """
        for widget in (
            self.form_box,
            self.adv_box,
            self.monitor_box,
            self.chk_ask_password,
            self.queue_box,
            self.btn_test,
            self.btn_save,
            self.btn_remove,
        ):
            widget.setEnabled(enabled)

    def _clear_form(self) -> None:
        self.chk_enabled.setChecked(True)
        self.txt_name.setText("")
        self.txt_hostname.setText("")
        self.txt_username.setText("")
        self.spin_port.setValue(22)
        self.txt_key.setText("")
        self.cmb_distro.setCurrentText("")
        self.txt_jump.setText("")
        self.txt_remote_root.setText("~/moleditpy_jobs")
        self.txt_equal_path.setText("")
        self.spin_max_concurrent.setValue(0)
        self.cmb_concurrency.setCurrentIndex(max(0, self.cmb_concurrency.findData(MODE_RUNNER)))
        self.spin_runner_cores.setValue(self.spin_runner_cores.minimum())
        self.spin_runner_memory.setValue(self.spin_runner_memory.minimum())
        self.chk_detect_resources.setChecked(False)
        self.chk_load_profile.setChecked(True)
        self.txt_login.setPlainText("")
        self.txt_options.setPlainText("")
        self.spin_connect_timeout.setValue(10)
        self.spin_command_timeout.setValue(60)
        self.txt_submit_options.setText("")
        self.chk_monitor_usage.setChecked(True)
        self.spin_monitor_interval.setValue(0)
        self.spin_poll_interval.setValue(0)
        self.chk_ask_password.setChecked(False)
        self._set_pause_checkbox(False)
        self.lbl_queue.setText("")
        self._set_editor_enabled(False)
        self.lbl_test.setText("No host selected - press Add to create one.")

    def _load_selected(self) -> None:
        if not self._reloading and not self._confirm_discard():
            # Put the selection back on the host whose edits were kept, without
            # reloading the form over the top of them.
            self._reselect_current()
            return
        host = self._selected_host()
        self._current = host
        if host is None:
            self._clear_form()
            return
        self._set_editor_enabled(True)
        self.chk_enabled.setChecked(bool(host.enabled))
        self.txt_name.setText(host.name)
        self.txt_hostname.setText(host.hostname)
        self.txt_username.setText(host.username)
        self.spin_port.setValue(int(host.port or 22))
        # Blocked: setting this while restoring a saved host must not trigger
        # _suggest_local_scheduler, or a host someone deliberately set up with
        # backend=local + scheduler=shell would have its scheduler silently
        # rewritten to the PowerShell one every time this dialog opens.
        self.cmb_backend.blockSignals(True)
        index = self.cmb_backend.findData(host.backend)
        self.cmb_backend.setCurrentIndex(max(0, index))
        self.cmb_backend.blockSignals(False)
        index = self.cmb_scheduler.findData(host.scheduler)
        self.cmb_scheduler.setCurrentIndex(max(0, index))
        self.txt_key.setText(host.key_path)
        self.cmb_distro.setCurrentText(host.wsl_distro or "")
        self.txt_jump.setText(host.jump_host)
        self.txt_remote_root.setText(host.remote_root)
        self.txt_equal_path.setText(host.equal_path or "")
        self.spin_max_concurrent.setValue(max(0, int(host.max_concurrent or 0)))
        index = self.cmb_concurrency.findData(host.concurrency_mode or MODE_LANES)
        self.cmb_concurrency.setCurrentIndex(max(0, index))
        self.chk_detect_resources.setChecked(bool(host.runner_detect))
        # A detecting host stores 0 for both, so the boxes show their minimum
        # rather than a budget it never had.
        self.spin_runner_cores.setValue(max(1, int(host.runner_cores or 0)))
        # Stored in MB, shown in GB: nobody sizes a machine in megabytes.
        self.spin_runner_memory.setValue(max(1, int(host.runner_memory_mb or 0) // 1024))
        self.chk_load_profile.setChecked(bool(host.load_profile))
        self._update_concurrency_row()
        self.txt_login.setPlainText("\n".join(host.login_commands or []))
        self.txt_options.setPlainText("\n".join(host.ssh_options or []))
        self.spin_connect_timeout.setValue(int(host.connect_timeout or 10))
        self.spin_command_timeout.setValue(int(host.command_timeout or 60))
        self.txt_submit_options.setText(host.submit_options or "")
        self.chk_monitor_usage.setChecked(bool(host.monitor_usage))
        self.spin_monitor_interval.setValue(max(0, int(host.monitor_interval or 0)))
        self.spin_poll_interval.setValue(max(0, int(host.poll_interval or 0)))
        self.spin_monitor_interval.setEnabled(self.chk_monitor_usage.isChecked())
        self.chk_ask_password.setChecked(bool(host.ask_password))
        # Explicitly, not only from the combo's signal: selecting a host whose
        # backend matches the one already shown changes no index, and the box
        # would keep the state the previous host left it in.
        self._update_backend_hint()
        self.lbl_test.setText("")
        self._loaded = self._snapshot()
        self._refresh_queue_state()

    def _snapshot(self) -> dict:
        """What the form holds now, as the profile it would save."""
        if self._current is None:
            return {}
        # Collected onto a copy: _collect writes into the profile it is given,
        # and asking "has this changed?" must not be what changes it.
        from copy import deepcopy

        return self._collect(deepcopy(self._current)).to_dict()

    def _is_dirty(self) -> bool:
        if self._current is None or not self.form_box.isEnabled():
            return False
        return self._snapshot() != self._loaded

    def _confirm_discard(self) -> bool:
        """Ask before losing edits. False means the user wants to keep them.

        Only for a window on screen. A dialog that was never shown cannot have
        been closed by anybody, and a modal question raised from tear-down --
        or from a test -- is one nobody is there to answer.
        """
        if not self.isVisible() or not self._is_dirty():
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"'{self._current.name}' has changes that are not saved.\n\nSave them?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            # Without reloading: the list has already moved to the host the
            # user clicked, and re-selecting the saved one would take it back.
            self._save_current(reload=False)
            return True
        return answer == QMessageBox.StandardButton.Discard

    def _reselect_current(self) -> None:
        """Move the list selection back without reloading the form."""
        if self._current is None:
            return
        self.list.blockSignals(True)
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole) == self._current.id:
                self.list.setCurrentRow(row)
                break
        self.list.blockSignals(False)

    # No closeEvent override on purpose. QDialog's own calls reject(), then
    # ignores the event if the dialog is still visible -- which is exactly the
    # veto one here was written to perform, and asking first meant asking
    # twice: closing a dirty profile and choosing Discard put the same question
    # straight back up, because discarding leaves the form dirty and the
    # delegated reject() then asked about it again.

    def accept(self) -> None:
        if not self._confirm_discard():
            return
        super().accept()

    def reject(self) -> None:
        # Esc closes a dialog without a closeEvent, which is exactly the way a
        # profile gets typed in and lost.
        if not self._confirm_discard():
            return
        super().reject()

    def _add_host(self) -> None:
        host = HostProfile(name="new host", remote_root="~/moleditpy_jobs")
        self.store.add_host(host)
        self._reload_list(select_id=host.id)

    def _remove_host(self) -> None:
        host = self._selected_host()
        if host is None:
            return
        confirm = QMessageBox.question(
            self,
            "Remove host",
            f"Remove '{host.name}'? Presets for this host are removed too.\n"
            "Jobs already submitted stay in the list but can no longer be polled.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_host(host.id)
        # Do not keep a secret for a host that no longer exists.
        self.service.set_password(host.id, "")
        self._reload_list()

    # --- editing ------------------------------------------------------------

    def _browse_key(self) -> None:
        return self.profile_form._browse_key()

    def _browse_equal_path(self) -> None:
        return self.profile_form._browse_equal_path()

    def _update_concurrency_row(self) -> None:
        """The helper is only offered where nothing else is scheduling."""
        # Both no-queue schedulers have a runner; a real cluster does not need
        # one and should not be offered it.
        shell = self.cmb_scheduler.currentData() in (SCHEDULER_SHELL, SCHEDULER_WINDOWS)
        self.cmb_concurrency.setEnabled(shell)
        if not shell and self.cmb_concurrency.currentData() == MODE_RUNNER:
            self.cmb_concurrency.setCurrentIndex(self.cmb_concurrency.findData(MODE_LANES))
        runner = shell and self.cmb_concurrency.currentData() == MODE_RUNNER
        detect = self.chk_detect_resources.isChecked()
        self.chk_detect_resources.setEnabled(runner)
        # Grey rather than hidden while detecting: the numbers are still worth
        # seeing, and pressing Detect fills them in without handing over.
        self.spin_runner_cores.setEnabled(runner and not detect)
        self.spin_runner_memory.setEnabled(runner and not detect)
        # Nothing to hold or to send limits to unless there is a helper.
        self.queue_box.setVisible(runner)

    def _on_detect_toggled(self, checked: bool) -> None:
        """Detection is opt-in, so it only greys the fields it takes over."""
        self._update_concurrency_row()
        if checked:
            self.lbl_queue.setText("The helper will read the machine's own cores and memory.")

    def _reload_distributions(self) -> None:
        """Offer the distributions that are installed, keeping what is typed."""
        from .transport.wsl import list_distributions

        current = self.cmb_distro.currentText().strip()
        self.cmb_distro.blockSignals(True)
        self.cmb_distro.clear()
        self.cmb_distro.addItem("")
        for name in list_distributions():
            self.cmb_distro.addItem(name)
        self.cmb_distro.setCurrentText(current)
        self.cmb_distro.blockSignals(False)

    def _suggest_local_scheduler(self) -> None:
        """Nudge a fresh switch to "This machine" toward the scheduler that
        needs nothing installed.

        The bash-based scheduler is what ``HostProfile`` defaults to, and on
        Windows that means Git Bash or WSL has to already be on the machine --
        the plugin's own PowerShell scheduler needs neither, and exists
        precisely for this case, but a user who never opens the Scheduler
        dropdown never finds it, and instead hits a shell-not-found or (worse,
        where Git Bash unpredictably $HOME's it) a "no such file or directory"
        the first time they submit. Suggesting it here, once, at the moment
        the backend becomes local, is cheap and easy to override.

        Only on an actual change to *this* combo: ``_load_selected`` blocks
        its signals while restoring a saved host, so reopening one someone
        deliberately set up with the bash scheduler is never rewritten under
        them.
        """
        if not _running_on_windows():
            return
        if self.cmb_backend.currentData() != BACKEND_LOCAL:
            return
        if self.cmb_scheduler.currentData() != SCHEDULER_SHELL:
            return
        index = self.cmb_scheduler.findData(SCHEDULER_WINDOWS)
        if index >= 0:
            self.cmb_scheduler.setCurrentIndex(index)

    def _update_backend_hint(self) -> None:
        backend = self.cmb_backend.currentData()
        local = backend in (BACKEND_LOCAL, BACKEND_WSL)
        self._set_ssh_fields_enabled(not local)
        self.cmb_distro.setVisible(backend == BACKEND_WSL)
        self.row_distro_label.setVisible(backend == BACKEND_WSL)
        if backend == BACKEND_WSL:
            self._reload_distributions()
            if not wsl_available():
                from .transport.wsl import INSTALL_HINT as WSL_HINT

                self.lbl_backend_hint.setText(WSL_HINT)
            else:
                self.lbl_backend_hint.setText(
                    "Runs the job inside WSL. Remote root is a Linux path there "
                    "(/home/you/jobs), and input files are copied across for you."
                )
            self.txt_equal_path.setEnabled(False)
            self.lbl_key_tip.setVisible(False)
            self.chk_ask_password.setEnabled(False)
            return
        # A local host's remote root already is a path on this machine, so a
        # second local path standing in for it would be the same directory
        # under two names. The host uses its own root for everything equal_path
        # buys a remote one (HostProfile.local_root), so there is nothing to
        # fill in -- which the greyed box now says, rather than leaving it
        # looking like a field that ought to work and does not.
        self.txt_equal_path.setEnabled(backend != BACKEND_LOCAL)
        self.txt_equal_path.setToolTip(
            "Not needed for a host that is this machine: its Remote root above "
            "is already a directory here, so results open from it directly and "
            "an input saved inside it selects this host by itself."
            if backend == BACKEND_LOCAL
            else EQUAL_PATH_TIP
        )
        # Only where a password is actually on offer; the other backends never
        # ask for one, so the advice would be noise.
        self.lbl_key_tip.setVisible(backend == BACKEND_PARAMIKO)
        # And the box itself is live only there: OpenSSH runs in batch mode and
        # cannot do password authentication at all, so ticking it there looked
        # like a choice and did nothing.
        self.chk_ask_password.setEnabled(backend == BACKEND_PARAMIKO)
        if backend == BACKEND_LOCAL:
            # Which shell has to be there follows the scheduler: a Windows host
            # is driven entirely through PowerShell and needs no bash at all.
            kind = (
                SHELL_POWERSHELL
                if self.cmb_scheduler.currentData() == SCHEDULER_WINDOWS
                else SHELL_POSIX
            )
            if local_shell_available(kind):
                self.lbl_backend_hint.setText(
                    "Runs the job here, with no network at all. Remote root is a "
                    "directory on this machine; hostname and keys are not used."
                )
            elif kind == SHELL_POWERSHELL:
                self.lbl_backend_hint.setText(POWERSHELL_HINT)
            else:
                self.lbl_backend_hint.setText(LOCAL_INSTALL_HINT)
            return
        if self.cmb_scheduler.currentData() == SCHEDULER_WINDOWS:
            self.lbl_backend_hint.setText(
                "A Windows machine over SSH. Commands are sent as encoded "
                "PowerShell, so the server's default SSH shell may be either "
                "cmd or PowerShell."
            )
            return
        if backend == BACKEND_PARAMIKO and not paramiko_available():
            self.lbl_backend_hint.setText(
                "paramiko is not installed - run 'pip install paramiko' to use this backend."
            )
        elif backend == BACKEND_PARAMIKO:
            self.lbl_backend_hint.setText(
                "Authenticates with the private key above (or ~/.ssh's default "
                "keys if none is set), an ssh-agent, or a password -- and keeps "
                "one SSH session open for the whole session, which is what the "
                "live host panel samples through. A password is held in memory "
                "for this session only and never written to disk."
            )
        else:
            self.lbl_backend_hint.setText(
                "Uses your ~/.ssh/config, agent and keys. Batch mode: password logins are not "
                "possible with this backend."
            )

    def _set_ssh_fields_enabled(self, enabled: bool) -> None:
        """Nothing about the network applies when the host is this machine."""
        for widget in (
            self.txt_hostname,
            self.txt_username,
            self.spin_port,
            self.txt_key,
            self.txt_jump,
            self.spin_connect_timeout,
        ):
            widget.setEnabled(enabled)

    def _collect(self, host: HostProfile) -> HostProfile:
        return self.profile_form._collect(host)

    def _save_current(self, reload: bool = True) -> Optional[HostProfile]:
        """Write the form to the selected profile.

        ``reload`` rebuilds the list and re-selects the saved host, which is
        right for the Save button and wrong when the save is happening because
        the user is on their way to a *different* host: re-selecting would
        undo the click that started it.
        """
        host = self._current or self._selected_host()
        if host is None:
            return None
        self._collect(host)
        self.store.add_host(host)
        self._reschedule_polling()
        # Before the reload, not after: reloading re-selects, and a stale
        # snapshot at that moment is what made Save ask to save again.
        self._loaded = self._snapshot()
        if reload:
            self._reload_list(select_id=host.id)
        # After the reload, which re-selects and so clears this label. Saving
        # was silent before, which is indistinguishable from a Save that did
        # nothing at all.
        self.lbl_test.setText(f"Saved '{host.name}'.")
        return host

    def _reschedule_polling(self) -> None:
        """A changed per-host interval takes effect now, not after a restart."""
        poller = getattr(self.service, "poller", None)
        if poller is not None and hasattr(poller, "reschedule"):
            poller.reschedule()

    def _persist_current(self) -> Optional[HostProfile]:
        """Apply the form to the selected profile without rebuilding the list.

        ``_save_current`` reloads the list, and reloading re-selects, which
        reloads the form: harmless for a one-shot connection test, but it would
        fight a control whose state is being read back from the host.
        """
        host = self._current or self._selected_host()
        if host is None:
            return None
        self._collect(host)
        self.store.save_settings()
        return host

    # --- the queue on the host ----------------------------------------------

    def _set_pause_checkbox(self, paused: bool) -> None:
        """Show a state without asking the host to change to it."""
        self._syncing_pause = True
        try:
            self.chk_pause.setChecked(bool(paused))
        finally:
            self._syncing_pause = False

    def _refresh_queue_state(self) -> None:
        """Read whether the selected host's queue is held.

        One small command, and only when a host that has a queue is selected in
        a dialog the user opened deliberately. A host that would pop a password
        prompt is left alone: a dialog appearing because you clicked a name in
        a list is not something anyone asked for.
        """
        host = self._current
        if host is not None and host.id == self._queue_state_for:
            # Already asked for this host. Saving the profile reloads the list,
            # which re-selects, which lands here -- so without this, pressing
            # Save or Test Connection put another command on the wire.
            return
        self._set_pause_checkbox(False)
        self._queue_state_for = ""
        if host is None or not host.uses_remote_runner:
            self.lbl_queue.setText("")
            return
        self._queue_state_for = host.id
        if needs_password(self.service, host):
            self.chk_pause.setEnabled(False)
            self.lbl_queue.setText("Test the connection first to read the queue.")
            return
        self.chk_pause.setEnabled(False)
        self.lbl_queue.setText("Reading the queue...")
        host_id = host.id

        def work() -> bool:
            transport = self.service.transport_for(host)
            try:
                return queue_paused(transport, host)
            finally:
                transport.close()

        def ok(paused: bool) -> None:
            # The selection may have moved on while the answer was in flight,
            # and it would be describing a different host by the time it lands.
            if self._current is None or self._current.id != host_id:
                return
            self.chk_pause.setEnabled(True)
            self._set_pause_checkbox(paused)
            self.lbl_queue.setText("The queue is held." if paused else "The queue is running.")

        def failed(message: str) -> None:
            if self._current is None or self._current.id != host_id:
                return
            self.chk_pause.setEnabled(True)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not read the queue."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed, owner=self)

    def _on_pause_toggled(self, checked: bool) -> None:
        if self._syncing_pause:
            return
        host = self._persist_current()
        if host is None or not host.uses_remote_runner:
            return
        if not ensure_password(self.service, host, self):
            self._set_pause_checkbox(not checked)
            return
        self.chk_pause.setEnabled(False)
        self.lbl_queue.setText("Holding the queue..." if checked else "Letting the queue run...")

        def work() -> bool:
            transport = self.service.transport_for(host)
            try:
                return set_queue_paused(transport, host, checked)
            finally:
                transport.close()

        def ok(paused: bool) -> None:
            self.chk_pause.setEnabled(True)
            self.lbl_queue.setText(
                "The queue is held. Jobs already running continue."
                if paused
                else "The queue is running."
            )

        def failed(message: str) -> None:
            self.chk_pause.setEnabled(True)
            # Back to what the host still says, rather than leaving the box
            # claiming a state the host never took.
            self._set_pause_checkbox(not checked)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not change the queue."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed, owner=self)

    def _detect_resources(self) -> None:
        """Fill the two budgets from what the host actually has."""
        host = self._persist_current()
        if host is None:
            return
        if not ensure_password(self.service, host, self):
            return
        self.btn_detect.setEnabled(False)
        self.lbl_queue.setText("Asking the host...")

        def work() -> tuple:
            transport = self.service.transport_for(host)
            try:
                return probe_resources(transport, host)
            finally:
                transport.close()

        def ok(found: tuple) -> None:
            self.btn_detect.setEnabled(True)
            cores, memory_mb, threads = found
            if not cores and not memory_mb:
                self.lbl_queue.setText("The host did not say what it has.")
                return
            if cores or memory_mb:
                # Filling the fields is the opposite of handing the budget over:
                # the point of the button is to see the numbers and then decide.
                self.chk_detect_resources.setChecked(False)
            if cores:
                self.spin_runner_cores.setValue(min(cores, self.spin_runner_cores.maximum()))
            if memory_mb:
                # Rounded down to whole gigabytes, which is the unit the field
                # is in: offering a budget larger than the machine would defeat
                # the point of asking.
                self.spin_runner_memory.setValue(
                    min(memory_mb // 1024, self.spin_runner_memory.maximum())
                )
            # Threads are named separately on purpose: a user who knows the
            # machine as "12 cores" should see why the budget says 8, rather
            # than assume the detection is broken.
            threads_note = (
                f" ({threads} hardware threads, but a calculation gains nothing"
                " from running on more than one thread per core)"
                if threads > cores > 0
                else ""
            )
            self.lbl_queue.setText(
                f"{host.name} reports {cores or '?'} cores and "
                f"{(memory_mb // 1024) if memory_mb else '?'} GB{threads_note}. "
                "Lower them to leave room for other users."
            )

        def failed(message: str) -> None:
            self.btn_detect.setEnabled(True)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not ask the host."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed, owner=self)

    def _apply_queue_limits(self) -> None:
        host = self._persist_current()
        if host is None or not host.uses_remote_runner:
            return
        if not ensure_password(self.service, host, self):
            return
        self.btn_apply_limits.setEnabled(False)
        self.lbl_queue.setText("Sending the limits...")

        def work() -> None:
            transport = self.service.transport_for(host)
            try:
                apply_queue_limits(transport, host)
            finally:
                transport.close()

        def ok(_result) -> None:
            self.btn_apply_limits.setEnabled(True)
            cores = host.runner_cores or 0
            # 0 is no job limit (see remote_runner.slots_for), not one job.
            jobs = (
                f"at most {host.max_concurrent} job(s)"
                if host.max_concurrent
                else "as many jobs as fit"
            )
            self.lbl_queue.setText(
                f"The helper will run {jobs}, "
                + (f"using up to {cores} core(s)." if cores else "using every core it finds.")
            )

        def failed(message: str) -> None:
            self.btn_apply_limits.setEnabled(True)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not send the limits."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed, owner=self)

    # --- connection test ----------------------------------------------------

    def _test_connection(self) -> None:
        host = self._save_current()
        if host is None:
            self.lbl_test.setText("Select a host first.")
            return
        if not host.hostname and not host.is_local:
            self.lbl_test.setText("Enter a hostname first.")
            return
        if not ensure_password(self.service, host, self):
            self.lbl_test.setText("Cancelled.")
            return
        self.btn_test.setEnabled(False)
        self.lbl_test.setText("Connecting...")

        def work() -> str:
            transport = self.service.transport_for(host)
            try:
                return transport.test_connection()
            finally:
                transport.close()

        def ok(remote_name: str) -> None:
            self.btn_test.setEnabled(True)
            self.lbl_test.setText(f"Connected to {remote_name or host.hostname}.")

        def failed(message: str) -> None:
            self.btn_test.setEnabled(True)
            self.lbl_test.setText(message.splitlines()[0] if message else "Connection failed.")
            if "known_hosts" in message.lower() or HostKeyRejected.__name__ in message:
                self._offer_trust(host)

        run_async(self.service.pool, work, on_success=ok, on_error=failed, owner=self)

    def _offer_trust(self, host: HostProfile) -> None:
        """Show the key the host is offering, and file it only if it is accepted.

        The key is read *before* the question, not after it: a prompt that
        cannot show a fingerprint is asking the user to agree to something
        nobody has seen. This is the one moment a host's identity is decided,
        and the whole value of it is the comparison against what the site
        published -- so the dialog shows what ``ssh`` would show.
        """
        from .transport.paramiko_backend import PARAMIKO_AVAILABLE, read_host_key

        if not PARAMIKO_AVAILABLE:
            QMessageBox.information(
                self,
                "Unknown host key",
                "This host is not in known_hosts. Connect once with 'ssh "
                f"{host.target}' in a terminal and accept the fingerprint.",
            )
            return
        self.lbl_test.setText("Reading the host's key...")
        # On a worker: reading the key is a TCP connection and an SSH
        # handshake, up to twenty seconds against a slow host, and it froze the
        # whole window while it waited.
        run_async(
            self.service.pool,
            lambda: read_host_key(host.hostname, host.port),
            on_success=lambda found: self._confirm_host_key(*found),
            on_error=lambda message: QMessageBox.warning(self, "Host key", message),
            owner=self,
        )

    def _confirm_host_key(self, key, hostname: str, port: int) -> None:
        """Show the fingerprint, and file the key only if it is accepted."""
        from .transport.paramiko_backend import add_host_key, key_fingerprint

        where = hostname if int(port or 22) == 22 else f"{hostname}:{int(port)}"
        confirm = QMessageBox.question(
            self,
            "Unknown host key",
            f"{where} is not in your known_hosts file.\n\n"
            f"Key type:\t{key.get_name()}\n"
            f"Fingerprint:\t{key_fingerprint(key)}\n\n"
            "Check that against the fingerprint your site publishes, or against "
            "'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub' run on the host "
            "itself. Add this key to known_hosts?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            fingerprint = add_host_key(key, hostname, port)
        except Exception as exc:
            QMessageBox.warning(self, "Host key", str(exc))
            return
        self.lbl_test.setText(f"Host key added ({fingerprint}). Test again.")
