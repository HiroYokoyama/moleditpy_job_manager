"""Dependency traversal, lane balancing and failure propagation without persistence."""

from __future__ import annotations

from typing import List, Optional

from .models import STATE_DONE, STATE_UPLOADING, Job


def _will_run(job: Job) -> bool:
    """Active, or on its way to the host right now.

    For chaining and slot counting only. Submission is asynchronous, so a job
    submitted a moment ago is still UPLOADING -- and leaving it out meant the
    next file of a batch saw nothing queued: "run one after another" put every
    file behind the same predecessor, and a lane limit let the whole batch
    start at once. The worker waits for the predecessor's queue id itself
    (JobService._chain_pid), so chaining behind an uploading job is safe.
    """
    return job.is_active or job.state == STATE_UPLOADING


class DependencyGraph:
    def __init__(self, jobs):
        self.jobs = jobs

    def chain_tail(self, host_id: str) -> Optional[Job]:
        """The job a new one should queue behind on this host, if any.

        The tail of the chain: appending to the newest active job makes
        successive submissions line up instead of all starting at once.
        """
        candidates = [
            job for job in self.jobs.values() if job.host_id == host_id and _will_run(job)
        ]
        # Don't queue behind an already-stranded job -- that would strand this one too.
        runnable = [job for job in candidates if self.chain_blocker(job) is None]
        if not runnable:
            return None
        return max(runnable, key=lambda job: job.submitted_at or job.updated_at)

    def runnable_jobs(self, host_id: str) -> List[Job]:
        """Active jobs on this host that are still going to run."""
        return [
            job
            for job in self.jobs.values()
            if job.host_id == host_id and _will_run(job) and self.chain_blocker(job) is None
        ]

    def chain_lanes(self, host_id: str) -> List[List[Job]]:
        """The chains currently in flight on this host, oldest job first.

        A "lane" is one dependency chain; with nothing else to serialise them,
        the lane count is what a slot limit counts.
        """
        active = self.runnable_jobs(host_id)
        by_id = {job.id: job for job in active}
        # A job with an active successor is not the end of its chain.
        followed = {job.after_job_id for job in active if job.after_job_id in by_id}
        lanes: List[List[Job]] = []
        for tail in active:
            if tail.id in followed:
                continue
            chain = [tail]
            cursor = tail
            # Guard against a cycle (a job list is a file, opened from anywhere)
            # which would otherwise loop forever on the GUI thread.
            seen = {tail.id}
            while cursor.after_job_id in by_id and cursor.after_job_id not in seen:
                cursor = by_id[cursor.after_job_id]
                seen.add(cursor.id)
                chain.append(cursor)
            lanes.append(list(reversed(chain)))
        return lanes

    def free_slot(self, host_id: str, limit: int) -> bool:
        """True when a job submitted now would start straight away."""
        return limit <= 0 or len(self.chain_lanes(host_id)) < limit

    def chain_lane_tail(self, host_id: str, limit: int) -> Optional[Job]:
        """What a new job should queue behind to respect a slot limit.

        None means "start now" (no limit, or a free lane). Otherwise joins the
        *shortest* lane, so submissions balance across lanes rather than piling
        onto one chain.
        """
        if limit <= 0:
            return None
        lanes = self.chain_lanes(host_id)
        if len(lanes) < limit:
            return None
        shortest = min(lanes, key=len)
        return shortest[-1]

    def chain_blocker(self, job: Job) -> Optional[Job]:
        """The dead job that will stop ``job`` ever starting, if any.

        Under ``afterok`` a failed/cancelled predecessor leaves everything
        behind it PENDING forever. The whole chain is walked, not just the
        immediate predecessor: in A(failed) <- B <- C, C is exactly as dead as
        B (bug once had it counting as a live lane, holding a slot forever).
        ``chain_any`` is read at the link that meets the failure: a job behind
        one that *ended* badly is released, one behind a job that never starts
        is not.
        """
        from .schedulers import get_scheduler

        if not job.is_active:
            return None
        cursor = job
        # Guard against a cycle (see chain_lanes) walking forever on the GUI thread.
        seen = {job.id}
        while cursor.after_job_id:
            predecessor = self.jobs.get(cursor.after_job_id)
            if predecessor is None or predecessor.id in seen:
                return None
            if not predecessor.is_terminal:
                # Still going to run, unless something further back is dead.
                seen.add(predecessor.id)
                cursor = predecessor
                continue
            if predecessor.state == STATE_DONE or cursor.chain_any:
                return None
            try:
                scheduler = get_scheduler(cursor.scheduler)
            except ValueError:
                return None
            return None if scheduler.chain_releases_on_failure else predecessor
        return None

    def dependents_of(self, job_id: str, recursive: bool = False) -> List[Job]:
        """Every job chained behind this one. ``recursive`` follows to the end."""
        direct = [job for job in self.jobs.values() if job.after_job_id == job_id]
        if not recursive:
            return direct
        found: List[Job] = []
        seen = {job_id}
        queue = list(direct)
        while queue:
            job = queue.pop(0)
            if job.id in seen:
                continue
            seen.add(job.id)
            found.append(job)
            queue.extend(j for j in self.jobs.values() if j.after_job_id == job.id)
        return found
