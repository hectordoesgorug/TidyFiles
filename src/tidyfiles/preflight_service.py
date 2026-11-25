"""Bounded background execution for read-only preflight requests."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from threading import Condition, Event, Thread, current_thread
from typing import Callable
from uuid import UUID

from tidyfiles.execution_preflight import (
    PreflightDiagnostic,
    PreflightProgress,
    PreflightRequest,
    PreflightResult,
    PreflightState,
    _initial_result,
    run_preflight,
)


@dataclass(slots=True)
class _Job:
    request: PreflightRequest
    callback: Callable[[PreflightProgress], None] | None
    future: Future[PreflightResult] = field(default_factory=Future)
    cancelled: Event = field(default_factory=Event)

    def terminal(self, state: PreflightState, code: str) -> PreflightResult:
        return replace(
            _initial_result(self.request), state=state, diagnostic=PreflightDiagnostic(code)
        )


class PreflightService:
    """One active request and one replaceable pending request."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._active: _Job | None = None
        self._pending: _Job | None = None
        self._latest_id: UUID | None = None
        self._result: PreflightResult | None = None
        self._closed = False
        self._thread = Thread(target=self._work, name="TidyFiles-preflight", daemon=True)
        self._thread.start()

    def submit(
        self,
        request: PreflightRequest,
        *,
        progress_callback: Callable[[PreflightProgress], None] | None = None,
    ) -> Future[PreflightResult]:
        job = _Job(request, progress_callback)
        # Cancellation is delivered as a structured result through the service.
        job.future.set_running_or_notify_cancel()
        with self._condition:
            if self._closed:
                raise RuntimeError("Preflight service is closed")
            if request.request_id == self._latest_id or (
                self._active is not None and request.request_id == self._active.request.request_id
            ):
                raise ValueError("A new preflight requires a new request ID")
            displaced = self._pending
            if self._active is not None:
                self._active.cancelled.set()
            self._pending = job
            self._latest_id = request.request_id
            self._result = None
            self._condition.notify()
        if displaced is not None:
            displaced.future.set_result(
                displaced.terminal(PreflightState.STALE_INPUT, "superseded")
            )
        return job.future

    def current_result(self) -> PreflightResult | None:
        with self._condition:
            return self._result

    def cancel(self, request_id: UUID) -> bool:
        with self._condition:
            if request_id != self._latest_id:
                return False
            pending = self._pending
            if pending is None:
                if self._active is None or self._active.request.request_id != request_id:
                    return False
                self._active.cancelled.set()
                return True
            self._pending = None
            result = pending.terminal(PreflightState.CANCELLED, "cancelled")
            self._result = result
        pending.future.set_result(result)
        return True

    def close(self, *, wait: bool = True) -> None:
        with self._condition:
            self._closed = True
            self._result = None
            pending, self._pending = self._pending, None
            if self._active is not None:
                self._active.cancelled.set()
            self._condition.notify_all()
        if pending is not None:
            pending.future.set_result(pending.terminal(PreflightState.CANCELLED, "closed"))
        if wait and current_thread() is not self._thread:
            self._thread.join()

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                job, self._pending = self._pending, None
                assert job is not None
                self._active = job
            result = run_preflight(
                job.request,
                cancel_check=job.cancelled.is_set,
                progress_callback=job.callback,
            )
            with self._condition:
                if self._closed:
                    result = job.terminal(PreflightState.CANCELLED, "closed")
                elif job.request.request_id != self._latest_id:
                    result = job.terminal(PreflightState.STALE_INPUT, "superseded")
                else:
                    if job.cancelled.is_set() and result.state is not PreflightState.CANCELLED:
                        result = job.terminal(PreflightState.CANCELLED, "cancelled")
                    self._result = result
                self._active = None
            job.future.set_result(result)

    def __enter__(self) -> PreflightService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
