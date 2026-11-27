from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import customtkinter as ctk
import pytest

import tidyfiles.app as app_module
import tidyfiles.discovery as discovery_module
from tidyfiles.app import _ApplicationRoot, create_main_window
from tidyfiles.collection import (
    FileMetadataSnapshot,
    ImportDiagnostic,
    ImportMode,
    WorkingCollection,
)
from tidyfiles.config import default_config, load_config
from tidyfiles.discovery import DiscoverySession
from tidyfiles.ui_preferences import COLUMNS


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _wait_for(root: _ApplicationRoot, predicate: Callable[[], object], timeout: float = 10) -> None:
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
        app._dismiss_files_excluded(continue_review=False)
        app.close()
    else:
        root.destroy()


@pytest.mark.parametrize("scenario", [
    "disabled", "independent", "ok", "purge", "deduplicate", "changed", "unavailable",
    "cancel", "stale", "purge_then_deduplicate", "deduplicate_then_purge", "purge_partial",
])
def test_pre_verification_and_purge_workflow(tmp_path, monkeypatch, scenario):
    if _run_gui_test_in_isolated_process(f"test_pre_verification_and_purge_workflow[{scenario}]"):
        return
    from dataclasses import replace

    from PIL import Image

    import tidyfiles.managed_trash as trash
    import tidyfiles.storage as storage
    from tidyfiles.transaction_journal import JournalStore

    app_dir, local = tmp_path / "app", tmp_path / "local"
    app_dir.mkdir()
    local.mkdir()
    journal = JournalStore(app_dir=app_dir, local_app_data=local)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journal)
    monkeypatch.setattr(storage, "application_directory", lambda: app_dir)
    storage.managed_trash_path.cache_clear()
    if scenario == "unavailable":
        monkeypatch.setattr(trash, "managed_trash_path", lambda: None)
    good, duplicate, bad, unsupported = (
        tmp_path / name for name in ("a.txt", "b.txt", "broken.png", "unknown.jpg")
    )
    good.write_bytes(b"exact copy")
    duplicate.write_bytes(good.read_bytes())
    unsupported.write_bytes(b"unidentified image fixture")
    with Image.new("RGB", (16, 16)) as image:
        image.save(bad)
    original = bad.read_bytes()[:25]
    bad.write_bytes(original)
    second_bad = tmp_path / "second broken.png"
    partial = scenario == "purge_partial"
    if partial:
        second_bad.write_bytes(original)
    config = default_config()
    config["global_settings"].update(
        pre_verify=scenario != "disabled", duplicate_checker=scenario != "independent",
        perceptual_hashing=scenario not in {"disabled", "independent", "cancel", "stale"},
    )
    root = create_main_window(config)
    app = root.application
    verifies, exact, perceptual, popups = [], [], [], []
    entered, release = threading.Event(), threading.Event()
    transfer_release = threading.Event()
    verify = app._integrity_verifier.verify
    analyze_exact = app.import_controller._duplicate_analyzer.analyze
    analyze_perceptual = app._perceptual_analyzer.analyze
    execute_purge = app.trash_executor.execute_reviewed
    execute_deduplicate = app.trash_executor.execute_duplicates

    def check(entries, tool, **options):
        verifies.append(entries)
        entered.set()
        assert release.wait(10)
        rows = verify(entries, tool, **options)
        if scenario == "stale":
            stale = replace(rows[0].entry, source_path=str(tmp_path / "ignored"))
            return (replace(rows[0], entry=stale), *rows[1:])
        return rows

    def capture_exact(snapshot, **options):
        exact.append(snapshot.entries)
        return analyze_exact(snapshot, **options)

    def capture_perceptual(snapshot, *args, **options):
        perceptual.append(snapshot.entries)
        return analyze_perceptual(snapshot, *args, **options)

    monkeypatch.setattr(app._integrity_verifier, "verify", check)
    monkeypatch.setattr(app.import_controller._duplicate_analyzer, "analyze", capture_exact)
    monkeypatch.setattr(app._perceptual_analyzer, "analyze", capture_perceptual)

    def hold_transfer(execute):
        def transfer(scope):
            assert transfer_release.wait(5)
            return execute(scope)

        return transfer

    monkeypatch.setattr(app.trash_executor, "execute_reviewed", hold_transfer(execute_purge))
    monkeypatch.setattr(
        app.trash_executor, "execute_duplicates", hold_transfer(execute_deduplicate)
    )
    for popup in ("askyesno", "showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(app_module.messagebox, popup, lambda *args, **_: popups.append(args))

    try:
        root.update()
        app.rename_format.set("<IncNr>")
        files = (good, duplicate, bad, unsupported, *((second_bad,) if partial else ()))
        monkeypatch.setattr(app_module.filedialog, "askopenfilenames", lambda **_: tuple(
            map(str, files)
        ))
        app.add_files()
        if scenario != "disabled":
            _wait_for(root, entered.is_set)
            assert not exact and app.process_button.cget("state") == "disabled"
            if scenario == "cancel":
                app.cancel_import()
        release.set()
        _wait_for(root, lambda: not app.import_controller.busy)
        snapshot = app.collection.snapshot()
        if scenario in {"cancel", "stale"}:
            assert not snapshot.entries and not snapshot.excluded_corruption
            assert app._files_excluded_dialog is None and not exact
            assert bad.read_bytes() == original
            assert scenario != "stale" or "failed" in app.status.cget("text").lower()
            return
        assert len(snapshot.excluded_corruption) == (0 if scenario == "disabled" else 1 + partial)
        assert len(snapshot.excluded_duplicates) == (0 if scenario == "independent" else 1)
        if scenario == "disabled":
            assert not verifies and bad.name in {entry.original_filename for entry in exact[0]}
        elif exact:
            assert bad.name not in {entry.original_filename for entry in exact[0]}
        assert not perceptual and app.process_button.cget("state") == "disabled"
        assert bad.read_bytes() == original
        if scenario in {"changed", "purge_partial"}:
            incoming = tmp_path / "replacement.png"
            incoming.write_bytes(original)
            incoming.replace(bad)
        actions = {
            "purge": ("Purge",), "independent": ("Purge",), "changed": ("Purge",),
            "unavailable": ("Purge",),
            "purge_partial": ("Purge",), "deduplicate": ("Deduplicate",),
            "purge_then_deduplicate": ("Purge", "Deduplicate"),
            "deduplicate_then_purge": ("Deduplicate", "Purge"),
        }.get(scenario, ())
        dialog = app._files_excluded_dialog
        assert dialog is not None
        labels = [widget for widget in _descendants(dialog) if isinstance(widget, ctk.CTkLabel)]
        buttons = {
            widget.cget("text"): widget
            for widget in _descendants(dialog) if isinstance(widget, ctk.CTkButton)
        }
        assert ("Purge" in buttons) == bool(snapshot.excluded_corruption)
        assert ("Deduplicate" in buttons) == bool(snapshot.excluded_duplicates)
        close_protocol = dialog.protocol("WM_DELETE_WINDOW")
        dialog.tk.call(close_protocol)
        assert dialog.winfo_exists()
        for action in actions:
            transfer_release.clear()
            buttons[action].invoke()
            assert app._files_excluded_dialog is dialog
            assert app._processing_phase in {"purging", "deduplicating"}
            assert all(button.cget("state") == "disabled" for button in buttons.values())
            buttons["OK"].invoke()
            dialog.tk.call(close_protocol)
            app._dismiss_files_excluded()
            app.close()
            root.update()
            assert dialog.winfo_exists() and not app._closing
            assert app.process_button.cget("state") == "disabled"
            assert app.cancel_button.cget("state") == "disabled"
            transfer_release.set()
            _wait_for(root, lambda: app._processing_phase == "idle")
            current = app.collection.snapshot()
            assert current.entries == snapshot.entries
            assert app._files_excluded_dialog is dialog and buttons["OK"].cget("state") == "normal"
            summary = labels[0].cget("text")
            assert f"Purge has {len(current.excluded_corruption)} file(s) pending" in summary
            assert f"Deduplicate has {len(current.excluded_duplicates)} file(s) pending" in summary
            for label, pending in (
                ("Purge", current.excluded_corruption),
                ("Deduplicate", current.excluded_duplicates),
            ):
                if label in buttons:
                    assert buttons[label].cget("state") == ("normal" if pending else "disabled")
            message = labels[1].cget("text")
            if partial:
                assert "Purged 1" in message and "1 remain excluded" in message
                assert "1 issue(s)" in message and "identity changed" in message
            elif scenario in {"changed", "unavailable"}:
                assert "Purged 0" in message and "1 remain excluded" in message
            else:
                assert ("Purged 1" if action == "Purge" else "Moved 1") in message
                assert "0 remain excluded" in message
            assert not popups and not perceptual and not app._process_ready()
            assert app.process_button.cget("state") == "disabled"
        buttons["OK"].invoke()
        assert not dialog.winfo_exists()
        if scenario not in {"disabled", "independent"}:
            assert app._processing_phase == "perceptual"
            assert app.process_button.cget("state") == "disabled"
        _wait_for(root, app._process_ready)
        snapshot = app.collection.snapshot()
        assert tuple(str(entry.entry_id) for entry in snapshot.entries) == app.tree.get_children("")
        assert all(
            app.tree.set(str(entry.entry_id), "new_filename").startswith(f"{index:02}")
            for index, entry in enumerate(snapshot.entries, 1)
        )
        assert not popups and good.exists() and unsupported.exists()
        if scenario not in {"disabled", "independent"}:
            assert len(perceptual) == 1
            assert {row.original_filename for row in perceptual[0]} == {good.name, unsupported.name}
        if scenario in {"purge", "independent"} or len(actions) == 2:
            assert not bad.exists() and duplicate.exists() == (len(actions) == 1)
            assert not snapshot.excluded_corruption
            assert bool(snapshot.excluded_duplicates) == (scenario == "purge")
            assert [path.read_bytes() for path in (app_dir / "trash").glob("*/*.png")] == [original]
        elif scenario in {"changed", "unavailable", "purge_partial"}:
            assert bad.read_bytes() == original and duplicate.exists()
            assert snapshot.excluded_corruption and snapshot.excluded_duplicates
            status = app.status.cget("text")
            assert f"Purged {int(partial)}" in status and "1 remain excluded" in status
            assert ("unavailable" if scenario == "unavailable" else "identity changed") in status
            if partial:
                assert not second_bad.exists()
        else:
            assert bad.exists()
            assert duplicate.exists() == (scenario != "deduplicate")
            if scenario != "disabled":
                assert snapshot.excluded_corruption
                assert app.controls["purge"].cget("state") == "normal"
                transfer_release.set()
                app.controls["purge"].invoke()
                _wait_for(root, lambda: app._processing_phase == "idle")
                assert not bad.exists() and not app.collection.snapshot().excluded_corruption
                assert not popups
    finally:
        release.set()
        transfer_release.set()
        _close_app(app, root)
        storage.managed_trash_path.cache_clear()


@pytest.mark.parametrize("enabled", [False, True])
def test_post_verification_before_auto_clean(tmp_path, monkeypatch, enabled):
    if _run_gui_test_in_isolated_process(f"test_post_verification_before_auto_clean[{enabled}]"):
        return
    from PIL import Image

    from tidyfiles.transaction_journal import JournalStore

    folder, local = tmp_path / "import", tmp_path / "local"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    local.mkdir()
    journal = JournalStore(app_dir=tmp_path / "app", local_app_data=local)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journal)
    good, bad = nested / "good.txt", nested / "broken.png"
    good.write_bytes(b"ordinary text")
    with Image.new("RGB", (16, 16)) as image:
        image.save(bad)
    broken = bad.read_bytes()[:25]
    bad.write_bytes(broken)
    clutter = nested / "Thumbs.db"
    clutter.touch()
    config_path = tmp_path / "config.json"
    root = create_main_window(default_config(), config_path)
    app = root.application
    entered, release = threading.Event(), threading.Event()
    calls, summaries, statuses = [], [], []
    verify = app._integrity_verifier.verify

    def check(entries, tool, **options):
        calls.extend(entries)
        entered.set()
        assert release.wait(10)
        return verify(entries, tool, **options)

    def display(_title, message, **_options):
        summaries.append(message)
        statuses.extend(app.tree.set(item, "status") for item in app.tree.get_children(""))

    monkeypatch.setattr(app._integrity_verifier, "verify", check)
    monkeypatch.setattr(app_module.messagebox, "showinfo", display)
    monkeypatch.setattr(app_module.messagebox, "showwarning", display)
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *_args, **_options: True)
    try:
        root.update()
        app.open_settings()
        settings = app.settings_window
        assert settings is not None
        switches = {
            widget.cget("text"): widget for widget in _descendants(settings)
            if isinstance(widget, ctk.CTkSwitch)
        }
        assert not switches["Pre-verify"].get() and not switches["Post-verify"].get()
        if enabled:
            switches["Post-verify"].toggle()
            assert load_config(config_path)["global_settings"]["post_verify"] is True
        settings.destroy()
        monkeypatch.setattr(app_module.filedialog, "askdirectory", lambda **_: str(folder))
        app.add_folder()
        _wait_for(root, lambda: not app.import_controller.busy)
        app.auto_clean_switch.select()
        app._on_auto_clean_changed()
        app.rename_format.set("<Name> renamed")
        _wait_for(root, app._process_ready)
        app.process_button.invoke()
        success, damaged = nested / "good renamed.txt", nested / "broken renamed.png"
        if enabled:
            _wait_for(root, entered.is_set)
            _wait_for(root, lambda: app._processing_phase == "post_verify")
            assert {Path(entry.source_path) for entry in calls} == {success, damaged}
            assert success.exists() and damaged.exists() and clutter.exists()
            assert not summaries and app.process_button.cget("state") == "disabled"
        release.set()
        _wait_for(root, lambda: app._processing_phase == "idle" and bool(summaries))
        assert good.exists() is False and bad.exists() is False
        assert not clutter.exists() and damaged.read_bytes() == broken
        assert not app.collection.snapshot().excluded_corruption
        if enabled:
            assert set(statuses) == {"Corrupted", "Verified"}
            assert "1 verified · 1 corrupted" in summaries[0]
            assert "broken renamed.png · Corrupted" in summaries[0]
        else:
            assert not calls and "Post-verify" not in summaries[0]
        assert "Auto-clean removed 1 item(s)" in summaries[0]
    finally:
        release.set()
        _close_app(app, root)


