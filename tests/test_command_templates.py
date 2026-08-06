"""Built-in command templates, the [tag] syntax, and the user's own templates.

The command line is the one thing the plugin cannot infer: every site spells
its launcher differently. These check that the built-in starting points match
what each MoleditPy input generator actually writes, that both placeholder
spellings resolve, and that a saved template survives in settings.json.
"""

import os
import tempfile
import unittest

from job_manager.command_templates import (
    TEMPLATES,
    CommandTemplate,
    extension_of,
    suggest,
    templates_for,
)
from job_manager.models import SubmitPreset
from job_manager.schedulers import get_scheduler
from job_manager.schedulers.base import format_command, placeholder_values
from job_manager.store import JobStore


class TestTheBuiltInSet(unittest.TestCase):
    def test_every_program_moleditpy_writes_input_for_is_covered(self):
        labels = " ".join(t.label.lower() for t in TEMPLATES)
        for program in (
            "orca",
            "gaussian",
            "cp2k",
            "gamess",
            "mopac",
            "nwchem",
            "psi4",
            "pyscf",
            "quantum espresso",
            "vasp",
            "xtb",
        ):
            self.assertIn(program, labels, program)

    def test_labels_are_unique(self):
        labels = [t.label for t in TEMPLATES]
        self.assertEqual(len(labels), len(set(labels)))

    def test_every_command_uses_only_known_placeholders(self):
        known = set(placeholder_values("mol.inp", SubmitPreset()))
        for template in TEMPLATES:
            rendered = format_command(template.command, "mol.inp", SubmitPreset())
            self.assertNotIn("{", rendered, template.label)
            self.assertNotIn("[", rendered, template.label)
        self.assertIn("output", known)

    def test_the_custom_entry_is_empty_so_it_clears_the_field(self):
        custom = [t for t in TEMPLATES if t.label.startswith("Custom")]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0].command, "")

    def test_vasp_takes_no_input_filename(self):
        vasp = next(t for t in TEMPLATES if t.label == "VASP")
        self.assertNotIn("{input}", vasp.command)
        self.assertIn("INCAR", vasp.note)


class TestMatchingAnInputFile(unittest.TestCase):
    def test_extensions_match_what_the_generators_write(self):
        for filename, expected in (
            ("mol.gjf", "Gaussian 16"),
            ("mol.com", "Gaussian 16"),
            ("mol.nw", "NWChem"),
            ("mol.mop", "MOPAC"),
            ("mol.py", "PySCF (Python script)"),
            ("mol.xyz", "xTB (optimisation)"),
        ):
            self.assertEqual(templates_for(filename)[0].label, expected, filename)

    def test_an_ambiguous_extension_is_not_guessed(self):
        # .inp is ORCA, CP2K and GAMESS; picking one silently would be worse.
        self.assertIsNone(suggest("mol.inp"))

    def test_an_unambiguous_extension_is_suggested(self):
        self.assertEqual(suggest("mol.nw").label, "NWChem")

    def test_an_unknown_extension_suggests_nothing(self):
        self.assertIsNone(suggest("mol.qqq"))
        self.assertIsNone(suggest(""))

    def test_nothing_is_dropped_when_reordering(self):
        self.assertEqual(len(templates_for("mol.gjf")), len(TEMPLATES))

    def test_extension_of_is_case_insensitive(self):
        self.assertEqual(extension_of("MOL.GJF"), ".gjf")


