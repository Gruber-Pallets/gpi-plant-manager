# Staffing Hours Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a read-only Staffing Hours tab that lets supervisors review actual clocked time or Odoo payroll hours over weekly, biweekly pay-period, monthly, and custom ranges.

**Architecture:** A new staffing_hours domain module owns date-range resolution, the configurable biweekly anchor, Odoo-batch verification, attendance/work-entry aggregation, and report view models. Narrow Odoo client reads supply normalized records; a new authenticated Staffing route renders the report with URL-backed filters and a small settings control keeps the pay-period anchor adjustable.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, vanilla JavaScript/CSS, PostgreSQL app_settings, Odoo XML-RPC, pytest.

## Global Constraints

- The feature is read-only: it must not write, approve, delete, or alter Odoo attendance, work entries, or payslip batches.
- All hour math and named ranges use shift_config.SITE_TZ and plant-local dates.
- The default payroll anchor is 2026-08-16 and the default cycle is 14 days.
- Odoo payroll-batch information verifies or overrides pay-period shortcuts; unavailable batch data falls back to the configured anchor with an explicit notice.
- A failed attendance, work-entry, or employee read must render a report error, never a silently partial report.
- Preserve existing user changes in the workspace; stage only files owned by the task.
- Add a short, child-friendly CHANGELOG.md entry for the shipped feature before its implementation push.

---

## File structure

| Path | Responsibility |
| --- | --- |
| src/zira_dashboard/staffing_hours.py | Pure date, pay-period, aggregation, filter, and report-view-model rules. |
| src/zira_dashboard/_odoo_attendance.py | Normalize Odoo attendance intervals that overlap a full selected range. |
| src/zira_dashboard/_odoo_payroll.py | Normalize payroll work entries, employee departments, and payslip-batch ranges. |
| src/zira_dashboard/odoo_client.py | Public, read-only façades for the new Odoo operations. |
| src/zira_dashboard/routes/staffing_hours.py | Validate query parameters, invoke the domain service, and render the Hours page. |
| src/zira_dashboard/routes/settings.py | Render and validate the pay-period anchor/cycle settings form. |
| src/zira_dashboard/templates/staffing_hours.html | Hours toolbar, table, summary filters, expandable daily details, and report errors. |
| src/zira_dashboard/templates/_staffing_subnav.html | Add the active-aware Hours tab. |
| src/zira_dashboard/templates/settings.html | Add the small payroll-period configuration form to Settings. |
| src/zira_dashboard/static/staffing_hours.css | Responsive report and details styling. |
| src/zira_dashboard/static/staffing_hours.js | Accessible row-detail toggling only; filtering remains server-rendered. |
| src/zira_dashboard/app.py | Import and register the Hours router. |
| tests/test_staffing_hours.py | Pure date, verification, aggregation, and filter tests. |
| tests/test_staffing_hours_route.py | Route query, context, error, and template rendering tests. |
| tests/test_staffing_hours_settings.py | Settings-form validation and persistence tests. |
| tests/test_odoo_attendance_for_day.py | Extend Odoo attendance tests for overlapping-range reads. |
| tests/test_odoo_payroll.py | Extend Odoo payroll tests for work-entry, department, and payslip-batch reads. |
| tests/test_staffing_static.py | Static assertions for the Staffing subnav and Hours assets. |
| CHANGELOG.md | One new plain-language entry describing the completed feature. |

## Task 1: Create the pay-period and hours-report domain module

**Files:**
- Create: src/zira_dashboard/staffing_hours.py
- Create: tests/test_staffing_hours.py

**Interfaces:**
- Consumes: app_settings.get_setting / app_settings.set_setting, plant-local date and datetime values, roster people with name and employee_id, and normalized Odoo records from Tasks 2–3.
- Produces: PayPeriodConfig, PayrollBatch, PeriodResolution, HoursReport, HoursRow, and the exact functions the route and Settings page call: current_pay_period_config(), save_pay_period_config(), resolve_hours_range(), and build_hours_report().

- [ ] **Step 1: Write failing configuration and range tests**

~~~python
def test_this_pay_period_uses_the_august_16_biweekly_anchor():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, date(2026, 8, 27), _no_batches
    )

    assert (result.start, result.end) == (date(2026, 8, 16), date(2026, 8, 29))
    assert result.verification == "anchor"


