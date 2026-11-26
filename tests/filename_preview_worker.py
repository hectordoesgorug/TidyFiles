from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from pathlib import Path
from uuid import UUID

import pytest

import tidyfiles.app as app_module
from tidyfiles.app import _FORMAT_PRESETS, create_main_window
from tidyfiles.config import default_config
from tidyfiles.filename_evaluator import FileEvaluationContext, evaluate_batch
from tidyfiles.filename_format import TokenKind, parse_format
from tidyfiles.filename_integration import EvaluationProgress, EvaluationStage
from tidyfiles.format_reference import (
    FORMAT_REFERENCE,
    TOKEN_GROUPS,
    TOKEN_TEMPLATES,
    validate_insertion_templates,
)


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from tidyfiles.transaction_journal import JournalStore

    directory = tmp_path_factory.mktemp("filename-preview")
    (directory / "local").mkdir()
    journals = JournalStore(app_dir=directory / "app", local_app_data=directory / "local")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
        config_path = directory / "config.json"
        root = create_main_window(default_config(), config_path)
        root.update()
        app = root.application
        yield root, app, config_path
        if not app._closing:
            app.close()


def _pump_until(root, predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.002)
    root.update()
    assert predicate(), f"condition not reached; status={root.application.status.cget('text')!r}"


def _wait_import(root, app) -> None:
    _pump_until(root, lambda: not app.import_controller.busy)
    root.update()


def _wait_preview(root, app, *, errors: bool = False) -> None:
    expected = "Preview contains errors" if errors else "Preview complete"
    _pump_until(root, lambda: app.status.cget("text").startswith(expected))


def _import_files(root, app, paths: tuple[Path, ...]) -> None:
    app.import_controller.start_files(tuple(str(path) for path in paths))
    _wait_import(root, app)


def _row(app, filename: str) -> tuple[str, tuple[str, ...]]:
    for item_id in app.tree.get_children(""):
        values = app.tree.item(item_id, "values")
        if values[1] == filename:
            return item_id, values
    raise AssertionError(f"row not found: {filename}")


def _evaluate(expression: str, stem: str, *, root: str = "root") -> str:
    context = FileEvaluationContext(
        original_stem=stem,
        file_identifier=f"{stem}.txt",
        import_root_name=root,
        parent_name="parent",
        parent_identity=r"C:\root\parent",
        physical_file_count=5,
        physical_directory_count=2,
        source_extension=".txt",
    )
    result = evaluate_batch(parse_format(expression), (context,))
    assert result.is_valid, result.errors
    assert result.files[0].validated_filename is not None
    return result.files[0].validated_filename


def test_reference_templates_and_examples_match_engine() -> None:
    validate_insertion_templates()
    assert len(TOKEN_TEMPLATES) == len(TokenKind) == 20
    assert sum(len(tokens) for _category, tokens in TOKEN_GROUPS) == 20
    assert all(kind.value in FORMAT_REFERENCE for kind in TokenKind)
    assert all(parse_format(template).parts for template in TOKEN_TEMPLATES.values())

    expected = {
        "<Name>": ("Report", "Report.txt"),
        "<Name>.<Ext>": ("Report", "Report.txt"),
        "<DirName>_<Name>": ("Report", "root_Report.txt"),
        "<SubDirName>_<Name>": ("Report", "parent_Report.txt"),
        "<NumFiles>": ("Report", "5.txt"),
        "<NumDirs>": ("Report", "2.txt"),
        "<NumItems>": ("Report", "1.txt"),
        "<IncNr>": ("Report", "01.txt"),
        "<IncNrDir>": ("Report", "01.txt"),
        "<DecrNr>": ("Report", "01.txt"),
        "<IncAlpha>": ("Report", "a.txt"),
        "<Word:2>": ("The Blue Ocean", "Blue.txt"),
        "<Substr>": ("Oceanview", "Ocean.txt"),
        "<RSubstr>": ("Oceanview", "nview.txt"),
        "<RemoveL:2>": ("Report", "port.txt"),
        "<RemoveR:2>": ("Report", "Repo.txt"),
        "<Move1:2>": ("abcdef", "bcadef.txt"),
        "<RepS:1,X>": ("abc", "Xbc.txt"),
        "<RepM:2,1,X>": ("abcd", "Xcd.txt"),
        "<Ins0:X>": ("abc", "Xabc.txt"),
    }
    assert {kind.value for kind in TokenKind} == {
        kind.value
        for _category, tokens in TOKEN_GROUPS
        for kind, _template in tokens
    }
    for expression, (stem, filename) in expected.items():
        assert _evaluate(expression, stem) == filename


