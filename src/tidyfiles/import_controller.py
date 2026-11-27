"""Coordinate bounded discovery, Tk-thread consumption, and table finalization."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any
from uuid import UUID

from tidyfiles.collection import PreparedImportOrder, WorkingCollection
from tidyfiles.config import DuplicatePreferences
from tidyfiles.discovery import (
    DiscoveryDisposition,
    DiscoveryItem,
    DiscoveryReport,
    DiscoverySession,
    DiscoveryState,
    FilesystemDiscovery,
)
from tidyfiles.duplicate_analysis import (
    DuplicateAnalysisResult,
    DuplicateAnalysisState,
    DuplicateAnalyzer,
    DuplicateProgress,
)

ViewState = tuple[tuple[UUID, ...], tuple[UUID, ...]]
ActivityCallback = Callable[[str, DiscoverySession | None], None]
ItemsCallback = Callable[[tuple[DiscoveryItem, ...]], None]
OrderCallback = Callable[[tuple[UUID, ...], int, int, bool], None]
RemoveCallback = Callable[[tuple[UUID, ...]], None]


class ImportController:
    """Own one import lifecycle while keeping widget work on the Tk thread."""

    POLL_MS = 16
    CONSUME_LIMIT = 16
    MAX_CONSUMED_PER_TICK = 32
    CALLBACK_BUDGET_SECONDS = 0.008
    RENDER_CHUNK = 96

    def __init__(
        self,
        root: Any,
        collection: WorkingCollection,
        *,
        capture_view_state: Callable[[], ViewState],
        on_activity: ActivityCallback,
        on_items: ItemsCallback,
        on_order_chunk: OrderCallback,
        on_remove_chunk: RemoveCallback,
        on_rollback_complete: Callable[[tuple[UUID, ...], ViewState], None],
        on_rollback_rebuild: Callable[[ViewState], None],
        on_finished: Callable[[DiscoveryReport], None],
        on_error: Callable[[str], None],
        on_shutdown: Callable[[], None],
        discovery: FilesystemDiscovery | None = None,
        duplicate_preferences: Callable[[], DuplicatePreferences] | None = None,
        duplicate_analyzer: DuplicateAnalyzer | None = None,
    ) -> None:
        self.root = root
        self.collection = collection
        self.discovery = discovery or FilesystemDiscovery(collection)
        self.capture_view_state = capture_view_state
        self.on_activity = on_activity
        self.on_items = on_items
        self.on_order_chunk = on_order_chunk
        self.on_remove_chunk = on_remove_chunk
        self.on_rollback_complete = on_rollback_complete
        self.on_rollback_rebuild = on_rollback_rebuild
        self.on_finished = on_finished
        self.on_error = on_error
        self.on_shutdown = on_shutdown
        self._duplicate_preferences = duplicate_preferences
        self._duplicate_analyzer = duplicate_analyzer
        self.duplicate_result: DuplicateAnalysisResult | None = None
        self.duplicate_progress: DuplicateProgress | None = None
        self._prepared_duplicates: DuplicateAnalysisResult | None = None
        self._import_duplicate_preferences: DuplicatePreferences | None = None

        self.session: DiscoverySession | None = None
        self._phase = "idle"
        self._after_id: Any = None
        self._closed = False
        self._shutdown_requested = False
        self._prior_view: ViewState = ((), ())
        self._new_entry_ids: list[UUID] = []
        self._new_entry_set: set[UUID] = set()
        self._prepared_order: PreparedImportOrder | None = None
        self._prepare_error: Exception | None = None
        self._prepare_done: threading.Event | None = None
        self._render_order: tuple[UUID, ...] = ()
        self._render_offset = 0
        self._remove_offset = 0
        self._order_was_changed = False
        self._started_at: float | None = None
        self._order_started_at: float | None = None
        self._time_to_first_visible_seconds: float | None = None
        self._final_order_seconds: float | None = None
        self._time_to_all_rows_rendered_seconds: float | None = None
        self._discovery_duration_seconds: float | None = None

    @property
    def busy(self) -> bool:
        return self._phase != "idle"

    @property
    def cancellation_applicable(self) -> bool:
        return (
            self.session is not None
            and not self.session.is_terminal
            and self._phase in {"discovering", "preparing", "rendering"}
        )

    @property
    def performance(self) -> dict[str, float | int | None]:
        return {
            "time_to_first_visible_seconds": self._time_to_first_visible_seconds,
            "discovery_duration_seconds": self._discovery_duration_seconds,
            "final_order_seconds": self._final_order_seconds,
            "time_to_all_rows_rendered_seconds": self._time_to_all_rows_rendered_seconds,
            "new_rows_rendered": len(self._new_entry_ids),
        }

    def start_files(self, paths: Iterable[str | os.PathLike[str]] | str) -> bool:
        if isinstance(paths, (str, os.PathLike)):
            selected = (paths,)
        else:
            selected = tuple(paths)
        if not selected:
            return False
        return self._start(lambda: self.discovery.begin_files(selected))

    def start_folder(self, root_path: str | os.PathLike[str]) -> bool:
        if not root_path:
            return False
        return self._start(lambda: self.discovery.begin_folder(root_path))

    def _start(self, begin: Callable[[], DiscoverySession]) -> bool:
        if self.busy or self._closed or self._shutdown_requested:
            return False
        try:
            prior_view = self.capture_view_state()
            preferences = self._duplicate_preferences() if self._duplicate_preferences else None
            session = begin()
        except Exception as error:
            self.on_error(str(error))
            return False

        self.session = session
        self._prior_view = prior_view
        self._new_entry_ids = []
        self._new_entry_set = set()
        self._prepared_order = None
        self._prepared_duplicates = None
        self.duplicate_progress = None
        self._import_duplicate_preferences = preferences
        self._prepare_error = None
        self._prepare_done = None
        self._render_order = ()
        self._render_offset = 0
        self._remove_offset = 0
        self._order_was_changed = False
        self._started_at = time.perf_counter()
        self._order_started_at = None
        self._time_to_first_visible_seconds = None
        self._final_order_seconds = None
        self._time_to_all_rows_rendered_seconds = None
        self._discovery_duration_seconds = None
        self._phase = "discovering"
        try:
            session.start()
            self.on_activity(self._phase, session)
        except Exception as error:
            self._fail_current(error)
        self._schedule(0)
        return True

    def cancel(self) -> bool:
        if not self.cancellation_applicable:
            return False
        assert self.session is not None
        self.session.cancel()
        self.on_activity("cancelling", self.session)
        self._schedule(0)
        return True

    def request_shutdown(self) -> bool:
        """Return True if idle and ready to close, otherwise cancel and drain first."""
        self._shutdown_requested = True
        if not self.busy:
            return True
        self.cancel()
        return False

    def dispose(self) -> None:
        self._closed = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _schedule(self, delay_ms: int | None = None) -> None:
        if self._closed or not self.busy or self._after_id is not None:
            return
        try:
            self._after_id = self.root.after(
                self.POLL_MS if delay_ms is None else delay_ms, self._poll
            )
        except Exception:
            self._closed = True

    def _poll(self) -> None:
        self._after_id = None
        if self._closed or self.session is None:
            return
        try:
            if self._phase == "discovering":
                self._poll_discovery()
            elif self._phase == "preparing":
                self._poll_preparation()
            elif self._phase == "rendering":
                self._poll_rendering()
            elif self._phase == "restoring":
                self._poll_restoration()
            elif self._phase == "removing":
                self._poll_removal()
        except Exception as error:
            if self._phase in {"restoring", "removing"}:
                self._finish_terminal(rollback_error=error)
            else:
                self._fail_current(error)
        if self.busy:
            self._schedule()

    def _poll_discovery(self) -> None:
        assert self.session is not None
        session = self.session
        started = time.perf_counter()
        consumed: list[DiscoveryItem] = []
        while (
            len(consumed) < self.MAX_CONSUMED_PER_TICK
            and time.perf_counter() - started < self.CALLBACK_BUDGET_SECONDS
        ):
            batch = session.consume_batch(
                limit=min(
                    self.CONSUME_LIMIT,
                    self.MAX_CONSUMED_PER_TICK - len(consumed),
                ),
                defer_finalization=True,
            )
            if not batch:
                break
            consumed.extend(batch)

        for item in consumed:
            if (
                item.disposition is DiscoveryDisposition.ACCEPTED
                and item.entry is not None
                and item.entry.entry_id not in self._new_entry_set
            ):
                self._new_entry_set.add(item.entry.entry_id)
                self._new_entry_ids.append(item.entry.entry_id)
        if consumed:
            self.on_items(tuple(consumed))
            if (
                self._time_to_first_visible_seconds is None
                and self._new_entry_ids
                and self._started_at is not None
            ):
                self._time_to_first_visible_seconds = time.perf_counter() - self._started_at
        self.on_activity(self._phase, session)

        if session.is_terminal:
            self._handle_terminal()
        elif session.ready_to_finalize:
            self._start_preparation()

    def _start_preparation(self) -> None:
        assert self.session is not None
        session = self.session
        session_id = session.session_id
        preferences = self._import_duplicate_preferences
        analyzer = self._duplicate_analyzer
        done = threading.Event()
        self._phase = "preparing"
        self._order_started_at = time.perf_counter()
        self._prepare_done = done
        self.on_activity(self._phase, self.session)

        def prepare() -> None:
            try:
                order = self.collection.prepare_import_order(session_id)
                if preferences is not None and preferences.enabled and analyzer is not None:
                    snapshot = self.collection.duplicate_analysis_snapshot(order)
                    result = analyzer.analyze(
                        snapshot,
                        algorithm=preferences.algorithm,
                        workers=preferences.workers,
                        cancel_check=session.cancellation_requested,
                        progress_callback=self._duplicate_progress,
                    )
                    if result.state is DuplicateAnalysisState.COMPLETE:
                        if (result.collection_id, result.revision) != (
                            order.collection_id, order.revision
                        ):
                            raise ValueError("Exact duplicate analysis is stale")
                        self._prepared_duplicates = result
                        order = replace(order, duplicate_pairs=tuple(
                            (entry_id, group.keeper_id)
                            for group in result.groups for entry_id in group.redundant_ids
                        ))
                    else:
                        session.cancel()
                self._prepared_order = order
            except Exception as error:
                self._prepare_error = error
            finally:
                done.set()

        threading.Thread(
            target=prepare,
            name=f"TidyFiles-order-{session_id}",
            daemon=True,
        ).start()

    def _duplicate_progress(self, progress: DuplicateProgress) -> None:
        self.duplicate_progress = progress

    def _poll_preparation(self) -> None:
        assert self.session is not None and self._prepare_done is not None
        session = self.session
        if not self._prepare_done.is_set():
            self.on_activity(self._phase, session)
            return
        if self._order_started_at is not None:
            self._final_order_seconds = time.perf_counter() - self._order_started_at
        if session.is_terminal:
            self._handle_terminal()
            return
        if self._prepare_error is not None:
            self._fail_current(self._prepare_error)
            return
        if session.state is DiscoveryState.CANCELLING or self._shutdown_requested:
            self._begin_restore()
            return
        assert self._prepared_order is not None
        excluded = {entry_id for entry_id, _ in self._prepared_order.duplicate_pairs}
        order = self._prepared_order.entry_ids
        self._render_order = tuple(entry_id for entry_id in order if entry_id not in excluded)
        self._phase = "rendering"
        self._render_offset = 0
        self.on_activity(self._phase, session)

    def _poll_rendering(self) -> None:
        assert self.session is not None and self._prepared_order is not None
        session = self.session
        if session.state is DiscoveryState.CANCELLING or self._shutdown_requested:
            self._begin_restore()
            return
        order = self._render_order
        end = min(self._render_offset + self.RENDER_CHUNK, len(order))
        if end > self._render_offset:
            self._order_was_changed = True
            self.on_order_chunk(order, self._render_offset, end, True)
            self._render_offset = end
            self.on_activity(self._phase, session)
            return

        try:
            session.finish_deferred(self._prepared_order)
        except Exception as error:
            self._fail_current(error)
            return
        if session.state in {DiscoveryState.CANCELLED, DiscoveryState.FAILED}:
            self._handle_terminal()
            return
        self.duplicate_result = self._prepared_duplicates
        if self._started_at is not None:
            self._time_to_all_rows_rendered_seconds = time.perf_counter() - self._started_at
        self._handle_terminal()

    def _begin_restore(self) -> None:
        assert self.session is not None
        if not self.session.is_terminal and self.session.ready_to_finalize:
            self.session.finish_deferred()
        if not self.session.is_terminal:
            self._phase = "discovering"
            self.on_activity("cancelling", self.session)
            return
        self._phase = "restoring" if self._order_was_changed else "removing"
        self._render_offset = 0
        self._remove_offset = 0
        self.on_activity(self._phase, self.session)

    def _poll_restoration(self) -> None:
        assert self.session is not None
        order = self._prior_view[0]
        end = min(self._render_offset + self.RENDER_CHUNK, len(order))
        if end > self._render_offset:
            self.on_order_chunk(order, self._render_offset, end, True)
            self._render_offset = end
            self.on_activity(self._phase, self.session)
            return
        self._phase = "removing"
        self._remove_offset = 0
        self.on_activity(self._phase, self.session)

    def _poll_removal(self) -> None:
        assert self.session is not None
        end = min(self._remove_offset + self.RENDER_CHUNK, len(self._new_entry_ids))
        if end > self._remove_offset:
            self.on_remove_chunk(tuple(self._new_entry_ids[self._remove_offset:end]))
            self._remove_offset = end
            self.on_activity(self._phase, self.session)
            return
        self.on_rollback_complete(tuple(self._new_entry_ids), self._prior_view)
        self._finish_terminal()

    def _handle_terminal(self) -> None:
        assert self.session is not None
        if self.session.state in {DiscoveryState.CANCELLED, DiscoveryState.FAILED}:
            self._begin_restore()
            return
        self._finish_terminal()

    def _fail_current(self, error: Exception) -> None:
        session = self.session
        if session is None:
            self.on_error(str(error))
            return
        if not session.is_terminal:
            session.fail(f"Import integration failed: {error}")
            if session.ready_to_finalize:
                session.finish_deferred()
        if session.is_terminal:
            self._begin_restore()
        else:
            self._phase = "discovering"

    def _finish_terminal(self, *, rollback_error: Exception | None = None) -> None:
        assert self.session is not None
        report = self.session.report
        self._discovery_duration_seconds = report.duration_seconds
        self.session = None
        self._phase = "idle"
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            message = None
            if rollback_error is not None:
                message = f"Could not update the table during import rollback: {rollback_error}"
                try:
                    self.on_rollback_rebuild(self._prior_view)
                except Exception as error:
                    message += f"\nTable rebuild failed: {error}. Use Refresh Preview to retry."
            try:
                self.on_activity("idle", None)
                self.on_finished(report)
            except Exception as error:
                self.on_error(str(error))
            if message is not None:
                self.on_error(message)
        finally:
            if self._shutdown_requested:
                self._closed = True
                self.on_shutdown()
