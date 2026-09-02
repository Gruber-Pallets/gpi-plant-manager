# Odoo-owned 2s review workflow setup

Status: audited configuration; duplicate-database verification pending.

This runbook defines the Odoo 19 configuration for one authoritative 2s
Improvement row and one linked task. It is deliberately fail-closed. Keep every
application entry point disabled until all duplicate-database checks pass.

## 2026-09-02 architecture decision

Use the native Odoo Studio webhook and immediate authenticated application
readback. Odoo's public `/web/hook/<rule_uuid>` controller executes the
automation and returns exactly `{"status":"ok"}` on success. The acknowledgement
is never proof of final state. The calling application's server immediately
reads the exact task and reference through authenticated JSON-RPC, then builds
and validates `ReviewActionResult` locally.

Official Odoo 19 evidence:

- [Webhook controller](https://github.com/odoo/odoo/blob/19.0/addons/base_automation/controllers/main.py)
- [Automation implementation](https://github.com/odoo/odoo/blob/19.0/addons/base_automation/models/base_automation.py)
- [Safe evaluation implementation](https://github.com/odoo/odoo/blob/19.0/odoo/tools/safe_eval.py)
- [Odoo 19 automation documentation](https://www.odoo.com/documentation/19.0/applications/studio/automated_actions.html)

Accept only HTTP 200 with the exact JSON object `{"status":"ok"}` as an
acknowledgement. For a deliberately invalid negative exercise, accept only the
native HTTP 500 with exact JSON `{"status":"error"}` as a known Odoo rejection.
Every other HTTP status or body—including 502/503/504 gateway responses and
malformed JSON—is an unknown outcome, not a known rejection. This classification
is shared by both call paths: a positive caller that receives the exact known
rejection, or a negative caller that receives the exact known acknowledgement,
fails with a definitive response mismatch rather than an unknown outcome. A
mismatched, missing, duplicate, or stale readback is failure or conflict;
neither application invents success. After a timeout or unknown response, read
the exact identity first. If positive-action readback proves the transition
landed, return that authoritative state. Otherwise do not retry or clean up
until the caller has handled the unresolved outcome.

## Permanent contract

Use these exact technical names and values.

| Purpose | Exact value |
| --- | --- |
| Reference model | `x_2s_improvements` |
| Task model | `project.task` |
| Project model | `project.project` |
| Stage model | `project.task.type` |
| User model | `res.users` |
| Employee model | `hr.employee` |
| Review project | `GPI OS Manager - TASKS` |
| Review initial stage | `General` |
| Review meeting stage | `L10` |
| Plant coding project | `Plant Manager` |
| Sales coding project | `Sales Manager` |
| Initial coding stage | `New` |
| Initial assignee login | `dale@gruberpallets.com` |
| Creation rule | `GPI 2s: Create and Link Task` |
| Review rule | `GPI 2s: Review Action Webhook` |
| Digital lifecycle rule | `GPI 2s: Sync Digital Lifecycle` |

The reference fields are:

| Field | Meaning |
| --- | --- |
| `x_name` | Display title |
| `x_studio_source` | Submitting application |
| `x_studio_source_id` | Stable identity inside that application |
| `x_studio_type` | Exact feedback type |
| `x_studio_status` | Authoritative reference state |
| `x_studio_submitted_by` | Active submitting `hr.employee` |
| `x_studio_completed_by` | Active acting `hr.employee` for a terminal action |
| `x_studio_date_start` | Submission date |
| `x_studio_date_stop` | Terminal date or time |
| `x_studio_notes` | Original request text; lifecycle actions never change it |
| `x_studio_image` | Original image; lifecycle actions never change it |
| `x_studio_after_image` | Result image |
| `x_studio_linked_task` | Exact authoritative `project.task` |
| `x_studio_linked_wo` | Must stay empty for review types |

`x_studio_type` must have exactly these five stored values:

1. `Digital`
2. `Digital - New Feature`
3. `Physical - Issue`
4. `Physical - Suggestion`
5. `2s Improvement`

`x_studio_status` must have exactly `Requested`, `In-Progress`, `Completed`,
and `Declined`. Supported Source values are exactly `GPI Plant Manager` and
`GPI Sales Manager`.

## Local secrets and gates

Copy these names to the server secret manager. Never paste the generated URL
into browser code, screenshots, support messages, fixtures, command output, or
version control.

```dotenv
ODOO_REVIEW_ACTION_WEBHOOK_URL=
ODOO_REVIEW_TEST_DB_UUID=
ODOO_FEEDBACK_WORKFLOW_V2_ENABLED=false
ODOO_IMPROVEMENTS_EXPECTED_COMPANY=
```

The dedicated read credentials remain the existing
`ODOO_IMPROVEMENTS_URL`, `ODOO_IMPROVEMENTS_DB`,
`ODOO_IMPROVEMENTS_LOGIN`, and `ODOO_IMPROVEMENTS_API_KEY`. The production UUID
fence remains `ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID`.

The RPC user must be able to read `database.uuid`, its own `company_id`, the
exact company row, model/field metadata, projects, task stages, users,
employees, automation rules, and their related `ir.actions.server` rows. The
read-only audit requests only these exposed rule fields: `id`, `name`, `active`,
`model_id`, `trigger`, `filter_domain`, `trigger_field_ids`, `record_getter`,
`log_webhook_calls`, `action_server_ids`, and `webhook_uuid`; and only these
related action fields: `id`, `state`, `code`, and `model_id`. The duplicate
exercise additionally needs create/write access to disposable improvement/task
rows and archive access to those same rows. Do not grant broader production
write access for this checker.

Use this gate order without skipping a step:

1. Keep `ODOO_FEEDBACK_WORKFLOW_V2_ENABLED=false` in every application.
2. Duplicate the approved Odoo database using the normal Odoo process.
3. Put only duplicate-database credentials in the operator shell.
4. Read `database.uuid` from that duplicate and store the exact canonical value
   in `ODOO_REVIEW_TEST_DB_UUID`.
5. Store the authenticated user's exact current company name in
   `ODOO_IMPROVEMENTS_EXPECTED_COMPANY` and confirm it is the approved duplicate
   company.
6. Generate the webhook URL from the one inspected review rule. Its origin must
   equal `ODOO_IMPROVEMENTS_URL`, and its final UUID must equal that rule's
   stored `webhook_uuid`. `ODOO_IMPROVEMENTS_URL` must be a database-specific
   duplicate origin; a shared multi-database origin without deterministic host
   routing is unsupported and must not be exercised.
7. Run the read-only checker. It must report V2, one project, both stages, one
   active Dale login, three enabled rules, audited related actions, matched
   company, bound webhook, and unique source identities.
8. Run the guarded duplicate exercise and retain the safe count-only output.
9. Run each application's V2 preflight while its UI gate remains false.
10. Deploy Odoo-authoritative inbound lifecycle synchronization and the native
   acknowledgement plus exact-readback action clients.
11. Enable the UI gate one application at a time and verify one exact source
    identity end to end.

## Duplicate-database evidence record

Record evidence in the deployment ticket, not in this repository. Record only:

- duplicate created-at time and approver;
- canonical duplicate UUID fingerprint approved by the operator;
- checker commit SHA;
- fixed checker lines and exit code;
- four disposable rows, four linked tasks, and five action kinds verified;
- cleanup result showing only those disposable rows and tasks were archived;
- application preflight exit codes;
- the person who approved production rollout.

Do not record credentials, the full webhook URL, request bodies, user API keys,
or original improvement notes.

## Rule 1: create and link one task

Create one active Automation Rule with these exact settings:

| Setting | Exact value |
| --- | --- |
| Name | `GPI 2s: Create and Link Task` |
| Model | `x_2s_improvements` |
| Trigger | On create and edit (`on_create_or_write`) |
| Apply on | domain below |
| When updating | six fields below |
| Action | Execute Code |

Apply on:

```python
[
    ('x_studio_source', 'in', ['GPI Plant Manager', 'GPI Sales Manager']),
    ('x_studio_status', '=', 'Requested'),
    ('x_studio_linked_task', '=', False),
    ('x_studio_linked_wo', '=', False),
]
```

When updating must contain exactly:

- `x_studio_source`
- `x_studio_source_id`
- `x_studio_type`
- `x_studio_status`
- `x_studio_linked_task`
- `x_studio_linked_wo`

Paste this complete Execute Code block. It resolves record IDs from exact names
and rejects missing or ambiguous configuration. Odoo runs the task creation and
reference write in the same transaction.

```python
SUPPORTED_SOURCES = ('GPI Plant Manager', 'GPI Sales Manager')
DIGITAL_TYPES = ('Digital', 'Digital - New Feature')
REVIEW_TYPES = ('Physical - Issue', 'Physical - Suggestion', '2s Improvement')
DIGITAL_PROJECTS = {
    'GPI Plant Manager': 'Plant Manager',
    'GPI Sales Manager': 'Sales Manager',
}

source = record.x_studio_source
source_id = record.x_studio_source_id
submission_type = record.x_studio_type
if source not in SUPPORTED_SOURCES:
    raise UserError('Unsupported 2s Improvement Source.')
if not source_id or not source_id.strip():
    raise UserError('The 2s Improvement Source ID is required.')

matches = env['x_2s_improvements'].with_context(active_test=False).search([
    ('x_studio_source', '=', source),
    ('x_studio_source_id', '=', source_id),
], limit=2)
if len(matches) != 1 or matches.id != record.id:
    raise UserError('Source and Source ID must identify exactly one 2s Improvement.')

if record.x_studio_linked_task:
    task = record.x_studio_linked_task
else:
    if submission_type in REVIEW_TYPES:
        if record.x_studio_linked_wo:
            raise UserError('A review item cannot have a linked work order.')
        project_name = 'GPI OS Manager - TASKS'
        stage_name = 'General'
    elif submission_type in DIGITAL_TYPES:
        project_name = DIGITAL_PROJECTS[source]
        stage_name = 'New'
    else:
        raise UserError('Unsupported 2s Improvement Type.')

    projects = env['project.project'].with_context(active_test=False).search([
        ('name', '=', project_name),
        ('active', '=', True),
    ], limit=2)
    if len(projects) != 1:
        raise UserError('The target project must resolve exactly once.')

    stages = env['project.task.type'].with_context(active_test=False).search([
        ('name', '=', stage_name),
        ('project_ids', 'in', [projects.id]),
        ('active', '=', True),
    ], limit=2)
    if len(stages) != 1:
        raise UserError('The target project stage must resolve exactly once.')

    dale_users = env['res.users'].with_context(active_test=False).search([
        ('login', '=ilike', 'dale@gruberpallets.com'),
    ], limit=2)
    if (
        len(dale_users) != 1
        or not dale_users.active
        or dale_users.login.lower() != 'dale@gruberpallets.com'
    ):
        raise UserError('The Dale user must resolve to one active exact login.')

    task = env['project.task'].create({
        'name': record.x_name or ('%s: %s' % (submission_type, source_id)),
        'project_id': projects.id,
        'stage_id': stages.id,
        'user_ids': [(6, 0, [dale_users.id])],
        'state': '01_in_progress',
    })
    link_values = {'x_studio_linked_task': task.id}
    if submission_type in DIGITAL_TYPES:
        link_values['x_studio_status'] = 'In-Progress'
    record.write(link_values)
    task.message_post(
        body='Created from Source %s, Source ID %s, Type %s.' % (
            source,
            source_id,
            submission_type,
        ),
        subtype_xmlid='mail.mt_note',
    )

action = {'linkedTaskId': task.id}
```

If the custom model supports a database-level SQL constraint, add a unique
constraint over `(x_studio_source, x_studio_source_id)` before enabling this
rule. Odoo Studio Online does not expose a portable way to install that SQL
constraint. The code therefore rejects duplicates before creation and the
checker scans all supported source identities. That search is not a substitute
for a database constraint during two simultaneous creates; keep application
submission gates closed until the deployment owner records which safeguard the
target edition provides.

## Rule 2: one review action webhook

Create one Automation Rule with these exact settings:

| Setting | Exact value |
| --- | --- |
| Name | `GPI 2s: Review Action Webhook` |
| Model | `x_2s_improvements` |
| Trigger | On webhook (`on_webhook`) |
| Apply on | empty (`[]`) |
| When updating | empty |
| Log Calls | off, because request bodies contain user notes |
| Target Record | code below |
| Action | Execute Code |

Target Record:

```python
model.with_context(active_test=False).search([
    ('x_studio_source', '=', payload.get('source')),
    ('x_studio_source_id', '=', payload.get('sourceId')),
    ('x_studio_linked_task', '=', int(payload.get('taskId') or 0)),
], limit=1)
```

The request and success result contract is:

```ts
export type ReviewActionRequest = {
  taskId: number;
  source: "GPI Plant Manager" | "GPI Sales Manager";
  sourceId: string;
  action: "accept" | "decline" | "assign" | "complete" | "move_l10";
  actorUserId: number;
  assigneeUserId?: number;
  note?: string;
};

export type ReviewActionResult = {
  ok: true;
  task: { id: number; state: string; stageId: number; assigneeUserIds: number[] };
  improvement: { id: number; status: string; linkedTaskId: number; dateStop: string | null };
};
```

This block validates the whole request before writing, locks both records,
encodes the transition table once, changes task and reference in one
transaction, and checks fresh internal reads before Odoo sends its native
acknowledgement. The calling application performs the separate authoritative
JSON-RPC readback described above.

```python
ALLOWED_KEYS = {
    'taskId', 'source', 'sourceId', 'action', 'actorUserId',
    'assigneeUserId', 'note',
}
REQUIRED_KEYS = {'taskId', 'source', 'sourceId', 'action', 'actorUserId'}
ALLOWED_SOURCES = ('GPI Plant Manager', 'GPI Sales Manager')
ALLOWED_ACTIONS = ('accept', 'decline', 'assign', 'complete', 'move_l10')
REVIEW_TYPES = ('Physical - Issue', 'Physical - Suggestion', '2s Improvement')
MAX_NOTE_LENGTH = 2000

if not isinstance(payload, dict):
    raise UserError('The review request must be one JSON object.')
keys = set(payload.keys())
if keys - ALLOWED_KEYS or REQUIRED_KEYS - keys:
    raise UserError('The review request fields do not match the contract.')

task_id = payload.get('taskId')
actor_user_id = payload.get('actorUserId')
assignee_user_id = payload.get('assigneeUserId')
source = payload.get('source')
source_id = payload.get('sourceId')
action_name = payload.get('action')
note_value = payload.get('note')

if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
    raise UserError('taskId must be a positive integer.')
if isinstance(actor_user_id, bool) or not isinstance(actor_user_id, int) or actor_user_id <= 0:
    raise UserError('actorUserId must be a positive integer.')
if assignee_user_id is not None and (
    isinstance(assignee_user_id, bool)
    or not isinstance(assignee_user_id, int)
    or assignee_user_id <= 0
):
    raise UserError('assigneeUserId must be a positive integer when supplied.')
if source not in ALLOWED_SOURCES:
    raise UserError('The review Source is not supported.')
if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 255:
    raise UserError('The review Source ID is invalid.')
if action_name not in ALLOWED_ACTIONS:
    raise UserError('The review action is not supported.')
if note_value is not None and not isinstance(note_value, str):
    raise UserError('The review note must be text.')
note = note_value.strip() if isinstance(note_value, str) else ''
if len(note) > MAX_NOTE_LENGTH:
    raise UserError('The review note is too long.')
if action_name in ('decline', 'complete') and not note:
    raise UserError('Decline and Complete require a note.')
if action_name == 'assign' and assignee_user_id is None:
    raise UserError('Assign requires assigneeUserId.')
if action_name != 'assign' and assignee_user_id is not None:
    raise UserError('assigneeUserId is allowed only for Assign.')

references = env['x_2s_improvements'].with_context(active_test=False).search([
    ('x_studio_source', '=', source),
    ('x_studio_source_id', '=', source_id),
], limit=2)
if len(references) != 1:
    raise UserError('Source and Source ID must identify exactly one review item.')
reference = references
if not record or record.id != reference.id:
    raise UserError('The webhook target does not match the exact review item.')
env.cr.execute(
    'SELECT id FROM x_2s_improvements WHERE id = %s FOR UPDATE',
    [reference.id],
)
reference.invalidate_recordset()
if 'active' not in reference._fields or not reference.active:
    raise UserError('The review item is archived or lacks the required active field.')

tasks = env['project.task'].with_context(active_test=False).search([
    ('id', '=', task_id),
], limit=2)
if len(tasks) != 1 or not tasks.active:
    raise UserError('The linked review task is unavailable.')
task = tasks
env.cr.execute(
    'SELECT id FROM project_task WHERE id = %s FOR UPDATE',
    [task.id],
)
task.invalidate_recordset()
if not task.active:
    raise UserError('The linked review task became unavailable.')

if reference.x_studio_source != source or reference.x_studio_source_id != source_id:
    raise UserError('The review Source identity does not match.')
if reference.x_studio_type not in REVIEW_TYPES:
    raise UserError('The linked item is not a review Type.')
if reference.x_studio_linked_wo:
    raise UserError('A review item cannot have a linked work order.')
if not reference.x_studio_linked_task or reference.x_studio_linked_task.id != task.id:
    raise UserError('The linked task does not match taskId.')

projects = env['project.project'].with_context(active_test=False).search([
    ('name', '=', 'GPI OS Manager - TASKS'),
    ('active', '=', True),
], limit=2)
if len(projects) != 1 or task.project_id.id != projects.id:
    raise UserError('The review task project does not match.')
general_stages = env['project.task.type'].with_context(active_test=False).search([
    ('name', '=', 'General'),
    ('project_ids', 'in', [projects.id]),
    ('active', '=', True),
], limit=2)
l10_stages = env['project.task.type'].with_context(active_test=False).search([
    ('name', '=', 'L10'),
    ('project_ids', 'in', [projects.id]),
    ('active', '=', True),
], limit=2)
if len(general_stages) != 1 or len(l10_stages) != 1:
    raise UserError('The review stages must resolve exactly once in the review project.')
actor_users = env['res.users'].with_context(active_test=False).search([
    ('id', '=', actor_user_id),
    ('active', '=', True),
], limit=2)
actor_employees = env['hr.employee'].with_context(active_test=False).search([
    ('user_id', '=', actor_user_id),
    ('active', '=', True),
], limit=2)
if len(actor_users) != 1 or len(actor_employees) != 1:
    raise UserError('The actor must map to one active user and employee.')
if actor_user_id not in task.user_ids.ids:
    raise UserError('The actor must be currently assigned to the review task.')

current_status = reference.x_studio_status
if current_status in ('Completed', 'Declined'):
    raise UserError('The review item is already terminal.')
if current_status == 'Requested':
    if action_name not in ('accept', 'decline'):
        raise UserError('Requested allows only Accept or Decline.')
    if task.state != '01_in_progress' or task.stage_id.id != general_stages.id:
        raise UserError('Requested requires the open task in General.')
if current_status == 'In-Progress':
    if action_name not in ('assign', 'complete', 'move_l10'):
        raise UserError('In-Progress does not allow that action.')
    if (
        task.state != '03_approved'
        or task.stage_id.id not in (general_stages.id, l10_stages.id)
    ):
        raise UserError('In-Progress requires an approved task in an allowed stage.')
if current_status not in ('Requested', 'In-Progress'):
    raise UserError('The review item has an unknown state.')
if reference.x_studio_date_stop or reference.x_studio_completed_by:
    raise UserError('A nonterminal review item cannot contain terminal fields.')

target_user = env['res.users']
target_employee = env['hr.employee']
if action_name == 'assign':
    target_users = env['res.users'].with_context(active_test=False).search([
        ('id', '=', assignee_user_id),
        ('active', '=', True),
    ], limit=2)
    target_employees = env['hr.employee'].with_context(active_test=False).search([
        ('user_id', '=', assignee_user_id),
        ('active', '=', True),
    ], limit=2)
    if len(target_users) != 1 or len(target_employees) != 1:
        raise UserError('The assignee must map to one active user and employee.')
    target_user = target_users
    target_employee = target_employees

stop_value = False
if action_name in ('decline', 'complete'):
    stop_field_type = reference._fields['x_studio_date_stop'].type
    if stop_field_type == 'date':
        stop_value = datetime.date.today().isoformat()
    elif stop_field_type == 'datetime':
        stop_value = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    else:
        raise UserError('The completion date field has an unsupported type.')

original_stage_id = task.stage_id.id
original_assignee_ids = sorted(task.user_ids.ids)

# Odoo's native webhook controller catches action errors. Explicitly roll back
# every request write before re-raising so its HTTP 500 cannot commit a partial
# task/reference/chatter transition. This try/except uses allowed safe-eval
# bytecode; a with-statement does not.
try:
    if action_name == 'accept':
        task.write({'state': '03_approved'})
        reference.write({'x_studio_status': 'In-Progress'})
        chatter = 'Accepted by employee %s.' % actor_employees.id
    elif action_name == 'decline':
        task.write({'state': '1_canceled'})
        reference.write({
            'x_studio_status': 'Declined',
            'x_studio_date_stop': stop_value,
            'x_studio_completed_by': actor_employees.id,
        })
        chatter = 'Declined by employee %s. Reason: %s' % (actor_employees.id, note)
    elif action_name == 'assign':
        task.write({'user_ids': [(6, 0, [target_user.id])]})
        chatter = 'Assigned by employee %s to employee %s.' % (
            actor_employees.id,
            target_employee.id,
        )
    elif action_name == 'complete':
        task.write({'state': '1_done'})
        reference.write({
            'x_studio_status': 'Completed',
            'x_studio_date_stop': stop_value,
            'x_studio_completed_by': actor_employees.id,
        })
        chatter = 'Completed by employee %s. Result: %s' % (actor_employees.id, note)
    else:
        task.write({'stage_id': l10_stages.id})
        chatter = 'Moved to L10 by employee %s.' % actor_employees.id

    task.message_post(body=chatter, subtype_xmlid='mail.mt_note')
    task.invalidate_recordset()
    reference.invalidate_recordset()
    task_values = task.read(['id', 'state', 'stage_id', 'user_ids'])[0]
    reference_values = reference.read([
        'id', 'x_studio_status', 'x_studio_linked_task', 'x_studio_date_stop',
    ])[0]
    if task_values['id'] != task.id:
        raise UserError('The review task internal readback did not match.')
    if (
        not reference_values['x_studio_linked_task']
        or reference_values['x_studio_linked_task'][0] != task.id
    ):
        raise UserError('The review relationship internal readback did not match.')
    if action_name == 'accept':
        expected_state = '03_approved'
        expected_status = 'In-Progress'
        expected_stage_id = original_stage_id
        expected_assignee_ids = original_assignee_ids
        expected_stop = False
    elif action_name == 'decline':
        expected_state = '1_canceled'
        expected_status = 'Declined'
        expected_stage_id = original_stage_id
        expected_assignee_ids = original_assignee_ids
        expected_stop = stop_value
    elif action_name == 'assign':
        expected_state = '03_approved'
        expected_status = 'In-Progress'
        expected_stage_id = original_stage_id
        expected_assignee_ids = [target_user.id]
        expected_stop = False
    elif action_name == 'complete':
        expected_state = '1_done'
        expected_status = 'Completed'
        expected_stage_id = original_stage_id
        expected_assignee_ids = original_assignee_ids
        expected_stop = stop_value
    else:
        expected_state = '03_approved'
        expected_status = 'In-Progress'
        expected_stage_id = l10_stages.id
        expected_assignee_ids = original_assignee_ids
        expected_stop = False
    actual_stop = reference_values['x_studio_date_stop'] or False
    if (
        task_values['state'] != expected_state
        or task_values['stage_id'][0] != expected_stage_id
        or sorted(task_values['user_ids']) != expected_assignee_ids
        or reference_values['x_studio_status'] != expected_status
        or actual_stop != expected_stop
    ):
        raise UserError('The review transition internal readback did not match.')
except Exception:
    env.cr.rollback()
    raise
```

After Odoo returns HTTP 200 and exactly `{"status":"ok"}`, the application
server reads by exact `x_studio_source` plus `x_studio_source_id`, requires one
active reference, requires its `x_studio_linked_task` to equal `taskId`, and
reads that exact active task. It then constructs the documented
`ReviewActionResult` from only `id`, `state`, `stage_id`, `user_ids`, `x_studio_status`,
`x_studio_linked_task`, and `x_studio_date_stop`. It returns success only if
that result matches the requested transition. The webhook URL and actor/source
fields never reach browser code.

The transition table represented by that one block is:

| Current reference | Action | Task write | Reference write |
| --- | --- | --- | --- |
| Requested | accept | `state=03_approved` | `In-Progress` |
| Requested | decline | `state=1_canceled` | `Declined`, stop, closer |
| In-Progress | assign | exact one active assignee | unchanged |
| In-Progress | complete | `state=1_done` | `Completed`, stop, closer |
| In-Progress | move_l10 | exact `L10` stage | unchanged |
| Completed or Declined | any | none; conflict | none; conflict |

## Rule 3: digital task lifecycle

Create one active rule on `project.task` named
`GPI 2s: Sync Digital Lifecycle`. Use On create and edit with When updating set
to exactly `state`. Apply on is empty because the authoritative relationship is
resolved in code, not by task title.

Paste this Execute Code block:

```python
DIGITAL_TYPES = ('Digital', 'Digital - New Feature')
TERMINAL_TASK_STATES = ('1_done', '1_canceled')

for task in records:
    references = env['x_2s_improvements'].with_context(active_test=False).search([
        ('x_studio_linked_task', '=', task.id),
        ('x_studio_type', 'in', DIGITAL_TYPES),
    ], limit=2)
    if len(references) > 1:
        raise UserError('A digital task is linked to more than one 2s Improvement.')
    if len(references) == 1:
        reference = references
        env.cr.execute(
            'SELECT id FROM x_2s_improvements WHERE id = %s FOR UPDATE',
            [reference.id],
        )
        reference.invalidate_recordset()
        if task.state == '1_done':
            employees = env['hr.employee'].with_context(active_test=False).search([
                ('user_id', '=', env.user.id),
                ('active', '=', True),
            ], limit=2)
            if len(employees) != 1:
                raise UserError('The digital closer must map to one active employee.')
            stop_type = reference._fields['x_studio_date_stop'].type
            if stop_type == 'date':
                stop_value = datetime.date.today().isoformat()
            elif stop_type == 'datetime':
                stop_value = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            else:
                raise UserError('The completion date field has an unsupported type.')
            reference.write({
                'x_studio_status': 'Completed',
                'x_studio_date_stop': stop_value,
                'x_studio_completed_by': employees.id,
            })
            task.message_post(
                body='Digital improvement completed by employee %s.' % employees.id,
                subtype_xmlid='mail.mt_note',
            )
        elif task.state == '1_canceled':
            employees = env['hr.employee'].with_context(active_test=False).search([
                ('user_id', '=', env.user.id),
                ('active', '=', True),
            ], limit=2)
            if len(employees) != 1:
                raise UserError('The digital closer must map to one active employee.')
            stop_type = reference._fields['x_studio_date_stop'].type
            if stop_type == 'date':
                stop_value = datetime.date.today().isoformat()
            elif stop_type == 'datetime':
                stop_value = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            else:
                raise UserError('The completion date field has an unsupported type.')
            reference.write({
                'x_studio_status': 'Declined',
                'x_studio_date_stop': stop_value,
                'x_studio_completed_by': employees.id,
            })
            task.message_post(
                body='Digital improvement cancelled by employee %s.' % employees.id,
                subtype_xmlid='mail.mt_note',
            )
        elif reference.x_studio_status == 'Requested':
            reference.write({'x_studio_status': 'In-Progress'})
            task.message_post(
                body='Digital improvement work started.',
                subtype_xmlid='mail.mt_note',
            )
```

## Versioned action audit

The checker requires exactly one related Execute Code action on each rule. It
normalizes CRLF to LF, removes trailing whitespace from each line, trims outer
whitespace, and compares SHA-256. These hashes identify the complete blocks in
this runbook; they are not secrets.

| Rule | Model | Action type | Audited code SHA-256 |
| --- | --- | --- | --- |
| `GPI 2s: Create and Link Task` | `x_2s_improvements` | `code` | `c31f9d2c3cb8bb7147cf4f1babf828abd25edb7a23d12ed4ed8e04c1bd1d1208` |
| `GPI 2s: Review Action Webhook` | `x_2s_improvements` | `code` | `1c5b32f2f2a8bc269ceca77621896a6ccb473fda5c63e40f7228d3406a60d37f` |
| `GPI 2s: Sync Digital Lifecycle` | `project.task` | `code` | `6a27576c27d0aaa5a9675d8e92aade46918d2916e0f575856441a3d03b6a3bee` |

The webhook Target Record block is compared as normalized text, not merely by
hash. Log Calls must be off for that webhook. If an intentional Studio change
is approved, update the runbook block, its hash constant, and the hash-lock test
in one reviewed commit; never accept an unexplained live mismatch.

## Checker and duplicate exercise

Read-only verification:

```bash
python scripts/check_odoo_review_workflow.py
```

Before configuration it exits nonzero and prints only fixed field or setting
names. After configuration it exits zero with these fixed facts:

```text
OK contract=V2
OK project=one
OK stages=General,L10
OK dale=one-active
OK automations=creation,review,digital-enabled
OK automation-actions=audited
OK company=matched
OK webhook=duplicate-bound
OK source-identities=unique
```

The checker never prints the webhook URL, UUID, company value, rule code, or
credentials. It checks the exact active project, stages, login, three rule
cardinalities, triggers, domains, watched fields, related action cardinality,
action type/model/code hash, webhook Target Record text, Log Calls setting,
current company, and webhook binding. It does not claim to inspect Studio UI
settings that are not represented by the exposed fields listed in the setup
prerequisites above.

The disposable exercise is intentionally reachable only with both flags:

```bash
python scripts/check_odoo_review_workflow.py --exercise --allow-duplicate-db
```

Before any create, it requires:

- both explicit flags;
- a canonical `ODOO_REVIEW_TEST_DB_UUID`;
- a canonical production `ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID` fence;
- fresh live `database.uuid` equality;
- live UUID inequality with `ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID`;
- exact current-company equality with `ODOO_IMPROVEMENTS_EXPECTED_COMPANY`;
- exact webhook origin and stored-rule UUID binding;
- every read-only contract check passing;
- archival fields on both disposable models;
- the authenticated RPC user is the one exact active
  `dale@gruberpallets.com` user, with one active employee.

Immediately before every create, write, webhook POST, or cleanup archive, the
exercise freshly re-reads database UUID and current company. Before every POST
it also re-reads the one active webhook rule and proves the configured URL has
the duplicate base origin and that rule's exact canonical UUID.

The exercise creates four rows and expects one stable linked task per row even
after a repeated save. Before positive transitions it proves that unknown keys,
an unassigned actor, Complete-before-Accept, a mismatched linked task, a
temporarily wrong-project disposable task, and terminal replays are rejected
with exact state unchanged. It then exercises Accept, Decline, Assign,
Complete, and Move to L10, checks empty linked work orders and unchanged
original notes, restores its wrong-project fixture, and archives only the
name-verified IDs it created. Each positive action requires the native
acknowledgement and then performs a separate exact XML-RPC readback. A timeout
or any non-native response also performs that readback before the command can
decide whether the action landed; it never blindly retries. Negative exercises
also read back exact identity after an unknown response, then treat the result
as unresolved because unchanged immediate state cannot prove a proxied request
will not land later.

Before the first create, the exercise proves that all four random source IDs
and exact random task names are absent. Cleanup re-searches those identities
with `active_test=False`, validates each exact row title and linked task name,
and never relies only on IDs returned by RPC. If an XML-RPC mutation response
is lost, or a webhook remains unresolved after timeout, gateway, or malformed
response readback, it performs that ownership reconciliation but deliberately
defers archival and exits with a fixed unknown-outcome error. This prevents
cleanup from racing a request that may still commit. After the remote request
has certainly stopped, an operator
must locate the most recent duplicate-only rows by the documented disposable
title prefix, validate their source identity/task name relationship, and archive
only those disposable records. The checker never reports cleanup success for
an unknown remote outcome.

The negative checks prove reject-without-change behavior. They do not inject a
failure after the first write because native Studio exposes no safe per-request
fault-injection control. The Execute Code block's post-write invariant failures
explicitly call `env.cr.rollback()` before re-raising; validate that path only
in an approved disposable database using an approved temporary fault method,
never by weakening the production action contract.

## Rollback

Rollback never edits, deletes, merges, or unlinks business records.

1. Set `ODOO_FEEDBACK_WORKFLOW_V2_ENABLED=false` in Plant Manager, Sales
   Manager, and OS Manager, then restart each application.
2. Disable `GPI 2s: Create and Link Task` to stop new task creation.
3. Disable `GPI 2s: Review Action Webhook` to stop lifecycle mutations.
4. Disable `GPI 2s: Sync Digital Lifecycle` to stop digital state propagation.
5. Keep `2s Improvement` in the Type selection so existing rows remain valid.
6. Leave every existing reference, task, work-order link, note, picture, status,
   stage, and assignee unchanged.
7. Run the checker read-only and attach its safe nonzero result to the incident.
8. Diagnose and repair configuration only in a fresh duplicate database.

## Secret rotation

1. Close all three application UI gates.
2. Disable the review webhook rule.
3. In Odoo developer mode, open the exact rule
   `GPI 2s: Review Action Webhook` and use Renew once.
4. Copy the new generated URL directly into each server's secret manager under
   `ODOO_REVIEW_ACTION_WEBHOOK_URL`. Never put it in a ticket or shell history.
5. Redeploy or restart the server processes so they read the new secret.
6. Confirm the old URL returns an error without making a change.
7. Re-enable the rule only in the approved duplicate, run the read-only checker,
   and run the guarded acknowledgement-plus-readback exercise.
8. Rotate production only after the duplicate evidence is approved. Update one
   production application at a time while its gate is false.
9. Run read-only preflights from all applications, then reopen gates in the
   approved order.
10. If any readback differs, close gates and disable the rule; do not restore the
    old URL or modify records by hand.