def test_controls_live_results_invalidation_and_tab_state(runtime, tmp_path: Path) -> None:
    root, app, config_path = runtime
    assert app.rename_format.get() == ""
    assert tuple(app.format_field.cget("values")) == _FORMAT_PRESETS
    assert app.profiles_button.cget("state") == "disabled"
    assert app.insert_token_button.cget("state") == "normal"
    assert app.format_reference_button.cget("state") == "normal"
    assert app.process_button.cget("state") == "disabled"
    assert "Does not rescan" in app.refresh_preview_help.cget("text")
    assert app._preview_service._latest_request_id is None

    for _category, tokens in TOKEN_GROUPS:
        for _kind, template in tokens:
            parse_format(template)
    app.rename_format.set("LeftRight")
    entry = app.format_field._entry
    entry.icursor(4)
    app._insert_format_token("<Name>")
    assert app.rename_format.get() == "Left<Name>Right"
    assert entry.index("insert") == 10
    entry.selection_range(0, 4)
    app._insert_format_token("<Ext>")
    assert app.rename_format.get() == "<Ext><Name>Right"
    app.rename_format.set("abc")
    entry.selection_clear()
    entry.icursor(1)
    app._build_insert_token_menu()
    app._insert_token_submenus[0].invoke(0)
    assert app.rename_format.get() == "a<Name>bc"

    app.open_format_reference()
    reference_dialog = app._format_reference_dialog
    assert reference_dialog is not None
    app.open_format_reference()
    assert app._format_reference_dialog is reference_dialog
    assert app.rename_format.get() == "a<Name>bc"
    reference_dialog.destroy()
    root.update()
    app.open_format_reference()
    assert app._format_reference_dialog is not None
    app._format_reference_dialog.destroy()

    source = tmp_path / "source"
    source.mkdir()
    paths = []
    for name in ("One.txt", "Two.txt"):
        path = source / name
        path.write_text(name, encoding="utf-8")
        paths.append(path)
    app.rename_format.set("<Name> <IncNr>")
    submit = app._preview_service.submit
    submitted = []

    def count_submit(*args, **kwargs):
        submitted.append(1)
        return submit(*args, **kwargs)

    app._preview_service.submit = count_submit
    app.import_controller.start_files(tuple(str(path) for path in paths))
    while app.import_controller.busy:
        root.update()
        assert not submitted
        time.sleep(0.002)
    _wait_preview(root, app)
    assert len(submitted) == 1
    for preset in _FORMAT_PRESETS:
        app._on_preset_selected(preset)
        _wait_preview(root, app)
        assert all(
            app.tree.item(item_id, "values")[4] in {"Valid preview", "Unchanged"}
            for item_id in app.tree.get_children("")
        )
    submitted.clear()
    first_id, first = _row(app, "One.txt")
    _second_id, second = _row(app, "Two.txt")
    assert first[2] == "One 01.txt" and first[4] == "Valid preview"
    assert second[2] == "Two 02.txt" and second[4] == "Valid preview"
    assert app.tree.selection() == ()
    assert app._preview_filename_ids == {UUID(first_id), UUID(_second_id)}
    assert app._preview_problem_ids == set()

    snapshot = app.collection.snapshot
    app.collection.snapshot = lambda: (_ for _ in ()).throw(
        AssertionError("preview currency must not copy the collection")
    )
    try:
        assert app._preview_result is not None
        assert app._preview_result_is_current(app._preview_result)
        app._update_control_states()
    finally:
        app.collection.snapshot = snapshot

    app.tree.selection_set(first_id)
    root.update()
    time.sleep(0.35)
    root.update()
    assert not submitted

    app.tree.column("new_filename", width=317)
    entry.delete(0, "end")
    root.update()
    get_children = app.tree.get_children
    clear_scans = []

    def count_get_children(*args, **kwargs):
        clear_scans.append(1)
        return get_children(*args, **kwargs)

    app.tree.get_children = count_get_children
    for character in "<Name>_<IncNr>":
        entry.insert("end", character)
        root.update()
    app.tree.get_children = get_children
    assert len(clear_scans) == 1
    assert all(
        app.tree.item(item_id, "values")[2] == ""
        for item_id in app.tree.get_children("")
    )
    assert app._preview_filename_ids == set()
    assert app._preview_problem_ids == set()
    _wait_preview(root, app)
    assert app.tree.selection() == (first_id,)
    assert int(app.tree.column("new_filename", "width")) == 317
    assert _row(app, "One.txt")[1][2] == "One_01.txt"
    assert len(submitted) == 1

    app.tabs.set("Repacker")
    app._on_mode_change()
    root.update()
    assert app.tree.item(first_id, "values")[2] == ""
    assert app.tree.item(first_id, "values")[4] == "Imported"
    app.tabs.set("Bulk Rename")
    app._on_mode_change()
    _wait_preview(root, app)
    assert _row(app, "One.txt")[1][2] == "One_01.txt"
    assert len(submitted) == 1

    tree_item = app.tree.item
    writes = []

    def count_tree_writes(item_id, option=None, **kwargs):
        if kwargs:
            writes.append(item_id)
        return tree_item(item_id, option, **kwargs)

    app.tree.item = count_tree_writes
    app._start_preview_render(app._preview_result)
    _pump_until(root, lambda: app._preview_render_result is None)
    app.tree.item = tree_item
    assert writes == []

    app.rename_format.set("")
    root.update()
    assert _row(app, "One.txt")[1][2] == ""
    assert "required" in app.format_diagnostic.cget("text").lower()
    app._save_preferences()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "rename_format" not in saved
    assert "One_01" not in config_path.read_text(encoding="utf-8")


