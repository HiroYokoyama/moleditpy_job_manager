"""Find a bash that can actually run a script, or none at all.

``shutil.which("bash")`` is not enough on Windows: the first hit is normally
``C:\\Windows\\System32\\bash.exe``, the WSL launcher, which cannot see a
``G:\\...`` path and answers every script with

    wsl: Failed to translate 'G:\\DEV_MAIN\\...'

The tests that drive the helper queue then watched an empty queue until their
own timeout expired -- four files, forty seconds a test, all failing. That is
what a "hanging" suite looked like from the outside.

So a candidate is only accepted once it has run a real script from a real
temporary path and said so. Set ``MOLEDITPY_TEST_BASH`` to force a particular
interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

TOKEN = "moleditpy_bash_ok"

#: Windows ships this as the WSL entry point, not as a POSIX shell.
_WSL_SHIM = os.path.normcase(
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bash.exe")
)


def _candidates() -> List[str]:
    forced = os.environ.get("MOLEDITPY_TEST_BASH")
    if forced:
        return [forced]
    found = [shutil.which("bash")]
    if os.name == "nt":
        # Git for Windows, which is a real POSIX shell and does understand a
        # Windows path.
        found += [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ]
    return [path for path in found if path]


def _runs_a_script(path: str) -> bool:
    handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n")
    with handle:
        handle.write(f"echo {TOKEN}\n")
    try:
        result = subprocess.run([path, handle.name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    return TOKEN in (result.stdout or "")


def find_bash() -> Optional[str]:
    """The first interpreter that proves it can run a script, or None."""
    seen = set()
    for candidate in _candidates():
        key = os.path.normcase(candidate)
        if key in seen or key == _WSL_SHIM:
            continue
        seen.add(key)
        if _runs_a_script(candidate):
            return candidate
    return None


#: Probed once per process; the probe is a single sub-second subprocess.
BASH = find_bash()

__all__ = ["BASH", "find_bash"]
