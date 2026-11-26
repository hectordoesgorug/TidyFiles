from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import customtkinter as ctk

import tidyfiles.app as app_module
import tidyfiles.discovery as discovery_module
from tidyfiles.app import create_main_window
from tidyfiles.collection import FileMetadataSnapshot, ImportDiagnostic, ImportMode
from tidyfiles.config import default_config, load_config
from tidyfiles.discovery import DiscoverySession
from tidyfiles.ui_preferences import COLUMNS


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _wait_for(root, predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.001)
    assert predicate(), (
        f"Timed out waiting for app state: {root.application._processing_phase}; "
        f"{root.application.status.cget('text')}"
    )


def _import_files(app, root, monkeypatch, *paths: Path) -> None:
    monkeypatch.setattr(
        app_module.filedialog,
        "askopenfilenames",
        lambda **_options: tuple(str(path) for path in paths),
    )
    app.add_files()
    _wait_for(root, lambda: not app.import_controller.busy)


def _close_app(app, root) -> None:
    if app._closing:
        return
    if app._processing_phase == "idle":
        app.close()
    else:
        root.destroy()


def _run_gui_test_in_isolated_process(test_name: str) -> bool:
    marker = "TIDYFILES_ISOLATED_GUI_TEST"
    if os.environ.get(marker) == test_name:
        return False
    environment = os.environ.copy()
    environment[marker] = test_name
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__).resolve()}::{test_name}",
            "-q",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True


def _confirm_review(app, label: str) -> None:
    dialog = app._plan_review_dialog
    assert dialog is not None
    button = next(
        widget
        for widget in _descendants(dialog)
        if isinstance(widget, ctk.CTkButton) and widget.cget("text") == label
    )
    button.invoke()


