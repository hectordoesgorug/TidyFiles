"""CustomTkinter application shell and shared collection presentation."""

from __future__ import annotations

import mimetypes
import tkinter as tk
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from uuid import UUID

import customtkinter as ctk

from tidyfiles.collection import (
    CollectionEntry,
    ImportSessionSnapshot,
    ImportSessionState,
    WorkingCollection,
)
from tidyfiles.config import (
    ConfigError,
    default_config,
    get_sorting_preferences,
    save_config,
)
from tidyfiles.discovery import (
    DiscoveryReport,
    DiscoverySession,
    DiscoveryState,
)
from tidyfiles.import_controller import ImportController
from tidyfiles.sorting import SortCriterion, SortDirection
from tidyfiles.ui_preferences import (
    COLUMN_BY_ID,
    COLUMNS,
    MODE_COLUMNS,
    MODES,
    REQUIRED_COLUMNS,
    default_column_preferences,
    get_appearance_mode,
    get_column_preferences,
    restore_window_geometry,
    set_column_preferences,
)

_DEFAULT_MODE = MODES[0]
_FORMAT_PRESETS = (
    "<DirName> <IncNr>",
    "<SubDirName> <IncNr>",
    "<Name> <IncNr>",
)
_SORT_LABELS = {
    SortCriterion.FILE_TYPE_AND_NAME: "File Type + Name",
    SortCriterion.NAME: "Name",
    SortCriterion.EXTENSION: "Extension",
    SortCriterion.SIZE: "Size",
    SortCriterion.DATE_CREATED: "Date Created",
    SortCriterion.DATE_MODIFIED: "Date Modified",
    SortCriterion.PATH: "Path",
}
_SORT_CRITERIA = {label: criterion for criterion, label in _SORT_LABELS.items()}


def _disabled_button(master: Any, **options: Any) -> ctk.CTkButton:
    return ctk.CTkButton(
        master,
        state="disabled",
        fg_color=("gray82", "#3A3A3A"),
        hover_color=("gray82", "#3A3A3A"),
        text_color=("gray45", "gray65"),
        text_color_disabled=("gray45", "gray65"),
        **options,
    )


def _style_file_table(root: ctk.CTk) -> None:
    dark = ctk.get_appearance_mode() == "Dark"
    background = "#242424" if dark else "#FFFFFF"
    header_background = "#333333" if dark else "#E7E7E7"
    foreground = "#F2F2F2" if dark else "#202020"
    error_background = "#512C2C" if dark else "#FCE8E6"
    selection_background = "#1F6AA5"

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        "TidyFiles.Treeview",
        background=background,
        fieldbackground=background,
        foreground=foreground,
        rowheight=28,
        borderwidth=0,
        font=("Segoe UI", 10),
    )
    style.configure(
        "TidyFiles.Treeview.Heading",
        background=header_background,
        foreground=foreground,
        font=("Segoe UI", 10, "bold"),
        padding=(8, 6),
        relief="flat",
    )
    style.map(
        "TidyFiles.Treeview",
        background=[("selected", selection_background)],
        foreground=[("selected", "#FFFFFF")],
    )
    style.map(
        "TidyFiles.Treeview.Heading",
        background=[("active", header_background)],
        foreground=[("active", foreground)],
    )

    # A later evaluator can apply this tag while keeping a textual row status.
    root._tidyfiles_row_colors = (background, error_background)  # type: ignore[attr-defined]


def format_size(size_bytes: int) -> str:
    """Format a nonnegative byte count without losing the exact byte value."""
    value = float(size_bytes)
    if value < 0:
        raise ValueError("size_bytes must not be negative")
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        if value < 1024 or unit == "EiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _format_timestamp(timestamp_ns: int | None) -> str:
    if timestamp_ns is None:
        return "—"
    try:
        return datetime.fromtimestamp(timestamp_ns / 1_000_000_000).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (OverflowError, OSError, ValueError):
        return "—"


def _file_type(filename: str) -> str:
    media_type, _encoding = mimetypes.guess_type(filename, strict=False)
    return media_type or "—"


