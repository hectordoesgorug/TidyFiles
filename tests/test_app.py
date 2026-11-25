from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import customtkinter as ctk

import tidyfiles.app as app_module
import tidyfiles.discovery as discovery_module
from tidyfiles.app import create_main_window
from tidyfiles.collection import FileMetadataSnapshot, ImportMode
from tidyfiles.config import default_config, load_config
from tidyfiles.discovery import DiscoverySession
from tidyfiles.ui_preferences import COLUMNS


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def test_main_window_collection_columns_import_and_persistence(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config = default_config()
    config["global_settings"]["appearance_mode"] = "Dark"
    config["global_settings"]["window_geometry"] = "1400x900+5000+5000"
    config["global_settings"]["auto_clean"] = True
    root = create_main_window(config, config_path)
    app = root.application
    closed = False
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
        assert app.auto_clean_switch.cget("state") == "disabled"
        assert not app.auto_clean_switch.get()
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
                    release_walk.wait(5)

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
        close_release = threading.Event()

        def blocked_close_walk(session, selected_root):
            for index, record in enumerate(original_walk(session, selected_root)):
                yield record
                if index == 0:
                    close_entered.set()
                    close_release.wait(5)

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
        if not closed:
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
