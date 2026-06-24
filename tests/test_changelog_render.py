"""Unit tests for the /changelog markdown-to-cards renderer."""

from zira_dashboard.routes.changelog import _latest_deploy_when, _md_to_html


def test_structured_entry_has_groups_badge_title_and_key():
    md = (
        "# What's New\n\n"
        "Intro paragraph that should not render as a card.\n\n"
        "## 2026-06-24\n\n"
        "### 9:00 AM - Tasks redesign\n\n"
        "#### Features\n"
        "- **Date-only list views.** Tasks show a date in lists.\n"
        "#### Fixes\n"
        "- **Fixed empty deadline.** Odoo sent a blank date.\n"
    )

    out = _md_to_html(md)

    assert 'class="cl-entry"' in out
    assert 'data-key="2026-06-24T09:00"' in out
    assert 'data-feature="1"' in out
    assert '<span class="cl-entry-title">Tasks redesign</span>' in out
    assert '<span class="cl-entry-date">2026-06-24</span>' in out
    assert '<span class="cl-badge">New feature</span>' in out
    assert '<h4 class="cl-group-title">Features</h4>' in out
    assert '<h4 class="cl-group-title">Fixes</h4>' in out
    assert "<strong>Date-only list views.</strong>" in out
    assert 'class="cl-markread" data-key="2026-06-24T09:00"' in out


def test_legacy_prose_entry_is_highlights_with_no_badge():
    md = (
        "## 2026-06-09\n\n"
        "### 8:38 AM\n\n"
        "- **New Missed Punch Out alert.** Auto clock-out at midnight.\n"
        "- **Second note.** Another thing shipped.\n"
    )

    out = _md_to_html(md)

    assert 'data-key="2026-06-09T08:38"' in out
    assert 'data-feature="0"' in out
    assert 'class="cl-badge"' not in out
    assert '<h4 class="cl-group-title">Highlights</h4>' in out
    assert out.count("<li>") == 2


def test_untimed_deploy_falls_back_to_indexed_key():
    md = (
        "## 2026-06-01\n\n"
        "### Notes\n\n"
        "- **Something.** A change with no deploy time.\n"
    )

    out = _md_to_html(md)

    assert 'data-key="2026-06-01#0"' in out
    assert 'data-feature="0"' in out


def test_latest_deploy_ignores_titled_heading_suffix():
    md = (
        "## 2026-06-24\n\n"
        "### 10:11 AM - What's New cards\n\n"
        "- **Cards.** Changelog entries render as cards.\n"
    )

    assert _latest_deploy_when(md) == "2026-06-24T10:11"