def test_odoo_payroll_batch_override_is_visible_not_silent():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, date(2026, 8, 27),
        lambda _start, _end: [
            hours.PayrollBatch("Run", date(2026, 8, 15), date(2026, 8, 28))
        ],
    )

    assert (result.start, result.end, result.verification) == (
        date(2026, 8, 15), date(2026, 8, 28), "odoo_override"
    )
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours.py -q

Expected: FAIL because staffing_hours and its public types do not exist.

- [ ] **Step 3: Implement typed configuration and named-range resolution**

~~~python
PAY_PERIOD_SETTING = "staffing_hours_pay_period"
DEFAULT_PAY_PERIOD = {"anchor": "2026-08-16", "cycle_days": 14}


@dataclass(frozen=True)
class PayPeriodConfig:
    anchor: date
    cycle_days: int


@dataclass(frozen=True)
class PayrollBatch:
    name: str
    start: date
    end: date


@dataclass(frozen=True)
class PeriodResolution:
    start: date
    end: date
    verification: str
    notice: str | None
    error: str | None


def current_pay_period_config() -> PayPeriodConfig:
    raw = app_settings.get_setting(PAY_PERIOD_SETTING)
    if not isinstance(raw, dict):
        raw = DEFAULT_PAY_PERIOD
    return _validated_config(str(raw.get("anchor", "")), raw.get("cycle_days"))


def save_pay_period_config(anchor_raw: str, cycle_raw: str) -> PayPeriodConfig:
    config = _validated_config(anchor_raw, cycle_raw)
    app_settings.set_setting(
        PAY_PERIOD_SETTING,
        {"anchor": config.anchor.isoformat(), "cycle_days": config.cycle_days},
    )
    return config


def resolve_hours_range(
    preset: str, start_raw: str | None, end_raw: str | None, today: date,
    load_batches: Callable[[date, date], Sequence[PayrollBatch | Mapping[str, object]]],
) -> PeriodResolution:
    config = current_pay_period_config()
    start, end = _preset_bounds(preset, start_raw, end_raw, today, config)
    return _verify_pay_period_range(preset, start, end, load_batches)
~~~

Implement all seven named presets. Use Monday–Sunday bounds for this_week and
last_week; full calendar bounds for month presets; inclusive custom dates; and
floor((today - anchor).days / cycle_days) for the current/pay-period index.
Only pay-period presets call load_batches. Deduplicate batches by (start, end):
an exact range is verified, one distinct overlap is odoo_override, no batch or
an Odoo-read exception is unverified, and two or more distinct non-exact
overlaps return an explicit conflict error. The verification helper converts
each normalized Odoo dictionary into PayrollBatch before comparing dates.

- [ ] **Step 4: Add failing aggregation and exception tests**

~~~python
def test_clocked_report_splits_an_open_shift_at_midnight():
    report = hours.build_hours_report(
        source="clocked", roster=[_person("Ana", 7)],
        start=date(2026, 8, 16), end=date(2026, 8, 17),
        now=datetime(2026, 8, 18, 2, tzinfo=UTC),
        attendances=[_attendance(7, "2026-08-17T04:00:00+00:00", None)],
        work_entries=[], departments={7: "Recycled"},
    )

    assert report.rows[0].daily == (
        (date(2026, 8, 16), 1.0), (date(2026, 8, 17), 21.0)
    )
    assert report.rows[0].needs_attention is True


def test_payroll_report_separates_regular_and_overtime():
    report = hours.build_hours_report(
        source="payroll", roster=[_person("Ana", 7)], start=START, end=END,
        now=NOW, attendances=[], departments={7: "Recycled"},
        work_entries=[
            _entry(7, START, "WORK100", 38), _entry(7, END, "OVERTIME", 3)
        ],
    )

    assert (report.rows[0].regular_hours, report.rows[0].overtime_hours) == (38, 3)
~~~

- [ ] **Step 5: Implement source-neutral report models and filtering**

~~~python
@dataclass(frozen=True)
class HoursRecord:
    day: date
    label: str
    hours: float
    is_open: bool


@dataclass(frozen=True)
class HoursRow:
    name: str
    employee_id: int
    department: str | None
    daily: Sequence[tuple[date, float]]
    regular_hours: float
    overtime_hours: float
    total_hours: float
    open_shift: bool
    conflicting_record: bool
    records: Sequence[HoursRecord]

    @property
    def needs_attention(self) -> bool:
        return self.open_shift or self.conflicting_record


