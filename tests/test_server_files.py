import os
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("cryptography")

from job_manager.server_files import download_paths, entries, list_path, virtual_path
from job_manager.job_server_client.errors import ServerError

JOB = str(uuid.UUID(int=1))


class Client:
    def __init__(self):
        self.downloads = []
        self.tree = {
            "": [(JOB, "directory")],
            JOB: [("results", "directory"), ("top.txt", "file")],
            JOB + "/results": [("empty", "directory"), ("nested.txt", "file")],
            JOB + "/results/empty": [],
        }

    def request(self, method, route):
        assert method == "GET"
        query = parse_qs(urlsplit(route).query, keep_blank_values=True)
        path = query["path"][0]
        if route.startswith("/browse/stat?"):
            return {"exists": True, "type": "directory" if path in self.tree else "file"}
        return {
            "entries": [
                {"name": name, "path": (path + "/" if path else "") + name, "type": kind}
                for name, kind in self.tree[path]
            ],
            "next_offset": None,
        }

    def download(self, identifier, relative, target):
        assert identifier == JOB
        self.downloads.append(relative)
        Path(target).write_bytes(b"verified synthetic result")
        return Path(target)


def test_browse_and_download_nested_job_folder_preserves_empty_directories(tmp_path):
    client = Client()
    assert list_path(client, "/") == [JOB + "/"]
    assert list_path(client, "/" + JOB) == ["results/", "top.txt"]
    assert sorted(list_path(client, JOB, 3)) == ["results/nested.txt", "top.txt"]
    downloaded, skipped = download_paths(client, ["/" + JOB], tmp_path)
    assert skipped == [] and len(downloaded) == 2
    assert (tmp_path / JOB / "results" / "empty").is_dir()
    assert (tmp_path / JOB / "results" / "nested.txt").read_bytes() == b"verified synthetic result"


def test_download_does_not_overwrite_existing_files_or_duplicate_basenames(tmp_path):
    client = Client()
    target = tmp_path / "top.txt"
    target.write_text("keep")
    downloaded, skipped = download_paths(client, [JOB + "/top.txt"], tmp_path)
    assert downloaded == [] and len(skipped) == 1
    assert target.read_text() == "keep" and client.downloads == []
    downloaded, skipped = download_paths(client, [JOB + "/top.txt"], tmp_path, overwrite=True)
    assert len(downloaded) == 1 and skipped == []


@pytest.mark.parametrize(
    "path",
    [
        "../server.db",
        "//" + JOB,
        JOB + "/../escape",
        JOB + "/a:stream",
        JOB + "/bad\\path",
        JOB + "/",
        "invalid",
    ],
)
def test_virtual_paths_fail_before_network_or_local_writes(path, tmp_path):
    with pytest.raises((ValueError, ServerError)):
        download_paths(Client(), [path], tmp_path / "new")
    assert not (tmp_path / "new" / "escape").exists()


def test_server_cannot_return_paths_outside_requested_directory():
    client = Client()
    client.request = lambda *a: {
        "entries": [{"name": "escape", "path": JOB + "/escape", "type": "file"}]
    }
    with pytest.raises(ValueError, match="outside"):
        list(entries(client, JOB + "/results"))


def test_non_advancing_pagination_is_rejected():
    client = Client()
    client.request = lambda *a: {"entries": [], "next_offset": 0}
    with pytest.raises(ValueError, match="pagination"):
        list(entries(client, JOB))


