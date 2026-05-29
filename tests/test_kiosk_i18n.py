"""Kiosk t() helper: English-only passthrough, bilingual stacked markup,
format substitution, and graceful fallback for unknown strings."""
from __future__ import annotations

from markupsafe import Markup

from zira_dashboard import kiosk_i18n


class _Ctx(dict):
    """Stand-in for a Jinja context object (supports .get)."""


def _render(text, bilingual, **kw):
    return kiosk_i18n.t(_Ctx(bilingual=bilingual), text, **kw)


def test_english_only_passthrough():
    assert _render("Clock Out", False) == "Clock Out"


def test_bilingual_stacks_english_then_spanish():
    out = str(_render("Clock Out", True))
    assert '<span class="k-en">Clock Out</span>' in out
    assert '<span class="k-es">Marcar salida</span>' in out
    assert out.index("k-en") < out.index("k-es")


def test_unknown_string_falls_back_to_english():
    assert _render("Totally unknown label", True) == "Totally unknown label"


def test_format_substitution_both_languages():
    out = str(_render("Since {time}", True, time="2:30 PM"))
    assert "Since 2:30 PM" in out
    assert "Desde 2:30 PM" in out


def test_substituted_value_is_escaped():
    out = str(_render("Since {time}", True, time="<x>"))
    assert "<x>" not in out
    assert "&lt;x&gt;" in out


def test_every_translation_value_is_nonempty():
    assert all(v.strip() for v in kiosk_i18n.TRANSLATIONS.values())