def test_main_window_collection_columns_import_and_persistence(
    tmp_path: Path, monkeypatch
) -> None:
    from tidyfiles.transaction_journal import JournalStore

    config_path = tmp_path / "config.json"
    local_app_data = tmp_path / "local"
    local_app_data.mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=local_app_data)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    config = default_config()
    config["global_settings"]["appearance_mode"] = "Dark"
    config["global_settings"]["window_geometry"] = "1400x900+5000+5000"
    config["global_settings"]["auto_clean"] = True
    root = create_main_window(config, config_path)
    app = root.application
    closed = False
    close_release = threading.Event()
    try:
        root.update()
        assert root.active_mode == "Bulk Rename"
        assert app.collection is root.working_collection
        assert app.collection.snapshot().entries == ()
        assert app.tree.get_children("") == ()
        assert app.tree.cget("displaycolumns") == (
            "row_number",
            "filename",
            "new_filename",
            "size",
            "status",
        )
        assert app.appearance_mode == "Dark"
        assert root.winfo_x() + root.winfo_width() <= sum(app._virtual_screen_bounds()[::2])
        assert root.winfo_y() + root.winfo_height() <= sum(app._virtual_screen_bounds()[1::2])

        assert app.columns_button.cget("state") == "normal"
        assert app.auto_clean_switch.cget("state") == "normal"
        assert app.auto_clean_switch.get()
        assert not app.counter_switch.get()
        assert app.controls["add_files"].cget("state") == "normal"
        assert app.controls["add_folder"].cget("state") == "normal"
        for control in (
            app.controls["remove"],
            app.controls["clear_all"],
            *app.movement_buttons.values(),
            app.profiles_button,
            app.process_button,
        ):
            assert control.cget("state") == "disabled"
        assert app.insert_token_button.cget("state") == "normal"
        assert app.format_reference_button.cget("state") == "normal"
        assert app.sort_dropdown.cget("state") == "normal"
        assert app.sort_direction_button.cget("state") == "normal"

        app.open_columns()
        app.column_dialog.update_idletasks()
        checkboxes = {
            widget.cget("text"): widget
            for widget in _descendants(app.column_dialog)
            if isinstance(widget, ctk.CTkCheckBox)
        }
        assert checkboxes["Original filename"].cget("state") == "disabled"
        assert checkboxes["Original filename"].get() == 1
        assert checkboxes["New filename"].cget("state") == "disabled"
        assert checkboxes["Status"].cget("state") == "disabled"
        assert checkboxes["Extension"].cget("state") == "normal"
        assert checkboxes["Extension"].get() == 0
        app.column_dialog.destroy()
        assert app.rename_format.get() == ""

        session = app.collection.begin_import(ImportMode.ADD_FILES)
        photo = app.collection.register_path(
            session,
            r"C:\Synthetic\photo.jpg",
            metadata=FileMetadataSnapshot(size_bytes=2048),
        ).entry
        video = app.collection.register_path(session, r"C:\Synthetic\clip.mp4").entry
        app.collection.finalize_import(session)
        app.refresh_collection()
        photo_id, video_id = app.tree.get_children("")
        app.tree.selection_set(photo_id)
        tree = app.tree

        column_ids = (column.identifier for column in COLUMNS)
        photo_cells = dict(zip(column_ids, app._entry_values(1, photo)))
        column_ids = (column.identifier for column in COLUMNS)
        video_cells = dict(zip(column_ids, app._entry_values(2, video)))
        assert photo_cells["new_filename"] == ""
        assert photo_cells["group"] == ""
        assert photo_cells["status"] == "Scanning"
        assert photo_cells["resolution"] == "— (not probed)"
        assert photo_cells["length"] == ""
        assert video_cells["resolution"] == "— (not probed)"
        assert video_cells["length"] == "— (not probed)"

        app.tabs.set("Duplicate Finder")
        app._on_mode_change()
        assert app.collection is root.working_collection
        assert app.tree is tree
        assert app.tree.exists(photo_id) and app.tree.exists(video_id)
        assert app.tree.selection() == (photo_id,)
        assert app.tree.cget("displaycolumns") == (
            "row_number",
            "filename",
            "size",
            "group",
            "status",
        )
        assert app.tree.heading("filename", "text") == "Filename"
        assert "1 unavailable" in app.summary.cget("text")
        assert "Known size: 2.0 KiB" in app.summary.cget("text")

        app._set_column_visible("extension", True)
        app.tree.column("filename", width=440)
        app.tree.column("extension", width=145)
        app._persist_column_widths()
        app._restore_column_defaults()
        assert app.tree.cget("displaycolumns") == (
            "row_number",
            "filename",
            "size",
            "group",
            "status",
        )
        assert int(app.tree.column("filename", "width")) == 320

        app.tabs.set("Bulk Rename")
        app._on_mode_change()
        assert "extension" not in app.tree.cget("displaycolumns")
        app._set_column_visible("extension", True)
        app.tree.column("filename", width=510)
        app.tree.column("extension", width=110)
        app._persist_column_widths()
        assert "extension" in app.tree.cget("displaycolumns")
        assert int(app.tree.column("extension", "width")) == 110

        selected_dialogs = {}
        def cancel_files(**options):
            selected_dialogs["files_options"] = options
            return ()

        def cancel_folder(**options):
            selected_dialogs["folder_options"] = options
            return ""

        monkeypatch.setattr(app_module.filedialog, "askopenfilenames", cancel_files)
        monkeypatch.setattr(app_module.filedialog, "askdirectory", cancel_folder)
        app.add_files()
        app.add_folder()
        assert not app.import_controller.busy
        assert len(app.collection.snapshot().entries) == 2

        def wait_for_import(timeout: float = 8) -> None:
            deadline = time.monotonic() + timeout
            while app.import_controller.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.001)
            assert not app.import_controller.busy

        one_file = tmp_path / "one.txt"
        one_file.write_text("one")
        file_stats = one_file.stat()
        monkeypatch.setattr(
            app_module.filedialog,
            "askopenfilenames",
            lambda **options: selected_dialogs.update(files_options=options)
            or (str(one_file),),
        )
        app.add_files()
        assert app.controls["add_files"].cget("state") == "disabled"
        assert app.cancel_button.winfo_manager() == "grid"
        wait_for_import()
        assert app.controls["add_files"].cget("state") == "normal"
        assert app.tree.get_children("") == tuple(
            str(entry.entry_id) for entry in app.collection.snapshot().entries
        )
        assert app.tree.selection() == (photo_id,)
        assert len(app.collection.snapshot().entries) == 3
        one_entry = next(
            entry for entry in app.collection.snapshot().entries
            if entry.source_path == str(one_file)
        )
        assert one_entry.metadata.size_bytes == 3
        assert one_file.read_text() == "one"
        assert one_file.stat().st_mtime_ns == file_stats.st_mtime_ns
        assert selected_dialogs["files_options"]["filetypes"] == (("All Files", "*.*"),)

        other_file = tmp_path / "other.txt"
        other_file.write_text("other")
        monkeypatch.setattr(
            app_module.filedialog,
            "askopenfilenames",
            lambda **options: selected_dialogs.update(files_options=options)
            or (str(one_file), str(other_file)),
        )
        app.add_files()
        wait_for_import()
        assert len(app.collection.snapshot().entries) == 4
        same_entry = next(
            entry for entry in app.collection.snapshot().entries
            if entry.source_path == str(one_file)
        )
        assert same_entry.entry_id == one_entry.entry_id

        folder = tmp_path / "selected-folder"
        nested_file = folder / "nested" / "inside.bin"
        nested_file.parent.mkdir(parents=True)
        nested_file.write_bytes(b"folder")
        monkeypatch.setattr(
            app_module.filedialog,
            "askdirectory",
            lambda **options: selected_dialogs.update(folder_options=options)
            or str(folder),
        )
        app.add_folder()
        wait_for_import()
        folder_entry = next(
            entry for entry in app.collection.snapshot().entries
            if entry.source_path == str(nested_file)
        )
        assert folder_entry.import_root_path == str(folder)
        assert selected_dialogs["folder_options"]["mustexist"] is True

        denied_file = tmp_path / "denied.txt"
        denied_file.write_text("not read")
        original_lstat = os.lstat

        def inaccessible(path, *args, **kwargs):
            if os.fspath(path) == str(denied_file):
                raise PermissionError("simulated GUI import denial")
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(discovery_module.os, "lstat", inaccessible)
        monkeypatch.setattr(
            app_module.filedialog,
            "askopenfilenames",
            lambda **_options: (str(denied_file),),
        )
        app.add_files()
        wait_for_import()
        assert "Import incomplete" in app.status.cget("text")
        assert app.import_warnings_button.winfo_manager() == "grid"
        assert app.import_warnings_button.cget("text") == "Import issues (1)"
        app.open_import_details()
        detail_box = next(
            widget for widget in _descendants(app.root)
            if isinstance(widget, ctk.CTkTextbox)
        )
        assert str(denied_file) in detail_box.get("1.0", "end")
        assert "simulated GUI import denial" in detail_box.get("1.0", "end")
        for child in app.root.winfo_children():
            if isinstance(child, ctk.CTkToplevel) and child.title() == "Import issues":
                child.destroy()
        monkeypatch.setattr(discovery_module.os, "lstat", original_lstat)

        recovery_file = tmp_path / "recovered.txt"
        recovery_file.write_text("recovered")
        monkeypatch.setattr(
            app_module.filedialog,
            "askopenfilenames",
            lambda **_options: (str(recovery_file),),
        )
        app.add_files()
        wait_for_import()
        assert app.import_warnings_button.cget("text") == "Import issues (1)"
        assert len(app.collection.snapshot().incomplete_imports) == 1
        assert app.tree.get_children("") == tuple(
            str(entry.entry_id) for entry in app.collection.snapshot().entries
        )

        before_cancel = app.collection.snapshot()
        prior_selection = tuple(app.tree.selection())
        cancel_root = tmp_path / "cancel-folder"
        (cancel_root / "sub").mkdir(parents=True)
        (cancel_root / "first.txt").write_text("first")
        (cancel_root / "sub" / "second.txt").write_text("second")
        entered_wait = threading.Event()
        release_walk = threading.Event()
        original_walk = DiscoverySession._walk_folder

        def blocked_walk(session, selected_root):
            for index, record in enumerate(original_walk(session, selected_root)):
                yield record
                if index == 0:
                    entered_wait.set()
                    release_walk.wait()

        monkeypatch.setattr(DiscoverySession, "_walk_folder", blocked_walk)
        monkeypatch.setattr(
            app_module.filedialog,
            "askdirectory",
            lambda **_options: str(cancel_root),
        )
        try:
            app.add_folder()
            deadline = time.monotonic() + 5
            while (
                len(app.tree.get_children("")) <= len(before_cancel.entries)
                or not entered_wait.is_set()
            ) and time.monotonic() < deadline:
                root.update()
                time.sleep(0.001)
            assert entered_wait.is_set()
            assert len(app.tree.get_children("")) > len(before_cancel.entries)
            assert not app.import_controller.session.is_terminal
            app.tabs.set("Repacker")
            app._on_mode_change()
            assert app.active_mode == "Repacker"
            assert app.tree.selection() == prior_selection
            app.cancel_import()
            release_walk.set()
            wait_for_import()
        finally:
            release_walk.set()
        after_cancel = app.collection.snapshot()
        assert tuple(entry.entry_id for entry in after_cancel.entries) == tuple(
            entry.entry_id for entry in before_cancel.entries
        )
        assert after_cancel.folder_roots == before_cancel.folder_roots
        assert app.tree.selection() == prior_selection
        assert len(app.tree.get_children("")) == len(before_cancel.entries)
        assert "Import canceled" in app.status.cget("text")

        app.rename_format.set("<Name> draft")
        app.counter_switch.select()

        final_folder = tmp_path / "close-folder"
        final_folder.mkdir()
        (final_folder / "close.txt").write_text("close")
        close_entered = threading.Event()
        def blocked_close_walk(session, selected_root):
            for index, record in enumerate(original_walk(session, selected_root)):
                yield record
                if index == 0:
                    close_entered.set()
                    close_release.wait()

        monkeypatch.setattr(DiscoverySession, "_walk_folder", blocked_close_walk)
        app.add_folder()
        deadline = time.monotonic() + 5
        while (
            not close_entered.is_set()
            or len(app.tree.get_children("")) <= len(after_cancel.entries)
        ) and time.monotonic() < deadline:
            root.update()
            time.sleep(0.001)
        assert close_entered.is_set()
        app.open_columns()
        closing_dialog = app.column_dialog
        assert closing_dialog is not None
        dialog_destroyed = []
        closing_dialog.bind(
            "<Destroy>",
            lambda event: dialog_destroyed.append(True)
            if event.widget is closing_dialog
            else None,
            add="+",
        )
        app.close()
        assert app._close_pending
        assert not app._closing
        close_release.set()
        deadline = time.monotonic() + 5
        while not app._closing and time.monotonic() < deadline:
            root.update()
            time.sleep(0.001)
        assert app._closing
        assert app.column_dialog is None
        assert dialog_destroyed
        closed = True
    finally:
        close_release.set()
        if not closed and not app._closing:
            root.destroy()

    saved = load_config(config_path)
    preferences = saved["global_settings"]["column_preferences"]
    assert "extension" in preferences["bulk_rename"]["visible"]
    assert preferences["bulk_rename"]["widths"]["extension"] == 110
    assert preferences["duplicate_finder"]["visible"] == [
        "row_number",
        "filename",
        "size",
        "group",
        "status",
    ]
    assert preferences["duplicate_finder"]["widths"]["filename"] == 320
    assert saved["global_settings"]["appearance_mode"] == "Dark"
    assert "window_geometry" in saved["global_settings"]
    assert "rename_format" not in saved["global_settings"]
    assert "counter" not in saved["global_settings"]
    assert "collection" not in saved["global_settings"]
    assert saved["global_settings"]["auto_clean"] is True


