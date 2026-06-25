"""
Tests for the redesigned settings dialog.

Tests the settings registry, search scoring, and widget factory.
"""

import pytest

from src.ui.settings_dialog import (
    SettingDescriptor,
    SettingsDialog,
    SETTINGS_REGISTRY,
    _CATEGORY_ORDER,
)


class TestSettingDescriptor:
    """Tests for the SettingDescriptor dataclass."""

    def test_all_registry_entries_have_required_fields(self):
        for desc in SETTINGS_REGISTRY:
            assert desc.key, f"Missing key on {desc}"
            assert desc.label, f"Missing label on {desc.key}"
            assert desc.category, f"Missing category on {desc.key}"
            assert desc.subcategory, f"Missing subcategory on {desc.key}"
            assert desc.widget_type in (
                "path_dir",
                "path_file",
                "text",
                "spinbox",
                "combobox",
                "checkbox",
                "button",
                "readonly",
            ), f"Invalid widget_type on {desc.key}: {desc.widget_type}"

    def test_registry_keys_are_unique(self):
        keys = [d.key for d in SETTINGS_REGISTRY]
        assert len(keys) == len(set(keys)), "Duplicate keys found in SETTINGS_REGISTRY"

    def test_all_categories_are_in_order_list(self):
        categories = {d.category for d in SETTINGS_REGISTRY}
        for cat in categories:
            assert cat in _CATEGORY_ORDER, f"Category {cat!r} not in _CATEGORY_ORDER"


class TestSearchScoring:
    """Tests for the search match scoring logic."""

    def _score(self, desc, query):
        return SettingsDialog._match_score(None, desc, query)

    def test_exact_label_match_scores_high(self):
        desc = SettingDescriptor(
            key="test",
            label="Table font size",
            description="desc",
            category="Appearance",
            subcategory="Display",
            widget_type="spinbox",
            getter="get_x",
        )
        score = self._score(desc, "table font size")
        assert score >= 10

    def test_keyword_match_scores(self):
        desc = SettingDescriptor(
            key="test",
            label="Auto-round",
            description="desc",
            category="Editor",
            subcategory="Rounding",
            widget_type="checkbox",
            getter="get_x",
            keywords=["interpolation", "smooth"],
        )
        score = self._score(desc, "interpolation")
        assert score >= 7

    def test_no_match_returns_zero(self):
        desc = SettingDescriptor(
            key="test",
            label="Font size",
            description="text size",
            category="Appearance",
            subcategory="Display",
            widget_type="spinbox",
            getter="get_x",
        )
        assert self._score(desc, "zzzznotfound") == 0

    def test_prefix_match_bonus(self):
        desc = SettingDescriptor(
            key="test",
            label="Table font size",
            description="desc",
            category="Appearance",
            subcategory="Display",
            widget_type="spinbox",
            getter="get_x",
        )
        prefix_score = self._score(desc, "table")
        mid_score = self._score(desc, "font")
        assert prefix_score > mid_score

    def test_description_match(self):
        desc = SettingDescriptor(
            key="test",
            label="Auto-round",
            description="interpolation and smoothing results",
            category="Editor",
            subcategory="Rounding",
            widget_type="checkbox",
            getter="get_x",
        )
        score = self._score(desc, "smoothing")
        assert score >= 5


class TestHighlightText:
    """Tests for search text highlighting."""

    def test_highlights_match(self):
        result = SettingsDialog._highlight_text("Table font size", "font")
        assert "<span" in result
        assert "font" in result

    def test_no_match_returns_original(self):
        result = SettingsDialog._highlight_text("Table font size", "zzz")
        assert result == "Table font size"

    def test_empty_query_returns_original(self):
        result = SettingsDialog._highlight_text("Table font size", "")
        assert result == "Table font size"

    def test_case_insensitive_highlight(self):
        result = SettingsDialog._highlight_text("Table Font Size", "font")
        assert "<span" in result
