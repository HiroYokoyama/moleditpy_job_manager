import os
import time
import unittest

from job_manager.models import (
    ACTIVE_STATES,
    STATE_DONE,
    STATE_PENDING,
    STATE_RUNNING,
    TERMINAL_STATES,
    HostProfile,
    Job,
    SubmitPreset,
    new_id,
    sanitize_name,
)


class TestSanitizeName(unittest.TestCase):
    def test_spaces_and_symbols_become_underscores(self):
        self.assertEqual(sanitize_name("my job #1"), "my_job_1")

    def test_safe_characters_are_kept(self):
        self.assertEqual(sanitize_name("run-2.step_3"), "run-2.step_3")

    def test_empty_falls_back(self):
        self.assertEqual(sanitize_name(""), "job")
        self.assertEqual(sanitize_name("   "), "job")
        self.assertEqual(sanitize_name("///", fallback="x"), "x")

    def test_leading_and_trailing_separators_are_trimmed(self):
        self.assertEqual(sanitize_name("__job__"), "job")

    def test_path_traversal_cannot_survive(self):
        self.assertNotIn("/", sanitize_name("../../etc/passwd"))


class TestIds(unittest.TestCase):
    def test_ids_are_unique_and_short(self):
        ids = {new_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(len(i) == 12 for i in ids))


class TestHostProfile(unittest.TestCase):
    def test_target_includes_user(self):
        self.assertEqual(HostProfile(hostname="h", username="u").target, "u@h")

    def test_target_without_user(self):
        self.assertEqual(HostProfile(hostname="h").target, "h")

    def test_round_trip(self):
        host = HostProfile(name="c", hostname="h", ssh_options=["A=1"], port=2222)
        restored = HostProfile.from_dict(host.to_dict())
        self.assertEqual(restored, host)

    def test_unknown_keys_are_ignored(self):
        data = HostProfile(name="c").to_dict()
        data["invented_by_a_newer_version"] = True
        self.assertEqual(HostProfile.from_dict(data).name, "c")

    def test_no_secret_fields_exist(self):
        # A password must never be persistable; the model has no slot for one.
        self.assertNotIn("password", HostProfile().to_dict())

    def test_a_new_host_is_enabled(self):
        self.assertTrue(HostProfile().enabled)

    def test_a_host_saved_before_enabled_existed_stays_enabled(self):
        # Forward compatibility: from_dict ignores unknown keys and the
        # dataclass default applies, so an old settings.json with no
        # "enabled" key must not silently start skipping every host it lists.
        data = HostProfile(name="c").to_dict()
        del data["enabled"]
        self.assertTrue(HostProfile.from_dict(data).enabled)

    def test_a_new_host_has_no_equal_path(self):
        self.assertEqual(HostProfile().equal_path, "")

    def test_mirrored_path_with_no_equal_path(self):
        self.assertEqual(HostProfile(equal_path="").mirrored_path("out/calc.log"), "")

    def test_mirrored_path_joins_the_relative_parts(self):
        host = HostProfile(equal_path="/mnt/cluster")
        self.assertEqual(
            host.mirrored_path("out/calc.log"),
            os.path.join("/mnt/cluster", "out", "calc.log"),
        )

    def test_mirrored_path_accepts_backslashes(self):
        # A user pasting a Windows-flavoured relative path in from habit.
        host = HostProfile(equal_path="/mnt/cluster")
        self.assertEqual(
            host.mirrored_path("out\\calc.log"),
            os.path.join("/mnt/cluster", "out", "calc.log"),
        )

    def test_mirrored_path_with_no_relative_path(self):
        host = HostProfile(equal_path="/mnt/cluster")
        self.assertEqual(host.mirrored_path(""), "")


class TestSubmitPreset(unittest.TestCase):
    def test_defaults_are_not_shared_between_instances(self):
        first = SubmitPreset()
        second = SubmitPreset()
        first.fetch_globs.append("*.zzz")
        self.assertNotIn("*.zzz", second.fetch_globs)

    def test_round_trip(self):
        preset = SubmitPreset(modules=["orca/5"], nodes=4)
        self.assertEqual(SubmitPreset.from_dict(preset.to_dict()), preset)


class TestJob(unittest.TestCase):
    def test_active_and_terminal_are_disjoint(self):
        self.assertFalse(ACTIVE_STATES & TERMINAL_STATES)

    def test_is_active(self):
        self.assertTrue(Job(state=STATE_RUNNING).is_active)
        self.assertFalse(Job(state=STATE_DONE).is_active)

    def test_is_terminal(self):
        self.assertTrue(Job(state=STATE_DONE).is_terminal)
        self.assertFalse(Job(state=STATE_PENDING).is_terminal)

    def test_elapsed_zero_before_submission(self):
        self.assertEqual(Job().elapsed(), 0.0)

    def test_elapsed_uses_finish_time_when_finished(self):
        job = Job(submitted_at=1000.0, finished_at=1090.0)
        self.assertEqual(job.elapsed(now=99999.0), 90.0)

    def test_elapsed_counts_up_while_running(self):
        job = Job(submitted_at=1000.0)
        self.assertEqual(job.elapsed(now=1030.0), 30.0)

    def test_elapsed_never_negative(self):
        job = Job(submitted_at=2000.0)
        self.assertEqual(job.elapsed(now=1000.0), 0.0)

    def test_touch_sets_state_and_timestamp(self):
        job = Job()
        before = job.updated_at
        time.sleep(0.01)
        job.touch(STATE_RUNNING)
        self.assertEqual(job.state, STATE_RUNNING)
        self.assertGreater(job.updated_at, before)
        self.assertEqual(job.finished_at, 0.0)

    def test_touch_stamps_finish_on_terminal_state(self):
        job = Job()
        job.touch(STATE_DONE)
        self.assertGreater(job.finished_at, 0)

    def test_touch_does_not_move_an_existing_finish_stamp(self):
        job = Job(finished_at=555.0)
        job.touch(STATE_DONE)
        self.assertEqual(job.finished_at, 555.0)

    def test_round_trip(self):
        job = Job(name="j", input_files=["a.inp"], rc=1)
        self.assertEqual(Job.from_dict(job.to_dict()), job)


if __name__ == "__main__":
    unittest.main()
