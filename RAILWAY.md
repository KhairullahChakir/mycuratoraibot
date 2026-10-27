# Railway deployment: ano_bot

Prepared from local commit `92c9d9e`. These are manual deployment instructions;
preparing these files does not commit, push, deploy, or provision resources.

## Runtime configuration

`railway.json` selects the included Dockerfile and start command `python main.py`.
The image uses Python 3.13, unbuffered logs, and a non-root user. Dependencies come
from `requirements.txt`, including `asyncpg`, `SQLAlchemy[asyncio]`/`greenlet`, and
`tzdata` for `Asia/Almaty` and other IANA time zones. Existing dependencies remain.

The process is an always-on, long-polling background worker. It opens no HTTP
listener, needs no `PORT`, has no HTTP healthcheck, and requires no public domain.
See [Railway's background-worker guide](https://docs.railway.com/guides/cron-workers-queues).

Configuration in `railway.json`:

| Setting | Value |
| --- | --- |
| Builder | Dockerfile |
| Start command | `python main.py` |
| Replicas / regions | One replica in one region; no multi-region override |
| Restart policy | `ALWAYS` |
| Serverless / sleeping | Disabled |
| Cron schedule | None |
| HTTP healthcheck | None |
| Deployment overlap | 0 seconds |
| Shutdown draining time | 45 seconds |

These settings follow Railway's [configuration schema](https://railway.com/railway.schema.json).
`ALWAYS` restart is not available on Free/trial plans. Choose an appropriate
always-on plan yourself; no plan or paid resource has been created by this task.
Free/trial users must change the policy to `ON_FAILURE` with at most 10 retries,
which does not provide equivalent unattended recovery. See
[restart policies and plan limitations](https://docs.railway.com/deployments/restart-policy).

## 1. Preserve the database before switching hosts

Choose which PostgreSQL database the deployed bot will use. Keep using an existing
reachable database if that is the intended source of student data. If moving to
Railway PostgreSQL, manually back up and restore the existing data into the new
database **before** starting the deployed bot. Merely changing `DATABASE_URL` to a
new empty database does not transfer existing records.

Do not restore over an existing populated destination without a separate migration
plan. Keep dumps outside the repository. Confirm the expected student/homework
counts and configure database backups. Nothing in this preparation copies, deletes,
or modifies the current live database.

For Railway-hosted PostgreSQL, place the bot and database in the same project and
environment. Prefer the direct private endpoint, not a transaction-pooling proxy.
Railway documents [PostgreSQL connection variables](https://docs.railway.com/databases/postgresql)
and [private networking](https://docs.railway.com/networking/private-networking).
Private Railway hostnames are resolved inside Railway, not on your local PC.

## 2. Create/select the worker service manually

1. Review the code and tests locally. Commit/push yourself when ready if using
   GitHub. No Git operations are performed by the deployment preparation.
2. In Railway, select your intended project/environment. Create or select a normal
   application service named, for example, `ano-bot-worker`.
3. Select your repository and intended branch as its source. If the repository
   root is this `ano_bot` project, use Root Directory `/` and Railway Config File
   `/railway.json`.
4. If this folder is inside a larger repository, use Root Directory `/ano_bot`
   and Railway Config File `/ano_bot/railway.json`. The config-file path is relative
   to the repository root, even when a Root Directory is set. See
   [Railway build configuration](https://docs.railway.com/builds/build-configuration).
5. Verify Dockerfile detection and Start Command `python main.py`. Do not use
   Uvicorn, Gunicorn, multiple worker processes, an HTTP service, or a cron job.
6. Set one replica in one region. Disable Serverless/sleeping. Leave public
   networking, custom domains, HTTP healthchecks, and cron scheduling unset.
7. Confirm the settings table above, especially zero overlap and 45 seconds of
   draining time. Remove conflicting custom teardown environment overrides.
   Railway sends SIGTERM before SIGKILL during the configured
   [deployment teardown](https://docs.railway.com/deployments/deployment-teardown).
8. Leave the Pre-deploy Command empty. Schema initialization runs safely during
   worker startup after it owns the singleton lock, using runtime database access.

## 3. Add secure service variables

In the **worker service's Variables tab**, set:

| Variable | Value |
| --- | --- |
| `BOT_TOKEN` | Your real BotFather token, entered only in Railway's variables UI |
| `DATABASE_URL` | The intended existing PostgreSQL connection, or a reference to your Railway database |
| `ADMIN_IDS` | Your authorized positive numeric Telegram IDs, separated by commas |

If the database service is named `Postgres`, use Railway's reference expression
`${{Postgres.DATABASE_URL}}`. Replace `Postgres` with your actual service name;
check that the referenced URL uses the intended direct database endpoint.

The application accepts `postgres://`, `postgresql://`, and
`postgresql+asyncpg://` URLs. It converts them internally to SQLAlchemy's asyncpg
dialect while preserving encoded passwords. A supplied `sslmode` is translated to
asyncpg's `ssl` option. It does not turn off TLS or rewrite hostnames. Keep the SSL
mode provided by the database operator; external databases may require TLS/CA
configuration. See [SQLAlchemy asyncpg configuration](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#asyncpg).

Railway supplies `RAILWAY_ENVIRONMENT_ID`/`RAILWAY_PROJECT_ID`. When either is
present, `.env` loading is disabled. Locally, only this project's `.env` is loaded,
and it cannot override already-set environment variables. Invalid admin IDs or
database configuration fail with messages that omit credential values.

Never put real tokens or database URLs in source, Docker arguments, logs, or this
document. Do not upload `.env`. `.gitignore` and `.railwayignore` exclude credentials,
environments, caches, local databases, and dumps. `.dockerignore` allowlists only
the build files and application Python sources; the Dockerfile copies no local
configuration, tests, dumps, or Git history into the runtime image.

## 4. Deploy manually, then verify

Stop any old local/hosted process using the same bot token first. Do not reuse the
production token in preview services or a second environment.

Trigger deployment yourself after the variables and database are ready. The bot:

1. Acquires a PostgreSQL session advisory lock derived from its bot ID.
2. Runs additive schema initialization/migration and existing webinar-column setup.
3. Verifies its lock connection, removes any old Telegram webhook **without**
   discarding pending updates, and starts exactly one polling task.
4. Starts one scheduler task for webinar reminders, homework reminders, and queued
   notification delivery. Both reminder types keep their existing timing rules.

The existing `001_homework_management` migration remains additive and idempotent.
Its ledger and changes commit in one transaction under the existing migration
advisory lock. No table is dropped or recreated, and no sample homework is seeded.
Startup failure closes the bot session and database connections and exits nonzero.

Inspect logs for `Bot worker ready: one poller and one reminder scheduler` and
aiogram's polling startup, then test `/start`, `/myhomework`, `/admin`, `/stats`,
`/students`, `/support`, and support Reply/Close. Test a full homework creation,
submission, grading, feedback, and reminder flow. Test English, Russian, and
Kazakh. Verify a non-admin cannot open admin commands or callbacks.

Finally, redeploy once and verify a clean shutdown/startup, unchanged database
counts, continued reminders, and no Telegram polling-conflict errors. A deployment
can be marked active before the database lock is acquired because there is no HTTP
healthcheck; inspect logs and send a Telegram command to confirm operational readiness.

## Optional manual CLI deployment

Only run these yourself when ready to upload and deploy:

```powershell
Set-Location C:\Users\khair\Desktop\my_workplace\ano_bot
railway login
railway link
railway up --service ano-bot-worker
```

Select the already-intended project/environment/service during linking; verify
the target before `up`. Do not use `--no-gitignore`. Railway CLI respects both
`.gitignore` and `.railwayignore`; see [railway up file handling](https://docs.railway.com/cli/up).
Do not enable automatic PR deployments with the production token.

## Local validation and remaining limitations

From `ano_bot`, using the existing workspace virtual environment:

```powershell
& ..\venv\Scripts\python.exe -m compileall -q main.py app init_database.py tests
& ..\venv\Scripts\python.exe -m unittest discover -s tests -v
```

With Docker installed, you can additionally build the image locally:

```powershell
docker build -t ano-bot:railway .
```

The preparation tests use isolated SQLite queries, mocked PostgreSQL lease
connections and Telegram calls, and simulated shutdown signals. They do not
exercise a live Railway deployment, native PostgreSQL locking, real Telegram
delivery, or a Linux container. Docker was unavailable in the preparation
environment, so the image build still needs verification.

The singleton guard coordinates copies of this version using the same bot ID and
database. It cannot stop an older unguarded process or another process connected
to a different database. It requires direct/session-preserving PostgreSQL
connections. A watchdog stops the worker after connection loss or reconnection;
network-failure detection is not instantaneous.

On SIGTERM/SIGINT, polling stops, scheduled work is cancelled, active handlers get
a short drain period, the advisory-lock session closes, and Telegram/SQLAlchemy
connections are cleaned up. Unexpected worker-task failure stops the process for
Railway to restart. Do not run a separate reminder service.

In-progress conversational drafts remain in aiogram's existing MemoryStorage and
are lost on restart; saved student records, assignments, answers, feedback, support,
and notification state remain in PostgreSQL. Notification delivery retains the
existing at-most-once policy: ambiguous `sending`/`failed` attempts require manual
review rather than automatic retries that could duplicate messages. Webinar
reminder notices now use the same persistent queue, committed with their reminder
flags. Queued webinar notices are cancelled if the session has already started.

No `.env` changes, commits, pushes, deployments, or paid resources were made by
this preparation.