def test_parse_file_and_validation_errors_recover(runtime, tmp_path: Path) -> None:
    root, app, _config_path = runtime
    app.clear_all()
    root.update()
    source = tmp_path / "error-source"
    source.mkdir()
    paths = []
    for name in ("The Blue Ocean.txt", "Mountain.txt"):
        path = source / name
        path.write_text(name, encoding="utf-8")
        paths.append(path)
    app.rename_format.set("<Word:3>")
    _import_files(root, app, tuple(paths))
    _wait_preview(root, app, errors=True)
    ocean_id, ocean = _row(app, "The Blue Ocean.txt")
    mountain_id, mountain = _row(app, "Mountain.txt")
    assert ocean[2] == "Ocean.txt" and ocean[4] == "Valid preview"
    assert mountain[2] == "" and mountain[4] == "Invalid format"
    assert app.tree.item(ocean_id, "tags") == ("normal",)
    assert app.tree.item(mountain_id, "tags") == ("problem",)
    app._context_entry_id = next(
        entry.entry_id
        for entry in app.collection.snapshot().entries
        if entry.original_filename == "Mountain.txt"
    )
    app.open_preview_details()
    assert app._preview_details_dialog is not None
    assert app._preview_details_textbox is not None
    detail_text = app._preview_details_textbox.get("1.0", "end")
    assert "Mountain.txt" in detail_text and "Word index is outside" in detail_text
    assert app._preview_details_textbox.cget("state") == "disabled"
    app._preview_details_dialog.destroy()

    app.rename_format.set("<Name>_<IncNr:abc>")
    _wait_preview(root, app, errors=True)
    assert "unsigned decimal" in app.format_diagnostic.cget("text")
    assert app.tree.item(ocean_id, "values")[2] == ""
    assert app.tree.item(ocean_id, "values")[4] == "Invalid format"
    assert app.tree.item(mountain_id, "tags") == ("normal",)
    app.rename_format.set("<Name")
    _wait_preview(root, app, errors=True)
    assert "closing delimiter" in app.format_diagnostic.cget("text")
    app.rename_format.set("<Name>")
    _wait_preview(root, app)
    assert _row(app, "The Blue Ocean.txt")[1][2] == "The Blue Ocean.txt"
    assert _row(app, "The Blue Ocean.txt")[1][4] == "Unchanged"

    app.rename_format.set("CON")
    _wait_preview(root, app, errors=True)
    assert all(
        app.tree.item(item_id, "values")[4] == "Invalid filename"
        for item_id in app.tree.get_children("")
    )
    assert all(
        app.tree.item(item_id, "tags") == ("problem",)
        for item_id in app.tree.get_children("")
    )
    assert app.process_button.cget("state") == "disabled"


