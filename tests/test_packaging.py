"""The PyPI distribution is built from the same tree as the plugin zip.

These check what a wheel would silently get wrong: a subpackage find does not
pick up, a data file left behind, a console script pointing at nothing, or a
version that drifts from PLUGIN_VERSION. The installed wheel itself is
exercised by scripts/smoke_test_installed.py in the release and PyPI workflows.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import re

import pytest

tomllib = pytest.importorskip("tomllib")

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "job_manager"


def _pyproject() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def test_version_comes_from_plugin_version():
    assert _pyproject()["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "job_manager.PLUGIN_VERSION"
    }
    # setuptools reads the attribute without importing the package only while
    # it is a plain string literal; anything else would import Qt at build time.
    tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", "") == "PLUGIN_VERSION" for target in node.targets)
    ]
    assert len(values) == 1 and isinstance(values[0], ast.Constant)
    assert re.fullmatch(r"\d+\.\d+\.\d+", values[0].value)


def test_every_source_folder_is_a_package():
    # packages.find only takes folders with an __init__.py; a module in one
    # without it would be in the zip but missing from the wheel.
    for folder in [PACKAGE, *[p for p in PACKAGE.rglob("*") if p.is_dir()]]:
        if folder.name == "__pycache__" or not any(folder.glob("*.py")):
            continue
        assert (folder / "__init__.py").is_file(), f"{folder} has no __init__.py"


def test_package_data_files_exist():
    data = _pyproject()["tool"]["setuptools"]["package-data"]
    for package, names in data.items():
        folder = ROOT.joinpath(*package.split("."))
        for name in names:
            assert (folder / name).is_file(), f"{package}/{name} is missing"
    non_python = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix not in (".py", ".pyc")
    }
    declared = {
        f"{package.split('.', 1)[1].replace('.', '/')}/{name}"
        for package, names in data.items()
        for name in names
    }
    assert non_python <= declared, f"not declared as package data: {non_python - declared}"


@pytest.mark.parametrize("command", ["moleditpy-job-manager", "moleditpy-job"])
def test_console_scripts_point_at_functions(command):
    target = _pyproject()["project"]["scripts"][command]
    module_name, _, function = target.partition(":")
    if module_name == "job_manager.__main__":
        pytest.importorskip("PySide6")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, function))


def test_license_files_are_shipped():
    project = _pyproject()["project"]
    assert project["license"] == "LGPL-3.0-or-later"
    for name in project["license-files"]:
        assert (ROOT / name).is_file()


def test_release_workflow_expects_this_distribution():
    name = _pyproject()["project"]["name"]
    stem = re.sub(r"[-.]+", "_", name)
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert f"dist/{stem}-${{VERSION}}.tar.gz" in workflow
    assert f"https://pypi.org/p/{name}" in workflow
    assert "scripts/smoke_test_installed.py" in workflow
    assert "dist/*.publish.attestation" in workflow
    assert (ROOT / "scripts" / "smoke_test_installed.py").is_file()