@pytest.mark.parametrize(
    "choice", ["ok", "dialog_deduplicate", "finder_deduplicate", "failed", "finder_partial"]
)
def test_exact_duplicate_workflow(tmp_path, monkeypatch, caplog, choice):
    if _run_gui_test_in_isolated_process(f"test_exact_duplicate_workflow[{choice}]"):
        return
    import tidyfiles.duplicate_analysis as duplicate_module
    import tidyfiles.storage as storage
    from tidyfiles.transaction_journal import JournalStore

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    journals = JournalStore(app_dir=app_dir, local_app_data=local)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(storage, "application_directory", lambda: app_dir)
    monkeypatch.setattr(duplicate_module, "_hash_source", lambda *_: b"forced hash collision")
    storage.managed_trash_path.cache_clear()
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    folder = tmp_path / "import"
    folder.mkdir()
    keeper, different, excluded = (
        folder / "a-keeper.txt", folder / "m-different.txt", tmp_path / "z-copy.txt"
    )
    keeper.write_bytes(b"copy")
    excluded.write_bytes(b"copy")
    different.write_bytes(b"else")
    partial = choice == "finder_partial"
    changed = choice == "failed" or partial
    verified_copy = folder / "z-verified-copy.txt"
    if partial:
        verified_copy.write_bytes(b"copy")
    release = threading.Event()
    execute_duplicates = app.trash_executor.execute_duplicates
    popups = []

    def transfer(snapshot):
        assert release.wait(5), "Test did not release the authorized Trash transfer"
        return execute_duplicates(snapshot)

    monkeypatch.setattr(app.trash_executor, "execute_duplicates", transfer)

    def invoke(dialog, label):
        next(
            widget for widget in _descendants(dialog)
            if isinstance(widget, ctk.CTkButton) and widget.cget("text") == label
        ).invoke()

    def finish_deduplicate(dialog=None):
        assert app._files_excluded_dialog is dialog
        assert app._processing_phase == "deduplicating"
        assert app.progress.cget("mode") == "indeterminate"
        assert "Validating and moving exact duplicates" in app.status.cget("text")
        assert app.controls["add_files"].cget("state") == "disabled"
        assert app.process_button.cget("state") == "disabled"
        assert app.cancel_button.cget("state") == "disabled"
        if choice == "failed":
            app._invalidate_filename_preview(schedule=True)
        app.close()
        assert not app._closing
        release.set()
        _wait_for(root, lambda: app._processing_phase == "idle")
        assert app.progress.cget("mode") == "determinate"
        assert app._files_excluded_dialog is dialog
        assert not popups

    try:
        root.update()
        _import_files(app, root, monkeypatch, excluded)
        assert app._files_excluded_dialog is None
        app.rename_format.set("<IncNr>")
        monkeypatch.setattr(app_module.filedialog, "askdirectory", lambda **_: str(folder))
        app.add_folder()
        _wait_for(root, lambda: not app.import_controller.busy)
        snapshot = app.collection.snapshot()
        assert [row.source_path for row in snapshot.entries] == [str(keeper), str(different)]
        excluded_count = 2 if partial else 1
        assert len(snapshot.excluded_duplicates) == excluded_count
        duplicate = next(
            row for row in snapshot.excluded_duplicates if row.entry.source_path == str(excluded)
        )
        assert duplicate.keeper == snapshot.entries[0]
        assert app.tree.get_children("") == tuple(str(row.entry_id) for row in snapshot.entries)
        assert "Files: 2" in app.summary.cget("text")
        assert "Total size: 8 B" in app.summary.cget("text")
        dialog = app._files_excluded_dialog
        assert dialog is not None and dialog.title() == "Files Excluded"
        summary = next(
            widget.cget("text") for widget in _descendants(dialog)
            if isinstance(widget, ctk.CTkLabel)
        )
        assert f"{excluded_count} file(s) excluded as exact duplicates" in summary
        assert "2 file(s) remain for processing" in summary
        assert not app._process_ready()
        assert excluded.read_bytes() == b"copy"
        for popup in ("askyesno", "showinfo", "showwarning", "showerror"):
            monkeypatch.setattr(
                app_module.messagebox, popup, lambda *args, **kwargs: popups.append(args)
            )

        if changed:
            excluded.write_bytes(b"changed")
        if choice in {"dialog_deduplicate", "failed"}:
            invoke(dialog, "Deduplicate")
            finish_deduplicate(dialog)
            assert dialog.winfo_exists() and not app._process_ready()
        invoke(dialog, "OK")
        assert app._files_excluded_dialog is None
        _wait_for(root, app._process_ready)
        if choice == "dialog_deduplicate":
            assert "Moved 1 duplicate(s) to Trash. 0 remain excluded." in app.status.cget("text")
        elif choice == "failed":
            assert "Moved 0 duplicate(s) to Trash. 1 remain excluded." in app.status.cget("text")
            assert "changed since discovery" in app.status.cget("text")
        assert [
            app.tree.set(str(entry.entry_id), "new_filename") for entry in snapshot.entries
        ] == ["01.txt", "02.txt"]

        app.tabs.set("Duplicate Finder")
        app._on_mode_change()
        assert "group" not in app.tree.cget("displaycolumns")
        if choice != "dialog_deduplicate":
            entry_id = str(duplicate.entry.entry_id)
            assert len(app.tree.get_children("")) == (2 if partial else 1)
            assert app.tree.set(entry_id, "duplicate_of") == keeper.name
            status = "Trash failed" if choice == "failed" else "Duplicate"
            assert app.tree.set(entry_id, "status") == status
        if choice == "finder_deduplicate" or partial:
            app.process_button.invoke()
            finish_deduplicate()
            expected_remaining = 1 if partial else 0
            assert f"Moved 1 duplicate(s) to Trash. {expected_remaining} remain excluded." in (
                app.status.cget("text")
            )
        if partial:
            assert not verified_copy.exists()
            assert app.tree.get_children("") == (str(duplicate.entry.entry_id),)
            assert app.tree.set(str(duplicate.entry.entry_id), "status") == "Trash failed"
            assert "changed since discovery" in app.status.cget("text")
        if choice.endswith("deduplicate"):
            assert not excluded.exists()
            assert not app.tree.get_children("")
            assert [path.read_bytes() for path in (app_dir / "trash").glob("*/*.txt")] == [b"copy"]
            assert not app.collection.snapshot().excluded_duplicates
        else:
            assert excluded.read_bytes() == (b"changed" if changed else b"copy")
            assert app.collection.snapshot().excluded_duplicates == (duplicate,)
        if changed:
            assert "changed since discovery" in caplog.text
        assert not popups
        assert keeper.read_bytes() == b"copy" and different.read_bytes() == b"else"
        assert app.collection.snapshot().entries == snapshot.entries
    finally:
        release.set()
        _close_app(app, root)
        storage.managed_trash_path.cache_clear()


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