def test_process_button_runs_conflict_free_plan_without_review(tmp_path, monkeypatch) -> None:
    if _run_gui_test_in_isolated_process(
        "test_process_button_runs_conflict_free_plan_without_review"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    source = tmp_path / "source.txt"
    source.write_bytes(b"fixture contents")
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *_args, **_kwargs: None)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        _import_files(app, root, monkeypatch, source)
        app.rename_format.set("<Name> reviewed")
        _wait_for(root, app._process_ready)

        app.process_button.invoke()
        _wait_for(root, lambda: app._processing_request is not None)
        assert app._plan_review_dialog is None
        destination = tmp_path / "source reviewed.txt"
        assert source.exists()
        assert not destination.exists()

        _wait_for(
            root,
            lambda: app._processing_phase == "idle" and destination.exists(),
        )
        assert destination.read_bytes() == b"fixture contents"
        assert not source.exists()
        assert app.collection.snapshot().entries == ()
    finally:
        _close_app(app, root)


def test_collision_subset_processes_only_confirmed_unaffected_file(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_collision_subset_processes_only_confirmed_unaffected_file"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    blocked = tmp_path / "blocked.txt"
    free = tmp_path / "free.txt"
    blocked.write_bytes(b"blocked source")
    free.write_bytes(b"free source")
    obstacle = tmp_path / "blocked reviewed.txt"
    obstacle.write_bytes(b"unrelated destination")
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *_args, **_kwargs: None)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        _import_files(app, root, monkeypatch, blocked, free)
        app.rename_format.set("<Name> reviewed")
        _wait_for(root, app._process_ready)

        app.process_button.invoke()
        _wait_for(root, lambda: app._plan_review_dialog is not None)
        _confirm_review(app, "Process 1 unaffected files")
        free_destination = tmp_path / "free reviewed.txt"
        _wait_for(
            root,
            lambda: app._processing_phase == "idle" and free_destination.exists(),
        )

        assert free_destination.read_bytes() == b"free source"
        assert blocked.read_bytes() == b"blocked source"
        assert obstacle.read_bytes() == b"unrelated destination"
        remaining = app.collection.snapshot().entries
        assert len(remaining) == 1
        assert remaining[0].source_path == str(blocked)
        assert "Excluded" in app._processing_diagnostics_by_id[remaining[0].entry_id]
    finally:
        _close_app(app, root)