def test_local_junction_or_symlink_cannot_redirect_a_download(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "downloads"
    target.mkdir()
    link = target / JOB
    if os.name == "nt":
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises((ValueError, ServerError)):
            download_paths(Client(), [JOB], target, overwrite=True)
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


def test_verified_staging_remains_single_link_after_install(tmp_path):
    download_paths(Client(), [JOB + "/top.txt"], tmp_path)
    target = tmp_path / "top.txt"
    assert target.stat().st_nlink == 1
    assert target.read_bytes() == b"verified synthetic result"
    assert list(tmp_path.iterdir()) == [target]


def test_a_file_created_during_transfer_is_not_replaced(tmp_path):
    client = Client()
    original = client.download

    def racing_download(identifier, relative, target):
        result = original(identifier, relative, target)
        (tmp_path / "top.txt").write_bytes(b"keep concurrent file")
        return result

    client.download = racing_download
    downloaded, skipped = download_paths(client, [JOB + "/top.txt"], tmp_path)
    assert downloaded == [] and len(skipped) == 1
    assert (tmp_path / "top.txt").read_bytes() == b"keep concurrent file"


def test_server_root_is_not_a_download_target(tmp_path):
    with pytest.raises(ValueError, match="entire server"):
        download_paths(Client(), ["/"], tmp_path)
    assert virtual_path("/") == ""


def test_service_routes_server_browser_operations_without_a_shell(monkeypatch, tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from types import SimpleNamespace
    from unittest.mock import Mock
    from job_manager.service import JobService

    backend = Mock()
    backend.list_path.return_value = [JOB + "/"]
    backend.stat_path.return_value = {"type": "file"}
    backend.download_paths.return_value = (["synthetic.txt"], [])
    monkeypatch.setattr("job_manager.server_backend.backend_for", lambda host: backend)
    monkeypatch.setattr(
        "job_manager.service.run_async", lambda pool, work, **kw: kw["on_success"](work())
    )
    service = SimpleNamespace(pool=None)
    host = SimpleNamespace(is_job_server=True)
    done = Mock()
    JobService.list_host_path(service, host, "/", 1, done, pytest.fail)
    done.assert_called_with([JOB + "/"])
    JobService.stat_host_path(service, host, "/" + JOB, False, done, pytest.fail)
    done.assert_called_with({"type": "file"})
    JobService.download_host_paths(service, host, [JOB], str(tmp_path), done, pytest.fail)
    done.assert_called_with((["synthetic.txt"], [], str(tmp_path)))


def test_a_job_id_that_is_not_in_canonical_form_is_refused():
    with pytest.raises(ValueError, match="job UUID"):
        list_path(Client(), JOB.replace("-", ""))


@pytest.mark.parametrize(
    "listed, message",
    [
        ([("top.txt", "file"), ("top.txt", "file")], "duplicated"),
        ([("link", "symlink")], "Unsupported"),
    ],
)
def test_a_listing_with_duplicates_or_unknown_types_is_refused(listed, message):
    client = Client()
    client.tree[JOB] = listed
    with pytest.raises(ValueError, match=message):
        list_path(client, JOB)


def test_a_listing_split_across_pages_is_read_to_the_end():
    client = Client()
    pages = {
        0: {"entries": [{"name": "a", "path": JOB + "/a", "type": "file"}], "next_offset": 1},
        1: {"entries": [{"name": "b", "path": JOB + "/b", "type": "file"}], "next_offset": None},
    }
    client.request = lambda method, route: pages[
        int(parse_qs(urlsplit(route).query)["offset"][0])
    ]
    assert list_path(client, JOB) == ["a", "b"]


def test_a_tree_larger_than_the_limit_is_refused():
    client = Client()
    client.tree[JOB] = [("one", "directory"), ("two", "directory")]
    client.tree[JOB + "/one"] = [(f"f{i}", "file") for i in range(25000)]
    client.tree[JOB + "/two"] = [(f"f{i}", "file") for i in range(25000)]
    with pytest.raises(ValueError, match="too large"):
        list_path(client, JOB, 2)


def test_a_folder_whose_place_is_taken_by_a_file_is_skipped(tmp_path):
    (tmp_path / JOB).write_text("in the way")
    downloaded, skipped = download_paths(Client(), [JOB], tmp_path)
    assert downloaded == [] and "not a directory" in skipped[0][1]


def test_a_job_reported_as_a_file_is_refused(tmp_path):
    client = Client()
    del client.tree[JOB]
    with pytest.raises(ValueError, match="invalid file path"):
        download_paths(client, [JOB], tmp_path)
