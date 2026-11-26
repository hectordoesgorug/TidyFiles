"""CustomTkinter application shell and shared collection presentation."""

from __future__ import annotations

import mimetypes
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from uuid import UUID

import customtkinter as ctk

from tidyfiles.cleanup import (
    AutoCleanRequest,
    AutoCleanService,
    CleanupResult,
    TrashClearService,
)
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
    get_auto_clean_enabled,
    get_auto_clear_trash_on_exit,
    get_sorting_preferences,
    save_config,
    set_auto_clean_enabled,
    set_auto_clear_trash_on_exit,
    set_sorting_preferences,
)
from tidyfiles.destination_planning import (
    DestinationOptions,
    DestinationPlan,
    PlanState,
)
from tidyfiles.discovery import (
    DiscoveryReport,
    DiscoverySession,
    DiscoveryState,
)
from tidyfiles.execution_preflight import (
    PreflightProgress,
    PreflightRequest,
    PreflightStage,
    PreflightState,
    capture_review_plan,
)
from tidyfiles.filename_integration import (
    DiagnosticKind,
    EvaluationProgress,
    EvaluationStage,
    EvaluationState,
    FilenameEvaluationResult,
    FilenameEvaluationService,
)
from tidyfiles.format_reference import FORMAT_REFERENCE, TOKEN_GROUPS
from tidyfiles.import_controller import ImportController
from tidyfiles.modifying_jobs import ModifyingJobCoordinator
from tidyfiles.preflight_service import PreflightService
from tidyfiles.recovery import RecoveryInspection, RecoveryInspector
from tidyfiles.rename_executor import (
    RenameExecutor,
    RenameOutcome,
    RenameProgress,
    RenameScope,
    RenameState,
)
from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences
from tidyfiles.transaction_journal import JournalStore
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
_COLUMN_INDEX = {column.identifier: index for index, column in enumerate(COLUMNS)}
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


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes:02d}:{seconds:02d}"
    )


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
        self.coordinator = ModifyingJobCoordinator()
        self.journals = JournalStore()
        self.rename_executor = RenameExecutor(self.coordinator, self.journals)
        self.preflight_service = PreflightService()
        self.recovery_inspector = RecoveryInspector(self.journals)
        self.auto_clean_service = AutoCleanService(self.coordinator, self.journals)
        self.trash_clear_service = TrashClearService(self.coordinator, self.journals)
        self.active_mode = _DEFAULT_MODE
        self.column_preferences = {
            mode: get_column_preferences(self.config, mode) for mode in MODES
        }
        self._closing = False
        self._close_pending = False
        self._exit_cleanup_done = False
        self._processing_phase = "idle"
        self._processing_done: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._processing_progress: queue.Queue[Any] = queue.Queue(maxsize=64)
        self._processing_after: str | None = None
        self._processing_cancel = threading.Event()
        self._processing_started_at: float | None = None
        self._processing_snapshot: Any = None
        self._processing_options: DestinationOptions | None = None
        self._processing_acknowledged_import_ids: tuple[UUID, ...] = ()
        self._processing_plan: DestinationPlan | None = None
        self._processing_request: PreflightRequest | None = None
        self._processing_future: Any = None
        self._active_preflight_result: Any = None
        self._processing_intended_ids: tuple[UUID, ...] = ()
        self._processing_partial = False
        self._plan_review_dialog: ctk.CTkToplevel | None = None
        self._processing_diagnostics_by_id: dict[UUID, str] = {}
        self._maintenance_kind: str | None = None
        self._recovery_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._recovery_after: str | None = None
        self._recovery_dialog: ctk.CTkToplevel | None = None
        self._geometry_after: str | None = None
        self._appearance_after: str | None = None
        self._resize_started = False
        self.settings_window: ctk.CTkToplevel | None = None
        self._entries_by_id: dict[UUID, CollectionEntry] = {}
        self._display_order: list[UUID] = []
        self._display_positions: dict[UUID, int] = {}
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
        self._collection_render_previous_positions: dict[UUID, int] = {}
        self._collection_render_notify = False
        self._drag_press: tuple[str, int, int, int] | None = None
        self._drag_original_selection: tuple[UUID, ...] = ()
        self._dragging = False
        self._drag_selected: tuple[UUID, ...] = ()
        self._drag_target: tuple[str, bool] | None = None
        self._drag_prior_status = ""
        self.column_dialog: ctk.CTkToplevel | None = None
        self._preview_service: FilenameEvaluationService | None = None
        self._preview_progress: queue.Queue[EvaluationProgress] = queue.Queue(maxsize=64)
        self._preview_after: str | None = None
        self._preview_debounce_after: str | None = None
        self._preview_clear_after: str | None = None
        self._preview_render_after: str | None = None
        self._preview_generation = 0
        self._preview_request_id: UUID | None = None
        self._preview_future: Any = None
        self._preview_request_generation = 0
        self._preview_force_metadata = False
        self._preview_clear_ids: tuple[str, ...] = ()
        self._preview_clear_offset = 0
        self._preview_clear_generation = 0
        self._preview_clear_schedule = False
        self._preview_clear_status = "Pending"
        self._preview_clear_baseline = True
        self._preview_clear_applied: tuple[str, bool] | None = None
        self._preview_filename_ids: set[UUID] = set()
        self._preview_problem_ids: set[UUID] = set()
        self._preview_render_result: FilenameEvaluationResult | None = None
        self._preview_render_offset = 0
        self._preview_render_generation = 0
        self._preview_result: FilenameEvaluationResult | None = None
        self._preview_diagnostics_by_id: dict[UUID, Any] = {}
        self._preview_import_invalidated = False
        self._format_reference_dialog: ctk.CTkToplevel | None = None
        self._preview_details_dialog: ctk.CTkToplevel | None = None
        self._preview_details_textbox: ctk.CTkTextbox | None = None
        self._context_entry_id: UUID | None = None

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
        self._preview_service = FilenameEvaluationService()
        self.rename_format.trace_add("write", self._on_format_changed)
        self.root.bind(
            "<<TidyFilesCollectionChanged>>", self._on_collection_changed, add="+"
        )
        self._preview_after = self.root.after(20, self._poll_filename_preview)
        self._update_control_states()
        self._update_import_warnings()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._schedule_geometry_check, add="+")
        self._schedule_system_appearance_check()
        self._start_recovery_inspection()

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
            self.root, anchor="nw", height=154, command=self._on_mode_change
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
            command=self._on_preset_selected,
        )
        self.format_field._entry.bind("<Control-a>", self._select_all_format_text, add="+")
        self.format_field._entry.bind("<Control-A>", self._select_all_format_text, add="+")
        self.format_field.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.insert_token_button = ctk.CTkButton(
            formats,
            text="Insert Token",
            width=100,
            command=self._show_insert_token_menu,
        )
        self.insert_token_button.grid(row=0, column=1, padx=3)
        self.profiles_button = _disabled_button(formats, text="Profiles", width=85)
        self.profiles_button.grid(row=0, column=2, padx=3)
        self.format_reference_button = ctk.CTkButton(
            formats, text="Format Reference", width=125, command=self.open_format_reference
        )
        self.format_reference_button.grid(row=0, column=3, padx=(3, 0))
        self.refresh_preview_button = ctk.CTkButton(
            formats, text="Refresh Preview", width=112, command=self.refresh_filename_preview
        )
        self.refresh_preview_button.grid(row=0, column=4, padx=(6, 0))
        self.refresh_preview_help = ctk.CTkLabel(
            formats,
            text=(
                "Recalculates filenames and required directory counts. "
                "Does not rescan folders or change the collection."
            ),
            anchor="w",
            font=ctk.CTkFont(size=10),
        )
        self.refresh_preview_help.grid(
            row=1, column=0, columnspan=5, padx=4, pady=(0, 2), sticky="w"
        )
        self._format_border_normal = self.format_field.cget("border_color")
        self.format_diagnostic = ctk.CTkLabel(
            panel, text="", anchor="w", text_color=("#B42318", "#FF8D85")
        )
        self.format_diagnostic.grid(row=1, column=0, padx=8, pady=(0, 1), sticky="w")
        self.format_diagnostic.grid_remove()

        options = ctk.CTkFrame(panel, fg_color="transparent")
        options.grid(row=2, column=0, padx=4, pady=(0, 1), sticky="w")
        self.single_folder_switch = ctk.CTkSwitch(
            options,
            text="Single Folder",
            onvalue=True,
            offvalue=False,
            command=self._on_destination_option_changed,
        )
        self.single_folder_switch.grid(row=0, column=0, padx=(0, 14), sticky="w")
        self.auto_clean_switch = ctk.CTkSwitch(
            options,
            text="Auto-clean",
            onvalue=True,
            offvalue=False,
            command=self._on_auto_clean_changed,
        )
        if get_auto_clean_enabled(self.config):
            self.auto_clean_switch.select()
        self.auto_clean_switch.grid(row=0, column=1, padx=(0, 14), sticky="w")
        self.counter_switch = ctk.CTkSwitch(
            options, text="Counter", onvalue=True, offvalue=False
        )
        self.counter_switch.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(
            panel,
            text=(
                "Auto-clean permanently deletes Thumbs.db, .DS_Store, desktop.ini, and empty "
                "subfolders from imported Add Folder roots after a complete successful rename."
            ),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=10),
            wraplength=980,
        ).grid(row=3, column=0, padx=6, pady=(0, 3), sticky="ew")

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
        self.trash_clear_button = ctk.CTkButton(
            toolbar, text="Clear Trash", width=104, command=self.clear_trash
        )
        self.trash_clear_button.grid(row=0, column=8, padx=(6, 0), sticky="w")

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
        self.context_menu.add_command(
            label="Preview Details", command=self.open_preview_details, state="disabled"
        )
        self._context_menu_indices["preview_details"] = int(self.context_menu.index("end"))
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
            row.grid_columnconfigure(column, minsize=102, weight=0)
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
            command=self.cancel_current_work,
        )
        self.cancel_button.grid(row=0, column=3, padx=(0, 6), sticky="e")
        self.cancel_button.grid_remove()
        self.process_button = _disabled_button(
            footer, text="Process", width=105, command=self.start_rename_review
        )
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
        if self._processing_phase != "idle" or self._maintenance_kind is not None:
            self.tabs.set(self.active_mode)
            return
        if mode in self.mode_panels:
            previous = self.active_mode
            if mode != previous:
                self._close_columns()
                self._persist_column_widths()
                self._apply_mode(mode)
                if previous == "Bulk Rename":
                    self._leave_bulk_rename()
                elif mode == "Bulk Rename":
                    self._return_to_bulk_rename()
            else:
                self._apply_mode(mode)

    def _on_preset_selected(self, value: str) -> None:
        if value not in _FORMAT_PRESETS:
            return
        self.rename_format.set(value)
        self.format_field._entry.icursor("end")
        self.format_field._entry.focus_set()

    def _on_destination_option_changed(self) -> None:
        self._update_control_states()

    def _on_auto_clean_changed(self) -> None:
        enabled = bool(self.auto_clean_switch.get())
        previous = get_auto_clean_enabled(self.config)
        if enabled and not previous and not messagebox.askyesno(
            "Enable Auto-clean?",
            "After a complete successful rename, Auto-clean permanently deletes "
            "Thumbs.db, .DS_Store, desktop.ini, and empty subfolders from explicitly "
            "imported Add Folder roots. Enable this setting?",
            parent=self.root,
        ):
            self.auto_clean_switch.deselect()
            return
        set_auto_clean_enabled(self.config, enabled)
        self._save_preferences()
        if self.config_path is not None and self._last_save_error is not None:
            set_auto_clean_enabled(self.config, previous)
            self.auto_clean_switch.select() if previous else self.auto_clean_switch.deselect()

    def _on_auto_clear_trash_changed(self) -> None:
        enabled = bool(self.auto_clear_trash_switch.get())
        previous = get_auto_clear_trash_on_exit(self.config)
        if enabled and not previous and not messagebox.askyesno(
            "Enable Auto-Clear Trash?",
            "Recognized batches in the managed Trash folder will be permanently cleared "
            "after each normal application exit. Recovery-dependent files and unknown "
            "content are preserved. Enable this setting?",
            parent=self.settings_window or self.root,
        ):
            self.auto_clear_trash_switch.deselect()
            return
        set_auto_clear_trash_on_exit(self.config, enabled)
        self._save_preferences()
        if self.config_path is not None and self._last_save_error is not None:
            set_auto_clear_trash_on_exit(self.config, previous)
            if previous:
                self.auto_clear_trash_switch.select()
            else:
                self.auto_clear_trash_switch.deselect()

    def _show_insert_token_menu(self) -> None:
        menu = self._build_insert_token_menu()
        try:
            menu.tk_popup(
                self.insert_token_button.winfo_rootx(),
                self.insert_token_button.winfo_rooty() + self.insert_token_button.winfo_height(),
            )
        finally:
            menu.grab_release()

    def _build_insert_token_menu(self) -> tk.Menu:
        menu = getattr(self, "_insert_token_menu", None)
        if menu is None:
            menu = tk.Menu(self.root, tearoff=False)
            self._insert_token_submenus = []
            for category, tokens in TOKEN_GROUPS:
                submenu = tk.Menu(menu, tearoff=False)
                self._insert_token_submenus.append(submenu)
                menu.add_cascade(label=category, menu=submenu)
                for token, template in tokens:
                    submenu.add_command(
                        label=token.value,
                        command=lambda value=template: self._insert_format_token(value),
                    )
            self._insert_token_menu = menu
        return menu

    def _insert_format_token(self, template: str) -> None:
        entry = self.format_field._entry
        current = self.rename_format.get()
        try:
            start = entry.index("sel.first")
            end = entry.index("sel.last")
        except tk.TclError:
            start = end = entry.index("insert")
        self.rename_format.set(current[:start] + template + current[end:])
        entry.icursor(start + len(template))
        entry.focus_set()

    def open_format_reference(self) -> None:
        if self._closing:
            return
        if self._format_reference_dialog is not None:
            try:
                if self._format_reference_dialog.winfo_exists():
                    self._format_reference_dialog.deiconify()
                    self._format_reference_dialog.lift()
                    self._format_reference_dialog.focus_force()
                    return
            except tk.TclError:
                pass
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Filename Format Reference")
        dialog.geometry("760x720")
        dialog.minsize(600, 480)
        dialog.transient(self.root)
        self._format_reference_dialog = dialog
        dialog.bind(
            "<Destroy>",
            lambda event, window=dialog: self._on_preview_dialog_destroy(
                event, window, "reference"
            ),
            add="+",
        )
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        text = ctk.CTkTextbox(dialog, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("1.0", FORMAT_REFERENCE)
        text.configure(state="disabled")

    def _on_preview_dialog_destroy(
        self, event: tk.Event[Any], window: ctk.CTkToplevel, kind: str
    ) -> None:
        if event.widget is not window:
            return
        if kind == "reference" and self._format_reference_dialog is window:
            self._format_reference_dialog = None
        elif kind == "details" and self._preview_details_dialog is window:
            self._preview_details_dialog = None
            self._preview_details_textbox = None

    def _import_is_busy(self) -> bool:
        return (
            getattr(self, "import_controller", None) is not None
            and self.import_controller.busy
        ) or self.collection.capabilities.is_importing

    def _on_format_changed(self, *_args: Any) -> None:
        if self._closing:
            return
        self._set_format_diagnostic(None)
        self._invalidate_filename_preview(schedule=True)
        if not self.rename_format.get() and self._entries_by_id:
            self.format_diagnostic.configure(text="A rename format is required.")
            self.format_diagnostic.grid()
        self._update_control_states()

    def _on_collection_changed(self, _event: tk.Event[Any] | None = None) -> None:
        if self._closing:
            return
        self._invalidate_filename_preview(schedule=True)
        if not self.rename_format.get() and not self._entries_by_id:
            self._set_format_diagnostic(None)
        self._update_control_states()

    def _set_format_diagnostic(self, diagnostic: Any | None) -> None:
        if diagnostic is None:
            self.format_diagnostic.configure(text="")
            self.format_diagnostic.grid_remove()
            self.format_field.configure(border_color=self._format_border_normal)
            return
        detail = diagnostic.message
        if diagnostic.token is not None:
            detail += f" · {diagnostic.token}"
        if diagnostic.position is not None:
            detail += f" · offset {diagnostic.position}"
        self.format_diagnostic.configure(text=detail)
        self.format_diagnostic.grid()
        self.format_field.configure(border_color=("#B42318", "#F97066"))

    def _cancel_preview_after(self, name: str) -> None:
        after_id = getattr(self, name)
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass
        setattr(self, name, None)

    def _invalidate_filename_preview(
        self,
        *,
        schedule: bool,
        force_metadata: bool = False,
        invalidate_metadata: bool = False,
    ) -> None:
        if self._closing:
            return
        self._preview_generation += 1
        self._cancel_preview_after("_preview_debounce_after")
        self._cancel_preview_after("_preview_render_after")
        if self._preview_service is not None:
            self._preview_service.cancel(self._preview_request_id)
            if invalidate_metadata:
                self._preview_service.invalidate_metadata()
        self._preview_request_id = None
        self._preview_future = None
        self._preview_result = None
        self._preview_render_result = None
        self._preview_diagnostics_by_id.clear()
        self._preview_force_metadata = self._preview_force_metadata or force_metadata
        while True:
            try:
                self._preview_progress.get_nowait()
            except queue.Empty:
                break

        expression = self.rename_format.get()
        busy = self._import_is_busy() or self._collection_operation_pending
        has_entries = bool(self._entries_by_id)
        should_schedule = (
            schedule
            and self.active_mode == "Bulk Rename"
            and bool(expression)
            and has_entries
            and not busy
        )
        status = "Pending" if should_schedule else None
        if self.active_mode == "Bulk Rename" and should_schedule:
            self.status.configure(text="Filename preview pending")
        elif (
            self.active_mode == "Bulk Rename"
            and expression
            and not busy
            and has_entries
        ):
            self.status.configure(text="Filename preview pending")
        elif self.active_mode == "Bulk Rename" and not expression:
            if has_entries and not busy:
                self.format_diagnostic.configure(text="A rename format is required.")
                self.format_diagnostic.grid()

        self._begin_preview_clear(
            generation=self._preview_generation,
            schedule=should_schedule,
            status=status,
            baseline=not should_schedule,
        )
        self._update_control_states()

    def _begin_preview_clear(
        self,
        *,
        generation: int,
        schedule: bool,
        status: str | None,
        baseline: bool,
    ) -> None:
        target_status = status or "Imported"
        same_target = (
            self._preview_clear_status == target_status
            and self._preview_clear_baseline == baseline
        )
        if self._preview_clear_after is not None and same_target:
            self._preview_clear_generation = generation
            self._preview_clear_schedule = schedule
            return
        if self._preview_clear_after is not None:
            self._cancel_preview_after("_preview_clear_after")
        self._preview_clear_generation = generation
        self._preview_clear_schedule = schedule
        self._preview_clear_status = target_status
        self._preview_clear_baseline = baseline
        clear_key = (target_status, baseline)
        if self._preview_clear_applied == clear_key:
            self._finish_preview_clear(generation)
            return
        self._preview_clear_ids = tuple(self.tree.get_children(""))
        self._preview_clear_offset = 0
        if self._preview_clear_ids:
            self._preview_clear_after = self.root.after(0, self._clear_preview_chunk)
        else:
            self._finish_preview_clear(generation)

    def _clear_preview_chunk(self) -> None:
        self._preview_clear_after = None
        generation = self._preview_clear_generation
        if self._closing or generation != self._preview_generation:
            return
        end = min(self._preview_clear_offset + 96, len(self._preview_clear_ids))
        for item_id in self._preview_clear_ids[self._preview_clear_offset:end]:
            if not self.tree.exists(item_id):
                continue
            try:
                entry_id = UUID(item_id)
            except ValueError:
                continue
            entry = self._entries_by_id.get(entry_id)
            if entry is None:
                continue
            if entry_id in self._preview_filename_ids:
                self.tree.set(item_id, "new_filename", "")
                self._preview_filename_ids.discard(entry_id)
            current_status = self.tree.set(item_id, "status")
            baseline_status = (
                current_status if self._import_is_busy() else "Imported"
            )
            target_status = (
                baseline_status if self._preview_clear_baseline else self._preview_clear_status
            )
            if current_status != target_status:
                self.tree.set(item_id, "status", target_status)
            if entry_id in self._preview_problem_ids:
                self.tree.item(item_id, tags=("normal",))
                self._preview_problem_ids.discard(entry_id)
        self._preview_clear_offset = end
        if end < len(self._preview_clear_ids):
            self._preview_clear_after = self.root.after(1, self._clear_preview_chunk)
            return
        self._finish_preview_clear(generation)

    def _finish_preview_clear(self, generation: int) -> None:
        self._preview_clear_after = None
        if self._closing or generation != self._preview_generation:
            return
        self._preview_clear_applied = (
            self._preview_clear_status,
            self._preview_clear_baseline,
        )
        if self._preview_clear_schedule:
            self._preview_debounce_after = self.root.after(
                300, self._submit_filename_preview
            )

    def _submit_filename_preview(self) -> None:
        self._preview_debounce_after = None
        if self._closing or self._preview_service is None:
            return
        snapshot = self.collection.snapshot()
        expression = self.rename_format.get()
        if (
            self.active_mode != "Bulk Rename"
            or self._import_is_busy()
            or self._collection_operation_pending
            or not expression
            or not snapshot.entries
            or snapshot.capabilities.is_importing
        ):
            return
        generation = self._preview_generation
        force_metadata = self._preview_force_metadata
        self._preview_force_metadata = False
        while True:
            try:
                self._preview_progress.get_nowait()
            except queue.Empty:
                break

        def progress(update: EvaluationProgress) -> None:
            try:
                self._preview_progress.put_nowait(update)
            except queue.Full:
                try:
                    self._preview_progress.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._preview_progress.put_nowait(update)
                except queue.Full:
                    pass

        self.status.configure(text="Evaluating filenames")
        self.progress.set(0)
        try:
            self._preview_future = self._preview_service.submit(
                snapshot,
                expression,
                force_metadata_refresh=force_metadata,
                progress_callback=progress,
            )
        except RuntimeError as error:
            self.status.configure(text=f"Filename preview could not start: {error}")
            return
        self._preview_request_id = self._preview_future.request_id
        self._preview_request_generation = generation

    def _preview_result_is_current(self, result: FilenameEvaluationResult) -> bool:
        service = self._preview_service
        return bool(
            service is not None
            and not self._closing
            and not self._import_is_busy()
            and not self._collection_operation_pending
            and self.active_mode == "Bulk Rename"
            and result.request_id == self._preview_request_id
            and result.expression == self.rename_format.get()
            and service.is_current_revision(
                result,
                collection_id=self.collection.collection_id,
                revision=self.collection.revision,
                expression=self.rename_format.get(),
            )
        )

    def _poll_filename_preview(self) -> None:
        self._preview_after = None
        if self._closing:
            return
        request_id = self._preview_request_id
        latest_update: EvaluationProgress | None = None
        for _ in range(64):
            try:
                update = self._preview_progress.get_nowait()
            except queue.Empty:
                break
            if (
                update.request_id != request_id
                or request_id is None
                or self._import_is_busy()
            ):
                continue
            latest_update = update
        if (
            latest_update is not None
            and not self._collection_operation_pending
            and self.active_mode == "Bulk Rename"
        ):
            labels = {
                EvaluationStage.PARSING: "Parsing filename format",
                EvaluationStage.METADATA: "Acquiring directory counts",
                EvaluationStage.CONTEXTS: "Preparing filename context",
                EvaluationStage.EVALUATING: "Evaluating filenames",
            }
            self.status.configure(text=labels[latest_update.stage])
            if latest_update.total > 0:
                self.progress.set(min(1.0, latest_update.completed / latest_update.total))

        future = self._preview_future
        if future is not None and future.done():
            self._preview_future = None
            try:
                result = future.result()
            except Exception as error:
                if request_id == self._preview_request_id and not self._import_is_busy():
                    self.status.configure(text=f"Filename preview failed: {error}")
            else:
                if (
                    request_id == self._preview_request_id
                    and getattr(self, "_preview_request_generation", -1)
                    == self._preview_generation
                    and result.request_id == request_id
                ):
                    if self.active_mode == "Bulk Rename":
                        if self._preview_result_is_current(result):
                            self._start_preview_render(result)
                    elif (
                        self._preview_service is not None
                        and self._preview_service.is_current_revision(
                            result,
                            collection_id=self.collection.collection_id,
                            revision=self.collection.revision,
                            expression=self.rename_format.get(),
                        )
                    ):
                        self._preview_result = result
                        self._index_preview_diagnostics(result)
            if not self._import_is_busy():
                self.progress.set(0)

        self._preview_after = self.root.after(20, self._poll_filename_preview)

    def _start_preview_render(self, result: FilenameEvaluationResult) -> None:
        self._preview_clear_applied = None
        self._preview_render_result = result
        self._preview_render_offset = 0
        self._preview_render_generation = self._preview_generation
        parse_error = next(
            (
                diagnostic
                for diagnostic in result.diagnostics
                if diagnostic.kind is DiagnosticKind.PARSE
            ),
            None,
        )
        self._set_format_diagnostic(parse_error)
        self._index_preview_diagnostics(result)
        self.status.configure(text="Updating filename preview rows")
        if result.entries:
            self._preview_render_after = self.root.after(0, self._render_preview_chunk)
        else:
            self._finish_preview_render(result)

    def _index_preview_diagnostics(self, result: FilenameEvaluationResult) -> None:
        global_diagnostics = tuple(
            diagnostic
            for diagnostic in result.diagnostics
            if diagnostic.entry_id is None
        )
        priority = {
            DiagnosticKind.METADATA: 0,
            DiagnosticKind.TOKEN: 1,
            DiagnosticKind.VALIDATION: 2,
        }
        by_id: dict[UUID, Any] = {}
        for diagnostic in result.diagnostics:
            if diagnostic.entry_id is None:
                continue
            previous = by_id.get(diagnostic.entry_id)
            if previous is None or priority.get(diagnostic.kind, 3) < priority.get(
                previous.kind, 3
            ):
                by_id[diagnostic.entry_id] = diagnostic
        if global_diagnostics and any(
            diagnostic.kind is DiagnosticKind.PARSE for diagnostic in global_diagnostics
        ):
            by_id.update({item.entry.entry_id: global_diagnostics[0] for item in result.entries})
        self._preview_diagnostics_by_id = by_id

    def _preview_row_values(
        self, entry_id: UUID, filename: str, status: str, *, problem: bool
    ) -> None:
        item_id = str(entry_id)
        if not self.tree.exists(item_id):
            return
        if filename:
            self._preview_filename_ids.add(entry_id)
        else:
            self._preview_filename_ids.discard(entry_id)
        if self.tree.set(item_id, "new_filename") != filename:
            self.tree.set(item_id, "new_filename", filename)
        operation_detail = self._processing_diagnostics_by_id.get(entry_id)
        if operation_detail:
            status = f"{status} · {operation_detail}"
        if self.tree.set(item_id, "status") != status:
            self.tree.set(item_id, "status", status)
        was_problem = entry_id in self._preview_problem_ids
        if problem != was_problem:
            self.tree.item(item_id, tags=("problem" if problem else "normal",))
        if problem:
            self._preview_problem_ids.add(entry_id)
        elif entry_id in self._preview_problem_ids:
            self._preview_problem_ids.discard(entry_id)

    def _render_preview_chunk(self) -> None:
        self._preview_render_after = None
        result = self._preview_render_result
        if result is None or self._closing:
            return
        if (
            self._preview_render_generation != self._preview_generation
            or not self._preview_result_is_current(result)
        ):
            self._invalidate_filename_preview(schedule=True)
            return
        end = min(self._preview_render_offset + 96, len(result.entries))
        parse_error = next(
            (
                diagnostic
                for diagnostic in result.diagnostics
                if diagnostic.kind is DiagnosticKind.PARSE
            ),
            None,
        )
        for item in result.entries[self._preview_render_offset:end]:
            entry_id = item.entry.entry_id
            if parse_error is not None:
                self._preview_row_values(entry_id, "", "Invalid format", problem=False)
                continue
            diagnostic = self._preview_diagnostics_by_id.get(entry_id)
            if diagnostic is not None and diagnostic.kind is DiagnosticKind.METADATA:
                self._preview_row_values(entry_id, "", "Metadata unavailable", problem=True)
            elif diagnostic is not None and diagnostic.kind is DiagnosticKind.TOKEN:
                self._preview_row_values(entry_id, "", "Invalid format", problem=True)
            elif diagnostic is not None and diagnostic.kind is DiagnosticKind.VALIDATION:
                self._preview_row_values(entry_id, "", "Invalid filename", problem=True)
            elif item.is_valid:
                filename = item.validated_filename or ""
                status = (
                    "Unchanged"
                    if filename == item.entry.original_filename
                    else "Valid preview"
                )
                self._preview_row_values(entry_id, filename, status, problem=False)
            else:
                self._preview_row_values(entry_id, "", "Invalid format", problem=True)
        self._preview_render_offset = end
        if end < len(result.entries):
            self._preview_render_after = self.root.after(1, self._render_preview_chunk)
            return
        self._finish_preview_render(result)

    def _finish_preview_render(self, result: FilenameEvaluationResult) -> None:
        if not self._preview_result_is_current(result):
            self._invalidate_filename_preview(schedule=True)
            return
        self._preview_result = result
        self._preview_render_result = None
        self.progress.set(0)
        if result.is_valid:
            self.status.configure(
                text=(
                    f"Preview complete · {len(result.entries)} filenames · "
                    "destination review available"
                )
            )
        else:
            self.status.configure(
                text="Preview contains errors · the complete batch cannot be processed"
            )
        self._update_control_states()

    def _leave_bulk_rename(self) -> None:
        if self._preview_render_after is not None:
            self._cancel_preview_after("_preview_render_after")
            if self._preview_render_result is not None and self._preview_service is not None:
                if self._preview_service.is_current_revision(
                    self._preview_render_result,
                    collection_id=self.collection.collection_id,
                    revision=self.collection.revision,
                    expression=self.rename_format.get(),
                ):
                    self._preview_result = self._preview_render_result
            self._preview_render_result = None
        self._cancel_preview_after("_preview_clear_after")
        self._begin_preview_clear(
            generation=self._preview_generation,
            schedule=False,
            status=None,
            baseline=True,
        )

    def _return_to_bulk_rename(self) -> None:
        self._cancel_preview_after("_preview_clear_after")
        if self._preview_result is not None and self._preview_result_is_current(
            self._preview_result
        ):
            self._start_preview_render(self._preview_result)
        elif self.rename_format.get() and self._entries_by_id:
            if self._preview_future is None:
                self._invalidate_filename_preview(schedule=True)
        elif not self._entries_by_id:
            self._set_format_diagnostic(None)
        else:
            self.format_diagnostic.configure(text="A rename format is required.")
            self.format_diagnostic.grid()

    def refresh_filename_preview(self) -> None:
        if self._closing or self._import_is_busy() or self._collection_operation_pending:
            return
        self._invalidate_filename_preview(
            schedule=True, force_metadata=True, invalidate_metadata=True
        )

    def start_rename_review(self) -> None:
        if not self._process_ready():
            return
        snapshot = self.collection.snapshot()
        evaluation = self._preview_result
        assert evaluation is not None
        incomplete_ids = tuple(
            session.session_id for session in snapshot.incomplete_imports
        )
        if incomplete_ids:
            diagnostics = sum(len(session.diagnostics) for session in snapshot.incomplete_imports)
            if not messagebox.askyesno(
                "Acknowledge incomplete imports",
                f"{len(incomplete_ids)} import session(s) did not complete ({diagnostics} "
                "recorded issue(s)). Continue with the files that were collected? Each source "
                "will still be checked before processing.",
                parent=self.root,
            ):
                return
        self._processing_snapshot = snapshot
        self._processing_options = DestinationOptions(
            single_folder=bool(self.single_folder_switch.get()),
            managed_paths=self.journals.managed_paths,
        )
        self._processing_acknowledged_import_ids = incomplete_ids
        self._processing_auto_clean_enabled = bool(self.auto_clean_switch.get())
        self._processing_cancel.clear()
        self._processing_started_at = None
        self._submit_destination_plan(evaluation)

    def _submit_destination_plan(
        self, evaluation: FilenameEvaluationResult, notice: str | None = None
    ) -> None:
        snapshot = self._processing_snapshot
        options = self._processing_options
        if snapshot is None or options is None or not self.collection.is_current(snapshot):
            self._finish_processing("The collection changed before destination review.")
            return
        self._set_processing_phase("planning", "Checking destination paths…", cancellable=True)
        self._start_processing_poll()

        def work() -> None:
            try:
                plan = capture_review_plan(
                    snapshot,
                    evaluation,
                    current_request_id=evaluation.request_id,
                    options=options,
                    acknowledged_incomplete_import_ids=(
                        self._processing_acknowledged_import_ids
                    ),
                )
                payload = (plan, evaluation, notice, None)
            except Exception as error:
                payload = (None, evaluation, notice, error)
            self._processing_done.put(("plan", payload))

        threading.Thread(
            target=work, name="TidyFiles-destination-review", daemon=True
        ).start()

    def _show_destination_review(
        self,
        plan: DestinationPlan,
        evaluation: FilenameEvaluationResult,
        notice: str | None,
    ) -> None:
        self._processing_plan = plan
        self._set_processing_phase("review", "Review the destinations before renaming.")
        dialog = ctk.CTkToplevel(self.root)
        self._plan_review_dialog = dialog
        dialog.title("Review destination plan")
        dialog.geometry("900x600")
        dialog.minsize(680, 420)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self.cancel_current_work)
        body = ctk.CTkFrame(dialog)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        if plan.state is PlanState.VALID:
            summary = f"{len(plan.entries)} destinations are ready for final preflight."
        elif plan.state is PlanState.COLLISION_BLOCKED:
            summary = (
                f"{len(plan.collision_subset_ids)} of {len(plan.entries)} files can be processed "
                "as a collision-only subset. Conflicting and dependent files will remain listed."
            )
        else:
            summary = f"This batch cannot be processed: {plan.reason or plan.state.value}."
        if notice:
            summary = f"{notice}\n\n{summary}"
        ctk.CTkLabel(
            body, text=summary, anchor="w", justify="left", wraplength=850
        ).pack(fill="x", padx=8, pady=(8, 6))
        if plan.incomplete_imports:
            ctk.CTkLabel(
                body,
                text=(
                    f"Acknowledged incomplete imports: {len(plan.incomplete_imports)}. "
                    "Collected entries remain subject to fresh source validation."
                ),
                anchor="w",
                justify="left",
                wraplength=850,
            ).pack(fill="x", padx=8, pady=(0, 6))
        details = ctk.CTkTextbox(body, wrap="none")
        details.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        if plan.entries:
            lines = [
                f"{row.state.value} / {row.operation.value if row.operation else 'blocked'}"
                f"\n  {row.source_path}\n  → {row.destination_path or 'unavailable'}"
                f"\n  Candidate: {row.candidate_filename}"
                + (f"\n  Reason: {row.reason}" if row.reason else "")
                for row in plan.entries
            ]
            details.insert("1.0", "\n\n".join(lines))
        else:
            details.insert("1.0", plan.reason or plan.state.value)
        details.configure(state="disabled")
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=(0, 4))
        partial = plan.state is PlanState.COLLISION_BLOCKED
        can_confirm = plan.state is PlanState.VALID or bool(plan.collision_subset_ids)
        if can_confirm:
            intended = (
                plan.collision_subset_ids
                if partial
                else tuple(row.entry_id for row in plan.entries)
            )
            label = (
                f"Process {len(intended)} unaffected files"
                if partial
                else "Confirm rename"
            )
            ctk.CTkButton(
                actions,
                text=label,
                width=200,
                command=lambda: self._submit_preflight(
                    dialog, plan, evaluation, intended, partial
                ),
            ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            actions,
            text="Cancel",
            width=95,
            command=self.cancel_current_work,
        ).pack(side="right")
        dialog.grab_set()

    def _submit_preflight(
        self,
        dialog: ctk.CTkToplevel | None,
        plan: DestinationPlan,
        evaluation: FilenameEvaluationResult,
        intended: tuple[UUID, ...],
        partial: bool,
    ) -> None:
        if not self.collection.is_current(self._processing_snapshot):
            self.cancel_current_work()
            messagebox.showwarning(
                "Review expired", "The collection changed. Review the destinations again.",
                parent=self.root,
            )
            return
        if dialog is not None:
            self._plan_review_dialog = None
            dialog.destroy()
        options = self._processing_options
        assert options is not None and evaluation.expression is not None
        request = PreflightRequest(
            snapshot=self._processing_snapshot,
            reviewed_plan=plan,
            reviewed_evaluation=evaluation,
            current_evaluation_id=evaluation.request_id,
            expression=evaluation.expression,
            settings=evaluation.settings,
            options=options,
            intended_entry_ids=intended,
            acknowledged_incomplete_import_ids=(
                self._processing_acknowledged_import_ids
            ),
        )
        self._processing_plan = plan
        self._processing_intended_ids = intended
        self._processing_partial = partial
        self._processing_request = request
        self._processing_future = self.preflight_service.submit(
            request, progress_callback=self._queue_processing_progress
        )
        self._processing_future.add_done_callback(self._queue_preflight_result)
        self._set_processing_phase(
            "preflight", "Rechecking sources and destinations…", cancellable=True
        )
        self._start_processing_poll()

    def _queue_preflight_result(self, future: Any) -> None:
        try:
            result = future.result()
        except Exception as error:
            self._processing_done.put(("preflight_error", error))
        else:
            self._processing_done.put(("preflight", result))

    def _queue_processing_progress(self, update: Any) -> None:
        try:
            self._processing_progress.put_nowait(update)
        except queue.Full:
            pass

    def _start_processing_poll(self) -> None:
        if self._processing_after is None:
            self._processing_after = self.root.after(40, self._poll_processing)

    def _poll_processing(self) -> None:
        self._processing_after = None
        while True:
            try:
                update = self._processing_progress.get_nowait()
            except queue.Empty:
                break
            self._show_processing_progress(update)
        try:
            kind, payload = self._processing_done.get_nowait()
        except queue.Empty:
            kind = ""
            payload = None
        if kind:
            if kind == "plan":
                plan, evaluation, notice, error = payload
                if self._processing_cancel.is_set():
                    self._finish_processing("Destination review canceled.")
                elif error is not None:
                    self._finish_processing(f"Destination review failed: {error}")
                elif plan is not None:
                    if plan.state is PlanState.VALID and notice is None:
                        self._submit_preflight(
                            None,
                            plan,
                            evaluation,
                            tuple(row.entry_id for row in plan.entries),
                            False,
                        )
                    else:
                        self._show_destination_review(plan, evaluation, notice)
            elif kind == "preflight":
                self._handle_preflight_result(payload)
            elif kind == "preflight_error":
                self._finish_processing(f"Preflight failed safely: {payload}")
            elif kind == "rename":
                self._handle_rename_result(payload)
            elif kind == "trash_inspection":
                self._handle_trash_inspection(payload)
            elif kind == "trash_clear":
                self._handle_trash_clear(payload)
            elif kind == "exit_trash_clear":
                self._handle_exit_trash_clear(payload)
        if self._processing_phase not in {"idle", "review"}:
            self._start_processing_poll()

    def _show_processing_progress(self, update: Any) -> None:
        if isinstance(update, PreflightProgress):
            labels = {
                PreflightStage.INPUT: "Preparing validation…",
                PreflightStage.SOURCES: "Checking source files…",
                PreflightStage.METADATA: "Refreshing required metadata…",
                PreflightStage.EVALUATION: "Refreshing filename evaluation…",
                PreflightStage.COMPARISON: "Comparing reviewed candidates…",
                PreflightStage.DESTINATIONS: "Rechecking destinations…",
                PreflightStage.FINALIZATION: "Finishing validation…",
            }
            text = labels[update.stage]
            if update.total:
                text += f" {update.completed}/{update.total}"
                self.progress.set(update.completed / update.total)
            self.status.configure(text=text)
        elif isinstance(update, RenameProgress):
            job = self.coordinator.current
            modifying = bool(job and job.job_id == update.job_id and job.modifying)
            if modifying:
                self._set_processing_phase("rename_modifying", "Renaming files…")
                if self._processing_started_at is None:
                    self._processing_started_at = time.monotonic()
            else:
                self._set_processing_phase(
                    "rename_preparing",
                    "Preparing the journaled rename…",
                    cancellable=True,
                )
            if update.total:
                self.progress.set(update.completed / update.total)
            label = "Preparing rename"
            if modifying:
                started = self._processing_started_at or time.monotonic()
                elapsed = max(0.0, time.monotonic() - started)
                label = (
                    f"Renaming files: {update.completed}/{update.total} · "
                    f"Elapsed: {_format_elapsed(elapsed)}"
                )
                if update.completed >= 10 and elapsed >= 2 and update.total > update.completed:
                    remaining = elapsed * (update.total - update.completed) / update.completed
                    label += f" · ETA: {_format_elapsed(remaining + 0.5)}"
            self.status.configure(text=label)
        elif isinstance(update, tuple) and update[0] == "stage":
            self._set_processing_phase(update[1], update[2])

    def _handle_preflight_result(self, result: Any) -> None:
        self._processing_future = None
        self._active_preflight_result = None
        if self._close_pending:
            self._finish_processing("Preflight canceled before closing.")
            return
        if result.state is PreflightState.PASSED:
            self._active_preflight_result = result
            self._start_rename_execution(result)
            return
        if result.state in {PreflightState.REVIEW_REQUIRED, PreflightState.COLLISION_BLOCKED}:
            fresh = result.fresh_evaluation
            if fresh is not None and fresh.state is EvaluationState.VALID:
                self._submit_destination_plan(
                    fresh,
                    "Filesystem observations or reviewed filenames changed. Confirm the "
                    "updated destination plan before processing.",
                )
                return
        if result.diagnostic.entry_id is not None:
            self._processing_diagnostics_by_id[result.diagnostic.entry_id] = (
                result.diagnostic.detail or result.diagnostic.code.replace("_", " ")
            )
        self._finish_processing(
            f"Rename was not started: {result.diagnostic.detail or result.diagnostic.code}."
        )

    def _start_rename_execution(self, preflight: Any) -> None:
        self._active_preflight_result = preflight
        self._set_processing_phase(
            "rename_preparing", "Preparing the journaled rename…", cancellable=True
        )
        self.progress.set(0)
        self._start_processing_poll()
        snapshot = self._processing_snapshot
        partial = self._processing_partial
        auto_clean_enabled = self._processing_auto_clean_enabled

        def work() -> None:
            try:
                scope = RenameScope.from_review(preflight)
                result = self.rename_executor.execute(
                    preflight,
                    authorized=scope,
                    current_preflight=lambda: (
                        None
                        if self._processing_cancel.is_set()
                        else self._active_preflight_result
                    ),
                    progress=self._queue_processing_progress,
                )
                clean_result = None
                if auto_clean_enabled:
                    self._queue_processing_progress(
                        ("stage", "auto_cleaning", "Running eligible post-processing cleanup…")
                    )
                    outputs = tuple(
                        item.actual_path
                        for item in result.entries
                        if item.outcome in {RenameOutcome.RENAMED, RenameOutcome.UNCHANGED}
                        and item.actual_path is not None
                    )
                    clean_result = self.auto_clean_service.run(
                        AutoCleanRequest(
                            snapshot=snapshot,
                            enabled=True,
                            primary_succeeded=result.state is RenameState.SUCCEEDED,
                            partial=partial,
                            unresolved=result.state is RenameState.RECOVERY_REQUIRED,
                            output_paths=outputs,
                        )
                    )
                paths = {
                    item.entry_id: item.actual_path
                    for item in result.entries
                    if item.outcome
                    in {
                        RenameOutcome.FAILED_SAFE,
                        RenameOutcome.NOT_PROCESSED,
                        RenameOutcome.RECOVERY_REQUIRED,
                    }
                    and item.actual_path is not None
                }
                self._processing_done.put(
                    ("rename", (result, clean_result, paths, None))
                )
            except Exception as error:
                self._processing_done.put(("rename", (None, None, {}, error)))

        threading.Thread(
            target=work, name="TidyFiles-rename-executor", daemon=True
        ).start()

    def _handle_rename_result(self, payload: Any) -> None:
        result, clean_result, paths, error = payload
        if error is not None or result is None:
            self._finish_processing(f"Rename failed safely: {error}")
            return
        removed_ids = tuple(
            item.entry_id
            for item in result.entries
            if item.outcome in {RenameOutcome.RENAMED, RenameOutcome.UNCHANGED}
        )
        excluded = set(self._processing_plan.excluded_entry_ids if self._processing_plan else ())
        planned_ids = {entry.entry_id for entry in self._processing_snapshot.entries}
        excluded.update(planned_ids.difference(self._processing_intended_ids))
        for entry_id in excluded:
            self._processing_diagnostics_by_id[entry_id] = (
                "Excluded by the confirmed collision-only subset"
            )
        for item in result.entries:
            if item.outcome in {RenameOutcome.RENAMED, RenameOutcome.UNCHANGED}:
                self._processing_diagnostics_by_id.pop(item.entry_id, None)
            else:
                self._processing_diagnostics_by_id[item.entry_id] = (
                    item.detail or item.outcome.value.replace("_", " ")
                )
        removed = self.collection.remove_entries(removed_ids)
        for entry in removed:
            self._account_size(entry, -1)
            self._entries_by_id.pop(entry.entry_id, None)
            self._preview_filename_ids.discard(entry.entry_id)
            self._preview_problem_ids.discard(entry.entry_id)
            if self.tree.exists(str(entry.entry_id)):
                self.tree.delete(str(entry.entry_id))
        try:
            updated = self.collection.update_entry_paths(paths)
        except Exception as update_error:
            updated = ()
            self._processing_diagnostics_by_id.update(
                {entry_id: f"Verified path could not be refreshed in the collection: {update_error}"
                 for entry_id in paths}
            )
        for entry in updated:
            self._entries_by_id[entry.entry_id] = entry
        self._display_order = [
            entry_id for entry_id in self._display_order
            if entry_id in self._entries_by_id
        ]
        self._display_positions = {
            entry_id: index for index, entry_id in enumerate(self._display_order)
        }
        for index, entry_id in enumerate(self._display_order, 1):
            entry = self._entries_by_id[entry_id]
            detail = self._processing_diagnostics_by_id.get(entry_id)
            status = "Needs attention" if detail else "Imported"
            self.tree.item(
                str(entry_id),
                values=self._entry_values(index, entry, status=status),
            )
        self._update_import_warnings()
        if removed or updated:
            self._notify_collection_changed()
        self._update_summary()
        summary = self._rename_result_summary(result, clean_result, excluded)
        issues = bool(excluded) or result.state is not RenameState.SUCCEEDED or any(
            item.outcome not in {RenameOutcome.RENAMED, RenameOutcome.UNCHANGED}
            for item in result.entries
        )
        if clean_result is not None and (clean_result.detail or clean_result.failures):
            issues = True
        self._finish_processing(summary, show_dialog=True, warning=issues)

    def _rename_result_summary(
        self, result: Any, cleanup: CleanupResult | None, excluded: set[UUID]
    ) -> str:
        counts = {outcome: 0 for outcome in RenameOutcome}
        for item in result.entries:
            counts[item.outcome] += 1
        text = (
            f"Rename result: {counts[RenameOutcome.RENAMED]} renamed, "
            f"{counts[RenameOutcome.UNCHANGED]} unchanged, "
            f"{counts[RenameOutcome.FAILED_SAFE]} failed safely, "
            f"{counts[RenameOutcome.RECOVERY_REQUIRED]} need recovery, "
            f"{counts[RenameOutcome.NOT_PROCESSED] + len(excluded)} not processed."
        )
        lines = [result.detail] if result.detail else []
        if not lines:
            diagnostic = next(
                (
                    item.detail
                    for item in result.entries
                    if item.outcome
                    in {RenameOutcome.FAILED_SAFE, RenameOutcome.RECOVERY_REQUIRED}
                    and item.detail
                ),
                None,
            )
            if diagnostic:
                lines.append(diagnostic)
        if excluded:
            lines.append(f"{len(excluded)} file(s) were excluded from the confirmed subset.")
        if cleanup is not None:
            if cleanup.detail:
                lines.append(f"Auto-clean: {cleanup.detail}")
            lines.extend(
                f"Auto-clean failed: {item.path} — {item.reason or 'deletion failed'}"
                for item in cleanup.failures[:12]
            )
            if not cleanup.detail and not cleanup.failures:
                lines.append(f"Auto-clean removed {len(cleanup.deleted_paths)} item(s).")
        if lines:
            text += "\n\n" + "\n".join(lines)
        return text

    def _finish_processing(
        self, message: str, *, show_dialog: bool = False, warning: bool = False
    ) -> None:
        if self._plan_review_dialog is not None:
            try:
                self._plan_review_dialog.grab_release()
                self._plan_review_dialog.destroy()
            except tk.TclError:
                pass
            self._plan_review_dialog = None
        self._processing_phase = "idle"
        self._collection_operation_pending = False
        self._maintenance_kind = None
        self._processing_future = None
        self._processing_request = None
        self._active_preflight_result = None
        self._processing_plan = None
        self._processing_options = None
        self._processing_snapshot = None
        self._processing_started_at = None
        self.progress.set(0)
        self.cancel_button.grid_remove()
        self.status.configure(text=message)
        self._update_control_states()
        if show_dialog:
            if warning:
                messagebox.showwarning("Rename results", message, parent=self.root)
            else:
                messagebox.showinfo("Rename complete", message, parent=self.root)
        if self._close_pending:
            self._continue_close()

    def _set_processing_phase(
        self, phase: str, status: str, *, cancellable: bool = False
    ) -> None:
        self._processing_phase = phase
        self._collection_operation_pending = phase != "idle"
        self.status.configure(text=status)
        if phase == "idle":
            self.cancel_button.grid_remove()
        else:
            self.cancel_button.grid()
            self.cancel_button.configure(state="normal" if cancellable else "disabled")
        self._update_control_states()

    def cancel_current_work(self) -> None:
        if self.import_controller.busy:
            self.cancel_import()
            return
        phase = self._processing_phase
        if phase == "idle":
            return
        if phase == "review":
            self._finish_processing("Rename review canceled.")
            return
        if phase in {"rename_modifying", "auto_cleaning", "trash_clearing", "exit_trash_clear"}:
            self.status.configure(text="The current filesystem changes cannot be canceled safely.")
            return
        if phase == "rename_preparing":
            job = self.coordinator.current
            if job is not None:
                try:
                    canceled = self.coordinator.cancel_preparation(job.job_id)
                except Exception:
                    canceled = False
                if not canceled and job.modifying:
                    self._set_processing_phase("rename_modifying", "Renaming files…")
                    self.status.configure(
                        text="The current filesystem changes cannot be canceled safely."
                    )
                    return
            self._processing_cancel.set()
        else:
            self._processing_cancel.set()
            if phase == "preflight" and self._processing_request is not None:
                self.preflight_service.cancel(self._processing_request.request_id)
        self.status.configure(text="Canceling preparation…")

    def clear_trash(self) -> None:
        if self._closing or self._collection_operation_pending or self.import_controller.busy:
            return
        self._maintenance_kind = "trash_inspection"
        self._set_processing_phase("trash_inspecting", "Inspecting managed Trash…")
        self._start_processing_poll()

        def inspect() -> None:
            try:
                result = self.trash_clear_service.inspect()
                payload = (result, None)
            except Exception as error:
                payload = (None, error)
            self._processing_done.put(("trash_inspection", payload))

        threading.Thread(
            target=inspect, name="TidyFiles-trash-inspection", daemon=True
        ).start()

    def _handle_trash_inspection(self, payload: Any) -> None:
        summary, error = payload
        if self._close_pending:
            self._finish_processing("Trash inspection stopped before closing.")
            return
        if error is not None or summary is None:
            message = f"Trash inspection failed: {error or 'unknown error'}"
            self._finish_processing(message)
            messagebox.showwarning("Clear Trash", message, parent=self.root)
            return
        if summary.detail == "Managed Trash does not exist":
            self._finish_processing("Trash is empty.")
            messagebox.showinfo("Clear Trash", "Trash is empty.", parent=self.root)
            return
        if summary.detail:
            self._finish_processing(summary.detail)
            messagebox.showwarning("Clear Trash unavailable", summary.detail, parent=self.root)
            return
        if not summary.eligible_count:
            message = "Trash is empty."
            if summary.items:
                message += " Unsafe or recovery-dependent entries were preserved."
            self._finish_processing(message)
            messagebox.showinfo("Clear Trash", message, parent=self.root)
            return
        size = format_size(summary.eligible_size_bytes)
        if not messagebox.askyesno(
            "Permanently clear managed Trash?",
            f"Permanently delete {summary.eligible_count} eligible file(s) ({size}) from "
            "recognized TidyFiles Trash batches? Unrecognized entries and files needed for "
            "transaction recovery will be preserved.",
            icon="warning",
            parent=self.root,
        ):
            self._finish_processing("Trash clear canceled.")
            return
        self._maintenance_kind = "trash_clear"
        self._set_processing_phase("trash_clearing", "Clearing eligible managed Trash…")
        self._start_processing_poll()

        def clear() -> None:
            try:
                result = self.trash_clear_service.clear(manually_authorized=True)
                payload = (result, None)
            except Exception as clear_error:
                payload = (None, clear_error)
            self._processing_done.put(("trash_clear", payload))

        threading.Thread(
            target=clear, name="TidyFiles-trash-clear", daemon=True
        ).start()

    def _handle_trash_clear(self, payload: Any) -> None:
        result, error = payload
        if error is not None or result is None:
            message = f"Trash clear failed: {error or 'unknown error'}"
            self._finish_processing(message)
            messagebox.showwarning("Clear Trash", message, parent=self.root)
            return
        cleanup = result.cleanup
        deleted = len(cleanup.deleted_paths)
        failures = cleanup.failures
        message = f"Trash clear deleted {deleted} item(s)."
        if cleanup.detail:
            message += f"\n{cleanup.detail}"
        if failures:
            message += "\n\nSome eligible entries could not be deleted:"
            message += "\n" + "\n".join(
                f"{item.path}: {item.reason or 'deletion failed'}" for item in failures[:12]
            )
        self._finish_processing(message)
        if failures or cleanup.detail:
            messagebox.showwarning("Trash clear results", message, parent=self.root)
        else:
            messagebox.showinfo("Trash clear complete", message, parent=self.root)

    def _start_recovery_inspection(self) -> None:
        if self._closing:
            return

        def inspect() -> None:
            try:
                result = self.recovery_inspector.inspect()
                payload = (result, None)
            except Exception as error:
                payload = (None, error)
            self._recovery_queue.put(payload)

        threading.Thread(target=inspect, name="TidyFiles-startup-recovery", daemon=True).start()
        self._recovery_after = self.root.after(50, self._poll_recovery_inspection)

    def _poll_recovery_inspection(self) -> None:
        self._recovery_after = None
        try:
            inspection, error = self._recovery_queue.get_nowait()
        except queue.Empty:
            if not self._closing:
                self._recovery_after = self.root.after(50, self._poll_recovery_inspection)
            return
        if self._closing:
            return
        if error is not None:
            self._show_recovery_inspection(None, error)
        elif inspection is not None:
            if inspection.transactions or inspection.storage_issues:
                self._show_recovery_inspection(inspection)

    def _show_recovery_inspection(
        self, inspection: RecoveryInspection | None, error: Exception | None = None
    ) -> None:
        if self._recovery_dialog is not None:
            try:
                if self._recovery_dialog.winfo_exists():
                    self._recovery_dialog.lift()
                    return
            except tk.TclError:
                pass
        dialog = ctk.CTkToplevel(self.root)
        self._recovery_dialog = dialog
        dialog.title("Recovery requires attention")
        dialog.geometry("800x560")
        dialog.minsize(620, 420)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        body = ctk.CTkFrame(dialog)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(
            body,
            text=(
                "TidyFiles found transaction recovery information. No files were restored, "
                "moved, or deleted. Review the recorded stages and paths below."
                if error is None
                else f"Recovery inspection could not complete: {error}"
            ),
            anchor="w",
            justify="left",
            wraplength=740,
        ).pack(fill="x", padx=8, pady=(8, 8))
        details = ctk.CTkTextbox(body, wrap="word")
        details.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        lines: list[str] = []
        paths: list[str] = []
        if inspection is not None:
            for issue in inspection.storage_issues:
                lines.append(f"Recovery storage: {issue.status.value} · {issue.path}")
                paths.append(str(issue.path))
                if issue.detail:
                    lines.append(f"  {issue.detail}")
            for transaction in inspection.transactions:
                operation = (
                    transaction.operation.value
                    if transaction.operation
                    else "unknown operation"
                )
                lines.append(
                    f"Transaction {transaction.transaction_id} · "
                    f"{operation} "
                    f"· {transaction.journal_status.value}"
                )
                for location in transaction.journal_locations:
                    paths.append(str(location.path))
                    lines.append(f"  Journal: {location.status.value} · {location.path}")
                    if location.detail:
                        lines.append(f"    {location.detail}")
                for entry in transaction.entries:
                    lines.append(
                        f"  {entry.source_path} → {entry.destination_path}"
                        f" · stage {entry.recorded_stage} · {entry.recorded_outcome.value}"
                    )
                    if entry.detail:
                        lines.append(f"    {entry.detail}")
                    for observed in entry.paths:
                        paths.append(observed.path)
                        lines.append(
                            f"    {observed.role}: {observed.path} · "
                            f"{observed.current.occupancy.value}"
                        )
                        if observed.current.reason:
                            lines.append(f"      {observed.current.reason}")
        details.insert("1.0", "\n".join(lines) or "No transaction details are available.")
        details.configure(state="disabled")
        path_frame = ctk.CTkFrame(body, fg_color="transparent")
        path_frame.pack(fill="x", padx=8, pady=(0, 8))
        self._recovery_paths = tuple(dict.fromkeys(paths))
        self._recovery_path_list = tk.Listbox(path_frame, height=6, exportselection=False)
        scrollbar = tk.Scrollbar(path_frame, command=self._recovery_path_list.yview)
        self._recovery_path_list.configure(yscrollcommand=scrollbar.set)
        self._recovery_path_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")
        for path in self._recovery_paths:
            self._recovery_path_list.insert("end", path)
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkButton(
            actions,
            text="Open selected location in File Explorer",
            command=self._open_selected_recovery_location,
        ).pack(side="left")
        ctk.CTkButton(actions, text="Close", width=90, command=dialog.destroy).pack(
            side="right"
        )
        dialog.grab_set()

    def _open_selected_recovery_location(self) -> None:
        selection = self._recovery_path_list.curselection()
        if not selection:
            return
        path = self._recovery_paths[selection[0]]
        target = Path(path)
        if not target.exists() and target.parent.exists():
            target = target.parent
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(target)])
        except OSError as error:
            messagebox.showerror(
                "Could not open File Explorer", str(error), parent=self._recovery_dialog
            )

    def open_preview_details(self, entry_id: UUID | None = None) -> None:
        entry_id = entry_id or self._context_entry_id
        if self._closing or entry_id is None:
            return
        diagnostic = self._preview_diagnostics_by_id.get(entry_id)
        operation_detail = self._processing_diagnostics_by_id.get(entry_id)
        entry = self._entries_by_id.get(entry_id)
        if (diagnostic is None and operation_detail is None) or entry is None:
            return
        if self._preview_details_dialog is not None:
            try:
                if self._preview_details_dialog.winfo_exists():
                    self._preview_details_dialog.deiconify()
                    self._preview_details_dialog.lift()
                    self._set_preview_detail_text(entry, diagnostic, operation_detail)
                    return
            except tk.TclError:
                pass
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Filename Preview Details")
        dialog.geometry("620x380")
        dialog.minsize(480, 300)
        dialog.transient(self.root)
        self._preview_details_dialog = dialog
        dialog.bind(
            "<Destroy>",
            lambda event, window=dialog: self._on_preview_dialog_destroy(
                event, window, "details"
            ),
            add="+",
        )
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._preview_details_textbox = ctk.CTkTextbox(dialog, wrap="word")
        self._preview_details_textbox.pack(fill="both", expand=True, padx=12, pady=12)
        self._set_preview_detail_text(entry, diagnostic, operation_detail)

    def _set_preview_detail_text(
        self,
        entry: CollectionEntry,
        diagnostic: Any | None,
        operation_detail: str | None = None,
    ) -> None:
        details = [
            f"Source filename: {entry.original_filename}",
            f"Source path: {entry.source_path}",
        ]
        if diagnostic is not None:
            details.extend(
                (f"Category: {diagnostic.kind.value}", f"Reason: {diagnostic.message}")
            )
            if diagnostic.token is not None:
                details.append(f"Token: {diagnostic.token}")
            if diagnostic.position is not None:
                details.append(f"Expression offset: {diagnostic.position}")
            if diagnostic.required_tokens:
                details.append(
                    "Required metadata tokens: " + ", ".join(diagnostic.required_tokens)
                )
        if operation_detail:
            details.append(f"Processing result: {operation_detail}")
        result = self._preview_result
        if result is not None:
            item = next(
                (row for row in result.entries if row.entry.entry_id == entry.entry_id),
                None,
            )
            if item is not None:
                file_result = item.file_result
                candidate = None if file_result is None else file_result.candidate_filename
                if file_result is not None and file_result.validation_error is not None:
                    candidate = file_result.validation_error.candidate_filename
                if candidate is not None:
                    details.append(f"Candidate: {candidate!r}")
        textbox = self._preview_details_textbox
        if textbox is not None and textbox.winfo_exists():
            textbox.configure(state="normal")
            textbox.delete("1.0", "end")
            textbox.insert("1.0", "\n\n".join(details))
            textbox.configure(state="disabled")

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
        self._display_positions.clear()
        self._preview_filename_ids.clear()
        self._preview_problem_ids.clear()
        self._known_size_bytes = 0
        self._unknown_size_count = 0
        for index, entry in enumerate(entries, start=1):
            item_id = str(entry.entry_id)
            self._entries_by_id[entry.entry_id] = entry
            self._display_order.append(entry.entry_id)
            self._display_positions[entry.entry_id] = index - 1
            self._account_size(entry, 1)
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=self._entry_values(index, entry),
                tags=("normal",),
            )
        self._preview_clear_applied = ("Imported", True)
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

    def _set_display_order(self, order: tuple[UUID, ...] | list[UUID]) -> None:
        self._display_order = list(order)
        self._display_positions = {
            entry_id: position for position, entry_id in enumerate(self._display_order)
        }

    def _selected_entry_ids(self) -> tuple[UUID, ...]:
        try:
            selected = {UUID(item) for item in self.tree.selection()}
        except ValueError:
            return ()
        return tuple(
            sorted(
                (entry_id for entry_id in selected if entry_id in self._display_positions),
                key=self._display_positions.__getitem__,
            )
        )

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
            self.refresh_preview_button.configure(state="disabled")
            self._configure_process_button(False)
            self.trash_clear_button.configure(state="disabled")
            if (
                self._processing_phase != "idle"
                or self._maintenance_kind is not None
                or self._closing
                or self._close_pending
            ):
                self.settings_button.configure(state="disabled")
                self.format_field.configure(state="disabled")
                self.single_folder_switch.configure(state="disabled")
                self.auto_clean_switch.configure(state="disabled")
                if hasattr(self, "auto_clear_trash_switch"):
                    self.auto_clear_trash_switch.configure(state="disabled")
            return

        self.controls["add_files"].configure(state="normal")
        self.controls["add_folder"].configure(state="normal")
        has_entries = bool(self._entries_by_id)
        selected = self._selected_entry_ids()
        selected_set = set(selected)
        self.controls["remove"].configure(
            state="normal" if selected else "disabled"
        )
        clearable = has_entries
        if not clearable:
            snapshot = self.collection.snapshot()
            clearable = bool(snapshot.import_sessions or snapshot.folder_roots)
        self.controls["clear_all"].configure(
            state="normal" if clearable else "disabled"
        )
        self.sort_dropdown.configure(state="normal")
        self.sort_direction_button.configure(state="normal")
        self.refresh_preview_button.configure(
            state=(
                "normal"
                if has_entries and self.rename_format.get()
                else "disabled"
            )
        )
        self._configure_process_button(self._process_ready())
        self.trash_clear_button.configure(state="normal")
        self.settings_button.configure(state="normal")
        self.format_field.configure(state="normal")
        self.single_folder_switch.configure(state="normal")
        self.auto_clean_switch.configure(state="normal")
        if hasattr(self, "auto_clear_trash_switch"):
            self.auto_clear_trash_switch.configure(state="normal")
        positions = [self._display_positions[entry_id] for entry_id in selected]
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

    def _configure_process_button(self, enabled: bool) -> None:
        theme = ctk.ThemeManager.theme["CTkButton"]
        disabled_color = ("gray82", "#3A3A3A")
        disabled_text = ("gray45", "gray65")
        self.process_button.configure(
            state="normal" if enabled else "disabled",
            fg_color=theme["fg_color"] if enabled else disabled_color,
            hover_color=theme["hover_color"] if enabled else disabled_color,
            text_color=theme["text_color"] if enabled else disabled_text,
            text_color_disabled=(
                theme["text_color_disabled"] if enabled else disabled_text
            ),
        )

    def _process_ready(self) -> bool:
        result = self._preview_result
        return bool(
            self.active_mode == "Bulk Rename"
            and self._entries_by_id
            and result is not None
            and result.state is EvaluationState.VALID
            and result.is_valid
            and self._preview_result_is_current(result)
            and not self.import_controller.busy
            and not self.collection.capabilities.is_importing
        )

    def _notify_collection_changed(self) -> None:
        self.root.collection_revision = self.collection.revision  # type: ignore[attr-defined]
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
        self._invalidate_filename_preview(schedule=False)
        if not self._collection_operation_pending:
            self._collection_operation_pending = True
        self._collection_render_order = order
        self._collection_render_selection = selection
        self._collection_render_offset = 0
        self._collection_render_notify = notify
        previous_positions = self._display_positions
        first_changed = 0
        common_limit = min(len(self._display_order), len(order))
        while (
            first_changed < common_limit
            and self._display_order[first_changed] == order[first_changed]
        ):
            first_changed += 1
        self._collection_render_previous_positions = previous_positions
        self._set_display_order(order)
        self._collection_render_offset = first_changed
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
                if self._collection_render_previous_positions.get(entry_id) != index:
                    self.tree.set(str(entry_id), "row_number", str(index + 1))
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
        if set(surviving) != set(self.tree.selection()):
            self.tree.selection_set(surviving)
        notify = self._collection_render_notify
        self._collection_render_previous_positions = {}
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
        self._display_positions.clear()
        self._preview_filename_ids.clear()
        self._preview_problem_ids.clear()
        self._preview_clear_applied = ("Imported", True)
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
            self._preview_import_invalidated = False
            if self._progress_running:
                self.progress.stop()
                self._progress_running = False
            self.progress.set(0)
            self.cancel_button.grid_remove()
            self._update_control_states()
            return

        if not self._preview_import_invalidated:
            self._preview_import_invalidated = True
            self._invalidate_filename_preview(
                schedule=False, invalidate_metadata=True
            )

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
            self._display_positions[entry.entry_id] = len(self._display_order) - 1
            self._account_size(entry, 1)
        self._update_summary()

    def _apply_order_chunk(
        self, order: tuple[UUID, ...], start: int, end: int, finalized: bool
    ) -> None:
        if start == 0:
            self.tree.set_children("", *(str(entry_id) for entry_id in order))
            self._collection_render_previous_positions = self._display_positions
            self._set_display_order(order)
        for position in range(start, end):
            entry_id = order[position]
            entry = self._entries_by_id.get(entry_id)
            if entry is None:
                raise RuntimeError(f"Collection row is missing for entry {entry_id}")
            item_id = str(entry_id)
            if self._collection_render_previous_positions.get(entry_id) != position:
                self.tree.set(item_id, "row_number", str(position + 1))
            if entry_id in self._preview_filename_ids:
                self.tree.set(item_id, "new_filename", "")
                self._preview_filename_ids.discard(entry_id)
            status = "Imported" if finalized else "Scanning"
            if self.tree.set(item_id, "status") != status:
                self.tree.set(item_id, "status", status)
            if entry_id in self._preview_problem_ids:
                self.tree.item(item_id, tags=("normal",))
                self._preview_problem_ids.discard(entry_id)
        if end == len(order):
            self._collection_render_previous_positions = {}

    def _remove_rows_chunk(self, entry_ids: tuple[UUID, ...]) -> None:
        for entry_id in entry_ids:
            item_id = str(entry_id)
            if self.tree.exists(item_id):
                self.tree.delete(item_id)
            self._preview_filename_ids.discard(entry_id)
            self._preview_problem_ids.discard(entry_id)

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
        self._display_positions = {
            entry_id: position for position, entry_id in enumerate(self._display_order)
        }
        selection = [
            str(entry_id)
            for entry_id in previous_view[1]
            if self.tree.exists(str(entry_id))
        ]
        current_selection = set(self.tree.selection())
        if set(selection) != current_selection:
            if selection:
                self.tree.selection_set(selection)
            elif current_selection:
                self.tree.selection_remove(*current_selection)
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
        try:
            self._context_entry_id = UUID(item_id)
        except ValueError:
            self._context_entry_id = None
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
        has_preview_error = self._context_entry_id in self._preview_diagnostics_by_id
        has_process_result = self._context_entry_id in self._processing_diagnostics_by_id
        self.context_menu.entryconfigure(
            self._context_menu_indices["preview_details"],
            state="normal" if has_preview_error or has_process_result else "disabled",
        )
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
        dialog.geometry("470x350")
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
        self.auto_clear_trash_switch = ctk.CTkSwitch(
            content,
            text="Auto-Clear Trash on normal exit",
            onvalue=True,
            offvalue=False,
            command=self._on_auto_clear_trash_changed,
        )
        if get_auto_clear_trash_on_exit(self.config):
            self.auto_clear_trash_switch.select()
        self.auto_clear_trash_switch.pack(anchor="w", padx=10, pady=(2, 4))
        ctk.CTkLabel(
            content,
            text=(
                "When enabled, recognized managed Trash batches are permanently cleared "
                "after a normal exit. It does not run at startup. Column choices, widths, "
                "and window placement save automatically."
            ),
            anchor="w",
            wraplength=420,
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
        job = self.coordinator.current
        if (
            (job is not None and job.modifying)
            or self._processing_phase
            in {"rename_modifying", "auto_cleaning", "trash_clearing", "exit_trash_clear"}
        ):
            self.status.configure(
                text="Wait for the active filesystem changes to finish before closing."
            )
            return
        if self._processing_phase != "idle":
            self._close_pending = True
            self._update_control_states()
            self.cancel_current_work()
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

    def _continue_close(self) -> None:
        if not self._close_pending or self._processing_phase != "idle":
            return
        if not self.import_controller.request_shutdown():
            self.status.configure(
                text="Waiting for discovery to stop before closing · source files are unchanged"
            )
            self._update_control_states()
            self.cancel_button.configure(state="disabled")
            return
        self._finish_close()

    def _finish_close(self) -> None:
        if self._closing:
            return
        if not self._exit_cleanup_done:
            self._exit_cleanup_done = True
            if get_auto_clear_trash_on_exit(self.config):
                self._close_pending = True
                self._maintenance_kind = "exit_trash_clear"
                self._set_processing_phase(
                    "exit_trash_clear", "Auto-Clear Trash is running before normal exit…"
                )
                self._start_processing_poll()

                def clear() -> None:
                    try:
                        result = self.trash_clear_service.clear(
                            auto_clear_enabled=True, normal_exit=True
                        )
                        payload = (result, None)
                    except Exception as error:
                        payload = (None, error)
                    self._processing_done.put(("exit_trash_clear", payload))

                threading.Thread(
                    target=clear, name="TidyFiles-exit-trash-clear", daemon=True
                ).start()
                return
        self._closing = True
        self.import_controller.dispose()
        self.preflight_service.close(wait=False)
        for name in (
            "_preview_after",
            "_preview_debounce_after",
            "_preview_clear_after",
            "_preview_render_after",
        ):
            self._cancel_preview_after(name)
        if self._recovery_after is not None:
            try:
                self.root.after_cancel(self._recovery_after)
            except tk.TclError:
                pass
            self._recovery_after = None
        if self._preview_service is not None:
            self._preview_service.close(wait=False)
            self._preview_service = None
        for dialog in (
            self._format_reference_dialog,
            self._preview_details_dialog,
            self._recovery_dialog,
            self._plan_review_dialog,
        ):
            if dialog is not None:
                try:
                    dialog.destroy()
                except tk.TclError:
                    pass
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

    def _handle_exit_trash_clear(self, payload: Any) -> None:
        result, error = payload
        self._maintenance_kind = None
        self._collection_operation_pending = False
        self._processing_phase = "idle"
        if error is not None or result is None:
            messagebox.showwarning(
                "Auto-Clear Trash failed",
                str(error or "Trash clear did not return a result."),
                parent=self.root,
            )
        elif result.cleanup.failures or result.cleanup.detail not in {
            None,
            "No eligible Trash items",
        }:
            details = result.cleanup.detail or "Some Trash entries could not be deleted."
            failed = "\n".join(
                f"{item.path}: {item.reason or 'deletion failed'}"
                for item in result.cleanup.failures[:12]
            )
            messagebox.showwarning(
                "Auto-Clear Trash results",
                details + (f"\n\n{failed}" if failed else ""),
                parent=self.root,
            )
        self._update_control_states()
        self._finish_close()


def create_main_window(
    config: dict[str, Any] | None = None,
    config_path: Path | str | None = None,
) -> ctk.CTk:
    """Create the GUI and its one shared, initially empty working collection."""
    return _MainWindow(config, config_path).root
