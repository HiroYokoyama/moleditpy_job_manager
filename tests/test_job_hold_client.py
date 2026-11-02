"""Hold/Release are reachable through the shipped client and command line."""

import io
from contextlib import redirect_stdout

import pytest

pytest.importorskip("PySide6.QtCore")

from job_manager.api_client import main
from .api_support import in_thread
from .test_api_client import ClientTestCase


class TestHoldClient(ClientTestCase):
    def test_client_hold_then_cli_release_keeps_after_wait(self):
        parent = self.add_job(name="parent", state="RUNNING")
        child = self.add_job(name="child", state="PENDING", after_job_id=parent.id, priority=-10)
        held = in_thread(lambda: self.client.hold(child.id))
        assert held["job"]["state"] == "HELD"
        assert held["job"]["hold_reasons"] == ["Manual", "After: parent"]
        output = io.StringIO()
        with redirect_stdout(output):
            result = in_thread(
                lambda: main(
                    [
                        "--url=" + self.server.url(),
                        "--token=" + self.server.token(),
                        "release",
                        child.id,
                    ]
                )
            )
        assert result == 0
        assert child.state == "PENDING"
        assert child.after_job_id == parent.id and child.priority == -10
        assert "After: parent" in output.getvalue()
