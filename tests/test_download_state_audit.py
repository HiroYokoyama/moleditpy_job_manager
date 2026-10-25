"""Actual service callbacks with synthetic jobs and no network."""

import os
from unittest.mock import patch

from job_manager.models import STATE_DONE, STATE_RUNNING

from .test_service import ServiceTestCase


class TestAudit(ServiceTestCase):
    def test_missing_remote_file_does_not_silently_return_stale_cache(self):
        job = self.submit()
        cache = self.store.cache_dir(job.id, create=True)
        old = os.path.join(cache, "result.out")
        with open(old, "w", encoding="utf-8") as handle:
            handle.write("old calculation")
        ready, errors = [], []
        with patch("job_manager.service.fetch_results", return_value=[]):
            self.service.fetch_file_to_cache(job, "result.out", ready.append, errors.append)
        assert not ready
        assert errors
        with open(old, encoding="utf-8") as handle:
            assert handle.read() == "old calculation"

    def test_download_does_not_hide_running_job_or_undo_completion(self):
        job = self.submit()
        job.touch(STATE_RUNNING)

        def transfer(*args, **kwargs):
            assert job.is_active
            job.touch(STATE_DONE)
            return []

        with patch("job_manager.service.fetch_results", side_effect=transfer):
            self.service.download(job)
        assert job.state == STATE_DONE

    def test_download_failure_does_not_undo_completion(self):
        job = self.submit()
        job.touch(STATE_RUNNING)

        def transfer(*args, **kwargs):
            job.touch(STATE_DONE)
            raise OSError("connection reset")

        with patch("job_manager.service.fetch_results", side_effect=transfer):
            self.service.download(job)
        assert job.state == STATE_DONE
