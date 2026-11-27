"""The scrollable probable duplicate review and its selection controls."""

from __future__ import annotations

import io
from collections.abc import Callable
from uuid import UUID

import customtkinter as ctk
from PIL import Image

from tidyfiles.perceptual_analysis import ProbableGroup


class ProbableDuplicatesDialog(ctk.CTkToplevel):
    def __init__(
        self,
        parent: ctk.CTk,
        groups: tuple[ProbableGroup, ...],
        thumbnails: dict[UUID, bytes],
        *,
        on_delete: Callable[[tuple[UUID, ...]], None],
        on_skip: Callable[[], None],
        format_size: Callable[[int], str],
    ) -> None:
        super().__init__(parent)
        self._checks: dict[UUID, ctk.CTkCheckBox] = {}
        self._auto_select: set[UUID] = {
            match.file.entry.entry_id for group in groups for match in group.members[1:]
        }
        self.title("Probable Duplicates Found")
        self.geometry("700x530")
        self.minsize(570, 360)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", on_skip)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        body = ctk.CTkScrollableFrame(self)
        body.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        for index, group in enumerate(groups, start=1):
            frame = ctk.CTkFrame(body)
            frame.grid(row=index, column=0, padx=4, pady=(1, 8), sticky="ew")
            frame.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(frame, text=f"Similar Group {index}").grid(
                row=0, column=0, columnspan=3, padx=8, sticky="w"
            )
            for row, match in enumerate(group.members, start=1):
                file = match.file
                entry = file.entry
                check = ctk.CTkCheckBox(frame, text="", width=24)
                check.grid(row=row, column=0, padx=(8, 2), pady=3)
                self._checks[entry.entry_id] = check
                label = (
                    f"{entry.original_filename}\n"
                    f"{format_size(file.size_bytes) if file.size_bytes is not None else '—'}"
                    f" · {match.similarity:.1f}% similar"
                )
                ctk.CTkLabel(frame, text=label, justify="left", wraplength=445).grid(
                    row=row, column=1, padx=4, pady=3, sticky="w"
                )
                if (data := thumbnails.get(entry.entry_id)) is not None:
                    with io.BytesIO(data) as stream, Image.open(stream) as decoded:
                        thumbnail = ctk.CTkImage(light_image=decoded.copy(), size=decoded.size)
                    ctk.CTkLabel(frame, text="", image=thumbnail).grid(
                        row=row, column=2, padx=8, pady=3
                    )
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=1, column=0, padx=12, pady=(4, 12), sticky="e")
        for text, command in (
            ("Auto-select", lambda: self._select(self._auto_select)),
            ("Select All", lambda: self._select(set(self._checks))),
            ("Select None", lambda: self._select(set())),
            ("Delete Selected", lambda: on_delete(self.selected_ids())),
            ("Skip", on_skip),
        ):
            ctk.CTkButton(actions, text=text, width=102, command=command).pack(
                side="left", padx=3
            )
        self.grab_set()

    def _select(self, selected: set[UUID]) -> None:
        for entry_id, check in self._checks.items():
            check.select() if entry_id in selected else check.deselect()

    def selected_ids(self) -> tuple[UUID, ...]:
        return tuple(entry_id for entry_id, check in self._checks.items() if check.get())
