import unittest

from job_manager import remote_paths


class TestQuote(unittest.TestCase):
    def test_plain_path_is_untouched(self):
        self.assertEqual(remote_paths.quote("/scratch/jobs"), "/scratch/jobs")

    def test_spaces_are_quoted(self):
        self.assertIn("'", remote_paths.quote("/scratch/my jobs"))

    def test_a_leading_tilde_stays_expandable(self):
        # shlex.quote would produce '~/jobs', which the shell reads as a
        # directory literally named '~'.
        self.assertEqual(remote_paths.quote("~/jobs"), "~/jobs")

    def test_a_tilde_path_with_spaces_quotes_only_the_tail(self):
        result = remote_paths.quote("~/my jobs")
        self.assertTrue(result.startswith("~/"))
        self.assertIn("'", result)

    def test_bare_tilde(self):
        self.assertEqual(remote_paths.quote("~"), "~")

    def test_tilde_slash_only(self):
        self.assertEqual(remote_paths.quote("~/"), "~/")

    def test_empty(self):
        self.assertEqual(remote_paths.quote(""), "''")

    def test_shell_metacharacters_are_neutralised(self):
        for dangerous in ("a; rm -rf /", "a$(whoami)", "a`id`", "a|b", "a&b"):
            quoted = remote_paths.quote(dangerous)
            self.assertTrue(quoted.startswith("'"), dangerous)

    def test_an_embedded_single_quote_is_escaped(self):
        self.assertNotEqual(remote_paths.quote("it's"), "it's")


class TestJoin(unittest.TestCase):
    def test_joins_with_forward_slashes_even_on_windows(self):
        self.assertEqual(remote_paths.join("~/jobs", "run1", "a.inp"), "~/jobs/run1/a.inp")

    def test_empty_segments_are_dropped(self):
        self.assertEqual(remote_paths.join("~/jobs", "", "a.inp"), "~/jobs/a.inp")

    def test_basename_and_dirname(self):
        self.assertEqual(remote_paths.basename("/a/b/c.out"), "c.out")
        self.assertEqual(remote_paths.dirname("/a/b/c.out"), "/a/b")


class TestWrapLogin(unittest.TestCase):
    def test_no_login_commands_returns_the_command(self):
        self.assertEqual(remote_paths.wrap_login("squeue", []), "squeue")

    def test_none_is_tolerated(self):
        self.assertEqual(remote_paths.wrap_login("squeue", None), "squeue")

    def test_login_commands_are_prefixed_with_semicolons(self):
        # ';' not '&&': a profile line that returns non-zero must not block the
        # real command.
        result = remote_paths.wrap_login("squeue", ["source /etc/profile", "module purge"])
        self.assertEqual(result, "source /etc/profile; module purge; squeue")

    def test_blank_lines_are_ignored(self):
        self.assertEqual(remote_paths.wrap_login("ls", ["", "  "]), "ls")


if __name__ == "__main__":
    unittest.main()