@pytest.mark.parametrize(
    "scenario",
    ["ok", "deduplicate", "direct", "partial", "none", "disabled", "stale", "unique",
     "cumulative", "cancel"],
)
def test_perceptual_import_and_review(tmp_path, monkeypatch, scenario):
    if _run_gui_test_in_isolated_process(f"test_perceptual_import_and_review[{scenario}]"):
        return
    from dataclasses import replace

    from test_perceptual_analysis import _pattern

    import tidyfiles.storage as storage
    from tidyfiles.transaction_journal import JournalStore

    app_path, local = tmp_path / "app", tmp_path / "local"
    app_path.mkdir()
    local.mkdir()
    journals = JournalStore(app_dir=app_path, local_app_data=local)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(storage, "application_directory", lambda: app_path)
    storage.managed_trash_path.cache_clear()
    config = default_config()
    config["global_settings"].update(
        perceptual_hashing=scenario != "none", duplicate_checker=scenario != "disabled"
    )
    root = create_main_window(config)
    app = root.application
    a, b, keep, exact = (tmp_path / name for name in ("a.png", "b.png", "keep.txt", "z.png"))
    keep.write_bytes(b"other file")
    with _pattern() as image:
        image.save(a, compress_level=0)
        image.save(b, compress_level=9)
    if scenario in {"ok", "deduplicate", "cancel"}:
        exact.write_bytes(a.read_bytes())
    paths = (a, b, keep, exact) if exact.exists() else (a, b, keep)
    calls = []
    popups = []
    metadata_entered = threading.Event()
    metadata_release = threading.Event()
    metadata_cancelled = threading.Event()
    metadata_resumed = threading.Event()
    release = threading.Event()
    entered = threading.Event()
    analyze = app._perceptual_analyzer.analyze
    read = app._metadata_probe.read
    execute_duplicates = app.trash_executor.execute_duplicates

    def capture(snapshot, *args, **kwargs):
        calls.append(snapshot)
        if scenario == "cumulative" and len(calls) == 1:
            return analyze(snapshot, *args, **kwargs)
        entered.set()
        assert release.wait(10)
        result = analyze(snapshot, *args, **kwargs)
        return replace(result, revision=result.revision - 1) if scenario == "stale" else result

    def delay_metadata(entries, probe, *, cancelled, publish):
        if not metadata_entered.is_set():
            metadata_entered.set()
            while not cancelled() and not metadata_release.wait(0.01):
                pass
            if cancelled():
                metadata_cancelled.set()
                return
        elif scenario in {"deduplicate", "cancel"}:
            assert all(entry.original_filename != exact.name for entry in entries)
            metadata_resumed.set()
        return read(entries, probe, cancelled=cancelled, publish=publish)

    def transfer(snapshot):
        assert metadata_cancelled.is_set()
        assert app._metadata_done is not None and app._metadata_done.is_set()
        return execute_duplicates(snapshot)

    def invoke(dialog, label):
        next(
            widget for widget in _descendants(dialog)
            if isinstance(widget, ctk.CTkButton) and widget.cget("text") == label
        ).invoke()

    monkeypatch.setattr(app._perceptual_analyzer, "analyze", capture)
    monkeypatch.setattr(app._metadata_probe, "read", delay_metadata)
    if scenario in {"deduplicate", "cancel"}:
        monkeypatch.setattr(app.trash_executor, "execute_duplicates", transfer)
    for popup in ("askyesno", "showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(app_module.messagebox, popup, lambda *args, **_: popups.append(args))
    try:
        root.update()
        app.rename_format.set("<Name> renamed")
        if scenario in {"unique", "cumulative"}:
            _import_files(app, root, monkeypatch, a, keep)
        if scenario == "cumulative":
            _wait_for(root, app._process_ready)
            assert app._probable_dialog is None
            assert len(calls) == 1
            _import_files(app, root, monkeypatch, b)
        elif scenario != "unique":
            _import_files(app, root, monkeypatch, *paths)
        if scenario in {"none", "disabled"}:
            _wait_for(root, metadata_entered.is_set)
            _wait_for(root, app._process_ready)
            assert not calls
            assert app._probable_dialog is None
            metadata_release.set()
            row = next(
                item for item in app.collection.snapshot().entries if str(a) == item.source_path
            )
            _wait_for(root, lambda: app.tree.set(str(row.entry_id), "resolution") == "96x96")
            assert not popups
            return

        if exact.exists():
            dialog = app._files_excluded_dialog
            assert dialog is not None and not entered.is_set()
            assert app.process_button.cget("state") == "disabled"
            action = "Deduplicate" if scenario in {"deduplicate", "cancel"} else "OK"
            if action == "Deduplicate":
                _wait_for(root, metadata_entered.is_set)
                invoke(dialog, action)
                _wait_for(root, lambda: app._processing_phase == "idle")
                assert app._files_excluded_dialog is dialog and not entered.is_set()
                assert metadata_cancelled.is_set() and not exact.exists()
                assert not app._process_ready()
                _wait_for(root, metadata_resumed.is_set)
                assert not entered.is_set()
            invoke(dialog, "OK")
        _wait_for(root, entered.is_set)
        assert app.process_button.cget("state") == "disabled"
        assert all(entry.original_filename != exact.name for entry in calls[-1].entries)
        assert exact.exists() == (scenario == "ok")
        if scenario == "cancel":
            app.cancel_current_work()
        release.set()
        metadata_release.set()
        if scenario in {"stale", "unique", "cancel"}:
            _wait_for(root, lambda: app._processing_phase == "idle")
            assert app._probable_dialog is None
            notice = {"stale": "discarded", "unique": "complete", "cancel": "canceled"}
            assert notice[scenario] in app.status.cget("text")
            if scenario == "cancel":
                assert exact.name not in {
                    entry.original_filename for entry in app.collection.snapshot().entries
                }
                assert exact.name in {
                    item.name for item in (app_path / "trash").glob("*/*.png")
                }
        else:
            _wait_for(root, lambda: app._probable_dialog is not None)
            dialog = app._probable_dialog
            assert dialog is not None and dialog.title() == "Probable Duplicates Found"
            assert app.process_button.cget("state") == "disabled"
            assert not dialog.selected_ids()
            by_name = {entry.original_filename: entry.entry_id for entry in calls[-1].entries}
            ids = (by_name[a.name], by_name[b.name])
            invoke(dialog, "Auto-select")
            assert dialog.selected_ids() == ids[1:]
            invoke(dialog, "Select None")
            assert not dialog.selected_ids()
            invoke(dialog, "Select All")
            assert dialog.selected_ids() == ids
            assert sum(
                isinstance(widget, ctk.CTkLabel) and isinstance(widget.cget("image"), ctk.CTkImage)
                for widget in _descendants(dialog)
            ) == 2
            if scenario in {"partial", "direct"}:
                if scenario == "partial":
                    b.write_bytes(b"changed after review")
                invoke(dialog, "Delete Selected")
                assert app._probable_dialog is None
                assert app._processing_phase == "probable_trash"
                assert not app._process_ready()
                _wait_for(root, lambda: app._processing_phase == "idle")
                assert not a.exists()
                if scenario == "partial":
                    assert b.read_bytes() == b"changed after review"
                    assert "moved 1 to Trash" in app.status.cget("text")
                    assert "changed" in app.status.cget("text")
                else:
                    assert not b.exists()
                    assert "moved 2 to Trash" in app.status.cget("text")
                assert {entry.original_filename for entry in app.collection.snapshot().entries} == {
                    keep.name
                }
                if scenario == "partial":
                    _import_files(app, root, monkeypatch, b)
                    _wait_for(root, lambda: app._processing_phase == "idle")
                    assert {entry.original_filename
                            for entry in app.collection.snapshot().entries} == {keep.name}
            else:
                invoke(dialog, "Skip")
                assert a.exists() and b.exists()
        _wait_for(root, app._process_ready)
        assert len(app.tree.get_children("")) == len(app.collection.snapshot().entries)
        assert keep.exists() and not popups
    finally:
        release.set()
        metadata_release.set()
        _close_app(app, root)
        storage.managed_trash_path.cache_clear()


def test_portable_media_settings_do_not_block_rename_and_refresh_after_import(
    tmp_path, monkeypatch
):
    if _run_gui_test_in_isolated_process(
        "test_portable_media_settings_do_not_block_rename_and_refresh_after_import"
    ):
        return
    import tidyfiles.media_tools as media
    from tidyfiles.config import get_duplicate_preferences
    from tidyfiles.transaction_journal import JournalStore

    root_dir, source, local = (tmp_path / name for name in ("app", "source", "local"))
    for directory in (root_dir, source, local):
        directory.mkdir()
    for executable in ("ffmpeg.exe", "ffprobe.exe"):
        (source / executable).write_bytes(b"9.1 fixture")
    (source / "ffplay.exe").write_bytes(b"keep at source")
    journals = JournalStore(app_dir=root_dir, local_app_data=local)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(media, "application_directory", lambda: root_dir)
    release = threading.Event()
    started = threading.Event()
    approvals = []

    def startup_check(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return media.check_media_tools(*args, **kwargs)

    def version(path, args, **kwargs):
        assert args == ("-version",)
        suffix = path.read_bytes().decode().split()[0]
        return f"{path.stem} version {suffix}\n".encode()

    def decline(title, *_args, **_kwargs):
        approvals.append(title)
        return False

    monkeypatch.setattr(app_module, "check_media_tools", startup_check)
    monkeypatch.setattr(media, "run_media_command", version)
    config_path = tmp_path / "config.json"
    root = create_main_window(default_config(), config_path)
    app = root.application
    fixture = tmp_path / "ordinary.txt"
    fixture.write_text("ordinary", encoding="utf-8")
    try:
        root.update()
        _wait_for(root, started.is_set)
        _import_files(app, root, monkeypatch, fixture)
        app.rename_format.set("<Name> renamed")
        _wait_for(root, app._process_ready)
        assert app.media_tools is None
        assert fixture.exists()
        app.open_settings()
        assert app._media_import_button is not None
        assert app._media_import_button.cget("state") == "disabled"
        release.set()
        _wait_for(root, lambda: app._media_future is None)
        assert app._media_label is not None
        assert "Video perceptual hashing is unavailable" in app._media_label.cget("text")
        assert app._process_ready()

        options = app._duplicate_settings_widgets
        assert options is not None
        duplicate, _, _, perceptual, threshold = options
        assert not perceptual.get() and threshold.cget("state") == "disabled"
        perceptual.toggle()
        assert threshold.cget("state") == "normal"
        threshold.set("90%")
        app._save_duplicate_settings()
        duplicate.toggle()
        assert perceptual.cget("state") == threshold.cget("state") == "disabled"
        saved = get_duplicate_preferences(load_config(config_path))
        assert not saved.enabled and saved.perceptual_enabled and saved.perceptual_threshold == 90

        monkeypatch.setattr(app_module.filedialog, "askdirectory", lambda **_: "")
        app._media_import_button.invoke()
        assert not tuple(root_dir.glob("*.exe"))
        monkeypatch.setattr(app_module.filedialog, "askdirectory", lambda **_: str(source))
        monkeypatch.setattr(
            app_module.messagebox, "askyesno", decline,
        )
        app._media_import_button.invoke()
        _wait_for(root, lambda: app._media_future is None)
        assert app.media_tools is not None and app.media_tools.video_ready
        assert "ffmpeg.exe: 9.1" in app._media_label.cget("text")
        assert "ffprobe.exe: 9.1" in app._media_label.cget("text")
        assert not approvals
        assert sorted(path.name for path in root_dir.glob("*.exe")) == ["ffmpeg.exe", "ffprobe.exe"]
        assert all((source / name).exists() for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"))
        (source / "ffmpeg.exe").write_bytes(b"10.0 fixture")
        app._media_import_button.invoke()
        assert approvals == ["Replace portable media tools?"]
        assert (root_dir / "ffmpeg.exe").read_bytes() == b"9.1 fixture"

        monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *_args, **_kwargs: True)
        app._media_import_button.invoke()
        _wait_for(root, lambda: app._media_future is None)
        assert "ffmpeg.exe: 10.0" in app._media_label.cget("text")
    finally:
        release.set()
        _wait_for(root, lambda: app._media_future is None)
        _close_app(app, root)


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
    config["global_settings"].update(auto_clean=True, duplicate_checker=False)
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
        dialog = app.column_dialog
        assert dialog is not None
        dialog.update_idletasks()
        checkboxes = {
            widget.cget("text"): widget
            for widget in _descendants(dialog)
            if isinstance(widget, ctk.CTkCheckBox)
        }
        assert checkboxes["Original filename"].cget("state") == "disabled"
        assert checkboxes["Original filename"].get() == 1
        assert checkboxes["New filename"].cget("state") == "disabled"
        assert checkboxes["Status"].cget("state") == "disabled"
        assert checkboxes["Extension"].cget("state") == "normal"
        assert checkboxes["Extension"].get() == 0
        dialog.destroy()
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
        assert photo_cells["duplicate_of"] == ""
        assert photo_cells["status"] == "Scanning"
        assert photo_cells["resolution"] == "— (not probed)"
        assert photo_cells["length"] == ""
        assert video_cells["resolution"] == "— (not probed)"
        assert video_cells["length"] == "— (not probed)"
        assert app.tree.exists(photo_id) and app.tree.exists(video_id)
        assert "1 unavailable" in app.summary.cget("text")
        assert "Known size: 2.0 KiB" in app.summary.cget("text")

        app.tabs.set("Duplicate Finder")
        app._on_mode_change()
        assert app.collection is root.working_collection
        assert app.tree is tree
        assert not app.tree.get_children("")
        assert not app.tree.selection()
        assert app.tree.cget("displaycolumns") == (
            "row_number",
            "filename",
            "size",
            "duplicate_of",
            "status",
        )
        assert app.tree.heading("filename", "text") == "Filename"
        assert "Excluded: 0" in app.summary.cget("text")

        app._set_column_visible("extension", True)
        app.tree.column("filename", width=440)
        app.tree.column("extension", width=145)
        app._persist_column_widths()
        app._restore_column_defaults()
        assert app.tree.cget("displaycolumns") == (
            "row_number",
            "filename",
            "size",
            "duplicate_of",
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
        "duplicate_of",
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

        destination = tmp_path / "source reviewed.txt"
        submit = app.preflight_service.submit
        submissions = []

        def record_submission(request, *, progress_callback):
            assert app._plan_review_dialog is None
            assert source.exists() and not destination.exists()
            submissions.append(request)
            return submit(request, progress_callback=progress_callback)

        monkeypatch.setattr(app.preflight_service, "submit", record_submission)
        app.process_button.invoke()
        _wait_for(
            root,
            lambda: app._processing_phase == "idle" and destination.exists(),
        )
        assert len(submissions) == 1
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
        assert app._plan_review_dialog is None
        assert app.collection.snapshot().entries == ()
        assert "1 renamed" in app.status.cget("text")
        assert "Auto-clean removed 2 item(s)" in app.status.cget("text")
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
        destination = tmp_path / "source acknowledged.txt"
        _wait_for(
            root,
            lambda: app._processing_phase == "idle" and destination.exists(),
        )
        assert destination.read_bytes() == b"fixture contents"
        assert app._plan_review_dialog is None
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
            self.inspection = SimpleNamespace(
                eligible_count=1,
                eligible_directory_count=1,
                eligible_size_bytes=9,
                items=(),
                detail=None,
            )

        def inspect(self):
            return self.inspection

        def clear(self, **options):
            self.clear_calls.append(options)
            return SimpleNamespace(
                inspection=self.inspection,
                cleanup=SimpleNamespace(deleted_paths=("fixture-item",), items=(), detail=None),
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
        switch = app.auto_clear_trash_switch
        assert switch is not None and not switch.get()

        app.clear_trash()
        _wait_for(root, lambda: app._processing_phase == "idle")
        assert service.clear_calls == []

        app.clear_trash()
        _wait_for(root, lambda: app._processing_phase == "idle" and service.clear_calls)
        assert len(service.clear_calls) == 1
        assert service.clear_calls[0]["confirmed"] is service.inspection
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
                inspection=SimpleNamespace(items=()),
                cleanup=SimpleNamespace(
                    deleted_paths=(), failures=(), items=(), detail=None
                ),
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


@pytest.mark.parametrize("case", ("absent", "empty", "populated", "failed", "leftover", "disabled"))
def test_auto_clear_trash_exit_silently_handles_empty_and_cleared_trash(
    tmp_path, monkeypatch, case
) -> None:
    if _run_gui_test_in_isolated_process(
        f"test_auto_clear_trash_exit_silently_handles_empty_and_cleared_trash[{case}]"
    ):
        return
    if os.name != "nt":
        pytest.skip("Managed Trash filesystem checks require Windows")

    import tidyfiles.cleanup as cleanup_module
    from tidyfiles.transaction_journal import JournalStore

    warnings = []
    confirmations = []
    monkeypatch.setattr(
        app_module.messagebox,
        "showwarning",
        lambda title, message, **_options: warnings.append((title, message)),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda *_args, **_options: confirmations.append(True) or True,
    )
    def fail_remove_file(*_args):
        raise PermissionError("fixture deletion failure")

    local_app_data = tmp_path / "local"
    local_app_data.mkdir()
    trash_path = tmp_path / "managed-trash"
    if case != "absent":
        trash_path.mkdir()
    batch = trash_path / f"20261126T000000Z-{'0' * 32}"
    if case in {"populated", "failed", "disabled"}:
        batch.mkdir()
        (batch / "photo.txt").write_text("fixture contents", encoding="utf-8")
    elif case == "leftover":
        (trash_path / "unrecognized.txt").write_text("preserve", encoding="utf-8")

    monkeypatch.setattr(cleanup_module, "managed_trash_path", lambda: trash_path)
    if case == "failed":
        monkeypatch.setattr(cleanup_module, "remove_file", fail_remove_file)
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=local_app_data)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        if case != "disabled":
            app.open_settings()
            root.update()
            dialog = app.settings_window
            assert dialog is not None
            switch = app.auto_clear_trash_switch
            assert switch is not None
            switch.toggle()
            assert app.config["global_settings"]["auto_clear_trash_on_exit"]
            dialog.destroy()
            root.update()
            assert app.settings_window is None
            assert app.appearance_choice is None
            assert app.auto_clear_trash_switch is None

        app.close()
        _wait_for(root, lambda: app._closing)
    finally:
        _close_app(app, root)

    assert len(confirmations) == (0 if case == "disabled" else 1)
    if case == "absent":
        assert not trash_path.exists()
    elif case in {"empty", "populated"}:
        assert list(trash_path.iterdir()) == []
    elif case in {"failed", "disabled"}:
        assert (batch / "photo.txt").read_text(encoding="utf-8") == "fixture contents"
    else:
        assert (trash_path / "unrecognized.txt").read_text(encoding="utf-8") == "preserve"
    if case in {"failed", "leftover"}:
        [(title, message)] = warnings
        assert title == "Auto-Clear Trash results"
        assert ("fixture deletion failure" if case == "failed" else "unrecognized.txt") in message
    else:
        assert warnings == []


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
        dialog = app._recovery_dialog
        assert dialog is not None
        box = next(
            widget
            for widget in _descendants(dialog)
            if isinstance(widget, ctk.CTkTextbox)
        )
        details = box.get("1.0", "end")
        assert "before_rename" in details
        assert str(source) in details
        paths = app._recovery_path_list
        assert paths is not None
        paths.selection_set(0)
        app._open_selected_recovery_location()
        assert opened == [["explorer.exe", "/select,", str(journal_path)]]
    finally:
        _close_app(app, root)


def test_recovery_record_clearing_confirms_selection_and_refreshes_warnings(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_recovery_record_clearing_confirms_selection_and_refreshes_warnings"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore, RecoveryClearResult

    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    transaction_ids = (uuid4(), uuid4())
    transactions = tuple(
        SimpleNamespace(
            transaction_id=identifier,
            operation=SimpleNamespace(value="rename"),
            journal_status=SimpleNamespace(value="unresolved"),
            journal_locations=(),
            entries=(),
        )
        for identifier in transaction_ids
    )
    first = SimpleNamespace(transactions=transactions, storage_issues=())
    empty = SimpleNamespace(transactions=(), storage_issues=())

    class Inspector:
        def __init__(self, *_args):
            self.calls = 0

        def inspect(self):
            self.calls += 1
            return first if self.calls == 1 else empty

    inspector = Inspector()
    clears = []
    confirmations = []
    warnings = []
    infos = []

    def clear_records(selected, *, manually_authorized):
        clears.append((selected, manually_authorized))
        return RecoveryClearResult(selected, ())

    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module, "RecoveryInspector", lambda *_args: inspector)
    monkeypatch.setattr(journals, "clear_recovery_records", clear_records)
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda title, message, **_options: confirmations.append((title, message)) or True,
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showwarning",
        lambda title, message, **_options: warnings.append((title, message)),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showinfo",
        lambda title, message, **_options: infos.append((title, message)),
    )
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        _wait_for(root, lambda: app._recovery_dialog is not None)
        transactions_list = app._recovery_transaction_list
        clear_button = app._recovery_clear_button
        assert transactions_list is not None and clear_button is not None
        transactions_list.selection_set(0, 1)
        transactions_list.event_generate("<<ListboxSelect>>")
        root.update()
        assert clear_button.cget("state") == "normal"

        app._set_processing_phase("rename_preparing", "Preparing")
        assert clear_button.cget("state") == "disabled"
        app._clear_selected_recovery_records()
        assert clears == [] and confirmations == []
        assert "while processing is active" in warnings[-1][1]

        app._set_processing_phase("idle", "Ready")
        assert clear_button.cget("state") == "normal"
        app._clear_selected_recovery_records()
        _wait_for(root, lambda: inspector.calls >= 2 and app._recovery_after is None)

        assert clears == [(transaction_ids, True)]
        assert confirmations[0][0] == "Clear Recovery Records"
        assert "recovery information only" in confirmations[0][1]
        assert "does not move, restore, or delete" in confirmations[0][1]
        assert infos == [
            (
                "Recovery Records Cleared",
                "Cleared 2 recovery record(s). Affected files were not changed.",
            )
        ]
        assert app._recovery_dialog is None
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
        _wait_for(root, entered_modification.is_set)
        assert app._plan_review_dialog is None

        job = app.coordinator.current
        assert job is not None and job.modifying
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


def test_movement_button_geometry_stays_stable_during_collection_updates(
    tmp_path, monkeypatch
) -> None:
    if _run_gui_test_in_isolated_process(
        "test_movement_button_geometry_stays_stable_during_collection_updates"
    ):
        return
    from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences
    from tidyfiles.transaction_journal import JournalStore

    root_folder = tmp_path / "scan"
    root_folder.mkdir()
    for index in range(120):
        (root_folder / f"scan-{index:03d}.txt").write_text(str(index))
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application

    def geometry():
        root.update_idletasks()
        return tuple(
            (
                key,
                button.winfo_x(),
                button.winfo_y(),
                button.winfo_width(),
                button.winfo_height(),
            )
            for key, button in app.movement_buttons.items()
        )

    try:
        root.update()
        expected = geometry()
        observations = []
        monkeypatch.setattr(
            app_module.filedialog, "askdirectory", lambda **_options: str(root_folder)
        )
        app.add_folder()
        while app.import_controller.busy:
            root.update()
            observations.append(geometry())
            time.sleep(0.001)
        observations.append(geometry())

        app._start_sort(SortPreferences(SortCriterion.NAME, SortDirection.DESCENDING))
        while app._collection_operation_pending:
            root.update()
            observations.append(geometry())
            time.sleep(0.001)
        observations.append(geometry())

        app.rename_format.set("<Name> preview")
        _wait_for(root, app._process_ready)
        observations.append(geometry())
        assert observations
        assert all(item == expected for item in observations)
    finally:
        _close_app(app, root)


def test_process_button_colors_follow_availability_in_both_themes(tmp_path, monkeypatch):
    if _run_gui_test_in_isolated_process(
        "test_process_button_colors_follow_availability_in_both_themes"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    source = tmp_path / "source.txt"
    source.write_bytes(b"fixture")
    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
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
        for mode in ("Light", "Dark"):
            app._set_appearance_mode(mode)
            root.update()
            assert app.process_button.cget("state") == "disabled"
            assert app.process_button.cget("fg_color") == ("gray82", "#3A3A3A")

        app.rename_format.set("<Name> ready")
        _wait_for(root, app._process_ready)
        active_color = app_module.ctk.ThemeManager.theme["CTkButton"]["fg_color"]
        for mode in ("Light", "Dark"):
            app._set_appearance_mode(mode)
            root.update()
            assert app.process_button.cget("state") == "normal"
            assert app.process_button.cget("fg_color") == active_color
    finally:
        _close_app(app, root)


def test_settings_opens_repeatedly_after_switching_light_and_dark(tmp_path, monkeypatch):
    if _run_gui_test_in_isolated_process(
        "test_settings_opens_repeatedly_after_switching_light_and_dark"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        previous_dialog = None
        for mode, next_mode in (("Light", "Dark"), ("Dark", "Light")):
            app._set_appearance_mode(mode)
            root.update()
            app.settings_button.invoke()
            root.update()
            time.sleep(0.03)
            root.update()
            dialog = app.settings_window
            assert dialog is not None and dialog.state() == "normal"
            assert dialog is not previous_dialog
            assert app.appearance_mode == mode
            app._set_appearance_mode(next_mode)
            time.sleep(0.03)
            root.update()
            assert dialog.state() == "normal"
            # Simulate the hidden-but-existing window state after a theme change.
            dialog.withdraw()
            assert dialog.state() == "withdrawn"
            root.update()
            app.settings_button.invoke()
            time.sleep(0.03)
            root.update()
            assert app.settings_window is dialog
            assert dialog.state() == "normal"
            assert app.appearance_mode == next_mode
            dialog.destroy()
            root.update()
            assert app.settings_window is None
            assert app.appearance_choice is None
            assert app.auto_clear_trash_switch is None
            previous_dialog = dialog
    finally:
        _close_app(app, root)


def test_empty_managed_trash_states_show_message_without_clearing(tmp_path, monkeypatch):
    if _run_gui_test_in_isolated_process(
        "test_empty_managed_trash_states_show_message_without_clearing"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    class TrashService:
        def __init__(self):
            self.clear_calls = 0
            self.summaries = iter(
                (
                    SimpleNamespace(
                        eligible_count=0,
                        eligible_directory_count=0,
                        eligible_size_bytes=0,
                        items=(),
                        detail="Managed Trash does not exist",
                    ),
                    SimpleNamespace(
                        eligible_count=0,
                        eligible_directory_count=0,
                        eligible_size_bytes=0,
                        items=(),
                        detail=None,
                    ),
                )
            )

        def inspect(self):
            return next(self.summaries)

        def clear(self, **_options):
            self.clear_calls += 1
            raise AssertionError("An empty Trash must not call clear")

    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    service = TrashService()
    messages = []
    warnings = []
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    monkeypatch.setattr(app_module, "TrashClearService", lambda *_args: service)
    monkeypatch.setattr(
        app_module.messagebox,
        "showinfo",
        lambda title, message, **_options: messages.append((title, message)),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showwarning",
        lambda title, message, **_options: warnings.append((title, message)),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda *_args, **_options: pytest.fail(
            "Empty Trash must not ask for deletion confirmation"
        ),
    )
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        root.update()
        app.clear_trash()
        _wait_for(root, lambda: app._processing_phase == "idle" and messages)
        app.clear_trash()
        _wait_for(root, lambda: app._processing_phase == "idle" and len(messages) == 2)
        assert messages == [
            ("Clear Trash", "Trash is empty."),
            ("Clear Trash", "Trash is empty."),
        ]
        assert warnings == []
        assert service.clear_calls == 0
    finally:
        _close_app(app, root)


def test_manual_clear_trash_confirms_and_removes_empty_recognized_batches(tmp_path, monkeypatch):
    if _run_gui_test_in_isolated_process(
        "test_manual_clear_trash_confirms_and_removes_empty_recognized_batches"
    ):
        return
    import tidyfiles.cleanup as cleanup_module
    from tidyfiles.transaction_journal import JournalStore

    trash = tmp_path / "trash"
    batch = trash / "20261126_010203"
    batch.mkdir(parents=True)
    unknown = trash / "unknown"
    unknown.mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(cleanup_module, "managed_trash_path", lambda: trash)
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    confirmations = []
    results = []
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda _title, message, **_options: confirmations.append(message) or True,
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showwarning",
        lambda _title, message, **_options: results.append(message),
    )
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        app.clear_trash()
        _wait_for(root, lambda: app._processing_phase == "idle" and results)
        assert len(confirmations) == 1
        assert "0 inspected file(s)" in confirmations[0]
        assert "1 inspected directories if empty" in confirmations[0]
        assert "deleted 1 item(s)" in results[0]
        assert str(unknown) in results[0]
        assert unknown.is_dir() and not batch.exists()
    finally:
        _close_app(app, root)


def test_failed_import_view_rebuild_blocks_rename_until_refresh(tmp_path, monkeypatch):
    if _run_gui_test_in_isolated_process(
        "test_failed_import_view_rebuild_blocks_rename_until_refresh"
    ):
        return
    from tidyfiles.transaction_journal import JournalStore

    prior = tmp_path / "prior.txt"
    discarded = tmp_path / "discarded.txt"
    prior.write_bytes(b"prior")
    discarded.write_bytes(b"discarded")
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    try:
        _import_files(app, root, monkeypatch, prior)
        initial = app.collection.snapshot().entries
        selected = str(initial[0].entry_id)
        app.tree.selection_set(selected)
        app.rename_format.set("<Name> renamed")
        _wait_for(root, app._process_ready)
        controller = app.import_controller
        on_activity = controller.on_activity

        def cancel_at_rendering(phase, session):
            on_activity(phase, session)
            if phase == "rendering":
                assert controller.cancel()

        def fail_removal(_entries):
            raise RuntimeError("persistent removal failure")

        def fail_rebuild():
            raise RuntimeError("table unavailable")

        monkeypatch.setattr(controller, "on_activity", cancel_at_rendering)
        monkeypatch.setattr(controller, "on_remove_chunk", fail_removal)
        with monkeypatch.context() as attempt:
            attempt.setattr(app, "refresh_collection", fail_rebuild)
            _import_files(app, root, monkeypatch, discarded)
        assert app.collection.snapshot().entries == initial
        assert len(app.tree.get_children("")) == 2
        assert "persistent removal failure" in app.status.cget("text")
        assert "table unavailable" in app.status.cget("text")
        assert app.process_button.cget("state") == "disabled"
        assert app.refresh_preview_button.cget("state") == "normal"
        app.start_rename_review()
        assert app._processing_phase == "idle"
        assert prior.read_bytes() == b"prior" and discarded.read_bytes() == b"discarded"

        app.refresh_preview_button.invoke()
        _wait_for(root, app._process_ready)
        assert app.tree.get_children("") == (selected,)
        assert app.tree.selection() == (selected,)
    finally:
        _close_app(app, root)
    assert app._closing


def test_rename_progress_shows_measured_elapsed_and_delays_eta(tmp_path, monkeypatch):
    if _run_gui_test_in_isolated_process(
        "test_rename_progress_shows_measured_elapsed_and_delays_eta"
    ):
        return
    from tidyfiles.rename_executor import RenameProgress
    from tidyfiles.transaction_journal import JournalStore

    (tmp_path / "local").mkdir()
    journals = JournalStore(app_dir=tmp_path / "app", local_app_data=tmp_path / "local")
    monkeypatch.setattr(app_module, "JournalStore", lambda: journals)
    root = create_main_window(default_config(), tmp_path / "config.json")
    app = root.application
    job_id = uuid4()
    monkeypatch.setattr(
        app,
        "coordinator",
        SimpleNamespace(current=SimpleNamespace(job_id=job_id, modifying=True)),
    )
    try:
        root.update()
        app._processing_started_at = None
        app._show_processing_progress(RenameProgress(job_id, "before_rename", 5, 1200))
        assert "Renaming files: 5/1200" in app.status.cget("text")
        assert "Elapsed:" in app.status.cget("text")
        assert "ETA:" not in app.status.cget("text")

        app._processing_started_at = time.monotonic() - 25
        app._show_processing_progress(RenameProgress(job_id, "before_rename", 350, 1200))
        assert "Renaming files: 350/1200" in app.status.cget("text")
        assert "Elapsed: 00:25" in app.status.cget("text")
        assert "ETA: 01:01" in app.status.cget("text")
        assert app.progress.get() == pytest.approx(350 / 1200)
    finally:
        app._set_processing_phase("idle", "Ready")
        _close_app(app, root)


def test_large_rename_failure_summary_aggregates_unprocessed_rows():
    from tidyfiles.rename_executor import (
        RenameEntryResult,
        RenameOutcome,
        RenameResult,
        RenameState,
    )

    app = object.__new__(app_module._MainWindow)
    app._processing_snapshot = WorkingCollection().snapshot()
    app._processing_diagnostics_by_id = {}
    error = "Cannot persist checkpoint: [WinError 5] Access is denied."
    entries = tuple(
        RenameEntryResult(uuid4(), RenameOutcome.RENAMED)
        for _ in range(142)
    ) + (
        RenameEntryResult(uuid4(), RenameOutcome.FAILED_SAFE, detail=error),
        *(
            RenameEntryResult(uuid4(), RenameOutcome.NOT_PROCESSED)
            for _ in range(1057)
        ),
    )
    result = RenameResult(RenameState.RECOVERY_REQUIRED, uuid4(), entries, error)

    summary = app._rename_result_summary(result, None, set())

    assert "142 renamed" in summary
    assert "1 failed safely" in summary
    assert "1057 not processed" in summary
    assert "[WinError 5] Access is denied" in summary
    assert "synthetic-" not in summary
    assert len(summary) < 300