def test_invalid_filename_blocks_and_changed_preflight_requires_review(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_invalid_filename_blocks_and_changed_preflight_requires_review"
    ):
        return
    from tidyfiles.destination_planning import PlanState
    from tidyfiles.transaction_journal import JournalStore

    source = tmp_path / "source.txt"
    source.write_bytes(b"fixture contents")
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        _import_files(app, root, monkeypatch, source)
        app.rename_format.set("<UnknownToken>")
        _wait_for(
            root,
            lambda: app._preview_result is not None and not app._preview_result.is_valid,
        )
        assert app.process_button.cget("state") == "disabled"
        assert source.read_bytes() == b"fixture contents"

        app.rename_format.set("<Name> reviewed")
        _wait_for(root, app._process_ready)
        destination = tmp_path / "source reviewed.txt"
        submit_preflight = app.preflight_service.submit

        def occupy_destination_then_submit(request, *, progress_callback=None):
            destination.write_bytes(b"new unrelated content")
            return submit_preflight(request, progress_callback=progress_callback)

        monkeypatch.setattr(app.preflight_service, "submit", occupy_destination_then_submit)
        app.process_button.invoke()
        _wait_for(
            root,
            lambda: app._plan_review_dialog is not None
            and app._processing_plan is not None
            and app._processing_plan.state is PlanState.COLLISION_BLOCKED,
        )
        app.cancel_current_work()
        _wait_for(root, lambda: app._processing_phase == "idle")

        assert source.read_bytes() == b"fixture contents"
        assert destination.read_bytes() == b"new unrelated content"
        assert len(app.collection.snapshot().entries) == 1
    finally:
        _close_app(app, root)


