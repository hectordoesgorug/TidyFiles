from __future__ import annotations

from tidyfiles.config import default_config
from tidyfiles.ui_preferences import (
    DEFAULT_VISIBLE_COLUMNS,
    MODE_COLUMNS,
    MODES,
    REQUIRED_COLUMNS,
    default_column_preferences,
    get_appearance_mode,
    get_column_preferences,
    restore_window_geometry,
    set_column_preferences,
)


def test_each_mode_has_the_approved_default_columns() -> None:
    assert default_column_preferences("Bulk Rename").visible == (
        "row_number",
        "filename",
        "new_filename",
        "size",
        "status",
    )
    assert default_column_preferences("Duplicate Finder").visible == (
        "row_number",
        "filename",
        "size",
        "duplicate_of",
        "status",
    )
    assert default_column_preferences("Repacker").visible == (
        "row_number",
        "filename",
        "new_filename",
        "size",
        "resolution",
        "length",
        "status",
    )
    assert set(MODES) == set(DEFAULT_VISIBLE_COLUMNS)
    assert default_column_preferences("Bulk Rename").widths["filename"] == 280
    assert default_column_preferences("Duplicate Finder").widths["filename"] == 320


def test_saved_columns_are_mode_specific_and_keep_hidden_widths() -> None:
    config = default_config()
    config["global_settings"]["unrelated"] = "keep"
    set_column_preferences(
        config,
        "Bulk Rename",
        ("filename", "extension", "unknown"),
        {"filename": 475, "extension": 96},
    )

    rename = get_column_preferences(config, "Bulk Rename")
    duplicate = get_column_preferences(config, "Duplicate Finder")
    assert rename.visible == ("filename", "new_filename", "status", "extension")
    assert rename.widths["filename"] == 475
    assert rename.widths["extension"] == 96
    assert duplicate.visible == default_column_preferences("Duplicate Finder").visible
    assert config["global_settings"]["unrelated"] == "keep"


def test_invalid_saved_columns_recover_required_fields_and_safe_widths() -> None:
    config = default_config()
    config["global_settings"]["column_preferences"] = {
        "bulk_rename": {
            "visible": ["row_number", "unknown", 42],
            "widths": {
                "filename": 500,
                "extension": 20,
                "status": True,
                "unknown": 100,
            },
        },
        "duplicate_finder": {"visible": "broken", "widths": []},
    }

    rename = get_column_preferences(config, "Bulk Rename")
    duplicate = get_column_preferences(config, "Duplicate Finder")
    assert set(rename.visible) == {"row_number", *REQUIRED_COLUMNS["Bulk Rename"]}
    assert rename.widths["filename"] == 500
    assert rename.widths["extension"] == 85
    assert rename.widths["status"] == 130
    assert duplicate == default_column_preferences("Duplicate Finder")


def test_set_columns_ignores_unknown_ids_and_preserves_unrelated_modes() -> None:
    config = default_config()
    set_column_preferences(
        config,
        "Repacker",
        ("full_path", "group"),
        {"full_path": 700, "group": 250},
    )

    repacker = get_column_preferences(config, "Repacker")
    assert repacker.visible == (
        "filename",
        "new_filename",
        "status",
        "full_path",
    )
    assert repacker.widths["full_path"] == 700
    assert "group" not in MODE_COLUMNS["Repacker"]
    assert get_column_preferences(config, "Bulk Rename") == default_column_preferences(
        "Bulk Rename"
    )


def test_invalid_appearance_preference_defaults_to_system() -> None:
    config = default_config()
    config["global_settings"]["appearance_mode"] = ["invalid"]
    assert get_appearance_mode(config) == "System"
    config["global_settings"]["appearance_mode"] = "Dark"
    assert get_appearance_mode(config) == "Dark"


def test_saved_geometry_is_parsed_and_clamped_to_virtual_screen() -> None:
    bounds = (-1920, -120, 3840, 1200)

    assert restore_window_geometry("1600x900+120+90", bounds) == "1600x900+120+90"
    assert restore_window_geometry("1200x800+5000+5000", bounds) == "1200x800+720+280"
    assert restore_window_geometry("broken", bounds) == "1000x680+100+100"
    assert restore_window_geometry("100x100-4000-4000", bounds) == "760x520-1920-120"

