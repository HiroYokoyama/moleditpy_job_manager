"""Manual workflow input must stay data, even when it resembles shell code."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_blocks(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "run: |":
            indent = len(line) - len(line.lstrip()) + 2
            block = []
            for following in lines[index + 1 :]:
                if following.strip() and len(following) - len(following.lstrip()) < indent:
                    break
                block.append(following[indent:])
            yield "\n".join(block)


def test_untrusted_workflow_inputs_are_not_inserted_in_shell_code():
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        for block in run_blocks(workflow):
            assert "${{ github.event.inputs." not in block
            assert "${{ github.event.release.tag_name" not in block


@pytest.mark.parametrize("value", ["1.2.3", "1.2.$(touch injected-marker)", "1.2.3\nINJECTED=yes"])
def test_release_version_is_strict_data(tmp_path, value):
    bash = shutil.which("bash")
    if os.name == "nt":
        bash = r"C:\Program Files\Git\bin\bash.exe"
    if not bash or not Path(bash).exists():
        pytest.skip("bash unavailable")
    block = next(run_blocks(ROOT / ".github/workflows/release.yml"))
    env = {
        **os.environ,
        "EVENT_NAME": "workflow_dispatch",
        "INPUT_VERSION": value,
        "GITHUB_ENV": (tmp_path / "environment").as_posix(),
    }
    result = subprocess.run(
        [bash, "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == (0 if value == "1.2.3" else 1)
    assert not (tmp_path / "injected-marker").exists()
    if result.returncode == 0:
        assert (tmp_path / "environment").read_text().strip() == "VERSION=1.2.3"
