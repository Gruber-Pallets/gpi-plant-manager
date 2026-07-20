"""The scheduler cell label macro must preserve the disambiguated roster
label produced by ``odoo_sync._roster_names``.

Regression: two people whose Odoo names both start "Jesus M..." are stored
in ``people.name`` as distinct compact labels ("Jesus Ma." / "Jesus Mo."),
but the ``scheduled_operator_name`` macro re-abbreviated the last token to a
single initial, collapsing both back to an identical "Jesus M." on the
staffing grid. No data fix could survive that render-time truncation.
"""

from pathlib import Path

from jinja2 import Environment


def _operator_name_macro():
    """Render the REAL ``scheduled_operator_name`` macro from the template.

    We slice the macro definition straight out of ``staffing.html`` so the
    test executes the shipped macro, not a copy.
    """
    html = Path("src/zira_dashboard/templates/staffing.html").read_text()
    start = html.index("{% macro scheduled_operator_name")
    end = html.index("endmacro", start)
    end = html.index("%}", end) + len("%}")
    macro_src = html[start:end]
    tmpl = Environment().from_string(
        macro_src + "{{ scheduled_operator_name(name) }}"
    )
    return lambda name: tmpl.render(name=name)


def test_disambiguated_roster_labels_render_distinctly():
    render = _operator_name_macro()

    ma = render("Jesus Ma.")
    mo = render("Jesus Mo.")

    assert ma == "Jesus Ma."
    assert mo == "Jesus Mo."
    assert ma != mo


def test_deeper_collision_label_is_not_truncated():
    render = _operator_name_macro()

    # Three-way collision expands the surname further; the extra letters
    # must survive display.
    assert render("Jesus More.") == "Jesus More."
    assert render("Jesus Mora.") == "Jesus Mora."


def test_single_token_name_is_unchanged():
    render = _operator_name_macro()

    assert render("Humberto") == "Humberto"


def test_ordinary_two_token_label_is_preserved():
    render = _operator_name_macro()

    # people.name is already the canonical compact label ("First L."), so it
    # must be shown verbatim rather than re-abbreviated.
    assert render("Adrian A.") == "Adrian A."
