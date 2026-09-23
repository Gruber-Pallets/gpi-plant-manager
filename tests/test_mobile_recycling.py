"""Render phone cards without loading application services or a database."""
from pathlib import Path

import pytest
from html.parser import HTMLParser
from xml.etree.ElementTree import Element, SubElement
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).resolve().parents[1] / 'src/zira_dashboard/templates'


class Markup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.root = Element('root')
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = SubElement(self.stack[-1], tag, dict((k, v or '') for k, v in attrs))
        if tag not in {'input', 'br', 'hr', 'img', 'meta', 'link'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_data(self, data):
        node = self.stack[-1]
        if len(node):
            node[-1].tail = (node[-1].tail or '') + data
        else:
            node.text = (node.text or '') + data


def nodes(node, cls):
    return [n for n in node.iter() if cls in n.get('class', '').split()]


def one(node, cls):
    return next(iter(nodes(node, cls)), None)


def words(node):
    return ' '.join(' '.join(node.itertext()).split())


def render_mobile(**overrides):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    context = dict(
        total_units=270, pph_per_person=90, pph_per_person_ex_d4=80,
        uptime_pct=94, elapsed_minutes=180, refreshed_at='10:00 AM',
        is_today=True, is_range=False, range_includes_today=True,
        dismantler_bars=[], repair_bars=[], downtime_rows=[],
        dismantler_progress=[], repair_progress=[],
        dismantler_group_target=0, repair_group_target=0,
        dismantler_people=1, repair_people=1, operator_links_by_wc={},
        assignments_todo_by_wc={}, today='2026-09-23',
        can_operate=lambda: True,
    )
    context.update(overrides)
    return Markup(env.get_template('_recycling_mobile.html').render(**context)).root


def bar(units, expected, **extra):
    return dict(name='Repair 1', who='Alex <Rivera>', units=units, expected=expected, **extra)


@pytest.mark.parametrize('units, expected, state, width', [
    (90, 100, 'below', 75), (100, 100, 'met', 83.3333),
    (110, 100, 'met', 91.6667), (140, 100, 'met', 100),
    (0, 100, 'below', 0), (99.5, 100, 'below', 82.9167),
])
def test_live_goal_scale_and_strict_color(units, expected, state, width):
    html = render_mobile(repair_bars=[bar(units, expected)])
    row = one(html, 'rm-production-row')
    assert state in row.get('class')
    fill = one(row, 'rm-production-fill')
    assert float(fill.get('style').split(':')[1].strip('%; ')) == pytest.approx(width, abs=.001)
    assert one(row, 'rm-now').text.strip() == 'Now'
    assert 'ahead' not in words(row) and 'Goal so far' not in words(row)
    assert words(one(row, 'rm-identity')) == 'Alex <Rivera> Repair 1'
    assert row.find('rivera') is None


def test_missing_goal_is_neutral_not_red_or_invented():
    row = one(render_mobile(repair_bars=[bar(12, 0)]), 'rm-production-row')
    assert 'no-goal' in row.get('class')
    assert one(row, 'rm-now') is None
    assert 'No goal available' in words(row)


def test_historical_and_multi_operator_labels():
    operators = [dict(person_name='Alex', physically_present=True), dict(person_name='Jordan', physically_present=False)]
    row = one(render_mobile(repair_bars=[bar(100, 100, current_operators=operators)]), 'rm-production-row')
    assert words(one(row, 'rm-identity')) == 'Alex + Jordan Repair 1'
    row = one(render_mobile(is_today=False, is_range=True, repair_bars=[bar(100, 100)]), 'rm-production-row')
    assert one(row, 'rm-now').text.strip() == 'Goal'
    assert 'Alex' not in words(one(row, 'rm-identity'))


def test_downtime_sorted_shared_scale_and_names_inside_rows():
    html = render_mobile(downtime_rows=[dict(name='Repair 1', who='Alex', down=5), dict(name='Repair 2', who='Jordan', down=20)])
    rows = nodes(html, 'rm-downtime-row')
    assert [one(r, 'rm-downtime-minutes').text.strip() for r in rows] == ['20 min', '5 min']
    assert 'Jordan' in words(rows[0]) and 'Repair 2' in words(rows[0])
    widths = [float(one(r, 'rm-downtime-fill').get('style').split(':')[1].strip('%; ')) for r in rows]
    assert widths[0] == widths[1] * 4
    assert '6% downtime during tracked time' not in words(html)


def test_empty_view_and_worker_history_remain_available():
    assert 'No production data' in words(render_mobile())
    html = render_mobile(repair_bars=[bar(100, 100, segments=[dict(person_label='Earlier operator', time_label='7–8 AM', actual_units=40, goal_units=50)])])
    assert 'Earlier operator' in words(one(html, 'rm-production-row').find('details'))


def test_summary_metrics_remain_in_collapsed_details():
    html = render_mobile(
        repair_bars=[bar(90, 100), bar(50, 100)],
        downtime_rows=[dict(name='Repair 1', down=5), dict(name='Repair 2', down=20)],
        elapsed_minutes=180,
    )
    totals = one(html, 'rm-group-totals')
    assert 'open' not in totals.attrib
    assert '140 pallets · goal 200 · 70.0% of goal' in words(totals)
    downtime = next(d for d in html.iter('details') if d.find('summary').text == 'About downtime')
    assert 'open' not in downtime.attrib
    assert 'Total downtime: 25 min · Shift elapsed: 180 min' in words(downtime)


def test_progress_details_use_authoritative_group_goals():
    buckets = [dict(label='07:15', actual=10, target=5, in_progress=True)]
    html = render_mobile(
        dismantler_progress=buckets, repair_progress=buckets,
        dismantler_group_target=800, repair_group_target=400,
    )
    details = nodes(html, 'rm-intervals')
    assert len(details) == 2
    assert 'Goal 800 /hr · 200 /15 min' in words(details[0])
    assert 'Goal 400 /hr · 100 /15 min' in words(details[1])
    for detail in details:
        assert 'open' not in detail.attrib
        assert '07:15 · current' in words(detail)
        assert '10' in words(detail) and '5' in words(detail)


def test_zero_group_goal_remains_neutral_in_summary():
    totals = one(render_mobile(repair_bars=[bar(12, 0)]), 'rm-group-totals')
    assert '12 pallets · No goal available' in words(totals)
    assert '% of goal' not in words(totals)


def test_operator_navigation_is_in_details_and_uses_authoritative_links():
    html = render_mobile(
        repair_bars=[bar(100, 100)],
        downtime_rows=[dict(name='Repair 1', who='Alex', down=5)],
        operator_links_by_wc={'Repair 1': '/wc/repair-1?day=2026-09-23'},
    )
    assert all(identity.find('a') is None for identity in nodes(html, 'rm-identity'))
    links = nodes(html, 'rm-operator-link')
    assert len(links) == 2
    assert all(link.get('href') == '/wc/repair-1?day=2026-09-23' for link in links)
    assert all(any(link in list(detail.iter()) for detail in html.iter('details')) for link in links)
    assert not nodes(render_mobile(repair_bars=[bar(100, 100)]), 'rm-operator-link')
