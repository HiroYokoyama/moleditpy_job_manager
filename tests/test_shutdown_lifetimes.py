"""Shutdown must settle worker state without reviving a closing GUI."""

import gc
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from job_manager.api_core import ApiError  # noqa: E402
from job_manager.api_server import JobApiServer, _GuiBridge  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.standalone import _StandaloneWindows  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from job_manager.tasks import run_async, shutdown_pool  # noqa: E402
from .api_support import FakeService  # noqa: E402


@pytest.mark.parametrize("mode", ["close", "timeout"])
def test_gui_bridge_cancels_undelivered_requests(mode):
    bridge = _GuiBridge()
    requested = threading.Event()
    bridge._requested.connect(lambda box: requested.set(), Qt.ConnectionType.DirectConnection)
    called, errors = [], []

    def request():
        try:
            bridge.call(lambda: called.append(True), timeout=0.05 if mode == "timeout" else 5)
        except ApiError as exc:
            errors.append(exc)

    client = threading.Thread(target=request)
    client.start()
    assert requested.wait(2)
    if mode == "close":
        bridge.close()
    client.join(2)
    assert not client.is_alive()
    assert len(errors) == 1 and errors[0].status == 503
    QApplication.processEvents()
    assert called == []
    assert not bridge._pending
    bridge.deleteLater()


def test_stopping_api_with_http_request_queued_keeps_destruction_on_gui(tmp_path):
    api = JobApiServer(FakeService(JobStore(str(tmp_path))))
    assert api.parent() is QCoreApplication.instance()
    port = api.start(0)
    requested = threading.Event()
    destroyed, status = [], []
    api._bridge._requested.connect(lambda box: requested.set(), Qt.ConnectionType.DirectConnection)
    api.destroyed.connect(
        lambda: destroyed.append(threading.get_ident()), Qt.ConnectionType.DirectConnection
    )
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/hosts")
    request.add_header("Authorization", f"Bearer {api.token()}")

    def fetch():
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                status.append(reply.status)
        except urllib.error.HTTPError as exc:
            status.append(exc.code)

    client = threading.Thread(target=fetch)
    client.start()
    try:
        assert requested.wait(2)
        api.stop()
        api.deleteLater()
        api = None
        client.join(2)
        assert not client.is_alive()
        assert status == [503]
        assert destroyed == []
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert destroyed == [threading.get_ident()]
    finally:
        if api is not None:
            api.stop()
        client.join(6)


def test_pool_shutdown_drains_workers_settles_internal_state_and_suppresses_ui():
    pool = QThreadPool()
    pool.setMaxThreadCount(2)
    window = QWidget()
    entered = [threading.Event(), threading.Event()]
    gate = threading.Event()
    internal, ui, cancelled = [], [], []

    def work(index):
        entered[index].set()
        assert gate.wait(6)
        return index

    run_async(pool, lambda: work(0), on_success=internal.append)
    run_async(
        pool,
        lambda: work(1),
        on_success=ui.append,
        on_error=ui.append,
        on_finished=lambda: ui.append("finished"),
        owner=window,
    )
    assert all(event.wait(2) for event in entered)
    run_async(
        pool, lambda: pytest.fail("queued operation must be cleared"), on_error=cancelled.append
    )
    run_async(
        pool,
        lambda: pytest.fail("queued UI operation must be cleared"),
        on_error=ui.append,
        on_finished=lambda: ui.append("finished"),
        owner=window,
    )
    # Exceeds the former three-second shutdown timeout, while the GUI is blocked.
    release = threading.Timer(3.15, gate.set)
    release.start()
    try:
        shutdown_pool(pool)
        assert gate.is_set() and pool.activeThreadCount() == 0
        assert internal == [0]
        assert ui == []
        assert len(cancelled) == 1 and "shutting down" in cancelled[0]
        QApplication.processEvents()
        assert ui == []
        with pytest.raises(RuntimeError, match="shutting down"):
            run_async(pool, lambda: None)
    finally:
        gate.set()
        release.join(5)
        pool.waitForDone()
        window.deleteLater()


def test_service_shutdown_retires_poller_and_repeated_calls_do_not_touch_deleted_timers(tmp_path):
    service = JobService(JobStore(str(tmp_path)))
    poller = service.poller
    destroyed = []
    service.destroyed.connect(
        lambda: destroyed.append(threading.get_ident()), Qt.ConnectionType.DirectConnection
    )
    service.shutdown()
    service.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(service)
    assert destroyed == [threading.get_ident()]
    poller.start()
    poller.reschedule()
    poller.prime()
    assert poller.tick() == 0
    assert poller.refresh_now() is False


def test_shutdown_releases_last_window_reference_on_gui_thread():
    pool = QThreadPool()
    window = QWidget()
    destroyed = []
    window.destroyed.connect(
        lambda: destroyed.append(threading.get_ident()), Qt.ConnectionType.DirectConnection
    )
    entered, gate = threading.Event(), threading.Event()

    def work(retained_window):
        entered.set()
        assert gate.wait(5)
        assert shiboken6.isValid(retained_window)

    run_async(pool, lambda retained=window: work(retained), owner=window)
    del window
    assert entered.wait(2)
    release = threading.Timer(0.05, gate.set)
    release.start()
    try:
        shutdown_pool(pool)
    finally:
        gate.set()
        release.join(5)
        pool.waitForDone()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    assert destroyed == [threading.get_ident()]


def test_standalone_stop_shuts_down_service(tmp_path):
    service = JobService(JobStore(str(tmp_path)))
    standalone = _StandaloneWindows(QCoreApplication.instance(), service, str(tmp_path))
    with patch.object(service, "shutdown", wraps=service.shutdown) as shutdown:
        standalone.stop()
        shutdown.assert_called_once_with()
    standalone.deleteLater()
