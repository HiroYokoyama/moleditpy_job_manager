from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")

from job_manager.result_view import read_text_for_view  # noqa: E402


def test_growing_result_is_still_read_with_a_byte_limit(tmp_path):
    path = tmp_path / "growing.out"
    path.write_bytes(b"x" * 100)
    with patch("job_manager.result_view.os.path.getsize", return_value=1):
        text = read_text_for_view(str(path), limit=16)
    assert text == "x" * 16
