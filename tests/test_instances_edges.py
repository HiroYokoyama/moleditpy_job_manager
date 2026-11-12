"""The instance registry's error paths: what happens when the OS says no."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from job_manager import instances

TOKEN = "0" * 64


class EdgeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="instances_edges_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class TestTheLock(EdgeCase):
    def test_a_failed_unlock_still_closes_the_file(self):
        lock = instances.SingleInstanceLock(self.dir)
        self.assertTrue(lock.acquire())
        handle = lock._handle
        with patch.object(instances, "_unlock_handle", side_effect=OSError("busy")):
            lock.release()
        self.assertTrue(handle.closed)
        self.assertFalse(lock.held)


class TestTheLivenessProbe(EdgeCase):
    def test_a_responder_that_hangs_up_without_a_token_is_dead(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def hang_up():
            connection, _ = listener.accept()
            connection.close()

        thread = threading.Thread(target=hang_up, daemon=True)
        thread.start()
        alive = instances._socket_alive({"port": listener.getsockname()[1], "token": TOKEN})
        thread.join(timeout=2)
        self.assertFalse(alive)


class TestWritingAndRemoving(EdgeCase):
    def test_a_refused_write_is_raised_even_when_the_temp_file_cannot_go(self):
        path = os.path.join(self.dir, "instances", "1.json")
        with (
            patch.object(instances.os, "replace", side_effect=OSError("disk full")),
            patch.object(instances.os, "remove", side_effect=OSError("locked")),
            self.assertRaises(OSError),
        ):
            instances._write_atomically(path, {"pid": 1})

    def test_unregistering_survives_a_file_that_cannot_be_removed(self):
        with patch.object(instances.os, "remove", side_effect=PermissionError("open elsewhere")):
            instances.unregister_instance(self.dir)  # Logs; does not raise.

    def test_withdrawing_a_request_that_is_already_gone_is_quiet(self):
        instances.withdraw_request(self.dir, 4242)

    def test_a_request_is_read_even_when_it_cannot_be_removed(self):
        instances.send_request(self.dir, os.getpid(), "show")
        with patch.object(instances.os, "remove", side_effect=PermissionError("open elsewhere")):
            self.assertEqual(instances.take_request(self.dir), "show")


class TestReadingTheRegistry(EdgeCase):
    def test_a_registration_under_another_pids_name_is_ignored_not_deleted(self):
        folder = instances.instances_dir(self.dir)
        os.makedirs(folder)
        misnamed = os.path.join(folder, "101.json")
        with open(misnamed, "w", encoding="utf-8") as handle:
            json.dump({"pid": 102, "protocol": 2, "port": 1, "token": TOKEN}, handle)
        self.assertEqual(instances.live_instances(self.dir), [])
        self.assertTrue(os.path.exists(misnamed))


class TestClaiming(EdgeCase):
    def test_a_moleditpy_instance_is_not_asked_while_a_launch_holds_the_lock(self):
        holder = instances.SingleInstanceLock(self.dir)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        moleditpy = {"pid": 101, "role": instances.ROLE_MOLEDITPY}
        with (
            patch.object(instances, "live_instances", return_value=[moleditpy]),
            patch.object(instances, "send_request") as sent,
        ):
            lock, target = instances.claim_or_defer(self.dir, "show", timeout=0.05, poll=0.01)
        self.assertEqual((lock, target), (None, None))
        sent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
