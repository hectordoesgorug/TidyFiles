from __future__ import annotations

import subprocess
import sys
import threading
import time
import tkinter as tk
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import tidyfiles.app as app_module
from tidyfiles.app import _SORT_LABELS, create_main_window
from tidyfiles.collection import (
    FileMetadataSnapshot,
    ImportDiagnostic,
    ImportMode,
    OrderingMode,
)
from tidyfiles.config import ConfigError, load_config
from tidyfiles.discovery import DiscoverySession
from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences, sort_entries


@pytest.fixture(scope="module")
def app_runtime(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("collection-controls") / "config.json"
    root = create_main_window(load_config(config_path), config_path)
    root.update()
    runtime = {"root": root, "app": root.application, "config_path": config_path}
    yield runtime
    runtime["app"].close()


def _wait_for_idle(root, app, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while (
        app.import_controller.busy or app._collection_operation_pending
    ) and time.monotonic() < deadline:
        root.update()
        time.sleep(0.001)
    root.update()
    assert not app.import_controller.busy
    assert not app._collection_operation_pending


def _seed_folder(app, tmp_path: Path):
    if app.collection.snapshot().entries or app.collection.snapshot().import_sessions:
        app.clear_all()
        app.root.update()
    folder = tmp_path / "source"
    folder.mkdir()
    metadata = {
        "file10.txt": FileMetadataSnapshot(10, 500, 100),
        "file2.txt": FileMetadataSnapshot(2, 400, 200),
        "alpha.dat": FileMetadataSnapshot(None, None, None),
        "zero.txt": FileMetadataSnapshot(0, 0, 0),
        "missing.txt": FileMetadataSnapshot(5, 300, 300),
    }
    session = app.collection.begin_import(ImportMode.ADD_FOLDER, folder_root=folder)
    entries = {}
    for name, snapshot in metadata.items():
        path = folder / name
        path.write_bytes(name.encode("utf-8"))
        entries[name] = app.collection.register_path(
            session, path, metadata=snapshot
        ).entry
    app.collection.finalize_import(
        session,
        incomplete=True,
        diagnostics=(ImportDiagnostic(str(folder / "denied"), "access denied"),),
    )
    app.refresh_collection()
    return folder, entries


def _drag(app, root, source_id: UUID, target_id: UUID, *, after: bool) -> None:
    root.update()
    source_box = app.tree.bbox(str(source_id), "#1")
    target_box = app.tree.bbox(str(target_id), "#1")
    assert source_box and target_box
    source_x, source_y = source_box[0] + 8, source_box[1] + source_box[3] // 2
    target_x = target_box[0] + 8
    target_y = target_box[1] + (target_box[3] - 2 if after else 2)
    assert app.tree.identify_region(source_x, source_y) == "cell"
    app._begin_drag_candidate(
        SimpleNamespace(x=source_x, y=source_y, x_root=100, y_root=100)
    )
    app._track_row_drag(
        SimpleNamespace(x=target_x, y=target_y, x_root=target_x + 100, y_root=200)
    )


def _click_row(app, root, item_id: str, state: int = 0) -> None:
    box = app.tree.bbox(item_id, "#1")
    assert box
    position = {"x": box[0] + 8, "y": box[1] + box[3] // 2, "state": state}
    app.tree.event_generate("<ButtonPress-1>", **position)
    app.tree.event_generate("<ButtonRelease-1>", **position)
    root.update()


def _drag_with_native_events(
    app, root, source_id: UUID, target_id: UUID | None, *, after: bool = False
) -> None:
    root.update()
    tree = app.tree
    source_box = tree.bbox(str(source_id), "#1")
    assert source_box
    source_x = source_box[0] + 8
    source_y = source_box[1] + source_box[3] // 2
    if target_id is None:
        target_x, target_y = tree.winfo_width() + 20, tree.winfo_height() + 20
    else:
        target_box = tree.bbox(str(target_id), "#1")
        assert target_box
        target_x = target_box[0] + 8
        target_y = target_box[1] + (target_box[3] - 2 if after else 2)

    root_x, root_y = tree.winfo_rootx(), tree.winfo_rooty()
    tree.event_generate(
        "<ButtonPress-1>",
        x=source_x,
        y=source_y,
        rootx=root_x + source_x,
        rooty=root_y + source_y,
    )
    root.update()
    tree.event_generate(
        "<Motion>",
        x=target_x,
        y=target_y,
        rootx=root_x + target_x,
        rooty=root_y + target_y,
        state=0x0100,
    )
    root.update()
    tree.event_generate(
        "<ButtonRelease-1>",
        x=target_x,
        y=target_y,
        rootx=root_x + target_x,
        rooty=root_y + target_y,
        state=0x0100,
    )
    root.update()


def _resize_tree_column(app, root, identifier: str, delta: int) -> int:
    tree = app.tree
    tree.xview_moveto(0)
    root.update()
    displaycolumns = tuple(tree.cget("displaycolumns"))
    column_index = displaycolumns.index(identifier) + 1
    separators = [
        x
        for x in range(tree.winfo_width())
        if tree.identify_region(x, 5) == "separator"
        and tree.identify_column(x) == f"#{column_index}"
    ]
    assert separators, f"no visible separator for {identifier}"
    x = separators[len(separators) // 2]
    y = 5
    root_x, root_y = tree.winfo_rootx(), tree.winfo_rooty()
    tree.event_generate(
        "<ButtonPress-1>", x=x, y=y, rootx=root_x + x, rooty=root_y + y
    )
    root.update()
    tree.event_generate(
        "<Motion>",
        x=x + delta,
        y=y,
        rootx=root_x + x + delta,
        rooty=root_y + y,
        state=0x0100,
    )
    root.update()
    tree.event_generate(
        "<ButtonRelease-1>",
        x=x + delta,
        y=y,
        rootx=root_x + x + delta,
        rooty=root_y + y,
        state=0x0100,
    )
    root.update()
    return int(tree.column(identifier, "width"))


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def test_sort_controls_cover_all_criteria_directions_and_persist(
    tmp_path: Path, app_runtime
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    config_path = app_runtime["config_path"]
    assert set(app.sort_dropdown.cget("values")) == set(_SORT_LABELS.values())
    app.sort_choice.set("Name")
    app._on_sort_criterion("Name")
    _wait_for_idle(root, app)
    app._toggle_sort_direction()
    _wait_for_idle(root, app)
    assert app.collection.snapshot().entries == ()
    assert app.collection.sort_preferences == SortPreferences(
        SortCriterion.NAME, SortDirection.DESCENDING
    )

    script = """
import sys
import threading
import time
from pathlib import Path
from tidyfiles.app import create_main_window
from tidyfiles.config import load_config
from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences

path = Path(sys.argv[1])
root = create_main_window(load_config(path), path)
app = root.application
try:
    assert app.collection.sort_preferences == SortPreferences(
        SortCriterion.NAME, SortDirection.DESCENDING
    )
    entered = threading.Event()
    release = threading.Event()
    prepare = app.collection.prepare_sort_order
    def blocked_prepare(preferences):
        entered.set()
        if not release.wait(5):
            raise AssertionError("sort worker was not released")
        return prepare(preferences)
    app.collection.prepare_sort_order = blocked_prepare
    app._start_sort(app.collection.sort_preferences)
    assert entered.wait(5)
    app.close()
    release.set()
    time.sleep(0.05)
    assert app._closing
finally:
    if not app._closing:
        app.close()
"""
    subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert app.sort_choice.get() == "Name"
    assert app.sort_direction_button.cget("text") == "Descending"
    _seed_folder(app, tmp_path)
    entries = app.collection.snapshot().entries
    assert "Files: 5" in app.summary.cget("text")
    assert "Known size: 17 B" in app.summary.cget("text")
    assert "1 unavailable" in app.summary.cget("text")
    selected = next(entry for entry in entries if entry.original_filename == "file2.txt")
    app.tree.selection_set(str(selected.entry_id))

    for criterion in SortCriterion:
        app.sort_choice.set(_SORT_LABELS[criterion])
        app._on_sort_criterion(_SORT_LABELS[criterion])
        _wait_for_idle(root, app)
        current = app.collection.sort_preferences
        assert current.criterion is criterion
        expected = sort_entries(entries, current)
        assert app.tree.get_children("") == tuple(
            str(entry.entry_id) for entry in expected
        )
        assert app.tree.selection() == (str(selected.entry_id),)

        app._toggle_sort_direction()
        _wait_for_idle(root, app)
        current = app.collection.sort_preferences
        expected = sort_entries(entries, current)
        assert app.tree.get_children("") == tuple(
            str(entry.entry_id) for entry in expected
        )
        assert current.direction in {SortDirection.ASCENDING, SortDirection.DESCENDING}

    app.sort_choice.set("Name")
    app._on_sort_criterion("Name")
    _wait_for_idle(root, app)
    names = [entry.original_filename for entry in app.collection.snapshot().entries]
    assert names.index("file2.txt") < names.index("file10.txt")
    displayed_order = app.tree.get_children("")
    current_preferences = app.collection.sort_preferences
    for mode in ("Duplicate Finder", "Repacker", "Bulk Rename"):
        app.tabs.set(mode)
        app._on_mode_change()
        time.sleep(0.11)
        root.update()
        assert app.collection.snapshot().sort_preferences == current_preferences
        assert app.tree.get_children("") == displayed_order
        assert app.tree.selection() == (str(selected.entry_id),)
        assert app.sort_choice.get() == _SORT_LABELS[current_preferences.criterion]
        assert app.sort_direction_button.cget("text") == (
            "Ascending"
            if current_preferences.direction is SortDirection.ASCENDING
            else "Descending"
        )

    current_order = tuple(
        entry.entry_id for entry in app.collection.snapshot().entries
    )
    manual_order = tuple(reversed(current_order))
    assert app.collection.set_order(manual_order)
    app.refresh_collection()
    assert app.collection.ordering_mode is OrderingMode.MANUAL
    assert "Manual order · 5 files" in app.summary.cget("text")
    app.sort_choice.set("Name")
    app._on_sort_criterion("Name")
    _wait_for_idle(root, app)
    assert app.collection.ordering_mode is OrderingMode.AUTOMATIC
    assert "Manual order" not in app.summary.cget("text")


def test_movement_drag_remove_clear_and_shared_revision_notifications(
    tmp_path: Path, app_runtime
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    config_path = app_runtime["config_path"]
    try:
        folder, entries = _seed_folder(app, tmp_path)
        by_name = {name: entry.entry_id for name, entry in entries.items()}
        event_revisions = []
        root.bind(
            "<<TidyFilesCollectionChanged>>",
            lambda _event: event_revisions.append(root.collection_revision),
            add="+",
        )

        order = tuple(
            by_name[name]
            for name in (
                "alpha.dat",
                "file2.txt",
                "file10.txt",
                "missing.txt",
                "zero.txt",
            )
        )
        app.collection.set_order(order)
        app.refresh_collection()
        selected = (by_name["file2.txt"], by_name["missing.txt"])
        app.tree.selection_set(*(str(entry_id) for entry_id in selected))
        root.update()
        assert event_revisions == []
        assert app.movement_buttons["move_top"].cget("state") == "normal"
        app.movement_buttons["move_top"].invoke()
        _wait_for_idle(root, app)
        assert app.movement_buttons["move_top"].cget("state") == "disabled"
        assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == (
            by_name["file2.txt"],
            by_name["missing.txt"],
            by_name["alpha.dat"],
            by_name["file10.txt"],
            by_name["zero.txt"],
        )
        assert app.tree.selection() == tuple(str(entry_id) for entry_id in selected)
        assert "Manual order · 5 files" in app.summary.cget("text")

        app.movement_buttons["move_bottom"].invoke()
        _wait_for_idle(root, app)
        assert app.movement_buttons["move_bottom"].cget("state") == "disabled"
        assert tuple(entry.entry_id for entry in app.collection.snapshot().entries)[-2:] == selected
        app.movement_buttons["move_up"].invoke()
        _wait_for_idle(root, app)
        app.movement_buttons["move_down"].invoke()
        _wait_for_idle(root, app)
        order_before_noop = tuple(entry.entry_id for entry in app.collection.snapshot().entries)
        revision_before_noop = app.collection.snapshot().revision
        app._move_selected("move_bottom")
        assert tuple(
            entry.entry_id for entry in app.collection.snapshot().entries
        ) == order_before_noop
        assert app.collection.snapshot().revision == revision_before_noop

        app.tree.selection_set(*(str(entry_id) for entry_id in selected))
        _drag(app, root, selected[0], by_name["alpha.dat"], after=False)
        assert app.tree.item(str(by_name["alpha.dat"]), "tags")[-1] == "drop-target"
        assert app.status.cget("text").startswith("Drop before")
        app._finish_row_drag(SimpleNamespace())
        _wait_for_idle(root, app)
        expected = (
            selected[0],
            selected[1],
            by_name["alpha.dat"],
            by_name["file10.txt"],
            by_name["zero.txt"],
        )
        assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == expected
        assert app.tree.selection() == tuple(str(entry_id) for entry_id in selected)

        _drag(app, root, selected[0], by_name["zero.txt"], after=True)
        app._finish_row_drag(SimpleNamespace())
        expected = (
            by_name["alpha.dat"],
            by_name["file10.txt"],
            by_name["zero.txt"],
            selected[0],
            selected[1],
        )
        assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == expected

        _drag(app, root, selected[0], selected[1], after=True)
        app._finish_row_drag(SimpleNamespace())
        assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == expected

        _drag(app, root, selected[0], by_name["alpha.dat"], after=False)
        app._track_row_drag(
            SimpleNamespace(
                x=app.tree.winfo_width() + 20,
                y=app.tree.winfo_height() + 20,
                x_root=500,
                y_root=500,
            )
        )
        app._finish_row_drag(SimpleNamespace())
        assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == expected

        # An order revision during a drag invalidates the pending drop.
        _drag(app, root, selected[0], by_name["alpha.dat"], after=True)
        externally_changed = tuple(reversed(expected))
        assert app.collection.set_order(externally_changed)
        app._finish_row_drag(SimpleNamespace())
        assert "Collection changed during drag" in app.status.cget("text")
        app.refresh_collection()
        assert tuple(
            entry.entry_id for entry in app.collection.snapshot().entries
        ) == externally_changed

        added_path = folder / "new.txt"
        added_path.write_text("new")
        assert app.import_controller.start_files((added_path,))
        _wait_for_idle(root, app)
        imported_order = tuple(entry.entry_id for entry in app.collection.snapshot().entries)
        assert imported_order[:5] == externally_changed
        assert app.collection.ordering_mode is OrderingMode.MANUAL

        app.sort_choice.set("Name")
        app._on_sort_criterion("Name")
        _wait_for_idle(root, app)
        assert app.collection.ordering_mode is OrderingMode.AUTOMATIC
        assert len(event_revisions) >= 5

        remove_ids = (by_name["file2.txt"], by_name["missing.txt"])
        manual_order = tuple(
            reversed(tuple(entry.entry_id for entry in app.collection.snapshot().entries))
        )
        app.collection.set_order(manual_order)
        app.refresh_collection()
        retained_manual_order = tuple(
            entry_id for entry_id in manual_order if entry_id not in remove_ids
        )
        assert app.collection.ordering_mode is OrderingMode.MANUAL
        original_bytes = {name: (folder / name).read_bytes() for name in entries}
        roots_before = app.collection.snapshot().folder_roots
        warnings_before = app.collection.snapshot().incomplete_imports
        app.tree.selection_set(*(str(entry_id) for entry_id in remove_ids))
        app.controls["remove"].invoke()
        _wait_for_idle(root, app)
        remaining = tuple(entry.entry_id for entry in app.collection.snapshot().entries)
        assert remaining == retained_manual_order
        assert app.collection.ordering_mode is OrderingMode.MANUAL
        assert app.collection.snapshot().folder_roots == roots_before
        assert app.collection.snapshot().incomplete_imports == warnings_before
        assert app.import_warnings_button.cget("text") == "Import issues (1)"
        assert {name: (folder / name).read_bytes() for name in entries} == original_bytes

        old_id = by_name["file2.txt"]
        assert app.import_controller.start_files((folder / "file2.txt",))
        _wait_for_idle(root, app)
        reimported = next(
            entry for entry in app.collection.snapshot().entries
            if entry.source_path == str(folder / "file2.txt")
        )
        assert reimported.entry_id != old_id
        assert tuple(
            entry.entry_id for entry in app.collection.snapshot().entries
        ) == (*retained_manual_order, reimported.entry_id)
        assert app.collection.snapshot().incomplete_imports == warnings_before

        app._set_column_visible("extension", True)
        sort_preferences = app.collection.sort_preferences
        columns_before = deepcopy(app.config["global_settings"]["column_preferences"])
        settings_before = deepcopy(app.config["global_settings"])
        profiles_before = deepcopy(app.config["profiles"])
        app.controls["clear_all"].invoke()
        assert app.collection.snapshot().entries == ()
        assert app.collection.snapshot().import_sessions == ()
        assert app.collection.snapshot().folder_roots == ()
        assert app.collection.snapshot().incomplete_imports == ()
        assert app._last_import_report is None
        assert app.collection.ordering_mode is OrderingMode.AUTOMATIC
        assert app.collection.sort_preferences == sort_preferences
        assert app.config["global_settings"]["column_preferences"] == columns_before
        assert app.config["global_settings"] == settings_before
        assert app.config["profiles"] == profiles_before
        assert app.tree.get_children("") == ()
        assert app.import_warnings_button.winfo_manager() == ""
        assert all((folder / name).exists() for name in entries)
        assert app.summary.cget("text").startswith("Files: 0  Selected: 0")
        root.update()
        assert root.collection_revision == app.collection.snapshot().revision
        assert event_revisions[-1] == root.collection_revision
        assert load_config(config_path)["global_settings"]["column_preferences"] == columns_before
        assert all(
            current > previous
            for previous, current in zip(event_revisions, event_revisions[1:])
        )
    finally:
        app.clear_all()


def test_busy_controls_reject_sorts_and_stale_sort_results(
    tmp_path: Path,
    app_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    try:
        _seed_folder(app, tmp_path)
        first_id = app.collection.snapshot().entries[0].entry_id
        app.tree.selection_set(str(first_id))
        session = app.collection.begin_import(ImportMode.ADD_FILES)
        app._update_control_states()
        for key in ("add_files", "add_folder", "remove", "clear_all"):
            assert app.controls[key].cget("state") == "disabled"
        assert app.sort_dropdown.cget("state") == "disabled"
        assert all(button.cget("state") == "disabled" for button in app.movement_buttons.values())
        popups = []
        monkeypatch.setattr(app.context_menu, "tk_popup", lambda *args: popups.append(args))
        monkeypatch.setattr(app.context_menu, "grab_release", lambda: None)
        row = app.tree.bbox(str(first_id), "#1")
        app._show_tree_context_menu(
            SimpleNamespace(x=row[0] + 8, y=row[1] + 4, x_root=10, y_root=10)
        )
        assert app.context_menu.entrycget(
            app._context_menu_indices["remove"], "state"
        ) == "disabled"
        assert app.context_menu.entrycget(
            app._context_menu_indices["copy_path"], "state"
        ) == "normal"

        prior_preferences = app.collection.sort_preferences
        app._start_sort(SortPreferences(SortCriterion.PATH, SortDirection.ASCENDING))
        assert not app._collection_operation_pending
        assert app.collection.sort_preferences == prior_preferences
        app._begin_drag_candidate(
            SimpleNamespace(x=row[0] + 8, y=row[1] + 4, x_root=10, y_root=10)
        )
        assert app._drag_press is None
        app.collection.cancel_import(session)
        app._update_control_states()

        entered = threading.Event()
        release = threading.Event()
        prepare = app.collection.prepare_sort_order

        def blocked_prepare(preferences):
            prepared = prepare(preferences)
            entered.set()
            assert release.wait(5)
            return prepared

        monkeypatch.setattr(app.collection, "prepare_sort_order", blocked_prepare)
        app._start_sort(SortPreferences(SortCriterion.PATH, SortDirection.ASCENDING))
        assert entered.wait(5)
        current_order = tuple(
            entry.entry_id for entry in app.collection.snapshot().entries
        )
        stale_order = tuple(reversed(current_order))
        assert app.collection.set_order(stale_order)
        release.set()
        _wait_for_idle(root, app)
        assert "Sorting canceled" in app.status.cget("text")
        assert app.collection.sort_preferences == prior_preferences
        assert app.controls["add_files"].cget("state") == "normal"
        app.refresh_collection()

        def fail_sort(_preferences):
            raise RuntimeError("simulated sort worker failure")

        with monkeypatch.context() as patcher:
            patcher.setattr(app.collection, "prepare_sort_order", fail_sort)
            app._start_sort(SortPreferences(SortCriterion.NAME, SortDirection.ASCENDING))
            _wait_for_idle(root, app)
        assert "Sorting failed" in app.status.cget("text")
        assert not app._collection_operation_pending
        assert app.controls["add_files"].cget("state") == "normal"

        changed_events = []
        root.bind(
            "<<TidyFilesCollectionChanged>>",
            lambda _event: changed_events.append(root.collection_revision),
            add="+",
        )
        first_in_order = app.collection.snapshot().entries[0].entry_id
        app.tree.selection_set(str(first_in_order))
        original_set_children = app.tree.set_children

        def fail_tree_reorder(*_args, **_kwargs):
            raise RuntimeError("simulated Treeview order failure")

        with monkeypatch.context() as patcher:
            patcher.setattr(app.tree, "set_children", fail_tree_reorder)
            app._move_selected("move_bottom")
        assert not app._collection_operation_pending
        assert "order error" in app.status.cget("text")
        root.update()
        assert tuple(app.tree.get_children("")) == tuple(
            str(entry.entry_id) for entry in app.collection.snapshot().entries
        )
        assert changed_events[-1] == app.collection.snapshot().revision
        assert app.controls["add_files"].cget("state") == "normal"
        assert app.tree.set_children == original_set_children
    finally:
        app.clear_all()


def test_keyboard_and_context_actions_are_scoped_and_safe(
    tmp_path: Path, app_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    try:
        folder, entries = _seed_folder(app, tmp_path)
        ids = {name: entry.entry_id for name, entry in entries.items()}
        root.update()
        rows = app.tree.get_children("")
        app.tree.selection_set(rows[0])
        app.tree.focus(rows[0])
        _click_row(app, root, rows[1], state=0x0004)
        assert set(app.tree.selection()) == {rows[0], rows[1]}
        _click_row(app, root, rows[1], state=0x0004)
        assert app.tree.selection() == (rows[0],)
        app.tree.selection_set(rows[0])
        app.tree.focus(rows[0])
        _click_row(app, root, rows[3], state=0x0001)
        assert set(app.tree.selection()) == set(rows[:4])

        popups = []
        monkeypatch.setattr(app.context_menu, "tk_popup", lambda *args: popups.append(args))
        monkeypatch.setattr(app.context_menu, "grab_release", lambda: None)

        app.tree.selection_set(str(ids["file2.txt"]), str(ids["missing.txt"]))
        box = app.tree.bbox(str(ids["file2.txt"]), "#1")
        app._show_tree_context_menu(
            SimpleNamespace(x=box[0] + 8, y=box[1] + 4, x_root=100, y_root=100)
        )
        assert app.tree.selection() == (
            str(ids["file2.txt"]),
            str(ids["missing.txt"]),
        )
        open_file_state = app.context_menu.entrycget(
            app._context_menu_indices["open_file"], "state"
        )
        copy_path_state = app.context_menu.entrycget(
            app._context_menu_indices["copy_path"], "state"
        )
        assert open_file_state == "disabled"
        assert copy_path_state == "normal"

        # Right-clicking an already selected row preserves the multi-selection.
        app._show_tree_context_menu(
            SimpleNamespace(x=box[0] + 8, y=box[1] + 4, x_root=100, y_root=100)
        )
        assert app.tree.selection() == (
            str(ids["file2.txt"]),
            str(ids["missing.txt"]),
        )

        unselected_box = app.tree.bbox(str(ids["file10.txt"]), "#1")
        app._show_tree_context_menu(
            SimpleNamespace(
                x=unselected_box[0] + 8,
                y=unselected_box[1] + 4,
                x_root=100,
                y_root=100,
            )
        )
        assert app.tree.selection() == (str(ids["file10.txt"]),)
        assert app.context_menu.entrycget(
            app._context_menu_indices["open_file"], "state"
        ) == "normal"

        clipboard = []
        monkeypatch.setattr(root, "clipboard_clear", lambda: clipboard.clear())
        monkeypatch.setattr(root, "clipboard_append", lambda value: clipboard.append(value))
        app.tree.selection_set(str(ids["missing.txt"]), str(ids["file2.txt"]))
        app.copy_selected_filenames()
        selected_names = [
            entry.original_filename
            for entry in app.collection.snapshot().entries
            if entry.entry_id in {ids["missing.txt"], ids["file2.txt"]}
        ]
        assert clipboard == ["\n".join(selected_names)]
        app.copy_selected_paths()
        selected_paths = [
            entry.source_path
            for entry in app.collection.snapshot().entries
            if entry.entry_id in {ids["missing.txt"], ids["file2.txt"]}
        ]
        assert clipboard[-1] == "\n".join(selected_paths)

        launched = []
        monkeypatch.setattr(app_module.os, "startfile", lambda path: launched.append(path))
        app.tree.selection_set(str(ids["file2.txt"]))
        app.open_selected_file()
        assert launched == [str(folder / "file2.txt")]
        app.open_containing_folder()
        assert launched[-1] == str(folder)

        absent = folder / "missing-from-disk.txt"
        session = app.collection.begin_import(ImportMode.ADD_FILES)
        absent_entry = app.collection.register_path(session, absent).entry
        app.collection.finalize_import(session)
        app.refresh_collection()
        app.tree.selection_set(str(absent_entry.entry_id))
        app.open_selected_file()
        assert launched[-1] == str(folder)
        assert "Path is no longer available" in app.status.cget("text")

        script = folder / "unsafe.ps1"
        script.write_text("Write-Output safe test fixture")
        session = app.collection.begin_import(ImportMode.ADD_FILES)
        script_entry = app.collection.register_path(session, script).entry
        app.collection.finalize_import(session)
        app.refresh_collection()
        app.tree.selection_set(str(script_entry.entry_id))
        script_box = app.tree.bbox(str(script_entry.entry_id), "#1")
        app._show_tree_context_menu(
            SimpleNamespace(
                x=script_box[0] + 8,
                y=script_box[1] + 4,
                x_root=100,
                y_root=100,
            )
        )
        assert app.context_menu.entrycget(
            app._context_menu_indices["open_file"], "state"
        ) == "disabled"
        assert "executable content disabled" in app.context_menu.entrycget(
            app._context_menu_indices["open_file"], "label"
        )
        app.open_selected_file()
        assert launched[-1] == str(folder)

        heading_route = []
        monkeypatch.setattr(app, "_show_header_menu", lambda event: heading_route.append(event))
        app._begin_drag_candidate(SimpleNamespace(x=5, y=2, x_root=0, y_root=0))
        assert app._drag_press is None
        app._show_tree_context_menu(SimpleNamespace(x=5, y=2, x_root=0, y_root=0))
        assert heading_route
        popups_before_empty = len(popups)
        app._show_tree_context_menu(
            SimpleNamespace(x=5, y=app.tree.winfo_height() - 2, x_root=0, y_root=0)
        )
        assert len(popups) == popups_before_empty

        app.root.focus_force()
        app.tree.focus_force()
        root.update()
        app.tree.event_generate("<Control-a>")
        root.update()
        assert set(app.tree.selection()) == set(app.tree.get_children(""))

        app.rename_format.set("abc")
        entry_widget = app.format_field._entry
        assert app.tabs.get() == "Bulk Rename"
        assert entry_widget.winfo_ismapped()
        assert entry_widget.bind("<Control-a>")
        entry_widget.focus_force()
        root.update()
        assert root.focus_get() == entry_widget
        entry_widget.event_generate("<Control-a>", state=0x0004)
        root.update()
        assert entry_widget.selection_present()
        assert entry_widget.selection_get() == "abc"
        entry_widget.selection_clear()
        entry_widget.icursor(1)
        entry_widget.event_generate("<Delete>")
        root.update()
        assert app.rename_format.get() == "ac"
        assert len(app.collection.snapshot().entries) == 7

        removed_id = ids["file10.txt"]
        app.tree.selection_set(str(removed_id))
        app.tree.focus_set()
        app.tree.event_generate("<Delete>")
        _wait_for_idle(root, app)
        assert removed_id not in {entry.entry_id for entry in app.collection.snapshot().entries}
        assert (folder / "file10.txt").read_bytes() == b"file10.txt"
    finally:
        app.clear_all()


def test_native_drag_preserves_multi_selection_and_cancels_without_reordering(
    tmp_path: Path, app_runtime
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    _, entries = _seed_folder(app, tmp_path)
    by_name = {name: entry.entry_id for name, entry in entries.items()}
    original = tuple(
        by_name[name]
        for name in ("alpha.dat", "file2.txt", "file10.txt", "missing.txt", "zero.txt")
    )
    app.collection.set_order(original)
    app.refresh_collection()
    root.update()

    selected = (by_name["file2.txt"], by_name["missing.txt"])
    app.tree.selection_set(*(str(entry_id) for entry_id in selected))
    source_box = app.tree.bbox(str(selected[0]), "#1")
    target_box = app.tree.bbox(str(by_name["alpha.dat"]), "#1")
    assert source_box and target_box
    source_x, source_y = source_box[0] + 8, source_box[1] + source_box[3] // 2
    target_x, target_y = target_box[0] + 8, target_box[1] + 2
    root_x, root_y = app.tree.winfo_rootx(), app.tree.winfo_rooty()

    app.tree.event_generate(
        "<ButtonPress-1>",
        x=source_x,
        y=source_y,
        rootx=root_x + source_x,
        rooty=root_y + source_y,
    )
    root.update()
    assert app.tree.selection() == (str(selected[0]),)

    app.tree.event_generate(
        "<Motion>",
        x=target_x,
        y=target_y,
        rootx=root_x + target_x,
        rooty=root_y + target_y,
        state=0x0100,
    )
    root.update()
    assert app.tree.selection() == tuple(str(entry_id) for entry_id in selected)
    app.tree.event_generate(
        "<ButtonRelease-1>",
        x=target_x,
        y=target_y,
        rootx=root_x + target_x,
        rooty=root_y + target_y,
        state=0x0100,
    )
    _wait_for_idle(root, app)
    expected = (
        selected[0],
        selected[1],
        by_name["alpha.dat"],
        by_name["file10.txt"],
        by_name["zero.txt"],
    )
    assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == expected
    assert app.tree.selection() == tuple(str(entry_id) for entry_id in selected)

    app.tree.selection_set(*(str(entry_id) for entry_id in selected))
    _drag_with_native_events(app, root, selected[0], None)
    assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == expected
    assert app.tree.selection() == tuple(str(entry_id) for entry_id in selected)

    _click_row(app, root, str(by_name["alpha.dat"]))
    assert app.tree.selection() == (str(by_name["alpha.dat"]),)
    _click_row(app, root, str(by_name["file10.txt"]), state=0x0004)
    assert set(app.tree.selection()) == {
        str(by_name["alpha.dat"]),
        str(by_name["file10.txt"]),
    }
    _click_row(app, root, str(by_name["zero.txt"]), state=0x0001)
    assert str(by_name["zero.txt"]) in app.tree.selection()
    assert len(app.tree.selection()) > 1

    app.tree.selection_set(str(by_name["file10.txt"]))
    _drag_with_native_events(app, root, by_name["alpha.dat"], by_name["zero.txt"], after=True)
    expected_single = (
        selected[0],
        selected[1],
        by_name["file10.txt"],
        by_name["zero.txt"],
        by_name["alpha.dat"],
    )
    assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == expected_single
    assert app.tree.selection() == (str(by_name["alpha.dat"]),)


def test_native_column_resizing_persists_across_updates_tabs_and_restart(
    tmp_path: Path, app_runtime,
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    config_path = app_runtime["config_path"]
    size_width = int(app.tree.column("size", "width"))
    original_width = _resize_tree_column(app, root, "filename", 60)
    original_width = _resize_tree_column(app, root, "filename", -40)
    _resize_tree_column(app, root, "new_filename", -30)
    new_width = _resize_tree_column(app, root, "new_filename", 85)
    resized_size = _resize_tree_column(app, root, "size", 20)
    assert original_width == 300
    assert new_width == 335
    assert resized_size == size_width + 20
    assert app.column_preferences["Bulk Rename"].widths["filename"] == original_width
    assert app.tree.xview()[1] < 1

    app.refresh_collection()
    root.update()
    assert int(app.tree.column("filename", "width")) == original_width
    assert int(app.tree.column("new_filename", "width")) == new_width
    new_file = tmp_path / "width-update.txt"
    new_file.write_text("widths persist during collection updates")
    assert app.import_controller.start_files((new_file,))
    _wait_for_idle(root, app)
    assert int(app.tree.column("filename", "width")) == original_width
    assert int(app.tree.column("new_filename", "width")) == new_width

    root.geometry("900x650+20+20")
    root.update()
    assert int(app.tree.column("filename", "width")) == original_width
    assert int(app.tree.column("new_filename", "width")) == new_width

    app.tabs.set("Duplicate Finder")
    app._on_mode_change()
    assert int(app.tree.column("filename", "width")) == 320
    _resize_tree_column(app, root, "filename", 45)
    duplicate_width = _resize_tree_column(app, root, "filename", -25)
    assert duplicate_width == 340
    app.tabs.set("Bulk Rename")
    app._on_mode_change()
    assert int(app.tree.column("filename", "width")) == original_width
    assert int(app.tree.column("new_filename", "width")) == new_width

    script = """
from pathlib import Path
import sys
from tidyfiles.app import create_main_window
from tidyfiles.config import load_config
path = Path(sys.argv[1])
root = create_main_window(load_config(path), path)
app = root.application
try:
    root.update()
    assert int(app.tree.column('filename', 'width')) == 300
    assert int(app.tree.column('new_filename', 'width')) == 335
    app.tabs.set('Duplicate Finder')
    app._on_mode_change()
    root.update()
    assert int(app.tree.column('filename', 'width')) == 340
finally:
    app.close()
"""
    subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    app._restore_column_defaults()
    assert int(app.tree.column("filename", "width")) == 280
    assert int(app.tree.column("new_filename", "width")) == 280


def test_columns_dialog_is_single_instance_and_mode_scoped(
    app_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    app.columns_button.invoke()
    first = app.column_dialog
    assert first is not None

    menus = []

    class HeaderMenu:
        def __init__(self, *_args, **_kwargs):
            self.commands = {}
            menus.append(self)

        def add_command(self, *, label, command):
            self.commands[label] = command

        def tk_popup(self, *_args):
            pass

        def grab_release(self):
            pass

    monkeypatch.setattr(app_module.tk, "Menu", HeaderMenu)
    assert app.tree.identify_region(10, 5) == "heading"
    app._show_header_menu(SimpleNamespace(x=10, y=5, x_root=100, y_root=100))
    menus[-1].commands["Columns…"]()
    assert app.column_dialog is first
    assert sum(
        isinstance(child, app_module.ctk.CTkToplevel)
        and child.title().startswith("Columns")
        for child in root.winfo_children()
    ) == 1

    close_command = first.protocol("WM_DELETE_WINDOW")
    first.tk.call(close_command)
    root.update()
    assert app.column_dialog is None
    assert first.winfo_exists() == 0

    app.open_columns()
    done = next(
        widget
        for widget in _descendants(app.column_dialog)
        if isinstance(widget, app_module.ctk.CTkButton) and widget.cget("text") == "Done"
    )
    done.invoke()
    root.update()
    assert app.column_dialog is None

    app.open_columns()
    bulk_dialog = app.column_dialog
    app.tabs.set("Duplicate Finder")
    app._on_mode_change()
    root.update()
    assert app.column_dialog is None
    assert bulk_dialog.winfo_exists() == 0
    app.open_columns()
    duplicate_dialog = app.column_dialog
    assert duplicate_dialog is not None
    assert duplicate_dialog.title() == "Columns — Duplicate Finder"
    app.open_columns()
    assert app.column_dialog is duplicate_dialog
    bulk_visibility = app.column_preferences["Bulk Rename"].visible
    extension = next(
        widget
        for widget in _descendants(duplicate_dialog)
        if isinstance(widget, app_module.ctk.CTkCheckBox)
        and widget.cget("text") == "Extension"
    )
    extension.toggle()
    assert "extension" in app.column_preferences["Duplicate Finder"].visible
    assert app.column_preferences["Bulk Rename"].visible == bulk_visibility


def test_sort_preference_save_failure_preserves_existing_config(
    app_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    config_path = app_runtime["config_path"]
    original = config_path.read_bytes()
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *_args, **_kwargs: None)

    def fail_save(*_args, **_kwargs):
        raise ConfigError("simulated write failure")

    monkeypatch.setattr(app_module, "save_config", fail_save)
    try:
        app.sort_choice.set("Path")
        app._on_sort_criterion("Path")
        _wait_for_idle(root, app)
        assert config_path.read_bytes() == original
        assert app.collection.sort_preferences.criterion is SortCriterion.PATH
        assert "preference could not be saved" in app.status.cget("text")
    finally:
        app.clear_all()


def test_close_during_active_discovery_restores_prior_view_before_shutdown(
    app_runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = app_runtime["root"]
    app = app_runtime["app"]
    if app.collection.snapshot().entries:
        app.clear_all()
        root.update()

    callback_errors = []
    root.report_callback_exception = lambda *args: callback_errors.append(args)
    prior_path = tmp_path / "shutdown-prior.txt"
    prior_path.write_text("preserved", encoding="utf-8")
    assert app.import_controller.start_files((prior_path,))
    _wait_for_idle(root, app)
    prior_entry = app.collection.snapshot().entries[0]
    assert app.tree.selection() == ()
    root.update()

    rollback = app.import_controller.on_rollback_complete
    restored_selections = []

    def capture_restored_selection(removed_ids, previous_view):
        rollback(removed_ids, previous_view)
        restored_selections.append(app.tree.selection())

    app.import_controller.on_rollback_complete = capture_restored_selection
    folder = tmp_path / "active-discovery"
    folder.mkdir()
    (folder / "one.txt").write_text("one", encoding="utf-8")
    (folder / "two.txt").write_text("two", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    original_walk = DiscoverySession._walk_folder

    def blocked_walk(session, selected_root):
        for index, record in enumerate(original_walk(session, selected_root)):
            yield record
            if index == 0:
                entered.set()
                release.wait()

    monkeypatch.setattr(DiscoverySession, "_walk_folder", blocked_walk)
    try:
        assert app.import_controller.start_folder(folder)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            root.update()
            if entered.is_set() and len(app.tree.get_children("")) > 1:
                break
            time.sleep(0.002)
        assert entered.is_set() and len(app.tree.get_children("")) > 1
        assert app.import_controller.busy
        app.tree.selection_set(str(prior_entry.entry_id))
        root.update()
        started = time.monotonic()
        app.close()
        assert time.monotonic() - started < 1
        assert app._close_pending and not app._closing
        release.set()
        deadline = time.monotonic() + 8
        while not app._closing and time.monotonic() < deadline:
            try:
                root.update()
            except tk.TclError:
                break
            time.sleep(0.002)
        assert app._closing
        assert tuple(entry.entry_id for entry in app.collection.snapshot().entries) == (
            prior_entry.entry_id,
        )
        assert app._display_order == [prior_entry.entry_id]
        assert restored_selections == [()]
        assert callback_errors == []
        assert prior_path.read_text(encoding="utf-8") == "preserved"
    finally:
        release.set()
        if not app._closing:
            app.close()
            deadline = time.monotonic() + 8
            while not app._closing and time.monotonic() < deadline:
                try:
                    root.update()
                except tk.TclError:
                    break
                time.sleep(0.002)
