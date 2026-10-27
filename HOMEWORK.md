# Homework management

Admin access uses the existing `ADMIN_IDS`. No additional dependencies or
environment variables are required.

## Run and migrate

From PowerShell in `ano_bot`, using the existing workspace virtual environment:

```powershell
& ..\venv\Scripts\python.exe -m compileall -q main.py app init_database.py tests
& ..\venv\Scripts\python.exe -m unittest discover -s tests -v
& ..\venv\Scripts\python.exe init_database.py
& ..\venv\Scripts\python.exe main.py
```

The last two commands use the existing configured PostgreSQL database and bot
credentials. Tests use dummy configuration and isolated in-memory SQLite data;
they do not read `.env`, contact Telegram, or connect to the configured database.
Stop an older bot process before starting this version.

`init_db()` applies the versioned `001_homework_management` migration on startup,
so running `init_database.py` separately is optional. The migration adds a UTC
deadline, submission/review fields, assignment records, a notification queue,
and a `schema_migrations` ledger. It never drops or recreates existing tables.
PostgreSQL startup migrations run in a transaction under an advisory lock.

Existing homework, answers, and completion timestamps are retained. Historical
`completed=True` submissions become `approved` without inventing answers,
feedback, or reviewer IDs. Existing student/homework links become assignments.
Legacy date-only deadlines become 23:59:59 Almaty time on that date. The original
date column is retained. Unlinked historical homework is not assigned to everyone,
and no sample homework is generated on startup.

## Admin workflow

1. Send `/addhomework` and enter a title, instructions, and future deadline using
   `YYYY-MM-DD HH:MM` in Almaty time (UTC+05:00).
2. Enter `all` to assign to all current student records, or comma-separated
   Telegram IDs for up to 100 existing students. `all` is a snapshot at creation;
   later registrations are not automatically assigned historical work.
3. `/homeworks` shows active homework with assigned, pending-review, approved,
   rejected, and not-submitted counts. Lists use ten records per page.
4. `/submissions` shows pending answers, instructions, and student identity.
   Choose Approve or Reject and enter feedback. The decision is saved only after
   feedback is provided. Stale review buttons cannot overwrite a newer revision.
5. `/cancel` exits any input flow. Other existing admin commands remain available.

## Student workflow

Open My Homework or send `/myhomework`. Only explicitly assigned work is visible.
Open an assignment to read instructions, deadline, saved answer, status, and
feedback. Submit a text answer of 1–4000 characters before the deadline.

Answers pending review and approved work cannot be replaced. Rejected work may
be resubmitted before the deadline; each resubmission increments its revision.
Late submissions are refused, but existing answers remain reviewable and feedback
remains visible. Incomplete profile fields do not invent student data or block an
existing student's assignment; an unknown student is directed to `/start`.
The old sample completion button opens the actual homework list and cannot approve
work. `/stats` continues to count approved work through the existing `completed`
field, including historical completion records.

All student messages use the existing English, Russian, or Kazakh language value.
Unknown/missing language values fall back to English.

## Notifications and reminders

New-assignment and review notices are queued in the same transaction as the
corresponding change. The homework worker processes the queue every 60 seconds.
One reminder is queued per assignment during the final 24 hours before its
deadline, for work not submitted or approved. Repeated ticks and restarts do not
create additional reminders. Queued reminders are cancelled if the deadline has
passed or the student has since submitted/completed the work.

Unique event keys, row locks, and persisted delivery claims prevent automatic
duplicate sends across worker processes. Telegram rate-limit rejections retry only
the rejected chunk. Delivery uses at-most-once attempts for ambiguous failures:
`failed` or interrupted `sending` notices are not automatically retried because
Telegram has no message idempotency key. They require manual delivery review;
the assignment, answer, and feedback remain accessible in the bot regardless.
Long text is split into messages below Telegram's UTF-16 length limit.

## Live verification still needed

The automated tests run actual SQL and migration steps against SQLite with mocked
async transport and Telegram calls. PostgreSQL locking syntax is compiled and
checked, but PostgreSQL transaction/locking behavior is not exercised live.

On a test PostgreSQL database, verify migration of the old schema, restart
idempotence, and concurrent submission/review/worker behavior. In Telegram, verify
all three languages, new-assignment notices, feedback delivery, rejection and
resubmission, deadline cutoffs, reminders after restart, blocked recipients, and
rate limits. Also exercise registration, FAQ, webinars/reminders, student/admin
dashboards, and support Reply/Close to confirm the existing live flows.

The implementation does not modify `.env` or create any Git commits.
