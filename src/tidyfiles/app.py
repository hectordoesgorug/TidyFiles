"""CustomTkinter application shell and shared collection presentation."""

from __future__ import annotations

import mimetypes
import os
import threading
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
    OrderingMode,
    PreparedSortOrder,
    WorkingCollection,
)
from tidyfiles.config import (
    ConfigError,
    default_config,
    get_sorting_preferences,
    save_config,
    set_sorting_preferences,
)
from tidyfiles.discovery import (
    DiscoveryReport,
    DiscoverySession,
    DiscoveryState,
)
from tidyfiles.import_controller import ImportController
from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences
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
# shortcut: suffix checks cannot identify arbitrary launchable formats;
# upgrade if content inspection is approved.
_NON_LAUNCHABLE_SUFFIXES = {
    ".appx", ".application", ".bash", ".bat", ".cjs", ".cmd", ".com",
    ".exe", ".fish", ".hta", ".jar", ".js", ".jse", ".lnk", ".msi",
    ".msp", ".msix", ".php", ".pif", ".pl", ".py", ".pyc", ".pyw",
    ".ps1", ".ps1xml", ".ps2", ".ps2xml", ".psd1", ".psm1", ".rb",
    ".reg", ".scr", ".sh", ".url", ".vbe", ".vbs", ".wsf", ".wsh",
    ".zsh",
}


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
        self._collection_operation_pending = False
        self._collection_after: str | None = None
        self._sort_done: threading.Event | None = None
        self._sort_result: PreparedSortOrder | None = None
        self._sort_error: Exception | None = None
        self._collection_render_order: tuple[UUID, ...] = ()
        self._collection_render_selection: tuple[UUID, ...] = ()
        self._collection_render_offset = 0
        self._collection_render_notify = False
        self._drag_press: tuple[str, int, int, int] | None = None
        self._drag_original_selection: tuple[UUID, ...] = ()
        self._dragging = False
        self._drag_selected: tuple[UUID, ...] = ()
        self._drag_target: tuple[str, bool] | None = None
        self._drag_prior_status = ""
        self.column_dialog: ctk.CTkToplevel | None = None

        self.root = ctk.CTk()
        self.root.application = self  # type: ignore[attr-defined]
        self.root.working_collection = self.collection  # type: ignore[attr-defined]
        self.root.collection_revision = self.collection.snapshot().revision  # type: ignore[attr-defined]
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
        self._update_control_states()
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
        self.format_field._entry.bind("<Control-a>", self._select_all_format_text, add="+")
        self.format_field._entry.bind("<Control-A>", self._select_all_format_text, add="+")
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
        commands = {
            "add_files": self.add_files,
            "add_folder": self.add_folder,
            "remove": self.remove_selected,
            "clear_all": self.clear_all,
        }
        for column, (key, label, width) in enumerate(
            (
                ("add_files", "Add Files", 76),
                ("add_folder", "Add Folder", 82),
                ("remove", "Remove", 70),
                ("clear_all", "Clear All", 76),
            )
        ):
            button = ctk.CTkButton(
                toolbar, text=label, width=width, command=commands[key]
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
            command=self._on_sort_criterion,
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
        self.sort_direction_button = ctk.CTkButton(
            toolbar,
            text=direction_label,
            width=92,
            command=self._toggle_sort_direction,
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
                stretch=False,
                anchor="w",
            )
        background, error_background = self.root._tidyfiles_row_colors  # type: ignore[attr-defined]
        self.tree.tag_configure("normal", background=background)
        self.tree.tag_configure("problem", background=error_background)
        self.tree.tag_configure(
            "drop-target",
            background="#284B68" if ctk.get_appearance_mode() == "Dark" else "#CFE8FF",
        )
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))

        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=(8, 0))
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind("<<TreeviewSelect>>", self._update_summary, add="+")
        self.tree.bind("<ButtonPress-1>", self._track_column_resize, add="+")
        self.tree.bind("<ButtonPress-1>", self._begin_drag_candidate, add="+")
        self.tree.bind("<B1-Motion>", self._track_row_drag, add="+")
        self.tree.bind("<ButtonRelease-1>", self._save_column_resize, add="+")
        self.tree.bind("<ButtonRelease-1>", self._finish_row_drag, add="+")
        self.tree.bind("<Button-3>", self._show_tree_context_menu, add="+")
        self.tree.bind("<Control-a>", self._select_all_rows, add="+")
        self.tree.bind("<Control-A>", self._select_all_rows, add="+")
        self.tree.bind("<Delete>", self._remove_selected_key, add="+")
        self._build_collection_context_menu()

    def _build_collection_context_menu(self) -> None:
        self.context_menu = tk.Menu(self.root, tearoff=False)
        self._context_menu_indices: dict[str, int] = {}
        for key, label, command in (
            ("open_file", "Open File", self.open_selected_file),
            ("open_folder", "Open Containing Folder", self.open_containing_folder),
            ("copy_name", "Copy Filename", self.copy_selected_filenames),
            ("copy_path", "Copy Full Path", self.copy_selected_paths),
        ):
            self.context_menu.add_command(label=label, command=command)
            self._context_menu_indices[key] = int(self.context_menu.index("end"))
        self.context_menu.add_separator()
        for key, label, command in (
            ("remove", "Remove from List", self.remove_selected),
            ("move_top", "Move to Top", lambda: self._move_selected("move_top")),
            ("move_up", "Move Up", lambda: self._move_selected("move_up")),
            ("move_down", "Move Down", lambda: self._move_selected("move_down")),
            ("move_bottom", "Move to Bottom", lambda: self._move_selected("move_bottom")),
        ):
            self.context_menu.add_command(label=label, command=command)
            self._context_menu_indices[key] = int(self.context_menu.index("end"))

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
            button = ctk.CTkButton(
                row,
                text=text,
                width=98,
                command=lambda operation=key: self._move_selected(operation),
            )
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
                stretch=False,
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
            if mode != self.active_mode:
                self._close_columns()
                self._persist_column_widths()
            self._apply_mode(mode)

    def refresh_collection(self) -> None:
        """Render the shared collection snapshot using stable entry IDs as row IDs."""
        if (
            hasattr(self, "import_controller")
            and self.import_controller.busy
        ) or self._collection_operation_pending:
            raise RuntimeError("Cannot rebuild the table during a collection operation")
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
        manual = self.collection.ordering_mode is OrderingMode.MANUAL
        if self._unknown_size_count:
            size_text = (
                f"Known size: {format_size(self._known_size_bytes)} · "
                f"{self._unknown_size_count} unavailable"
            )
        else:
            size_text = f"Total size: {format_size(self._known_size_bytes)}"
        manual_text = f"Manual order · {len(self._entries_by_id)} files  " if manual else ""
        self.summary.configure(
            text=(
                f"{manual_text}Files: {len(self._entries_by_id)}  "
                f"Selected: {selected_count}  {size_text}"
            )
        )
        self._update_control_states()

    def _selected_entry_ids(self) -> tuple[UUID, ...]:
        try:
            selected = {UUID(item) for item in self.tree.selection()}
        except ValueError:
            return ()
        return tuple(entry_id for entry_id in self._display_order if entry_id in selected)

    def _update_control_states(self) -> None:
        if not hasattr(self, "controls"):
            return
        importing = (
            getattr(self, "import_controller", None) is not None
            and self.import_controller.busy
        ) or self.collection.capabilities.is_importing
        busy = (
            importing
            or self._collection_operation_pending
            or self._closing
            or self._close_pending
        )
        if busy:
            for control in self.controls.values():
                control.configure(state="disabled")
            self.sort_dropdown.configure(state="disabled")
            self.sort_direction_button.configure(state="disabled")
            for button in self.movement_buttons.values():
                button.configure(state="disabled")
            return

        self.controls["add_files"].configure(state="normal")
        self.controls["add_folder"].configure(state="normal")
        snapshot = self.collection.snapshot()
        selected = self._selected_entry_ids()
        selected_set = set(selected)
        self.controls["remove"].configure(
            state="normal" if selected else "disabled"
        )
        clearable = bool(snapshot.entries or snapshot.import_sessions or snapshot.folder_roots)
        self.controls["clear_all"].configure(
            state="normal" if clearable else "disabled"
        )
        self.sort_dropdown.configure(state="normal")
        self.sort_direction_button.configure(state="normal")
        positions_by_id = {
            entry_id: position for position, entry_id in enumerate(self._display_order)
        }
        positions = [positions_by_id[entry_id] for entry_id in selected]
        can_move = bool(positions)
        movement = {
            "move_top": can_move and min(positions) > 0 if positions else False,
            "move_up": can_move
            and any(
                position > 0 and self._display_order[position - 1] not in selected_set
                for position in positions
            ),
            "move_down": can_move
            and any(
                position + 1 < len(self._display_order)
                and self._display_order[position + 1] not in selected_set
                for position in positions
            ),
            "move_bottom": can_move and max(positions) < len(self._display_order) - 1
            if positions
            else False,
        }
        for key, button in self.movement_buttons.items():
            button.configure(state="normal" if movement[key] else "disabled")

    def _notify_collection_changed(self) -> None:
        revision = self.collection.snapshot().revision
        self.root.collection_revision = revision  # type: ignore[attr-defined]
        self.root.event_generate("<<TidyFilesCollectionChanged>>", when="tail")

    def _account_size(self, entry: CollectionEntry, direction: int) -> None:
        size = entry.metadata.size_bytes if entry.metadata is not None else None
        if size is None:
            self._unknown_size_count += direction
        else:
            self._known_size_bytes += direction * size

    def _on_sort_criterion(self, _value: str | None = None) -> None:
        current = self.collection.sort_preferences
        criterion = _SORT_CRITERIA.get(self.sort_choice.get())
        if criterion is None:
            self.sort_choice.set(_SORT_LABELS[current.criterion])
            return
        self._start_sort(SortPreferences(criterion, current.direction))

    def _toggle_sort_direction(self) -> None:
        current = self.collection.sort_preferences
        direction = (
            SortDirection.DESCENDING
            if current.direction is SortDirection.ASCENDING
            else SortDirection.ASCENDING
        )
        self._start_sort(SortPreferences(current.criterion, direction))

    def _start_sort(self, preferences: SortPreferences) -> None:
        if (
            self._collection_operation_pending
            or self.import_controller.busy
            or self.collection.capabilities.is_importing
            or self._closing
        ):
            return
        self._collection_operation_pending = True
        self._sort_done = threading.Event()
        self._sort_result = None
        self._sort_error = None
        self.status.configure(text=f"Sorting by {_SORT_LABELS[preferences.criterion]}…")
        self._update_control_states()

        def prepare() -> None:
            try:
                self._sort_result = self.collection.prepare_sort_order(preferences)
            except Exception as error:
                self._sort_error = error
            finally:
                self._sort_done.set()

        try:
            threading.Thread(target=prepare, name="TidyFiles-sort", daemon=True).start()
            self._collection_after = self.root.after(16, self._poll_sort)
        except Exception as error:
            self._restore_sort_controls()
            self._finish_collection_work()
            self.status.configure(text=f"Sorting could not start: {error}")

    def _poll_sort(self) -> None:
        self._collection_after = None
        if self._closing or not self._collection_operation_pending:
            return
        if self._sort_done is None or not self._sort_done.is_set():
            self._collection_after = self.root.after(16, self._poll_sort)
            return
        if self._sort_error is not None or self._sort_result is None:
            self._restore_sort_controls()
            self._finish_collection_work()
            self.status.configure(text=f"Sorting failed: {self._sort_error or 'no result'}")
            return

        prepared = self._sort_result
        selection = tuple(UUID(item) for item in self.tree.selection())
        try:
            changed = self.collection.apply_prepared_sorting(prepared)
        except Exception as error:
            self._restore_sort_controls()
            self._finish_collection_work()
            self.status.configure(text=f"Sorting canceled: {error}")
            return

        preferences = prepared.preferences
        self.sort_choice.set(_SORT_LABELS[preferences.criterion])
        self.sort_direction_button.configure(
            text=("Ascending" if preferences.direction is SortDirection.ASCENDING else "Descending")
        )
        self._persist_sort_preferences(preferences)
        unsaved = self.config_path is not None and self._last_save_error is not None
        if changed:
            snapshot = self.collection.snapshot()
            rendered = self._start_tree_order_render(
                tuple(entry.entry_id for entry in snapshot.entries),
                selection,
                notify=True,
            )
            if rendered:
                self.status.configure(
                    text=(
                        "Sorted · preference could not be saved"
                        if unsaved
                        else f"Sorted by {_SORT_LABELS[preferences.criterion]}"
                    )
                )
        else:
            self._finish_collection_work()
            self._update_summary()
            self.status.configure(
                text=(
                    "Sort applied · preference could not be saved"
                    if unsaved
                    else f"Sorted by {_SORT_LABELS[preferences.criterion]}"
                )
            )

    def _persist_sort_preferences(self, preferences: SortPreferences) -> None:
        config = deepcopy(self.config)
        set_sorting_preferences(config, preferences)
        self.config = config
        self.settings = self.config["global_settings"]
        self._save_preferences()

    def _restore_sort_controls(self) -> None:
        preferences = self.collection.sort_preferences
        self.sort_choice.set(_SORT_LABELS[preferences.criterion])
        self.sort_direction_button.configure(
            text=("Ascending" if preferences.direction is SortDirection.ASCENDING else "Descending")
        )

    def _start_tree_order_render(
        self,
        order: tuple[UUID, ...],
        selection: tuple[UUID, ...],
        *,
        notify: bool,
    ) -> bool:
        if not self._collection_operation_pending:
            self._collection_operation_pending = True
        self._collection_render_order = order
        self._collection_render_selection = selection
        self._collection_render_offset = 0
        self._collection_render_notify = notify
        self._display_order = list(order)
        try:
            self.tree.set_children("", *(str(entry_id) for entry_id in order))
        except Exception as error:
            self._collection_operation_pending = False
            try:
                self.refresh_collection()
            except Exception:
                pass
            self._update_control_states()
            if notify:
                self._notify_collection_changed()
            self.status.configure(
                text=f"Collection display refreshed after an order error: {error}"
            )
            return False
        self._update_control_states()
        self._collection_after = self.root.after(0, self._render_collection_order_chunk)
        return True

    def _render_collection_order_chunk(self) -> None:
        self._collection_after = None
        if self._closing or not self._collection_operation_pending:
            return
        order = self._collection_render_order
        start = self._collection_render_offset
        end = min(start + 96, len(order))
        try:
            for index in range(start, end):
                entry_id = order[index]
                entry = self._entries_by_id[entry_id]
                self.tree.item(str(entry_id), values=self._entry_values(index + 1, entry))
        except Exception as error:
            self._collection_operation_pending = False
            try:
                self.refresh_collection()
            except Exception:
                pass
            self._update_control_states()
            if self._collection_render_notify:
                self._notify_collection_changed()
            self.status.configure(
                text=f"Collection display refreshed after an update error: {error}"
            )
            return

        self._collection_render_offset = end
        if end < len(order):
            self._collection_after = self.root.after(1, self._render_collection_order_chunk)
            return
        surviving = [
            str(entry_id)
            for entry_id in self._collection_render_selection
            if self.tree.exists(str(entry_id))
        ]
        self.tree.selection_set(surviving)
        notify = self._collection_render_notify
        self._collection_operation_pending = False
        self._update_summary()
        self._update_control_states()
        if notify:
            self._notify_collection_changed()

    def _finish_collection_work(self) -> None:
        self._collection_operation_pending = False
        self._sort_done = None
        self._sort_result = None
        self._sort_error = None
        self._update_summary()
        self._update_control_states()

    def _move_selected(self, operation: str) -> None:
        if (
            self._collection_operation_pending
            or self.import_controller.busy
            or self.collection.capabilities.is_importing
        ):
            return
        selected = self._selected_entry_ids()
        method = {
            "move_top": self.collection.move_to_top,
            "move_up": self.collection.move_up,
            "move_down": self.collection.move_down,
            "move_bottom": self.collection.move_to_bottom,
        }.get(operation)
        if not selected or method is None:
            return
        try:
            changed = method(selected)
        except Exception as error:
            self.status.configure(text=f"Could not change collection order: {error}")
            return
        if not changed:
            self._update_control_states()
            return
        snapshot = self.collection.snapshot()
        if self._start_tree_order_render(
            tuple(entry.entry_id for entry in snapshot.entries), selected, notify=True
        ):
            self.status.configure(text="Applying manual order…")

    def remove_selected(self) -> None:
        if (
            self._collection_operation_pending
            or self.import_controller.busy
            or self.collection.capabilities.is_importing
        ):
            return
        selected = self._selected_entry_ids()
        try:
            removed = self.collection.remove_entries(selected)
        except Exception as error:
            self.status.configure(text=f"Could not remove collection entries: {error}")
            return
        if not removed:
            self._update_control_states()
            return
        for entry in removed:
            self._entries_by_id.pop(entry.entry_id, None)
            self._account_size(entry, -1)
        self.tree.delete(*(str(entry.entry_id) for entry in removed))
        snapshot = self.collection.snapshot()
        if self._start_tree_order_render(
            tuple(entry.entry_id for entry in snapshot.entries), (), notify=True
        ):
            self.status.configure(
                text=f"Removed {len(removed)} from list · source files are unchanged"
            )
        self._update_import_warnings(snapshot.import_sessions)

    def clear_all(self) -> None:
        if (
            self._collection_operation_pending
            or self.import_controller.busy
            or self.collection.capabilities.is_importing
        ):
            return
        if not self.collection.clear_all():
            self._update_control_states()
            return
        children = self.tree.get_children("")
        if children:
            self.tree.delete(*children)
        self._entries_by_id.clear()
        self._display_order.clear()
        self._known_size_bytes = 0
        self._unknown_size_count = 0
        self._last_import_report = None
        self._update_import_warnings(())
        self._update_summary()
        self._update_control_states()
        self.status.configure(text="Collection cleared · source files are unchanged")
        self._notify_collection_changed()

    def _capture_import_view_state(self) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
        return tuple(self._display_order), tuple(UUID(item) for item in self.tree.selection())

    def add_files(self) -> None:
        if self._closing or self.import_controller.busy or self._collection_operation_pending:
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
        if self._closing or self.import_controller.busy or self._collection_operation_pending:
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

    def _on_import_activity(
        self, phase: str, session: DiscoverySession | None
    ) -> None:
        if phase == "idle":
            if self._progress_running:
                self.progress.stop()
                self._progress_running = False
            self.progress.set(0)
            self.cancel_button.grid_remove()
            self._update_control_states()
            return

        self._update_control_states()
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
        self._update_summary()
        self._notify_collection_changed()

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

    def _select_all_rows(self, _event: tk.Event[Any]) -> str:
        self.tree.selection_set(*self.tree.get_children(""))
        self._update_summary()
        return "break"

    def _select_all_format_text(self, _event: tk.Event[Any]) -> str:
        self.format_field._entry.selection_range(0, "end")
        self.format_field._entry.icursor("end")
        return "break"

    def _remove_selected_key(self, _event: tk.Event[Any]) -> str:
        self.remove_selected()
        return "break"

    def _show_tree_context_menu(self, event: tk.Event[Any]) -> None:
        if self.tree.identify_region(event.x, event.y) == "heading":
            self._show_header_menu(event)
            return
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        if item_id not in self.tree.selection():
            self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self._update_summary()
        selected = self._selected_entries()
        one_entry = len(selected) == 1
        busy = self._collection_operation_pending or self.import_controller.busy
        executable = (
            one_entry
            and Path(selected[0].source_path).suffix.casefold()
            in _NON_LAUNCHABLE_SUFFIXES
        )
        states = {
            "open_file": "disabled" if not one_entry or executable else "normal",
            "open_folder": "normal" if one_entry else "disabled",
            "copy_name": "normal" if selected else "disabled",
            "copy_path": "normal" if selected else "disabled",
            "remove": self.controls["remove"].cget("state"),
            **{key: button.cget("state") for key, button in self.movement_buttons.items()},
        }
        for key, state in states.items():
            index = self._context_menu_indices[key]
            if key == "open_file" and executable:
                self.context_menu.entryconfigure(
                    index, label="Open File (executable content disabled)"
                )
            elif key == "open_file":
                self.context_menu.entryconfigure(index, label="Open File")
            if busy and key in {"remove", "move_top", "move_up", "move_down", "move_bottom"}:
                state = "disabled"
            self.context_menu.entryconfigure(index, state=state)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _selected_entries(self) -> tuple[CollectionEntry, ...]:
        return tuple(
            self._entries_by_id[entry_id]
            for entry_id in self._selected_entry_ids()
            if entry_id in self._entries_by_id
        )

    def _copy_to_clipboard(self, values: tuple[str, ...]) -> None:
        if not values:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(values))
        self.status.configure(text=f"Copied {len(values)} item{'s' if len(values) != 1 else ''}")

    def copy_selected_filenames(self) -> None:
        self._copy_to_clipboard(
            tuple(entry.original_filename for entry in self._selected_entries())
        )

    def copy_selected_paths(self) -> None:
        self._copy_to_clipboard(tuple(entry.source_path for entry in self._selected_entries()))

    def open_selected_file(self) -> None:
        entries = self._selected_entries()
        if len(entries) != 1:
            return
        path = Path(entries[0].source_path)
        if path.suffix.casefold() in _NON_LAUNCHABLE_SUFFIXES:
            self.status.configure(text="Opening executable or script files is disabled")
            return
        self._open_path(path, require_directory=False)

    def open_containing_folder(self) -> None:
        entries = self._selected_entries()
        if len(entries) == 1:
            self._open_path(Path(entries[0].source_path).parent, require_directory=True)

    def _open_path(self, path: Path, *, require_directory: bool) -> None:
        available = path.is_dir() if require_directory else path.is_file()
        if not available:
            self.status.configure(text=f"Path is no longer available: {path}")
            return
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            self.status.configure(text="Windows file opening is unavailable")
            return
        try:
            startfile(str(path))
        except OSError as error:
            self.status.configure(text=f"Could not open {path}: {error}")

    def _begin_drag_candidate(self, event: tk.Event[Any]) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            self._drag_press = None
            self._drag_original_selection = ()
            return
        item_id = self.tree.identify_row(event.y)
        if (
            not item_id
            or self._collection_operation_pending
            or self.import_controller.busy
            or self.collection.capabilities.is_importing
        ):
            self._drag_press = None
            self._drag_original_selection = ()
            return
        self._drag_press = (
            item_id,
            event.x_root,
            event.y_root,
            self.collection.snapshot().revision,
        )
        selected = set(self.tree.selection())
        self._drag_original_selection = tuple(
            entry_id for entry_id in self._display_order if str(entry_id) in selected
        )
        self._dragging = False

    def _track_row_drag(self, event: tk.Event[Any]) -> str | None:
        press = self._drag_press
        if press is None:
            return None
        if (
            self._collection_operation_pending
            or self.import_controller.busy
            or self.collection.capabilities.is_importing
        ):
            self._cancel_row_drag()
            return "break" if self._dragging else None
        if not self._dragging:
            if max(abs(event.x_root - press[1]), abs(event.y_root - press[2])) < 5:
                return None
            pressed_id = UUID(press[0])
            selected = (
                self._drag_original_selection
                if pressed_id in self._drag_original_selection
                else (pressed_id,)
            )
            if not selected:
                self._drag_press = None
                return None
            self._dragging = True
            self._drag_selected = selected
            self.tree.selection_set(*(str(entry_id) for entry_id in selected))
            self._drag_prior_status = self.status.cget("text")

        target_id = self.tree.identify_row(event.y)
        if not target_id or UUID(target_id) in self._drag_selected:
            self._clear_drop_indicator()
            return "break"
        bbox = self.tree.bbox(target_id)
        if not bbox:
            self._clear_drop_indicator()
            return "break"
        after = event.y >= bbox[1] + bbox[3] // 2
        self._set_drop_indicator(target_id, after)
        return "break"

    def _set_drop_indicator(self, item_id: str, after: bool) -> None:
        self._clear_drop_indicator()
        tags = tuple(tag for tag in self.tree.item(item_id, "tags") if tag != "drop-target")
        self.tree.item(item_id, tags=(*tags, "drop-target"))
        self._drag_target = (item_id, after)
        direction = "after" if after else "before"
        self.status.configure(text=f"Drop {direction} {self.tree.set(item_id, 'filename')}")

    def _clear_drop_indicator(self) -> None:
        if self._drag_target is not None:
            item_id = self._drag_target[0]
            if self.tree.exists(item_id):
                tags = tuple(tag for tag in self.tree.item(item_id, "tags") if tag != "drop-target")
                self.tree.item(item_id, tags=tags)
        self._drag_target = None

    def _finish_row_drag(self, _event: tk.Event[Any]) -> str | None:
        if not self._dragging:
            self._drag_press = None
            self._drag_original_selection = ()
            return None
        press = self._drag_press
        target = self._drag_target
        selected = self._drag_selected
        self._clear_drop_indicator()
        self._dragging = False
        self._drag_press = None
        self._drag_original_selection = ()
        self._drag_selected = ()
        if press is None or target is None:
            self.status.configure(text=self._drag_prior_status)
            return "break"
        snapshot = self.collection.snapshot()
        if snapshot.revision != press[3] or not snapshot.capabilities.can_reorder:
            self.status.configure(text="Collection changed during drag; order was not changed")
            return "break"
        target_id = UUID(target[0])
        selected_set = set(selected)
        if target_id in selected_set:
            self.status.configure(text=self._drag_prior_status)
            return "break"
        remaining = [
            entry.entry_id
            for entry in snapshot.entries
            if entry.entry_id not in selected_set
        ]
        position = remaining.index(target_id) + int(target[1])
        order = (*remaining[:position], *selected, *remaining[position:])
        try:
            changed = self.collection.set_order(order)
        except (ValueError, RuntimeError) as error:
            self.status.configure(text=f"Drag canceled: {error}")
            return "break"
        if changed:
            if self._start_tree_order_render(order, selected, notify=True):
                self.status.configure(text="Applying manual order…")
        else:
            self.status.configure(text=self._drag_prior_status)
        return "break"

    def _cancel_row_drag(self) -> None:
        self._clear_drop_indicator()
        self._drag_press = None
        self._drag_original_selection = ()
        self._drag_selected = ()
        if self._dragging:
            self.status.configure(text=self._drag_prior_status)
        self._dragging = False

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
        if self.column_dialog is not None:
            try:
                if self.column_dialog.winfo_exists():
                    self.column_dialog.deiconify()
                    self.column_dialog.lift()
                    self.column_dialog.focus_force()
                    return
            except tk.TclError:
                pass
            self.column_dialog = None
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Columns — {self.active_mode}")
        dialog.geometry("420x540")
        dialog.minsize(360, 400)
        dialog.transient(self.root)
        self.column_dialog = dialog
        dialog.bind(
            "<Destroy>",
            lambda event, window=dialog: self._on_columns_destroy(window, event),
            add="+",
        )
        dialog.protocol("WM_DELETE_WINDOW", self._close_columns)

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
        ctk.CTkButton(buttons, text="Done", command=self._close_columns, width=85).pack(
            side="right"
        )

    def _on_columns_destroy(
        self, dialog: ctk.CTkToplevel, event: tk.Event[Any]
    ) -> None:
        if event.widget is dialog and self.column_dialog is dialog:
            self.column_dialog = None

    def _close_columns(self) -> None:
        dialog = self.column_dialog
        if dialog is None:
            return
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:
            pass
        if self.column_dialog is dialog:
            self.column_dialog = None

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
        if self.column_dialog is not None and self.column_dialog.winfo_exists():
            self._close_columns()
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
        self.tree.tag_configure(
            "drop-target",
            background="#284B68" if ctk.get_appearance_mode() == "Dark" else "#CFE8FF",
        )

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
            self._update_control_states()
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
        self._close_columns()
        if self._collection_after is not None:
            try:
                self.root.after_cancel(self._collection_after)
            except Exception:
                pass
            self._collection_after = None
        self._cancel_row_drag()
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