def test_collection_order_metadata_refresh_and_source_safety(runtime, tmp_path: Path) -> None:
    root, app, _config_path = runtime
    app.clear_all()
    root.update()
    source = tmp_path / "counted"
    source.mkdir()
    paths = []
    for name in ("file1.txt", "file2.txt", "file10.txt"):
        path = source / name
        path.write_text(name, encoding="utf-8")
        paths.append(path)
    contents = {path: path.read_bytes() for path in paths}
    app.rename_format.set("Photo <IncNr>")
    _import_files(root, app, tuple(paths))
    _wait_preview(root, app)
    assert app._preview_result is not None
    app._preview_progress.put(
        EvaluationProgress(
            app._preview_result.request_id, EvaluationStage.EVALUATING, 1, 1
        )
    )
    _pump_until(root, lambda: app._preview_progress.empty())
    assert app.status.cget("text").startswith("Preview complete")
    ids_by_name = {name: _row(app, name)[0] for name in ("file1.txt", "file2.txt", "file10.txt")}
    assert _row(app, "file1.txt")[1][2] == "Photo 01.txt"
    assert _row(app, "file10.txt")[1][2] == "Photo 03.txt"

    app.tree.selection_set(ids_by_name["file10.txt"])
    app._move_selected("move_top")
    _pump_until(root, lambda: not app._collection_operation_pending)
    _wait_preview(root, app)
    assert _row(app, "file10.txt")[1][2] == "Photo 01.txt"

    cumulative = source / "file20.txt"
    cumulative.write_text("cumulative", encoding="utf-8")
    contents[cumulative] = cumulative.read_bytes()
    app.import_controller.start_files((str(cumulative),))
    _wait_import(root, app)
    _wait_preview(root, app)
    assert _row(app, "file20.txt")[1][2] == "Photo 04.txt"

    app.tree.selection_set(ids_by_name["file2.txt"])
    app.remove_selected()
    _pump_until(root, lambda: not app._collection_operation_pending)
    _wait_preview(root, app)
    assert not app.tree.exists(ids_by_name["file2.txt"])
    assert paths[1].read_bytes() == contents[paths[1]]
    assert _row(app, "file20.txt")[1][2] == "Photo 03.txt"

    outside = source / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    app.rename_format.set("<NumFiles>_<Name>")
    _wait_preview(root, app)
    assert _row(app, "file1.txt")[1][2] == "5_file1.txt"
    outside2 = source / "outside2.txt"
    outside2.write_text("external", encoding="utf-8")
    time.sleep(0.35)
    root.update()
    assert _row(app, "file1.txt")[1][2] == "5_file1.txt"
    app.refresh_filename_preview()
    _wait_preview(root, app)
    assert _row(app, "file1.txt")[1][2] == "6_file1.txt"
    assert app._preview_result is not None
    assert app._preview_result.directory_enumerations == 1

    imported_after_cache = source / "imported_after_cache.txt"
    imported_after_cache.write_text("new count", encoding="utf-8")
    contents[imported_after_cache] = imported_after_cache.read_bytes()
    app.import_controller.start_files((str(imported_after_cache),))
    _wait_import(root, app)
    _wait_preview(root, app)
    assert _row(app, "imported_after_cache.txt")[1][2] == "7_imported_after_cache.txt"
    assert app._preview_result is not None
    assert app._preview_result.directory_enumerations == 1

    app.rename_format.set("<Name>")
    _wait_preview(root, app)
    assert app._preview_result is not None
    assert app._preview_result.directory_enumerations == 0
    before = {path: path.read_bytes() for path in contents}
    moved_source = tmp_path / "counted-moved"
    source.rename(moved_source)
    app.rename_format.set("<NumFiles>_<Name>")
    _wait_preview(root, app, errors=True)
    old_request_id = app._preview_result.request_id
    app.refresh_filename_preview()
    _pump_until(
        root,
        lambda: app.status.cget("text").startswith("Preview contains errors")
        and app._preview_result is not None
        and app._preview_result.request_id != old_request_id,
    )
    assert all(
        app.tree.item(item_id, "values")[4] == "Metadata unavailable"
        for item_id in app.tree.get_children("")
    )
    moved_contents = {
        moved_source / path.name: (moved_source / path.name).read_bytes()
        for path in contents
    }
    assert moved_contents == {
        moved_source / path.name: data for path, data in before.items()
    }

    app.clear_all()
    root.update()
    assert app.collection.snapshot().entries == ()
    assert app.tree.get_children("") == ()
    assert {path.name: (moved_source / path.name).read_bytes() for path in contents} == {
        path.name: data for path, data in contents.items()
    }


def test_shutdown_does_not_wait_for_active_preview(runtime, tmp_path: Path) -> None:
    root, app, _config_path = runtime
    source = tmp_path / "shutdown-source"
    source.mkdir()
    path = source / "one.txt"
    path.write_text("unchanged", encoding="utf-8")
    app.rename_format.set("<Name>")
    _import_files(root, app, (path,))
    _wait_preview(root, app)

    entered = threading.Event()
    release = threading.Event()
    observe = app._preview_service.metadata_cache.observe

    def blocked_observe(*args, **kwargs):
        entered.set()
        release.wait(5)
        return observe(*args, **kwargs)

    app._preview_service.metadata_cache.observe = blocked_observe
    app.rename_format.set("<NumFiles>_<Name>")
    try:
        _pump_until(root, entered.is_set)
        started = time.monotonic()
        app.close()
        assert time.monotonic() - started < 1
        assert app._closing
        with pytest.raises(tk.TclError):
            root.winfo_exists()
    finally:
        release.set()
    assert path.read_text(encoding="utf-8") == "unchanged"
