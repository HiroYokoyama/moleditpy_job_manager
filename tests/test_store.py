import json
import os
import tempfile
import time
import unittest

from job_manager import store as store_module
from job_manager.models import STATE_DONE, STATE_RUNNING, HostProfile, Job, SubmitPreset
from job_manager.store import (
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    JobStore,
    atomic_write_json,
    default_data_dir,
    read_json,
)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jobstore_")
        self.store = JobStore(self.tmp)


class TestDataDir(unittest.TestCase):
    def test_default_is_outside_the_plugin_folder(self):
        # jobs.json beside the package would be destroyed by a plugin update.
        package_dir = os.path.dirname(os.path.abspath(store_module.__file__))
        previous = os.environ.pop(store_module.DATA_DIR_ENV, None)
        try:
            resolved = default_data_dir()
        finally:
            if previous is not None:
                os.environ[store_module.DATA_DIR_ENV] = previous
        self.assertNotEqual(os.path.abspath(resolved), package_dir)
        self.assertIn(".moleditpy", resolved)
        self.assertIn("job_manager", resolved)

    def test_env_override_wins(self):
        previous = os.environ.get(store_module.DATA_DIR_ENV)
        os.environ[store_module.DATA_DIR_ENV] = os.path.join(tempfile.gettempdir(), "jm_override")
        try:
            self.assertTrue(default_data_dir().endswith("jm_override"))
        finally:
            if previous is None:
                os.environ.pop(store_module.DATA_DIR_ENV, None)
            else:
                os.environ[store_module.DATA_DIR_ENV] = previous

    def test_download_root_lives_under_the_data_dir(self):
        self.assertTrue(store_module.default_download_root().startswith(default_data_dir()))


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atomic_")

    def test_writes_and_reads_back(self):
        path = os.path.join(self.tmp, "a.json")
        atomic_write_json(path, {"x": 1})
        self.assertEqual(read_json(path, None), {"x": 1})

    def test_creates_missing_directories(self):
        path = os.path.join(self.tmp, "deep", "nested", "a.json")
        atomic_write_json(path, [1, 2])
        self.assertTrue(os.path.exists(path))

    def test_no_temp_files_survive(self):
        path = os.path.join(self.tmp, "a.json")
        atomic_write_json(path, {"x": 1})
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".tmp_")]
        self.assertEqual(leftovers, [])

    def test_overwrite_keeps_the_file_valid(self):
        path = os.path.join(self.tmp, "a.json")
        atomic_write_json(path, {"x": 1})
        atomic_write_json(path, {"x": 2})
        self.assertEqual(read_json(path, None), {"x": 2})

    def test_unicode_survives(self):
        path = os.path.join(self.tmp, "u.json")
        atomic_write_json(path, {"name": "分子"})
        self.assertEqual(read_json(path, None)["name"], "分子")

    def test_read_missing_returns_default(self):
        self.assertEqual(read_json(os.path.join(self.tmp, "nope.json"), "d"), "d")

    def test_read_corrupt_returns_default(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(read_json(path, {"fallback": True}), {"fallback": True})


class TestHostsAndPresets(StoreTestCase):
    def test_add_and_persist_host(self):
        self.store.add_host(HostProfile(id="h1", name="alpha"))
        self.assertEqual(JobStore(self.tmp).hosts["h1"].name, "alpha")

    def test_host_list_is_sorted_case_insensitively(self):
        self.store.add_host(HostProfile(id="a", name="zeta"))
        self.store.add_host(HostProfile(id="b", name="Alpha"))
        self.assertEqual([h.name for h in self.store.host_list()], ["Alpha", "zeta"])

    def test_removing_a_host_removes_its_presets(self):
        self.store.add_host(HostProfile(id="h1"))
        self.store.add_preset(SubmitPreset(id="p1", host_id="h1"))
        self.store.add_preset(SubmitPreset(id="p2", host_id="other"))
        self.store.remove_host("h1")
        self.assertNotIn("p1", self.store.presets)
        self.assertIn("p2", self.store.presets)

    def test_removing_an_unknown_host_is_harmless(self):
        self.store.remove_host("ghost")

    def test_presets_for_host_filters(self):
        self.store.add_preset(SubmitPreset(id="p1", host_id="h1", name="b"))
        self.store.add_preset(SubmitPreset(id="p2", host_id="h1", name="a"))
        self.store.add_preset(SubmitPreset(id="p3", host_id="h2"))
        names = [p.name for p in self.store.presets_for_host("h1")]
        self.assertEqual(names, ["a", "b"])

    def test_remove_preset(self):
        self.store.add_preset(SubmitPreset(id="p1"))
        self.store.remove_preset("p1")
        self.assertEqual(self.store.presets, {})


class TestJobs(StoreTestCase):
    def test_jobs_persist_across_instances(self):
        self.store.add_job(Job(id="j1", name="one"))
        self.assertEqual(JobStore(self.tmp).jobs["j1"].name, "one")

    def test_jobs_are_stored_separately_from_settings(self):
        self.store.add_job(Job(id="j1"))
        with open(self.store.jobs_path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(len(document["jobs"]), 1)
        self.assertFalse(os.path.exists(self.store.settings_path))

    def test_job_list_is_newest_first(self):
        self.store.add_job(Job(id="old", submitted_at=100))
        self.store.add_job(Job(id="new", submitted_at=200))
        self.assertEqual([j.id for j in self.store.job_list()], ["new", "old"])

    def test_active_jobs_by_host_groups(self):
        self.store.add_job(Job(id="a", host_id="h1", state=STATE_RUNNING))
        self.store.add_job(Job(id="b", host_id="h1", state=STATE_RUNNING))
        self.store.add_job(Job(id="c", host_id="h2", state=STATE_RUNNING))
        self.store.add_job(Job(id="d", host_id="h2", state=STATE_DONE))
        grouped = self.store.active_jobs_by_host()
        self.assertEqual(sorted(grouped), ["h1", "h2"])
        self.assertEqual(len(grouped["h1"]), 2)
        self.assertEqual(len(grouped["h2"]), 1)

    def test_remove_job(self):
        self.store.add_job(Job(id="j1"))
        self.store.remove_job("j1")
        self.assertEqual(JobStore(self.tmp).jobs, {})


class TestPrune(StoreTestCase):
    def test_old_terminal_jobs_are_dropped(self):
        old = time.time() - 200 * 86400
        self.store.add_job(Job(id="old", state=STATE_DONE, finished_at=old))
        self.store.add_job(Job(id="fresh", state=STATE_DONE, finished_at=time.time()))
        self.assertEqual(self.store.prune(), 1)
        self.assertNotIn("old", self.store.jobs)
        self.assertIn("fresh", self.store.jobs)

    def test_active_jobs_are_never_pruned(self):
        old = time.time() - 500 * 86400
        self.store.add_job(Job(id="running", state=STATE_RUNNING, updated_at=old))
        self.assertEqual(self.store.prune(), 0)

    def test_zero_days_disables_pruning(self):
        self.store.add_job(Job(id="old", state=STATE_DONE, finished_at=0.0))
        self.assertEqual(self.store.prune(days=0), 0)


class TestPrefs(StoreTestCase):
    def test_set_and_get(self):
        self.store.set_pref("download_root", "/tmp/x")
        self.assertEqual(JobStore(self.tmp).get_pref("download_root"), "/tmp/x")

    def test_unknown_key_uses_supplied_default(self):
        self.assertEqual(self.store.get_pref("nope", "fallback"), "fallback")

    def test_poll_interval_default(self):
        self.assertEqual(self.store.poll_interval, 120)

    def test_poll_interval_is_floored(self):
        self.store.set_pref("poll_interval", 1)
        self.assertEqual(self.store.poll_interval, MIN_POLL_INTERVAL)

    def test_poll_interval_is_capped(self):
        self.store.set_pref("poll_interval", 10**6)
        self.assertEqual(self.store.poll_interval, MAX_POLL_INTERVAL)

    def test_poll_interval_survives_garbage(self):
        self.store.set_pref("poll_interval", "not a number")
        self.assertEqual(self.store.poll_interval, 120)

    def test_download_root_expands_user(self):
        self.store.set_pref("download_root", "~/somewhere")
        self.assertNotIn("~", self.store.download_root())


class TestCorruptFiles(unittest.TestCase):
    def test_a_corrupt_jobs_file_does_not_prevent_startup(self):
        tmp = tempfile.mkdtemp(prefix="corrupt_")
        with open(os.path.join(tmp, "jobs.json"), "w", encoding="utf-8") as handle:
            handle.write("garbage")
        store = JobStore(tmp)
        self.assertEqual(store.jobs, {})


if __name__ == "__main__":
    unittest.main()
