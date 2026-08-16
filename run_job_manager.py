"""Standalone launcher for MoleditPy Job Manager."""

from __future__ import annotations

import os
import sys

# Ensure local package directory is on sys.path
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from job_manager.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