def test_single_folder_and_auto_clean_use_the_original_folder_snapshot(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_single_folder_and_auto_clean_use_the_original_folder_snapshot"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    root_folder = tmp_path / "import-root"
    nested = root_folder / "nested"
    nested.mkdir(parents=True)
    source = nested / "source.txt"
    source.write_bytes(b"fixture contents")
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        monkeypatch.setattr(
            app_module.filedialog,
            "askdirectory",
            lambda **_options: str(root_folder),
        )
        app.add_folder()
        _wait_for(root, lambda: not app.import_controller.busy)
        clutter = root_folder / "desktop.ini"
        clutter.write_bytes(b"eligible clutter")

        app.single_folder_switch.select()
        app._on_destination_option_changed()
        app.auto_clean_switch.select()
        app._on_auto_clean_changed()
        assert app.config["global_settings"]["auto_clean"] is True
        app.rename_format.set("<Name> flattened")
        _wait_for(root, app._process_ready)

        app.process_button.invoke()
        _wait_for(root, lambda: app._processing_request is not None)
        assert app._plan_review_dialog is None
        destination = root_folder / "source flattened.txt"
        _wait_for(
            root,
            lambda: app._processing_phase == "idle"
            and destination.exists()
            and not clutter.exists(),
        )

        assert destination.read_bytes() == b"fixture contents"
        assert not source.exists()
        assert not nested.exists()
    finally:
        _close_app(app, root)


def test_incomplete_import_requires_acknowledgment_before_rename(tmp_path, monkeypatch) -> None:
    if _run_gui_test_in_isolated_process(
        "test_incomplete_import_requires_acknowledgment_before_rename"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    source = tmp_path / "source.txt"
    source.write_bytes(b"fixture contents")
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    acknowledgments = []
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda title, *_args, **_kwargs: acknowledgments.append(title) or True,
    )
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        _import_files(app, root, monkeypatch, source)
        session = app.collection.begin_import(ImportMode.ADD_FILES)
        app.collection.finalize_import(
            session,
            incomplete=True,
            diagnostics=(ImportDiagnostic(str(tmp_path), "simulated access denial"),),
        )
        app.refresh_collection()
        app._notify_collection_changed()
        app.rename_format.set("<Name> acknowledged")
        _wait_for(root, app._process_ready)

        app.process_button.invoke()
        assert acknowledgments == ["Acknowledge incomplete imports"]
        _wait_for(root, lambda: app._processing_request is not None)
        assert app._plan_review_dialog is None
        destination = tmp_path / "source acknowledged.txt"
        _wait_for(
            root,
            lambda: app._processing_phase == "idle" and destination.exists(),
        )
        assert destination.read_bytes() == b"fixture contents"
        assert app.collection.snapshot().incomplete_imports
    finally:
        _close_app(app, root)


def test_manual_trash_clear_requires_confirmation_and_auto_clear_defaults_off(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_manual_trash_clear_requires_confirmation_and_auto_clear_defaults_off"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    class TrashService:
        def __init__(self):
            self.clear_calls = []

        def inspect(self):
            return SimpleNamespace(
                eligible_count=1,
                eligible_size_bytes=9,
                items=(),
                detail=None,
            )

        def clear(self, **options):
            self.clear_calls.append(options)
            return SimpleNamespace(
                cleanup=SimpleNamespace(deleted_paths=("fixture-item",), failures=(), detail=None)
            )

    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    service = TrashService()
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module, "TrashClearService", lambda *_args: service)
    answers = iter((False, True))
    monkeypatch.setattr(
        app_module.messagebox, "askyesno", lambda *_args, **_kwargs: next(answers)
    )
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        app.open_settings()
        assert not app.auto_clear_trash_switch.get()

        app.clear_trash()
        _wait_for(root, lambda: app._processing_phase == "idle")
        assert service.clear_calls == []

        app.clear_trash()
        _wait_for(root, lambda: app._processing_phase == "idle" and service.clear_calls)
        assert service.clear_calls == [{"manually_authorized": True}]
    finally:
        _close_app(app, root)
        assert len(service.clear_calls) == 1


def test_auto_clear_trash_runs_only_during_enabled_normal_exit(tmp_path, monkeypatch) -> None:
    if _run_gui_test_in_isolated_process(
        "test_auto_clear_trash_runs_only_during_enabled_normal_exit"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    class TrashService:
        def __init__(self):
            self.clear_calls = []

        def inspect(self):
            raise AssertionError("Auto-Clear Trash must not inspect at startup")

        def clear(self, **options):
            self.clear_calls.append(options)
            return SimpleNamespace(
                cleanup=SimpleNamespace(deleted_paths=(), failures=(), detail=None)
            )

    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    service = TrashService()
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module, "TrashClearService", lambda *_args: service)
    config = default_config()
    config["global_settings"]["auto_clear_trash_on_exit"] = True
    root = create_main_window(config, tmp_path / "config.json")
    app = root.application
    root.update()

    app.close()
    _wait_for(root, lambda: app._closing)

    assert service.clear_calls == [
        {"auto_clear_enabled": True, "normal_exit": True}
    ]


def test_startup_recovery_notice_shows_paths_stages_and_explorer_location(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_startup_recovery_notice_shows_paths_stages_and_explorer_location"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    source = tmp_path / "source.txt"
    source.write_bytes(b"fixture")
    journal_path = tmp_path / "transaction.json"
    journal_path.write_text("read-only fixture")
    location = SimpleNamespace(
        record=None,
        path=journal_path,
        status=SimpleNamespace(value="valid"),
        detail=None,
    )
    observation = SimpleNamespace(
        role="source",
        path=str(source),
        current=SimpleNamespace(
            occupancy=SimpleNamespace(value="file"), reason=None
        ),
    )
    entry = SimpleNamespace(
        source_path=str(source),
        destination_path=str(tmp_path / "destination.txt"),
        recorded_stage="before_rename",
        recorded_outcome=SimpleNamespace(value="pending"),
        detail=None,
        paths=(observation,),
    )
    transaction = SimpleNamespace(
        transaction_id=uuid4(),
        operation=None,
        journal_status=SimpleNamespace(value="unresolved"),
        journal_locations=(location,),
        entries=(entry,),
    )
    inspection = SimpleNamespace(transactions=(transaction,), storage_issues=())

    class Inspector:
        def __init__(self, *_args):
            pass

        def inspect(self):
            return inspection

    opened = []
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module, "RecoveryInspector", Inspector)
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda arguments: opened.append(arguments))
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        _wait_for(root, lambda: app._recovery_dialog is not None)
        box = next(
            widget
            for widget in _descendants(app._recovery_dialog)
            if isinstance(widget, ctk.CTkTextbox)
        )
        details = box.get("1.0", "end")
        assert "before_rename" in details
        assert str(source) in details
        app._recovery_path_list.selection_set(0)
        app._open_selected_recovery_location()
        assert opened == [["explorer.exe", "/select,", str(journal_path)]]
    finally:
        _close_app(app, root)