class TestPlaceholderSpellings(unittest.TestCase):
    def setUp(self):
        self.preset = SubmitPreset(ntasks=8, cpus_per_task=4, memory="16G", queue="gpu")

    def render(self, template):
        return format_command(template, "mol.inp", self.preset)

    def test_square_brackets_work_like_braces(self):
        self.assertEqual(self.render("orca [input]"), "orca mol.inp")
        self.assertEqual(self.render("orca {input}"), "orca mol.inp")

    def test_the_output_tag(self):
        self.assertEqual(self.render("run [input] > [output]"), "run mol.inp > mol.out")

    def test_the_two_spellings_can_be_mixed(self):
        self.assertEqual(self.render("x {input} [output]"), "x mol.inp mol.out")

    def test_resource_tags(self):
        self.assertEqual(self.render("mpirun -np [ntasks]"), "mpirun -np 8")
        self.assertEqual(self.render("-c [cpus] --mem [memory]"), "-c 4 --mem 16G")

    def test_shell_braces_are_left_alone(self):
        # This used to make str.format raise, and the fallback then ran the
        # command with nothing substituted at all.
        rendered = self.render("awk '{print $1}' [input] > [output]")
        self.assertEqual(rendered, "awk '{print $1}' mol.inp > mol.out")

    def test_a_shell_test_expression_is_left_alone(self):
        rendered = self.render("if [ -f [input] ]; then orca [input]; fi")
        self.assertIn("[ -f mol.inp ]", rendered)

    def test_an_unknown_tag_is_left_verbatim(self):
        self.assertEqual(self.render("run [nonsense] {alsonot}"), "run [nonsense] {alsonot}")

    def test_an_empty_template_is_empty(self):
        self.assertEqual(self.render(""), "")

    def test_the_tags_reach_the_generated_script(self):
        preset = SubmitPreset(command_template="pw.x -in [input] > [output]")
        script = get_scheduler("slurm").build_script("j", preset, "si.in", "job.log")
        self.assertIn("pw.x -in si.in > si.out", script)


class TestUserTemplatesPersist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="user_templates_")
        self.store = JobStore(self.tmp)

    def reloaded(self):
        return JobStore(self.tmp)

    def test_a_saved_template_is_written_to_settings_json(self):
        self.store.add_user_template("My ORCA", "orca [input] > [output]")
        with open(os.path.join(self.tmp, "settings.json"), encoding="utf-8") as handle:
            self.assertIn("My ORCA", handle.read())

    def test_it_survives_a_restart(self):
        self.store.add_user_template("My ORCA", "orca [input] > [output]")
        self.assertEqual(
            self.reloaded().user_templates(),
            [{"label": "My ORCA", "command": "orca [input] > [output]"}],
        )

    def test_saving_the_same_label_replaces_it(self):
        self.store.add_user_template("mine", "first")
        self.store.add_user_template("mine", "second")
        templates = self.reloaded().user_templates()
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["command"], "second")

    def test_they_are_sorted_by_label(self):
        for label in ("zeta", "alpha", "Mid"):
            self.store.add_user_template(label, "x")
        self.assertEqual(
            [t["label"] for t in self.store.user_templates()], ["alpha", "Mid", "zeta"]
        )

    def test_removing_one(self):
        self.store.add_user_template("keep", "a")
        self.store.add_user_template("drop", "b")
        self.store.remove_user_template("drop")
        self.assertEqual([t["label"] for t in self.reloaded().user_templates()], ["keep"])

    def test_a_blank_label_is_refused(self):
        self.store.add_user_template("   ", "x")
        self.assertEqual(self.store.user_templates(), [])

    def test_a_corrupt_entry_does_not_break_the_list(self):
        self.store.set_pref("command_templates", ["nonsense", {"no_label": 1}, {"label": "ok"}])
        self.assertEqual(self.store.user_templates(), [{"label": "ok", "command": ""}])

    def test_a_user_template_renders_like_a_built_in(self):
        self.store.add_user_template("mine", "vasp_gam > [output]")
        saved = self.store.user_templates()[0]
        rendered = format_command(saved["command"], "POSCAR", SubmitPreset())
        self.assertEqual(rendered, "vasp_gam > POSCAR.out")

    def test_the_dataclass_accepts_a_saved_entry(self):
        self.store.add_user_template("mine", "run [input]")
        saved = self.store.user_templates()[0]
        template = CommandTemplate(saved["label"], saved["command"])
        self.assertEqual(template.extensions, ())


if __name__ == "__main__":
    unittest.main()
