import pytest

pytest.importorskip("PySide6")


@pytest.mark.parametrize("stamp", [1e100, -1e100])
def test_unrepresentable_job_timestamps_do_not_break_table_or_export(stamp):
    from job_manager.job_table import format_stamp
    from job_manager.store import _stamp

    assert format_stamp(stamp) == "-"
    assert _stamp(stamp) == ""
