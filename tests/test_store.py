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
    RECOMMENDED_MIN_POLL_INTERVAL,
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
        self.store.set_pref("poll_interval", 0)
        self.assertEqual(self.store.poll_interval, MIN_POLL_INTERVAL)

    def test_fast_polling_is_permitted(self):
        # Allowed on purpose -- for a local host or a short debug job -- but
        # the UI flags it; see poll_interval_is_aggressive.
        self.store.set_pref("poll_interval", 10)
        self.assertEqual(self.store.poll_interval, 10)

    def test_a_fast_interval_is_flagged_as_aggressive(self):
        self.store.set_pref("poll_interval", RECOMMENDED_MIN_POLL_INTERVAL - 1)
        self.assertTrue(self.store.poll_interval_is_aggressive)

    def test_the_recommended_interval_is_not_flagged(self):
        self.store.set_pref("poll_interval", RECOMMENDED_MIN_POLL_INTERVAL)
        self.assertFalse(self.store.poll_interval_is_aggressive)

    def test_the_default_is_not_flagged(self):
        self.assertFalse(self.store.poll_interval_is_aggressive)

    def test_the_floor_is_below_the_recommendation(self):
        self.assertLess(MIN_POLL_INTERVAL, RECOMMENDED_MIN_POLL_INTERVAL)

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


class ExportAndArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="export_")
        self.store = JobStore(self.tmp)
        self.now = time.time()
        self.store.add_job(
            Job(
                id="j1",
                name="alpha",
                host_name="hpc",
                remote_job_id="77",
                state=STATE_DONE,
                rc=0,
                submitted_at=self.now - 120,
                finished_at=self.now,
                remote_dir="~/jobs/alpha",
                command="orca alpha.inp > alpha.out",
            )
        )
        self.store.add_job(Job(id="j2", name="beta", state=STATE_RUNNING, submitted_at=self.now))

    def target(self, name):
        return os.path.join(self.tmp, name)


