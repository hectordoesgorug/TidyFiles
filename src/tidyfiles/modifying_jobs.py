"""Exclusive application-level coordination for future modifying operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Lock
from uuid import UUID, uuid4

from tidyfiles.transaction_journal import JournalSession
from tidyfiles.transaction_record import OperationType


class ModifyingJobError(RuntimeError):
    """The requested job transition is unavailable."""


@dataclass(frozen=True, slots=True)
class ModifyingJob:
    job_id: UUID
    operation: OperationType
    modifying: bool = False
    cancel_requested: bool = False


class ModifyingJobCoordinator:
    """The application owns one coordinator alongside its existing process mutex."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._job: ModifyingJob | None = None
        self._journal: JournalSession | None = None

    @property
    def current(self) -> ModifyingJob | None:
        with self._lock:
            return self._job

    @property
    def busy(self) -> bool:
        return self.current is not None

    @property
    def can_cancel(self) -> bool:
        job = self.current
        return bool(job and not job.modifying and not job.cancel_requested)

    @property
    def normal_close_permitted(self) -> bool:
        job = self.current
        return job is None or not job.modifying

    def reserve(self, operation: OperationType) -> ModifyingJob:
        with self._lock:
            if self._job is not None:
                raise ModifyingJobError("Another modifying job is active")
            self._job = ModifyingJob(uuid4(), operation)
            return self._job

    def _require_job(self, job_id: UUID) -> ModifyingJob:
        if self._job is None or self._job.job_id != job_id:
            raise ModifyingJobError("The request does not own the active job")
        return self._job

    def cancel_preparation(self, job_id: UUID) -> bool:
        with self._lock:
            job = self._require_job(job_id)
            if job.modifying or job.cancel_requested:
                return False
            self._job = replace(job, cancel_requested=True)
            return True

    def begin_modification(self, job_id: UUID, journal: JournalSession) -> None:
        """Enter the modifying phase only after its journal boundary succeeds."""
        with self._lock:
            job = self._require_job(job_id)
            if (
                job.modifying
                or job.cancel_requested
                or job.operation is not journal.record.operation
            ):
                raise ModifyingJobError("The job cannot start modification with this journal")
            journal.start()
            self._journal = journal
            self._job = replace(job, modifying=True)

    def finish(self, job_id: UUID) -> None:
        """Release ownership after the caller has stopped its workers and handled failure."""
        with self._lock:
            self._require_job(job_id)
            if self._journal is not None and self._journal.can_modify:
                raise ModifyingJobError("Record a verified outcome or recovery requirement first")
            self._journal = None
            self._job = None