def test_modifying_phase_disables_conflicting_actions_and_blocks_cancel_and_close(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_modifying_phase_disables_conflicting_actions_and_blocks_cancel_and_close"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    source = tmp_path / "source.txt"
    source.write_bytes(b"fixture contents")
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    entered_modification = threading.Event()
    release_modification = threading.Event()
    original_begin = app.coordinator.begin_modification

    def blocked_begin(job_id, journal):
        original_begin(job_id, journal)
        entered_modification.set()
        release_modification.wait(10)

    monkeypatch.setattr(app.coordinator, "begin_modification", blocked_begin)
    try:
        root.update()
        _import_files(app, root, monkeypatch, source)
        app.rename_format.set("<Name> reviewed")
        _wait_for(root, app._process_ready)
        app.process_button.invoke()
        _wait_for(root, lambda: app._processing_request is not None)
        assert app._plan_review_dialog is None
        _wait_for(root, entered_modification.is_set)

        assert app.coordinator.current.modifying
        app.cancel_current_work()
        assert app._processing_phase == "rename_modifying"
        assert not app._processing_cancel.is_set()
        assert app.cancel_button.cget("state") == "disabled"
        assert app.controls["add_files"].cget("state") == "disabled"
        assert app.process_button.cget("state") == "disabled"
        app.close()
        assert not app._closing
        assert source.exists()
        release_modification.set()
        destination = tmp_path / "source reviewed.txt"
        _wait_for(
            root,
            lambda: app._processing_phase == "idle" and destination.exists(),
        )
        assert destination.read_bytes() == b"fixture contents"
    finally:
        release_modification.set()
        _close_app(app, root)
