# Time-off approval emails

Feedback: **GPI-PM-FB-38**. Exact existing Odoo improvement: **210**.

Employees need an email when their time off is fully approved. The time clock
already has notices. Add email for new final approvals of current or future
leave, including approvals in Plant Manager and approvals pulled from Odoo.
Intermediate approval, denial, cancellation, and past leave do not send email.

Use Odoo's existing outgoing email service. A dedicated PostgreSQL outbox records
approval events in the same transaction as the leave state change. A database
trigger covers every approval writer, including the local approval fallback.
For newly imported approved leave, use Odoo's write timestamp and a durable
installation timestamp to exclude historical imports. Existing rows are not
backfilled. Each local request has at most one approval email.

A background worker checks that the request remains approved and its dates,
hours, and employee still match the approval snapshot before queuing email.
Cancelled, deleted, changed, inactive, and expired requests are skipped. Missing
or invalid email addresses remain pending and are retried after the employee
record is corrected. The recipient policy is configurable: personal then work
(recommended because Odoo has 29 personal addresses but only 5 work addresses),
personal only, or work only. Read addresses only for the exact employee from
Odoo. Do not add names to recipient searches or expose addresses in logs.

The message says “Your time off was approved,” gives dates with years, and
explains full days or partial-day times in the plant timezone. Include English
and Spanish. Omit the leave type, reason, and private notes. Use Odoo's configured
sender address with the display name GPI Plant Manager.

A stable random Message-ID identifies each email. Serialize workers with a
database advisory lock and commit the sending intent before the remote create.
Retain Odoo email records for readback. After a timeout, look for the same message;
never blindly create another. Record queue acceptance separately from delivery.
Odoo processes its normal mail queue; monitor outgoing, sent, failed, and cancelled
states. Failed or ambiguous delivery stays visible for operator attention.

Validation covers transactional capture, imports and migration replay, duplicate
polls, simultaneous workers, cancellation, missing addresses, recipient policy,
safe content, Odoo faults/timeouts, and mail-state readback. Run focused tests
against an isolated PostgreSQL database, then repository lint and the full suite.
Push implementation to origin/main and verify CI and Railway. Only then finish
feedback 38 through the local lifecycle and verify improvement 210 plus the
owner task obtained from feedback_task_delivery.