@dataclass(frozen=True)
class HoursReport:
    rows: Sequence[HoursRow]
    team_total_hours: float
    available_departments: Sequence[str]


def build_hours_report(
    *, source: Literal["clocked", "payroll"], roster: Sequence[object],
    start: date, end: date, now: datetime, attendances: Sequence[Mapping[str, object]],
    work_entries: Sequence[Mapping[str, object]], departments: Mapping[int, str | None],
    query: str = "", department: str = "", attention: str = "all",
) -> HoursReport:
    rows = _aggregate_rows(
        source, roster, start, end, now, attendances, work_entries, departments
    )
    return _filter_and_sort_rows(rows, query, department, attention)
~~~

For clocked time, clip every interval to the report bounds in plant-local time,
substitute now only for an open interval, and split its minutes across local
midnights. For payroll, include only valid active WORK100 and OVERTIME entries
whose date is in bounds. Reject unknown sources and attention filters before any
aggregate is returned. Include all active roster people with an Odoo id, even
when their total is zero; sort default rows by descending total, then
case-insensitive name. Implement all, approaching_40 (>= 36 and < 40),
over_40 (>= 40), and attention filters. Preserve the record label and hours
for every daily source item: clocked rows use local check-in/check-out labels
and an open marker; payroll rows use Regular or Overtime labels.

- [ ] **Step 6: Run the complete pure-domain suite**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours.py -q

Expected: PASS.

- [ ] **Step 7: Commit**

~~~bash
git add src/zira_dashboard/staffing_hours.py tests/test_staffing_hours.py
git commit -m "feat: add staffing hours report domain"
~~~

## Task 2: Add exact, read-only Odoo range adapters

**Files:**
- Modify: src/zira_dashboard/_odoo_attendance.py
- Modify: src/zira_dashboard/_odoo_payroll.py
- Modify: src/zira_dashboard/odoo_client.py
- Modify: tests/test_odoo_attendance_for_day.py
- Modify: tests/test_odoo_payroll.py

**Interfaces:**
- Consumes: the source/date requirements defined by staffing_hours.HoursReport in Task 1.
- Produces: odoo_client.fetch_attendance_intervals_for_range(employee_ids, start, end), fetch_payroll_work_entries(employee_ids, start, end), fetch_employee_departments(employee_ids), and fetch_payroll_batches(start, end).

- [ ] **Step 1: Write failing Odoo-query contract tests**

~~~python
def test_range_attendance_query_includes_open_shift_started_before_range(monkeypatch):
    monkeypatch.setattr(odoo_client, "execute", _capture_and_return([OPEN_ROW]))

    rows = odoo_client.fetch_attendance_intervals_for_range(
        [7], date(2026, 8, 16), date(2026, 8, 29)
    )

    assert rows == [{
        "id": 1, "employee_odoo_id": 7,
        "check_in": "2026-08-15T23:00:00+00:00", "check_out": None,
    }]
    assert ("check_in", "<", "2026-08-30 05:00:00") in _last_domain()


def test_payslip_batch_read_normalizes_the_period(monkeypatch):
    monkeypatch.setattr(odoo_client, "execute", _capture_and_return([
        {"id": 8, "name": "Workers", "date_start": "2026-08-16", "date_end": "2026-08-29"},
    ]))

    batch = odoo_client.fetch_payroll_batches(date(2026, 8, 16), date(2026, 8, 29))[0]
    assert (batch["start"], batch["end"]) == (date(2026, 8, 16), date(2026, 8, 29))
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_odoo_attendance_for_day.py tests/test_odoo_payroll.py -q

Expected: FAIL because the four range façade methods do not exist.

- [ ] **Step 3: Implement private Odoo queries and public façades**

~~~python
# _odoo_attendance.py
def fetch_attendance_intervals_for_range(execute_fn, employee_ids, start_day, end_day):
    start_local = datetime.combine(start_day, _time.min, tzinfo=shift_config.SITE_TZ)
    stop_local = datetime.combine(end_day + timedelta(days=1), _time.min, tzinfo=shift_config.SITE_TZ)
    domain = [
        "&", "&",
        ("employee_id", "in", sorted({int(i) for i in employee_ids})),
        ("check_in", "<", to_odoo_dt(stop_local)),
        "|", ("check_out", "=", False), ("check_out", ">", to_odoo_dt(start_local)),
    ]
    rows = execute_fn(
        "hr.attendance", "search_read", domain,
        fields=["id", "employee_id", "check_in", "check_out"],
        order="employee_id,check_in,id",
    )
    return [
        {
            "id": int(row["id"]),
            "employee_odoo_id": _unwrap_m2o(row["employee_id"]),
            "check_in": odoo_dt_to_iso(row["check_in"]),
            "check_out": odoo_dt_to_iso(row.get("check_out")),
        }
        for row in rows
        if row.get("id") and _unwrap_m2o(row.get("employee_id"))
        and odoo_dt_to_iso(row.get("check_in")) and not is_zero_duration_attendance(row)
    ]