class _MainWindow:
    """Own the one application collection and its presentation widgets."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.config = deepcopy(default_config() if config is None else config)
        self.settings = self.config["global_settings"]
        self.config_path = Path(config_path) if config_path is not None else None
        self.appearance_mode = get_appearance_mode(self.config)
        ctk.set_appearance_mode(self.appearance_mode)

        self.collection = WorkingCollection(get_sorting_preferences(self.config))
        self.active_mode = _DEFAULT_MODE
        self.column_preferences = {
            mode: get_column_preferences(self.config, mode) for mode in MODES
        }
        self._closing = False
        self._close_pending = False
        self._geometry_after: str | None = None
        self._appearance_after: str | None = None
        self._resize_started = False
        self.settings_window: ctk.CTkToplevel | None = None
        self._entries_by_id: dict[UUID, CollectionEntry] = {}
        self._display_order: list[UUID] = []
        self._known_size_bytes = 0
        self._unknown_size_count = 0
        self._import_issue_sessions: tuple[Any, ...] = ()
        self._progress_running = False
        self._last_import_report: DiscoveryReport | None = None

        self.root = ctk.CTk()
        self.root.application = self  # type: ignore[attr-defined]
        self.root.working_collection = self.collection  # type: ignore[attr-defined]
        self._configure_root()
        self._build_widgets()
        self._apply_mode(_DEFAULT_MODE)
        self.refresh_collection()
        self.import_controller = ImportController(
            self.root,
            self.collection,
            capture_view_state=self._capture_import_view_state,
            on_activity=self._on_import_activity,
            on_items=self._render_discovery_items,
            on_order_chunk=self._apply_order_chunk,
            on_remove_chunk=self._remove_rows_chunk,
            on_rollback_complete=self._complete_view_rollback,
            on_finished=self._on_import_finished,
            on_error=self._on_import_error,
            on_shutdown=self._finish_close,
        )
        self._set_import_buttons(False)
        self._update_import_warnings()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._schedule_geometry_check, add="+")
        self._schedule_system_appearance_check()

    def _configure_root(self) -> None:
        self.root.title("TidyFiles")
        self.root.minsize(760, 520)
        self.root.geometry("1000x680+100+100")
        self.root.update_idletasks()
        bounds = self._virtual_screen_bounds()
        geometry = restore_window_geometry(
            self.settings.get("window_geometry"), bounds
        )
        self.root.geometry(geometry)
        for row, weight in ((3, 1),):
            self.root.grid_rowconfigure(row, weight=weight)
        self.root.grid_columnconfigure(0, weight=1)

    def _virtual_screen_bounds(self) -> tuple[int, int, int, int]:
        return (
            self.root.winfo_vrootx(),
            self.root.winfo_vrooty(),
            self.root.winfo_vrootwidth(),
            self.root.winfo_vrootheight(),
        )

    def _build_widgets(self) -> None:
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(10, 3), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="TidyFiles",
            font=ctk.CTkFont(size=23, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.settings_button = ctk.CTkButton(
            header, text="Settings", width=95, command=self.open_settings
        )
        self.settings_button.grid(row=0, column=1, sticky="e")

        self.tabs = ctk.CTkTabview(
            self.root, anchor="nw", height=132, command=self._on_mode_change
        )
        self.tabs.grid(row=1, column=0, padx=16, pady=(0, 7), sticky="ew")
        self.mode_panels: dict[str, ctk.CTkFrame] = {}
        for mode in MODES:
            panel = self.tabs.add(mode)
            self.mode_panels[mode] = panel
            panel.grid_columnconfigure(0, weight=1)
        self._build_bulk_rename_panel(self.mode_panels["Bulk Rename"])
        self._build_placeholder_panel(
            self.mode_panels["Duplicate Finder"],
            "Duplicate analysis controls will be added with duplicate verification.",
        )
        self._build_placeholder_panel(
            self.mode_panels["Repacker"],
            "Repacking controls will be added with media conversion support.",
        )
        self.tabs.set(_DEFAULT_MODE)

        self._build_collection_toolbar()
        self._build_table()
        self._build_collection_footer()

    def _build_bulk_rename_panel(self, panel: ctk.CTkFrame) -> None:
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)
        formats = ctk.CTkFrame(panel, fg_color="transparent")
        formats.grid(row=0, column=0, padx=6, pady=(2, 1), sticky="ew")
        formats.grid_columnconfigure(0, weight=1)
        self.rename_format = tk.StringVar(master=self.root, value="")
        self.format_field = ctk.CTkComboBox(
            formats,
            variable=self.rename_format,
            values=_FORMAT_PRESETS,
            state="normal",
            height=30,
        )
        self.format_field.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.insert_token_button = _disabled_button(
            formats, text="Insert Token", width=100
        )
        self.insert_token_button.grid(row=0, column=1, padx=3)
        self.profiles_button = _disabled_button(formats, text="Profiles", width=85)
        self.profiles_button.grid(row=0, column=2, padx=3)
        self.format_reference_button = _disabled_button(
            formats, text="Format Reference", width=125
        )
        self.format_reference_button.grid(row=0, column=3, padx=(3, 0))

        options = ctk.CTkFrame(panel, fg_color="transparent")
        options.grid(row=1, column=0, padx=4, pady=(0, 1), sticky="w")
        self.single_folder_switch = ctk.CTkSwitch(
            options, text="Single Folder", onvalue=True, offvalue=False
        )
        self.single_folder_switch.grid(row=0, column=0, padx=(0, 14), sticky="w")
        self.auto_clean_switch = ctk.CTkSwitch(
            options,
            text="Auto-clean",
            onvalue=True,
            offvalue=False,
            state="disabled",
        )
        self.auto_clean_switch.grid(row=0, column=1, padx=(0, 14), sticky="w")
        self.counter_switch = ctk.CTkSwitch(
            options, text="Counter", onvalue=True, offvalue=False
        )
        self.counter_switch.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(
            panel,
            text=(
                "Auto-clean is unavailable until its disclosure and folder checks are implemented. "
                "Future deletion scope: Thumbs.db, .DS_Store, desktop.ini, and empty eligible "
                "subdirectories."
            ),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=10),
            wraplength=980,
        ).grid(row=2, column=0, padx=6, pady=(0, 3), sticky="ew")

    @staticmethod
    def _build_placeholder_panel(panel: ctk.CTkFrame, text: str) -> None:
        ctk.CTkLabel(panel, text=text, anchor="w").grid(
            row=0, column=0, padx=8, pady=10, sticky="ew"
        )

    def _build_collection_toolbar(self) -> None:
        toolbar = ctk.CTkFrame(self.root, fg_color="transparent")
        toolbar.grid(row=2, column=0, padx=16, pady=(0, 7), sticky="ew")
        for column in (0, 1, 2, 3):
            toolbar.grid_columnconfigure(column, weight=0)
        toolbar.grid_columnconfigure(10, weight=1)

        self.controls: dict[str, ctk.CTkButton] = {}
        for column, (key, label, width) in enumerate(
            (
                ("add_files", "Add Files", 76),
                ("add_folder", "Add Folder", 82),
                ("remove", "Remove", 70),
                ("clear_all", "Clear All", 76),
            )
        ):
            command = {
                "add_files": self.add_files,
                "add_folder": self.add_folder,
            }.get(key)
            button = (
                ctk.CTkButton(toolbar, text=label, width=width, command=command)
                if command is not None
                else _disabled_button(toolbar, text=label, width=width)
            )
            button.grid(row=0, column=column, padx=(0, 5), sticky="w")
            self.controls[key] = button

        ctk.CTkLabel(toolbar, text="Sort by:").grid(
            row=0, column=4, padx=(5, 4), sticky="e"
        )
        preferences = self.collection.sort_preferences
        self.sort_choice = tk.StringVar(
            master=self.root, value=_SORT_LABELS[preferences.criterion]
        )
        self.sort_dropdown = ctk.CTkOptionMenu(
            toolbar,
            variable=self.sort_choice,
            values=tuple(_SORT_CRITERIA),
            width=154,
            state="disabled",
            fg_color=("gray82", "#3A3A3A"),
            button_color=("gray82", "#3A3A3A"),
            button_hover_color=("gray82", "#3A3A3A"),
            text_color=("gray45", "gray65"),
        )
        self.sort_dropdown.grid(row=0, column=5, padx=(0, 5), sticky="w")
        direction_label = (
            "Ascending"
            if preferences.direction is SortDirection.ASCENDING
            else "Descending"
        )
        self.sort_direction_button = _disabled_button(
            toolbar, text=direction_label, width=92
        )
        self.sort_direction_button.grid(row=0, column=6, padx=(0, 5), sticky="w")
        self.columns_button = ctk.CTkButton(
            toolbar, text="Columns", width=82, command=self.open_columns
        )
        self.columns_button.grid(row=0, column=7, sticky="w")

    def _build_table(self) -> None:
        frame = ctk.CTkFrame(self.root, corner_radius=8)
        frame.grid(row=3, column=0, padx=16, pady=(0, 7), sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        _style_file_table(self.root)
        self.tree = ttk.Treeview(
            frame,
            columns=tuple(column.identifier for column in COLUMNS),
            show="headings",
            style="TidyFiles.Treeview",
            selectmode="extended",
        )
        for column in COLUMNS:
            self.tree.heading(column.identifier, text=column.label, anchor="w")
            self.tree.column(
                column.identifier,
                width=column.width,
                minwidth=column.minimum,
                stretch=column.identifier in {"filename", "new_filename"},
                anchor="w",
            )
        background, error_background = self.root._tidyfiles_row_colors  # type: ignore[attr-defined]
        self.tree.tag_configure("normal", background=background)
        self.tree.tag_configure("problem", background=error_background)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))

        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=(8, 0))
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind("<<TreeviewSelect>>", self._update_summary, add="+")
        self.tree.bind("<ButtonPress-1>", self._track_column_resize, add="+")
        self.tree.bind("<ButtonRelease-1>", self._save_column_resize, add="+")
        self.tree.bind("<Button-3>", self._show_header_menu, add="+")

    def _build_collection_footer(self) -> None:
        row = ctk.CTkFrame(self.root, fg_color="transparent")
        row.grid(row=4, column=0, padx=16, pady=(0, 5), sticky="ew")
        for column in range(4):
            row.grid_columnconfigure(column, weight=0)
        row.grid_columnconfigure(4, weight=1)
        self.movement_buttons: dict[str, ctk.CTkButton] = {}
        for column, (key, text) in enumerate(
            (
                ("move_top", "Move to Top"),
                ("move_up", "Move Up"),
                ("move_down", "Move Down"),
                ("move_bottom", "Move to Bottom"),
            )
        ):
            button = _disabled_button(row, text=text, width=98)
            button.grid(row=0, column=column, padx=(0, 4), sticky="w")
            self.movement_buttons[key] = button
        self.summary = ctk.CTkLabel(row, text="Files: 0  Selected: 0  Total size: 0 B")
        self.summary.grid(row=0, column=4, padx=(8, 0), sticky="e")

        footer = ctk.CTkFrame(self.root, fg_color="transparent")
        footer.grid(row=5, column=0, padx=16, pady=(0, 10), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(footer, text="Ready · No files loaded", anchor="w")
        self.status.grid(row=0, column=0, sticky="w")
        self.import_warnings_button = ctk.CTkButton(
            footer,
            text="Import issues",
            width=142,
            fg_color=("#B54708", "#7A2E0E"),
            hover_color=("#93370D", "#64240B"),
            command=self.open_import_details,
        )
        self.import_warnings_button.grid(row=0, column=1, padx=(8, 0), sticky="w")
        self.import_warnings_button.grid_remove()
        self.progress = ctk.CTkProgressBar(footer, width=150)
        self.progress.set(0)
        self.progress.grid(row=0, column=2, padx=12, sticky="e")
        self.cancel_button = ctk.CTkButton(
            footer,
            text="Cancel",
            width=82,
            state="disabled",
            command=self.cancel_import,
        )
        self.cancel_button.grid(row=0, column=3, padx=(0, 6), sticky="e")
        self.cancel_button.grid_remove()
        self.process_button = _disabled_button(footer, text="Process", width=105)
        self.process_button.grid(row=0, column=4, sticky="e")

    def _mode_heading(self, identifier: str) -> str:
        if identifier == "filename":
            return "Filename" if self.active_mode == "Duplicate Finder" else "Original filename"
        return COLUMN_BY_ID[identifier].label

    def _apply_mode(self, mode: str) -> None:
        self.active_mode = mode
        self.root.active_mode = mode  # type: ignore[attr-defined]
        preferences = self.column_preferences[mode]
        self.tree.configure(displaycolumns=preferences.visible)
        for identifier in MODE_COLUMNS[mode]:
            column = COLUMN_BY_ID[identifier]
            self.tree.heading(identifier, text=self._mode_heading(identifier))
            self.tree.column(
                identifier,
                width=preferences.widths[identifier],
                minwidth=column.minimum,
                stretch=identifier in {"filename", "new_filename"},
            )
        self.process_button.configure(
            text={
                "Bulk Rename": "Process",
                "Duplicate Finder": "Deduplicate",
                "Repacker": "Repack",
            }[mode]
        )

    def _on_mode_change(self) -> None:
        mode = self.tabs.get()
        if mode in self.mode_panels:
            self._apply_mode(mode)

    def refresh_collection(self) -> None:
        """Render the shared collection snapshot using stable entry IDs as row IDs."""
        if hasattr(self, "import_controller") and self.import_controller.busy:
            raise RuntimeError("Cannot rebuild the table during an active import")
        selected = set(self.tree.selection())
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        snapshot = self.collection.snapshot()
        entries = snapshot.entries
        self._entries_by_id.clear()
        self._display_order.clear()
        self._known_size_bytes = 0
        self._unknown_size_count = 0
        for index, entry in enumerate(entries, start=1):
            item_id = str(entry.entry_id)
            self._entries_by_id[entry.entry_id] = entry
            self._display_order.append(entry.entry_id)
            self._account_size(entry, 1)
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=self._entry_values(index, entry),
                tags=("normal",),
            )
        surviving = [str(entry.entry_id) for entry in entries if str(entry.entry_id) in selected]
        if surviving:
            self.tree.selection_set(surviving)
        self._update_summary()
        self._update_import_warnings(snapshot.import_sessions)

    @staticmethod
    def _entry_values(
        index: int, entry: CollectionEntry, *, status: str | None = None
    ) -> tuple[str, ...]:
        metadata = entry.metadata
        media_type, _encoding = mimetypes.guess_type(entry.original_filename, strict=False)
        applicable_resolution = bool(
            media_type and (media_type.startswith("image/") or media_type.startswith("video/"))
        )
        applicable_length = bool(
            media_type and (media_type.startswith("audio/") or media_type.startswith("video/"))
        )
        values = {
            "row_number": str(index),
            "filename": entry.original_filename,
            "new_filename": "",
            "size": format_size(metadata.size_bytes)
            if metadata is not None and metadata.size_bytes is not None
            else "—",
            "status": status
            if status is not None
            else ("Imported" if entry.is_finalized else "Scanning"),
            "group": "",
            "resolution": "— (not probed)" if applicable_resolution else "",
            "length": "— (not probed)" if applicable_length else "",
            "extension": entry.source_extension,
            "file_type": media_type or "—",
            "date_created": _format_timestamp(
                metadata.created_time_ns if metadata is not None else None
            ),
            "date_modified": _format_timestamp(
                metadata.modified_time_ns if metadata is not None else None
            ),
            "parent_directory": entry.parent_name or "",
            "import_root": entry.import_root_name or "",
            "full_path": entry.source_path,
        }
        return tuple(values[column.identifier] for column in COLUMNS)

    def _update_summary(self, _event: tk.Event[Any] | None = None) -> None:
        selected_count = len(self.tree.selection())
        if self._unknown_size_count:
            size_text = (
                f"Known size: {format_size(self._known_size_bytes)} · "
                f"{self._unknown_size_count} unavailable"
            )
        else:
            size_text = f"Total size: {format_size(self._known_size_bytes)}"
        self.summary.configure(
            text=f"Files: {len(self._entries_by_id)}  Selected: {selected_count}  {size_text}"
        )

    def _account_size(self, entry: CollectionEntry, direction: int) -> None:
        size = entry.metadata.size_bytes if entry.metadata is not None else None
        if size is None:
            self._unknown_size_count += direction
        else:
            self._known_size_bytes += direction * size

    def _capture_import_view_state(self) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
        return tuple(self._display_order), tuple(UUID(item) for item in self.tree.selection())

    def add_files(self) -> None:
        if self._closing or self.import_controller.busy:
            return
        try:
            selected = filedialog.askopenfilenames(
                parent=self.root,
                title="Add files",
                filetypes=(("All Files", "*.*"),),
            )
        except Exception as error:
            self._on_import_error(f"File selection failed: {error}")
            return
        if selected:
            self.import_controller.start_files(selected)

    def add_folder(self) -> None:
        if self._closing or self.import_controller.busy:
            return
        try:
            selected = filedialog.askdirectory(
                parent=self.root,
                title="Add folder recursively",
                mustexist=True,
            )
        except Exception as error:
            self._on_import_error(f"Folder selection failed: {error}")
            return
        if selected:
            self.import_controller.start_folder(selected)

    def cancel_import(self) -> None:
        self.import_controller.cancel()

    def _set_import_buttons(self, busy: bool) -> None:
        state = "disabled" if busy or self._closing or self._close_pending else "normal"
        self.controls["add_files"].configure(state=state)
        self.controls["add_folder"].configure(state=state)
        for key in ("remove", "clear_all"):
            self.controls[key].configure(state="disabled")

    def _on_import_activity(
        self, phase: str, session: DiscoverySession | None
    ) -> None:
        if phase == "idle":
            if self._progress_running:
                self.progress.stop()
                self._progress_running = False
            self.progress.set(0)
            self.cancel_button.grid_remove()
            self._set_import_buttons(False)
            return

        self._set_import_buttons(True)
        self.cancel_button.grid()
        cancellable = (
            self.import_controller.cancellation_applicable
            and phase not in {"cancelling", "restoring", "removing"}
            and not self._close_pending
        )
        self.cancel_button.configure(state="normal" if cancellable else "disabled")
        if not self._progress_running:
            self.progress.start()
            self._progress_running = True

        if self._close_pending:
            self.status.configure(
                text="Waiting for discovery to stop before closing · source files are unchanged"
            )
        elif phase in {"cancelling", "restoring", "removing"}:
            message = {
                "cancelling": "Canceling import...",
                "restoring": "Restoring the previous collection...",
                "removing": "Rolling back the current import...",
            }[phase]
            self.status.configure(text=message)
        elif session is not None and phase == "discovering":
            counters = session.counters
            action = (
                "Scanning selected folder"
                if session.mode.value == "add_folder"
                else "Scanning selected files"
            )
            self.status.configure(
                text=(
                    f"{action} · {counters.candidate_files} discovered · "
                    f"{counters.accepted} added · {counters.already_listed} already listed · "
                    f"{counters.excluded} excluded · {counters.inaccessible} errors"
                )
            )
        elif session is not None and phase == "preparing":
            self.status.configure(
                text=f"Preparing final collection order · {session.counters.accepted} added"
            )
        elif session is not None and phase == "rendering":
            rendered = min(
                self.import_controller._render_offset,
                len(self.import_controller._prepared_order.entry_ids)
                if self.import_controller._prepared_order is not None
                else 0,
            )
            self.status.configure(
                text=f"Applying final order · {rendered} rows updated"
            )

    def _render_discovery_items(self, items: tuple[Any, ...]) -> None:
        for item in items:
            entry = item.entry
            if entry is None or entry.entry_id in self._entries_by_id:
                continue
            item_id = str(entry.entry_id)
            if self.tree.exists(item_id):
                continue
            row_number = len(self._display_order) + 1
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=self._entry_values(row_number, entry, status="Scanning"),
                tags=("normal",),
            )
            self._entries_by_id[entry.entry_id] = entry
            self._display_order.append(entry.entry_id)
            self._account_size(entry, 1)
        self._update_summary()

    def _apply_order_chunk(
        self, order: tuple[UUID, ...], start: int, end: int, finalized: bool
    ) -> None:
        if start == 0:
            self.tree.set_children("", *(str(entry_id) for entry_id in order))
            self._display_order = list(order)
        for position in range(start, end):
            entry_id = order[position]
            entry = self._entries_by_id.get(entry_id)
            if entry is None:
                raise RuntimeError(f"Collection row is missing for entry {entry_id}")
            item_id = str(entry_id)
            self.tree.item(
                item_id,
                values=self._entry_values(
                    position + 1,
                    entry,
                    status="Imported" if finalized else "Scanning",
                ),
            )

    def _remove_rows_chunk(self, entry_ids: tuple[UUID, ...]) -> None:
        for entry_id in entry_ids:
            item_id = str(entry_id)
            if self.tree.exists(item_id):
                self.tree.delete(item_id)

    def _complete_view_rollback(
        self,
        removed_ids: tuple[UUID, ...],
        previous_view: tuple[tuple[UUID, ...], tuple[UUID, ...]],
    ) -> None:
        for entry_id in removed_ids:
            entry = self._entries_by_id.pop(entry_id, None)
            if entry is not None:
                self._account_size(entry, -1)
        self._display_order = list(previous_view[0])
        selection = [
            str(entry_id)
            for entry_id in previous_view[1]
            if self.tree.exists(str(entry_id))
        ]
        if selection:
            self.tree.selection_set(selection)
        self._update_summary()

    def _on_import_finished(self, report: DiscoveryReport) -> None:
        self._last_import_report = report
        snapshot = self.collection.snapshot()
        self._update_import_warnings(snapshot.import_sessions)
        counters = report.counters
        if report.state is DiscoveryState.COMPLETE:
            message = (
                f"Import complete · {counters.accepted} added · "
                f"{counters.already_listed} already listed"
            )
        elif report.state is DiscoveryState.INCOMPLETE:
            message = (
                f"Import incomplete · {counters.accepted} added · "
                f"{counters.inaccessible} discovery errors"
            )
        elif report.state is DiscoveryState.CANCELLED:
            message = "Import canceled · previous collection restored"
        else:
            message = "Import failed · previous collection restored"
        self.status.configure(text=message)

    def _update_import_warnings(
        self, sessions: tuple[ImportSessionSnapshot, ...] | None = None
    ) -> None:
        if sessions is None:
            sessions = self.collection.snapshot().import_sessions
        self._import_issue_sessions = tuple(
            session
            for session in sessions
            if session.state in {ImportSessionState.INCOMPLETE, ImportSessionState.FAILED}
        )
        if self._import_issue_sessions:
            self.import_warnings_button.configure(
                text=f"Import issues ({len(self._import_issue_sessions)})"
            )
            self.import_warnings_button.grid()
        else:
            self.import_warnings_button.grid_remove()

    def open_import_details(self) -> None:
        sessions = self._import_issue_sessions
        if not sessions:
            return
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Import issues")
        dialog.geometry("760x520")
        dialog.minsize(520, 360)
        dialog.transient(self.root)
        body = ctk.CTkFrame(dialog)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(
            body,
            text=(
                "Some imports did not complete successfully. Review the affected paths "
                "and discovery reasons below. These warnings remain until the collection "
                "is cleared."
            ),
            anchor="w",
            justify="left",
            wraplength=700,
        ).pack(fill="x", padx=8, pady=(8, 10))
        details = ctk.CTkTextbox(body, wrap="word")
        details.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for session in sessions:
            label = (
                "Incomplete import"
                if session.state is ImportSessionState.INCOMPLETE
                else "Failed import"
            )
            details.insert("end", f"{label} · {session.session_id}\n")
            for diagnostic in session.diagnostics:
                details.insert(
                    "end", f"  {diagnostic.path}\n    {diagnostic.reason}\n"
                )
            details.insert("end", "\n")
        details.configure(state="disabled")

    def _on_import_error(self, message: str) -> None:
        self.status.configure(text=message)

    def _show_header_menu(self, event: tk.Event[Any]) -> None:
        if self.tree.identify_region(event.x, event.y) != "heading":
            return
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Columns…", command=self.open_columns)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_columns(self) -> None:
        if self.root.winfo_exists() == 0:
            return
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Columns — {self.active_mode}")
        dialog.geometry("420x540")
        dialog.minsize(360, 400)
        dialog.transient(self.root)
        self.column_dialog = dialog

        content = ctk.CTkFrame(dialog)
        content.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(
            content,
            text="Show columns",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 5))
        checklist = ctk.CTkScrollableFrame(content, height=390)
        checklist.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        preferences = self.column_preferences[self.active_mode]
        visible = set(preferences.visible)
        required = REQUIRED_COLUMNS[self.active_mode]
        for identifier in MODE_COLUMNS[self.active_mode]:
            variable = tk.BooleanVar(master=dialog, value=identifier in visible)
            checkbox = ctk.CTkCheckBox(
                checklist,
                text=self._mode_heading(identifier),
                variable=variable,
                state="disabled" if identifier in required else "normal",
                command=lambda col=identifier, var=variable: self._set_column_visible(
                    col, var.get()
                ),
            )
            checkbox.pack(fill="x", padx=8, pady=4)
            if identifier in required:
                checkbox.select()

        buttons = ctk.CTkFrame(content, fg_color="transparent")
        buttons.pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkButton(
            buttons,
            text="Restore Defaults",
            command=self._restore_column_defaults,
        ).pack(side="left")
        ctk.CTkButton(buttons, text="Done", command=dialog.destroy, width=85).pack(
            side="right"
        )

    def _set_column_visible(self, identifier: str, is_visible: bool) -> None:
        mode = self.active_mode
        visible = set(self.column_preferences[mode].visible)
        if is_visible:
            visible.add(identifier)
        else:
            visible.discard(identifier)
        self._store_column_preferences(mode, visible)

    def _restore_column_defaults(self) -> None:
        mode = self.active_mode
        preferences = default_column_preferences(mode)
        self._store_column_preferences(mode, preferences.visible, preferences.widths)
        if hasattr(self, "column_dialog") and self.column_dialog.winfo_exists():
            self.column_dialog.destroy()
            self.open_columns()

    def _store_column_preferences(
        self,
        mode: str,
        visible: set[str] | tuple[str, ...],
        widths: dict[str, int] | None = None,
    ) -> None:
        current = self.column_preferences[mode]
        selected_widths = current.widths if widths is None else widths
        set_column_preferences(self.config, mode, tuple(visible), selected_widths)
        self.column_preferences[mode] = get_column_preferences(self.config, mode)
        if mode == self.active_mode:
            self._apply_mode(mode)
        self._save_preferences()

    def _track_column_resize(self, event: tk.Event[Any]) -> None:
        self._resize_started = self.tree.identify_region(event.x, event.y) == "separator"

    def _save_column_resize(self, _event: tk.Event[Any]) -> None:
        if not self._resize_started:
            return
        self._resize_started = False
        self.root.after_idle(self._persist_column_widths)

    def _persist_column_widths(self) -> None:
        if self._closing:
            return
        mode = self.active_mode
        current = self.column_preferences[mode]
        widths = dict(current.widths)
        for identifier in MODE_COLUMNS[mode]:
            width = int(self.tree.column(identifier, "width"))
            if width > 0:
                widths[identifier] = width
        if widths != current.widths:
            self._store_column_preferences(mode, current.visible, widths)

    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.focus_force()
            return
        dialog = ctk.CTkToplevel(self.root)
        self.settings_window = dialog
        dialog.title("Settings")
        dialog.geometry("420x220")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        content = ctk.CTkFrame(dialog)
        content.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(
            content, text="Appearance", anchor="w", font=ctk.CTkFont(weight="bold")
        ).pack(fill="x", padx=10, pady=(10, 4))
        theme = tk.StringVar(master=dialog, value=self.appearance_mode)
        choice = ctk.CTkOptionMenu(
            content,
            variable=theme,
            values=("System", "Light", "Dark"),
            command=self._set_appearance_mode,
        )
        self.appearance_choice = theme
        choice.pack(anchor="w", padx=10, pady=(0, 12))
        ctk.CTkLabel(
            content,
            text="Column choices, widths, and window placement save automatically.",
            anchor="w",
            wraplength=360,
        ).pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkButton(content, text="Done", width=85, command=dialog.destroy).pack(
            anchor="e", padx=10, pady=(0, 8)
        )

    def _set_appearance_mode(self, value: str) -> None:
        if value not in {"System", "Light", "Dark"}:
            return
        previous = self.appearance_mode
        self.appearance_mode = value
        ctk.set_appearance_mode(value)
        self._apply_table_appearance()
        if value == "System":
            self._schedule_system_appearance_check()
        elif self._appearance_after is not None:
            self.root.after_cancel(self._appearance_after)
            self._appearance_after = None
        self.settings["appearance_mode"] = value
        self._save_preferences()
        if self.config_path is not None and self._last_save_error is not None:
            self.appearance_mode = previous
            ctk.set_appearance_mode(previous)
            self.settings["appearance_mode"] = previous
            self.appearance_choice.set(previous)
            self._apply_table_appearance()
            if previous == "System":
                self._schedule_system_appearance_check()

    def _apply_table_appearance(self) -> None:
        _style_file_table(self.root)
        background, error_background = self.root._tidyfiles_row_colors  # type: ignore[attr-defined]
        self.tree.tag_configure("normal", background=background)
        self.tree.tag_configure("problem", background=error_background)

    def _schedule_system_appearance_check(self) -> None:
        if self.appearance_mode == "System" and self._appearance_after is None:
            self._appearance_after = self.root.after(1000, self._refresh_system_appearance)

    def _save_preferences(self) -> None:
        self._last_save_error: ConfigError | None = None
        if self.config_path is None:
            return
        try:
            save_config(self.config_path, self.config)
        except ConfigError as exc:
            self._last_save_error = exc
            messagebox.showerror(
                "Settings could not be saved",
                str(exc),
                parent=self.root,
            )

    def _schedule_geometry_check(self, _event: tk.Event[Any] | None = None) -> None:
        if self._closing:
            return
        if self._geometry_after is not None:
            self.root.after_cancel(self._geometry_after)
        self._geometry_after = self.root.after(250, self._constrain_geometry)

    def _constrain_geometry(self) -> None:
        self._geometry_after = None
        if self._closing:
            return
        current = self.root.geometry()
        safe = restore_window_geometry(current, self._virtual_screen_bounds())
        if current != safe:
            self.root.geometry(safe)

    def _refresh_system_appearance(self) -> None:
        self._appearance_after = None
        if self._closing:
            return
        if self.appearance_mode == "System":
            self._apply_table_appearance()
            self._schedule_system_appearance_check()

    def close(self) -> None:
        if self._closing or self._close_pending:
            return
        if not self.import_controller.request_shutdown():
            self._close_pending = True
            self._set_import_buttons(True)
            self.cancel_button.configure(state="disabled")
            self.status.configure(
                text="Waiting for discovery to stop before closing · source files are unchanged"
            )
            return
        self._finish_close()

    def _finish_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.import_controller.dispose()
        geometry = restore_window_geometry(
            self.root.geometry(), self._virtual_screen_bounds()
        )
        self.settings["window_geometry"] = geometry
        if self._geometry_after is not None:
            self.root.after_cancel(self._geometry_after)
        if self._appearance_after is not None:
            self.root.after_cancel(self._appearance_after)
        self._save_preferences()
        self.root.destroy()


def create_main_window(
    config: dict[str, Any] | None = None,
    config_path: Path | str | None = None,
) -> ctk.CTk:
    """Create the GUI and its one shared, initially empty working collection."""
    return _MainWindow(config, config_path).root