class TestJsonExport(ExportAndArchiveTestCase):
    def test_it_writes_the_raw_records(self):
        path = self.store.export_jobs(self.target("out.json"))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual({j["id"] for j in payload["jobs"]}, {"j1", "j2"})

    def test_the_records_round_trip_back_into_jobs(self):
        self.store.export_jobs_json(self.target("out.json"))
        with open(self.target("out.json"), encoding="utf-8") as handle:
            restored = [Job.from_dict(raw) for raw in json.load(handle)["jobs"]]
        self.assertEqual({j.name for j in restored}, {"alpha", "beta"})
        self.assertEqual(next(j for j in restored if j.id == "j1").rc, 0)

    def test_the_extension_chooses_the_format(self):
        self.store.export_jobs(self.target("out.json"))
        with open(self.target("out.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(1), "{")


class TestCsvExport(ExportAndArchiveTestCase):
    def rows(self):
        import csv

        self.store.export_jobs(self.target("out.csv"))
        with open(self.target("out.csv"), encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_one_row_per_job_plus_a_header(self):
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["name"] for row in rows}, {"alpha", "beta"})

    def test_the_columns_are_the_documented_ones(self):
        self.assertEqual(list(self.rows()[0]), list(store_module.EXPORT_COLUMNS))

    def test_states_exit_codes_and_ids(self):
        row = next(r for r in self.rows() if r["name"] == "alpha")
        self.assertEqual(row["state"], STATE_DONE)
        self.assertEqual(row["exit_code"], "0")
        self.assertEqual(row["queue_id"], "77")
        self.assertEqual(row["host"], "hpc")

    def test_timestamps_are_readable(self):
        row = next(r for r in self.rows() if r["name"] == "alpha")
        self.assertRegex(row["submitted"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertEqual(row["finished"][:4], time.strftime("%Y"))

    def test_a_missing_timestamp_is_blank_not_1970(self):
        row = next(r for r in self.rows() if r["name"] == "beta")
        self.assertEqual(row["finished"], "")

    def test_a_missing_exit_code_is_blank_not_none(self):
        row = next(r for r in self.rows() if r["name"] == "beta")
        self.assertEqual(row["exit_code"], "")

    def test_a_multiline_command_stays_on_one_row(self):
        self.store.jobs["j1"].command = "module load orca\norca a.inp"
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("\n", next(r for r in rows if r["name"] == "alpha")["command"])

    def test_an_empty_list_still_writes_a_header(self):
        self.store.jobs = {}
        self.assertEqual(self.rows(), [])
        with open(self.target("out.csv"), encoding="utf-8") as handle:
            self.assertIn("queue_id", handle.readline())


class TestTheJobExtension(ExportAndArchiveTestCase):
    def test_the_live_list_uses_it(self):
        self.assertTrue(self.store.jobs_path.endswith(store_module.JOB_EXTENSION))
        self.assertTrue(os.path.exists(self.store.jobs_path))

    def test_the_contents_are_still_ordinary_json(self):
        with open(self.store.jobs_path, encoding="utf-8") as handle:
            self.assertIn("jobs", json.load(handle))

    def test_a_pre_extension_file_is_still_read(self):
        directory = tempfile.mkdtemp(prefix="legacy_")
        legacy = os.path.join(directory, store_module.LEGACY_JOBS_FILENAME)
        atomic_write_json(legacy, {"version": 1, "jobs": [{"id": "old1", "name": "carried"}]})
        store = JobStore(directory)
        self.assertEqual(store.jobs["old1"].name, "carried")

    def test_the_new_name_wins_when_both_exist(self):
        directory = tempfile.mkdtemp(prefix="both_")
        atomic_write_json(
            os.path.join(directory, store_module.LEGACY_JOBS_FILENAME),
            {"jobs": [{"id": "stale"}]},
        )
        atomic_write_json(
            os.path.join(directory, store_module.JOBS_FILENAME), {"jobs": [{"id": "current"}]}
        )
        self.assertEqual(list(JobStore(directory).jobs), ["current"])

    def test_saving_migrates_to_the_new_name(self):
        directory = tempfile.mkdtemp(prefix="migrate_")
        atomic_write_json(
            os.path.join(directory, store_module.LEGACY_JOBS_FILENAME),
            {"jobs": [{"id": "old1", "name": "carried"}]},
        )
        store = JobStore(directory)
        store.save_jobs()
        self.assertTrue(os.path.exists(os.path.join(directory, store_module.JOBS_FILENAME)))
        self.assertEqual(JobStore(directory).jobs["old1"].name, "carried")

    def test_an_archive_written_before_the_extension_is_still_listed(self):
        directory = self.store.archive_dir()
        os.makedirs(directory, exist_ok=True)
        atomic_write_json(os.path.join(directory, "jobs_20200101_000000.json"), {"jobs": []})
        self.store.clear_jobs()
        names = [os.path.basename(p) for p in self.store.archived_files()]
        self.assertIn("jobs_20200101_000000.json", names)
        self.assertEqual(len(names), 2)


class TestTheArchivedFlagTravelsInTheFile(ExportAndArchiveTestCase):
    """Location does not decide what is history -- the file says so itself, so
    a cleared list stays read-only after being moved, copied or mailed on."""

    def test_a_cleared_list_is_flagged_archived(self):
        archived, _ = self.store.clear_jobs()
        with open(archived, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIs(payload["archived"], True)
        self.assertIn("archived_at", payload)

    def test_the_live_list_is_not(self):
        with open(self.store.jobs_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIs(payload["archived"], False)
        self.assertNotIn("archived_at", payload)

    def test_an_export_is_not(self):
        path = self.store.export_jobs(self.target("exported.pmejbs"))
        with open(path, encoding="utf-8") as handle:
            self.assertIs(json.load(handle)["archived"], False)

    def test_reading_reports_the_flag(self):
        archived, _ = self.store.clear_jobs()
        jobs, is_archived = self.store.read_job_list(archived)
        self.assertTrue(is_archived)
        self.assertEqual({j.name for j in jobs}, {"alpha", "beta"})

    def test_the_flag_survives_being_moved_out_of_the_folder(self):
        archived, _ = self.store.clear_jobs()
        moved = self.target("somewhere_else.pmejbs")
        os.replace(archived, moved)
        _jobs, is_archived = self.store.read_job_list(moved)
        self.assertTrue(is_archived, "the flag should travel with the file")

    def test_an_export_read_back_is_working_data(self):
        path = self.store.export_jobs(self.target("exported.pmejbs"))
        jobs, is_archived = self.store.read_job_list(path)
        self.assertFalse(is_archived)
        self.assertEqual(len(jobs), 2)

    def test_a_file_without_the_flag_is_working_data(self):
        path = self.target("legacy.pmejbs")
        atomic_write_json(path, {"version": 1, "jobs": [{"id": "x"}]})
        _jobs, is_archived = self.store.read_job_list(path)
        self.assertFalse(is_archived)

    def test_reading_does_not_import_anything(self):
        archived, _ = self.store.clear_jobs()
        self.store.read_job_list(archived)
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(JobStore(self.tmp).jobs, {})

    def test_a_missing_file_yields_nothing(self):
        self.assertEqual(self.store.read_job_list(self.target("nope.pmejbs")), ([], False))

    def test_a_corrupt_file_yields_nothing(self):
        path = self.target("bad.pmejbs")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(self.store.read_job_list(path), ([], False))


class TestUsingAnotherJobFile(ExportAndArchiveTestCase):
    """Opening a list switches to it; it does not merge into the current one."""

    def other_file(self):
        path = self.store.export_jobs(self.target("other.pmejbs"))
        self.store.clear_jobs()
        return path

    def test_the_jobs_come_from_the_opened_file(self):
        path = self.other_file()
        self.assertEqual(self.store.use_jobs_file(path), 2)
        self.assertEqual({j.name for j in self.store.jobs.values()}, {"alpha", "beta"})

    def test_later_changes_are_written_there(self):
        path = self.other_file()
        self.store.use_jobs_file(path)
        self.store.add_job(Job(id="j9", name="new one"))
        with open(path, encoding="utf-8") as handle:
            names = {j["name"] for j in json.load(handle)["jobs"]}
        self.assertIn("new one", names)

    def test_the_default_file_is_left_alone(self):
        path = self.other_file()
        self.store.use_jobs_file(path)
        self.store.add_job(Job(id="j9", name="new one"))
        with open(self.store.default_jobs_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["jobs"], [])

    def test_switching_back_restores_the_default(self):
        self.store.use_jobs_file(self.other_file())
        self.store.use_jobs_file("")
        self.assertTrue(self.store.using_default_jobs_file())
        self.assertEqual(self.store.jobs, {})

    def test_the_choice_does_not_survive_a_restart(self):
        path = self.other_file()
        self.store.use_jobs_file(path)
        self.assertTrue(JobStore(self.tmp).using_default_jobs_file())

    def test_using_the_default_is_reported(self):
        self.assertTrue(self.store.using_default_jobs_file())
        self.store.use_jobs_file(self.other_file())
        self.assertFalse(self.store.using_default_jobs_file())


class TestClearingArchivesFirst(ExportAndArchiveTestCase):
    def test_the_list_is_emptied(self):
        self.store.clear_jobs()
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(JobStore(self.tmp).jobs, {})

    def test_the_archive_lands_in_old_with_the_date_in_its_name(self):
        archived, count = self.store.clear_jobs()
        self.assertEqual(count, 2)
        self.assertEqual(os.path.basename(os.path.dirname(archived)), store_module.ARCHIVE_DIRNAME)
        name = os.path.basename(archived)
        self.assertRegex(name, r"^jobs_\d{8}_\d{6}\.\w+$")
        self.assertTrue(name.endswith(store_module.JOB_EXTENSION), name)

    def test_the_archive_holds_the_cleared_jobs(self):
        archived, _ = self.store.clear_jobs()
        with open(archived, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual({j["id"] for j in payload["jobs"]}, {"j1", "j2"})
        self.assertIn("archived_at", payload)

    def test_a_second_clear_does_not_overwrite_the_first(self):
        first, _ = self.store.clear_jobs(when=self.now)
        self.store.add_job(Job(id="j3", name="gamma"))
        second, _ = self.store.clear_jobs(when=self.now)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.archived_files()), 2)

    def test_archives_are_listed_newest_first(self):
        self.store.clear_jobs(when=self.now - 86400)
        self.store.add_job(Job(id="j4"))
        self.store.clear_jobs(when=self.now)
        names = [os.path.basename(p) for p in self.store.archived_files()]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_clearing_an_empty_list_is_harmless(self):
        self.store.clear_jobs()
        archived, count = self.store.clear_jobs()
        self.assertEqual(count, 0)
        self.assertTrue(os.path.exists(archived))

    def test_hosts_and_presets_are_untouched(self):
        self.store.add_host(HostProfile(id="h1", name="keep"))
        self.store.add_preset(SubmitPreset(id="p1", host_id="h1"))
        self.store.clear_jobs()
        reloaded = JobStore(self.tmp)
        self.assertIn("h1", reloaded.hosts)
        self.assertIn("p1", reloaded.presets)


if __name__ == "__main__":
    unittest.main()