# _odoo_payroll.py
def fetch_work_entries_for_range(execute_fn, employee_ids, start_day, end_day):
    _by_code, codes_by_id = _type_maps(execute_fn)
    rows = execute_fn(
        "hr.work.entry", "search_read",
        [("active", "=", True), ("employee_id", "in", sorted(set(employee_ids))),
         ("date", ">=", start_day.isoformat()), ("date", "<=", end_day.isoformat())],
        fields=_WORK_FIELDS, order="employee_id,date,id",
    )
    return [_normalize_work(row, codes_by_id) for row in rows]


def fetch_employee_departments(execute_fn, employee_ids):
    rows = execute_fn(
        "hr.employee", "search_read", [("id", "in", sorted(set(employee_ids)))],
        fields=["id", "department_id"],
    )
    return {
        int(row["id"]): _m2o_name(row.get("department_id")) or None
        for row in rows if row.get("id")
    }


def fetch_payslip_batches(execute_fn, start_day, end_day):
    rows = execute_fn(
        "hr.payslip.run", "search_read",
        [("date_start", "<=", end_day.isoformat()), ("date_end", ">=", start_day.isoformat())],
        fields=["id", "name", "date_start", "date_end"], order="date_start,id",
    )
    return [
        {"name": str(row.get("name") or ""), "start": date.fromisoformat(row["date_start"]),
         "end": date.fromisoformat(row["date_end"])}
        for row in rows if row.get("date_start") and row.get("date_end")
    ]


# odoo_client.py
def fetch_attendance_intervals_for_range(employee_ids, start_day, end_day):
    return _odoo_attendance.fetch_attendance_intervals_for_range(
        execute, employee_ids, start_day, end_day
    )


def fetch_payroll_work_entries(employee_ids, start_day, end_day):
    return _odoo_payroll.fetch_work_entries_for_range(
        execute, employee_ids, start_day, end_day
    )


def fetch_employee_departments(employee_ids):
    return _odoo_payroll.fetch_employee_departments(execute, employee_ids)


def fetch_payroll_batches(start_day, end_day):
    return _odoo_payroll.fetch_payslip_batches(execute, start_day, end_day)
~~~

Use Odoo search_read only. Query payroll batches with an overlap domain on
hr.payslip.run.date_start/date_end; return normalized name, start, and end
dates. Query employee departments with hr.employee.department_id for the
report roster ids. Do not add any Odoo write method and do not change the
existing payroll-guard input contract.

- [ ] **Step 4: Run focused adapter regression tests**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_odoo_attendance_for_day.py tests/test_odoo_payroll.py tests/test_staffing_hours.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add src/zira_dashboard/_odoo_attendance.py src/zira_dashboard/_odoo_payroll.py src/zira_dashboard/odoo_client.py tests/test_odoo_attendance_for_day.py tests/test_odoo_payroll.py
git commit -m "feat: read staffing hours from odoo"
~~~

## Task 3: Make payroll-period configuration editable in Settings

**Files:**
- Modify: src/zira_dashboard/routes/settings.py
- Modify: src/zira_dashboard/templates/settings.html
- Create: tests/test_staffing_hours_settings.py

**Interfaces:**
- Consumes: staffing_hours.current_pay_period_config() and save_pay_period_config(anchor_raw, cycle_raw) from Task 1.
- Produces: a Settings context key staffing_hours_pay_period and a POST-only settings/staffing-hours-pay-period endpoint that redirects to settings?saved=1&section=timeclock.

- [ ] **Step 1: Write failing Settings tests**

~~~python
def test_hours_pay_period_form_renders_current_anchor(monkeypatch):
    monkeypatch.setattr(
        settings.staffing_hours, "current_pay_period_config",
        lambda: PayPeriodConfig(date(2026, 8, 16), 14),
    )
    context = _render_settings_context(monkeypatch)

    assert context["staffing_hours_pay_period"]["anchor"] == "2026-08-16"


