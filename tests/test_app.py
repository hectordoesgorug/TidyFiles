from __future__ import annotations

from pathlib import Path

import customtkinter as ctk

from tidyfiles.app import create_main_window
from tidyfiles.collection import FileMetadataSnapshot, ImportMode
from tidyfiles.config import default_config, load_config
from tidyfiles.ui_preferences import COLUMNS


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def test_main_window_collection_columns_controls_and_persistence(tmp_path: Path) -> None:
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
        for control in (
            *app.controls.values(),
            *app.movement_buttons.values(),
            app.sort_dropdown,
            app.sort_direction_button,
            app.insert_token_button,
            app.profiles_button,
            app.format_reference_button,
            app.process_button,
        ):
            assert control.cget("state") == "disabled"

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
        assert photo_cells["status"] == ""
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

        app.rename_format.set("<Name> draft")
        app.counter_switch.select()
        app.close()
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
