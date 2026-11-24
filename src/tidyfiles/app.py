"""Minimal CustomTkinter application shell."""

from __future__ import annotations

from tkinter import ttk

import customtkinter as ctk

_MODES = ("Bulk Rename", "Duplicate Finder", "Repacker")


def _style_file_table(root: ctk.CTk) -> None:
    dark = ctk.get_appearance_mode() == "Dark"
    background = "#242424" if dark else "#F5F5F5"
    header_background = "#333333" if dark else "#E7E7E7"
    foreground = "#F2F2F2" if dark else "#202020"
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
        relief="flat",
    )
    style.map(
        "TidyFiles.Treeview",
        background=[("selected", selection_background)],
        foreground=[("selected", "#FFFFFF")],
    )


def create_main_window() -> ctk.CTk:
    """Create the empty, non-operational M0-C window shell."""
    ctk.set_appearance_mode("System")
    root = ctk.CTk()
    root.title("TidyFiles")
    root.geometry("1000x680")
    root.minsize(760, 520)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(2, weight=1)

    title = ctk.CTkLabel(
        root,
        text="TidyFiles",
        font=ctk.CTkFont(size=24, weight="bold"),
        anchor="w",
    )
    title.grid(row=0, column=0, padx=20, pady=(16, 8), sticky="ew")

    tabs = ctk.CTkTabview(root, anchor="nw")
    tabs.grid(row=1, column=0, padx=16, pady=(0, 10), sticky="ew")
    for mode in _MODES:
        tab = tabs.add(mode)
        message = ctk.CTkLabel(
            tab,
            text=f"{mode} controls will be added in a later milestone.",
            anchor="w",
        )
        message.grid(row=0, column=0, padx=12, pady=10, sticky="ew")
        tab.grid_columnconfigure(0, weight=1)
    tabs.set(_MODES[0])

    table_frame = ctk.CTkFrame(root, corner_radius=8)
    table_frame.grid(row=2, column=0, padx=16, pady=(0, 10), sticky="nsew")
    table_frame.grid_columnconfigure(0, weight=1)
    table_frame.grid_rowconfigure(0, weight=1)

    _style_file_table(root)
    columns = ("name", "type", "size", "modified")
    table = ttk.Treeview(
        table_frame,
        columns=columns,
        show="headings",
        style="TidyFiles.Treeview",
        selectmode="extended",
    )
    for column, heading, width in (
        ("name", "Name", 420),
        ("type", "Type", 160),
        ("size", "Size", 120),
        ("modified", "Modified", 180),
    ):
        table.heading(column, text=heading)
        table.column(column, width=width, minwidth=80, stretch=column == "name")
    table.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)

    scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
    scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=10)
    table.configure(yscrollcommand=scrollbar.set)

    status = ctk.CTkLabel(root, text="Ready · No files loaded", anchor="w")
    status.grid(row=3, column=0, padx=20, pady=(0, 12), sticky="ew")
    return root