def test_hours_pay_period_post_rejects_invalid_anchor(monkeypatch):
    response = asyncio.run(settings.settings_save_staffing_hours_pay_period(
        _request({"anchor": "not-a-date", "cycle_days": "14"})
    ))

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours_settings.py -q

Expected: FAIL because the route, context, and form do not exist.

- [ ] **Step 3: Implement configuration form and validated save route**

~~~python
@router.post("/settings/staffing-hours-pay-period")
async def settings_save_staffing_hours_pay_period(request: Request):
    form = await request.form()
    try:
        staffing_hours.save_pay_period_config(
            form.get("anchor", ""), form.get("cycle_days", "")
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/settings?section=timeclock&error={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse("/settings?saved=1&section=timeclock", status_code=303)
~~~

Render date and number inputs inside the existing Timeclock settings section.
Require a valid ISO date and a positive integer cycle no greater than 31. Seed
no database row: current_pay_period_config() must keep returning the approved
August 16 / 14-day default until an administrator saves a replacement.
Import quote_plus alongside the existing urlencode import in settings.py.

- [ ] **Step 4: Run Settings suite**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours_settings.py tests/test_settings_auto_work_centers.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/settings.html tests/test_staffing_hours_settings.py
git commit -m "feat: configure staffing hours pay period"
~~~

## Task 4: Build the authenticated Staffing Hours page

**Files:**
- Create: src/zira_dashboard/routes/staffing_hours.py
- Create: src/zira_dashboard/templates/staffing_hours.html
- Create: src/zira_dashboard/static/staffing_hours.css
- Create: src/zira_dashboard/static/staffing_hours.js
- Modify: src/zira_dashboard/templates/_staffing_subnav.html
- Modify: src/zira_dashboard/app.py
- Create: tests/test_staffing_hours_route.py
- Modify: tests/test_staffing_static.py

**Interfaces:**
- Consumes: Task 1 range/report API, Task 2 Odoo read façades, and Task 3 persisted configuration.
- Produces: GET /staffing/hours, query parameters source, range, start, end, q, department, and attention, plus a new active == hours Staffing subnav item.

- [ ] **Step 1: Write failing route and static-template tests**

~~~python
def test_hours_route_preserves_filters_and_renders_clocked_total(monkeypatch):
    _stub_hours_dependencies(monkeypatch)

    response = TestClient(app).get(
        "/staffing/hours?source=clocked&range=this_pay_period&q=Ana&attention=over_40"
    )

    assert response.status_code == 200
    assert "Ana" in response.text and "42.0" in response.text
    assert "source=clocked" in response.text
    assert "attention=over_40" in response.text


def test_staffing_subnav_has_hours_tab():
    html = Path("src/zira_dashboard/templates/_staffing_subnav.html").read_text()

    assert 'href="/staffing/hours"' in html
    assert "active == 'hours'" in html
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours_route.py tests/test_staffing_static.py -q

Expected: FAIL because the router, template, and assets do not exist.

- [ ] **Step 3: Implement route with complete-or-error data loading**

~~~python
@router.get("/staffing/hours", response_class=HTMLResponse)
def staffing_hours(
    request: Request, source: str = Query("clocked"), range: str = Query("this_week"),
    start: str | None = Query(None), end: str | None = Query(None), q: str = Query(""),
    department: str = Query(""), attention: str = Query("all"),
):
    resolution = hours.resolve_hours_range(
        range, start, end, plant_today(), odoo_client.fetch_payroll_batches
    )
    if resolution.error:
        return _render_hours(
            request, source=source, selected_range=range, start=start, end=end,
            query=q, department=department, attention=attention,
            resolution=resolution, report=None, error=resolution.error,
        )
    try:
        report = _load_complete_report(source, resolution, q, department, attention)
    except Exception:
        log.exception("staffing hours report failed")
        return _render_hours(
            request, source=source, selected_range=range, start=start, end=end,
            query=q, department=department, attention=attention,
            resolution=resolution, report=None,
            error="Hours could not be refreshed. Try again soon.",
        )
    return _render_hours(
        request, source=source, selected_range=range, start=start, end=end,
        query=q, department=department, attention=attention,
        resolution=resolution, report=report, error=None,
    )
~~~

_load_complete_report must load the current active, non-reserve roster first,
then call the minimum necessary Odoo reads for the selected source and employee
ids. Any source-data failure takes the error path; only payroll-batch failure
is handled by the resolver unverified-anchor fallback. Pass active=hours, the
selected filters, available departments, range labels, report rows, and the
period-verification notice to staffing_hours.html.

- [ ] **Step 4: Implement server-rendered UI and accessible details**

~~~html
<details class="hours-row-detail">
  <summary aria-label="Show daily hours for {{ row.name }}">{{ row.name }}</summary>
  <table>
    <caption>{{ row.name }} daily hours</caption>
    <tbody>
      {% for record in row.records %}
      <tr>
        <th scope="row">{{ record.day }}</th>
        <td>{{ record.label }}{% if record.is_open %} (clocked in){% endif %}</td>
        <td>{{ "%.2f"|format(record.hours) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</details>
~~~

Use GET links/forms for the source switch, range chips, filter controls, and
custom-range popover. Keep current query fields on every range/source link. Use
a responsive table with tabular numeric totals; show regular/overtime columns
only for payroll. Render an explicit source label, open-shift badge, Odoo
verification/unverified/override notice, team total, zero-result message, and
the three summary-filter chips. CSS must collapse gracefully on small screens;
JavaScript may enhance details focus/escape behavior but must not be required
to reveal totals.

- [ ] **Step 5: Register router and run page-level tests**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours_route.py tests/test_staffing_static.py tests/test_base_app_template.py -q

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add src/zira_dashboard/routes/staffing_hours.py src/zira_dashboard/templates/staffing_hours.html src/zira_dashboard/static/staffing_hours.css src/zira_dashboard/static/staffing_hours.js src/zira_dashboard/templates/_staffing_subnav.html src/zira_dashboard/app.py tests/test_staffing_hours_route.py tests/test_staffing_static.py
git commit -m "feat: add staffing hours review"
~~~

## Task 5: Verify the integrated feature and publish the user-facing note

**Files:**
- Modify: CHANGELOG.md
- Modify: tests/test_staffing_hours.py
- Modify: tests/test_staffing_hours_route.py

**Interfaces:**
- Consumes: the finished report route, pure domain module, Odoo adapters, and Settings route from Tasks 1–4.
- Produces: verified end-to-end behavior and a short What’s New note suitable for a 10-year-old.

- [ ] **Step 1: Add integration tests for approved end-to-end cases**

~~~python
def test_hours_route_uses_anchor_when_odoo_has_no_batch(monkeypatch):
    _stub_hours_dependencies(monkeypatch, batches=[])

    response = TestClient(app).get("/staffing/hours?range=this_pay_period")

    assert "Aug 16, 2026 – Aug 29, 2026" in response.text
    assert "Odoo has not verified this pay period yet" in response.text


def test_hours_route_refuses_to_show_partial_data(monkeypatch):
    _stub_hours_dependencies(monkeypatch, attendance_error=RuntimeError("Odoo down"))

    response = TestClient(app).get("/staffing/hours?source=clocked")

    assert "Hours could not be refreshed. Try again soon." in response.text
    assert "Ana" not in response.text
~~~

- [ ] **Step 2: Run test to verify integration safety**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours.py tests/test_staffing_hours_route.py -q

Expected: PASS.

- [ ] **Step 3: Add child-friendly changelog entry**

~~~markdown
### See weekly work hours

- **Staffing now has an Hours tab.** Pick this week, a pay period, a month, or your own dates to see how many hours each person worked. You can also switch between clocked time and payroll time, so it is easier to spot overtime or a missing clock-out.
~~~

Insert the entry at the top of the current release section without editing
historical notes. Do not mention routes, Odoo models, or implementation details.

- [ ] **Step 4: Run focused regression suite and static checks**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_hours.py tests/test_staffing_hours_route.py tests/test_staffing_hours_settings.py tests/test_odoo_attendance_for_day.py tests/test_odoo_payroll.py tests/test_staffing_static.py -q

Expected: PASS with no skipped failure caused by the new feature.

- [ ] **Step 5: Inspect diff and commit verification work**

~~~bash
git diff --check
git status --short
git add CHANGELOG.md tests/test_staffing_hours.py tests/test_staffing_hours_route.py
git commit -m "docs: explain staffing hours review"
~~~

Confirm only Task 5 files are staged; do not accidentally add unrelated
workspace changes.
