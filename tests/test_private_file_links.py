import os
import logging
from job_manager.api_core import write_private_file


def test_secret_writer_does_not_follow_predictable_temporary_hardlink(tmp_path):
    victim = tmp_path / "keep.txt"
    victim.write_text("keep me")
    path = tmp_path / "api_token"
    os.link(victim, str(path) + ".tmp" + str(os.getpid()))
    write_private_file(str(path), "audit-secret")
    assert victim.read_text() == "keep me"
    assert path.read_text() == "audit-secret"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_web_monitor_does_not_log_bookmark_token(caplog):
    from job_manager.web_monitor import _Handler

    handler = object.__new__(_Handler)
    handler.command = "GET"
    handler.path = "/?token=audit-secret"
    with caplog.at_level(logging.DEBUG):
        handler.log_message('"%s" %s %s', "GET /?token=audit-secret HTTP/1.1", "200", "10")
    assert "audit-secret" not in caplog.text
    assert "GET" in caplog.text
