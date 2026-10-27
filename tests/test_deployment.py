"""Offline deployment/configuration/lifecycle tests. No live services or .env."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import runpy
import signal
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

from app import runtime
from app.settings import async_database_url, parse_admin_ids


ROOT = Path(__file__).resolve().parents[1]
config = ModuleType("app.config")
config.BOT_TOKEN = "123456:test"
config.DATABASE_URL = "postgresql+asyncpg://test:test@localhost/test"
previous = sys.modules.get("app.config")
sys.modules["app.config"] = config
try:
    main = importlib.import_module("main")
finally:
    if previous is None:
        sys.modules.pop("app.config", None)
    else:
        sys.modules["app.config"] = previous


class ConfigurationTests(unittest.TestCase):
    def test_railway_urls_and_sslmode_are_compatible_with_asyncpg(self):
        for scheme in ("postgres", "postgresql", "postgresql+asyncpg"):
            url = async_database_url(f"{scheme}://test:dummy%40word@postgres.railway.internal:5432/railway?sslmode=require")
            self.assertEqual(url.drivername, "postgresql+asyncpg")
            self.assertEqual(url.password, "dummy@word")
            _, options = PGDialect_asyncpg().create_connect_args(url)
            self.assertEqual(options["ssl"], "require")
            self.assertNotIn("sslmode", options)
            self.assertEqual(options["host"], "postgres.railway.internal")
        self.assertEqual(async_database_url("postgresql://test:dummy@[::1]:5432/db").host, "::1")

    def test_invalid_urls_and_admin_ids_have_safe_errors(self):
        for raw in (None, "not-a-url-with-secret", "sqlite:///test.db", "postgresql://test:secret@host", "postgresql://test:secret@host/db?sslmode=wrong"):
            with self.assertRaises(ValueError) as caught:
                async_database_url(raw)
            self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(parse_admin_ids("123, 456,123,"), {123, 456})
        self.assertEqual(parse_admin_ids(""), set())
        for raw in ("-1", "0", "abc", "１２", "9" * 100):
            with self.assertRaises(ValueError):
                parse_admin_ids(raw)

    def test_railway_uses_environment_and_never_loads_dotenv(self):
        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT_ID": "test", "BOT_TOKEN": "test-token", "DATABASE_URL": "test-url"}, clear=True), patch("dotenv.load_dotenv") as load:
            values = runpy.run_path(str(ROOT / "app/config.py"))
            load.assert_not_called()
            self.assertEqual(values["BOT_TOKEN"], "test-token")
            self.assertEqual(values["DATABASE_URL"], "test-url")

    def test_local_dotenv_is_explicit_and_cannot_override_environment(self):
        with patch.dict(os.environ, {"BOT_TOKEN": "test-token"}, clear=True), patch("dotenv.load_dotenv") as load:
            runpy.run_path(str(ROOT / "app/config.py"))
            load.assert_called_once_with(ROOT / ".env", override=False)

    def test_worker_deployment_has_no_http_server_or_second_replica(self):
        config = json.loads((ROOT / "railway.json").read_text())
        deploy = config["deploy"]
        self.assertEqual(deploy["startCommand"], "python main.py")
        self.assertEqual(deploy["numReplicas"], 1)
        self.assertEqual(deploy["overlapSeconds"], 0)
        self.assertGreaterEqual(deploy["drainingSeconds"], 30)
        self.assertIsNone(deploy["healthcheckPath"])
        self.assertIsNone(deploy["cronSchedule"])
        self.assertFalse(deploy["sleepApplication"])
        docker = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("COPY . ", docker)
        self.assertNotIn("EXPOSE", docker)
        self.assertIn('CMD ["python", "main.py"]', docker)
        self.assertIn("**", (ROOT / ".dockerignore").read_text().splitlines())
        requirements = (ROOT / "requirements.txt").read_text()
        self.assertIn("asyncpg==", requirements)
        self.assertIn("SQLAlchemy[asyncio]", requirements)
        self.assertIn("tzdata", requirements)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = AsyncMock()
        row = MagicMock()
        row.one.return_value = (True, 123)
        self.connection.execute.return_value = row
        self.connection.scalar.return_value = 123
        self.engine = AsyncMock()
        self.engine.connect.return_value = self.connection
        self.bot = AsyncMock()
        self.bot.id = 123456
        self.dispatcher = MagicMock()
        self.dispatcher._handle_update_tasks = set()
        self.poll_started = asyncio.Event()
        self.poll_stopped = asyncio.Event()
        self.scheduler_started = asyncio.Event()

        async def poll(*args, **kwargs):
            self.poll_started.set()
            await self.poll_stopped.wait()

        async def stop():
            self.poll_stopped.set()

        async def scheduler(bot):
            self.scheduler_started.set()
            await asyncio.Event().wait()

        self.dispatcher.start_polling = AsyncMock(side_effect=poll)
        self.dispatcher.stop_polling = AsyncMock(side_effect=stop)
        self.initialize = AsyncMock()
        self.scheduler = AsyncMock(side_effect=scheduler)

    async def test_one_poller_and_scheduler_stop_and_close_all_resources(self):
        stop = asyncio.Event()
        task = asyncio.create_task(runtime.run_bot(self.bot, self.dispatcher, self.engine, self.initialize, self.scheduler, stop))
        await asyncio.wait_for(self.poll_started.wait(), 1)
        await asyncio.wait_for(self.scheduler_started.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.initialize.assert_awaited_once()
        self.dispatcher.start_polling.assert_awaited_once_with(self.bot, handle_signals=False, close_bot_session=False)
        self.scheduler.assert_awaited_once_with(self.bot)
        self.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        self.dispatcher.stop_polling.assert_awaited_once()
        self.connection.invalidate.assert_awaited_once()
        self.connection.close.assert_awaited_once()
        self.bot.session.close.assert_awaited_once()
        self.engine.dispose.assert_awaited_once()

    async def test_failed_migration_cleans_up_before_polling_starts(self):
        self.initialize.side_effect = SQLAlchemyError("test failure")
        with self.assertRaises(SQLAlchemyError):
            await runtime.run_bot(self.bot, self.dispatcher, self.engine, self.initialize, self.scheduler)
        self.dispatcher.start_polling.assert_not_awaited()
        self.scheduler.assert_not_awaited()
        self.connection.invalidate.assert_awaited_once()
        self.bot.session.close.assert_awaited_once()
        self.engine.dispose.assert_awaited_once()

    async def test_lease_loss_stops_polling_and_scheduler(self):
        with patch.object(runtime, "watch_worker_lock", new_callable=AsyncMock, side_effect=RuntimeError("Lease lost")):
            with self.assertRaisesRegex(RuntimeError, "Lease lost"):
                await runtime.run_bot(self.bot, self.dispatcher, self.engine, self.initialize, self.scheduler)
        self.poll_stopped.set()
        self.assertTrue(self.dispatcher.stop_polling.await_count)
        self.engine.dispose.assert_awaited_once()

    async def test_lock_contention_waits_without_starting_another_poller(self):
        unavailable, acquired = MagicMock(), MagicMock()
        unavailable.one.return_value = (False, 456)
        acquired.one.return_value = (True, 456)
        self.connection.execute.side_effect = [unavailable, acquired]
        with patch.object(runtime.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            async with runtime.singleton_worker(self.engine, self.bot.id) as (_, pid):
                self.assertEqual(pid, 456)
                self.dispatcher.start_polling.assert_not_awaited()
            sleep.assert_awaited_once_with(1)
        self.assertEqual(self.connection.execute.await_count, 2)
        self.assertEqual(runtime.worker_lock_key(self.bot.id), runtime.worker_lock_key(self.bot.id))
        self.assertNotEqual(runtime.worker_lock_key(self.bot.id), runtime.worker_lock_key(self.bot.id + 1))
        self.connection.invalidate.assert_awaited_once()

    async def test_watchdog_rejects_a_reconnected_database_session(self):
        self.connection.scalar.return_value = 999
        with patch.object(runtime.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "singleton session lost"):
                await runtime.watch_worker_lock(self.connection, 123)

    async def test_shutdown_while_waiting_for_previous_deployment(self):
        attempted = asyncio.Event()
        unavailable = MagicMock()
        unavailable.one.return_value = (False, 456)
        async def attempt(*args):
            attempted.set()
            return unavailable
        self.connection.execute.side_effect = attempt
        stop = asyncio.Event()
        task = asyncio.create_task(runtime.run_bot(self.bot, self.dispatcher, self.engine,
                                                   self.initialize, self.scheduler, stop))
        await asyncio.wait_for(attempted.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.initialize.assert_not_awaited()
        self.dispatcher.start_polling.assert_not_awaited()
        self.connection.invalidate.assert_awaited_once()
        self.engine.dispose.assert_awaited_once()

    async def test_lock_is_rechecked_after_migrations_before_polling(self):
        self.connection.scalar.return_value = 999
        with self.assertRaisesRegex(RuntimeError, "singleton session lost"):
            await runtime.run_bot(self.bot, self.dispatcher, self.engine, self.initialize, self.scheduler)
        self.initialize.assert_awaited_once()
        self.dispatcher.start_polling.assert_not_awaited()
        self.bot.delete_webhook.assert_not_awaited()
        self.engine.dispose.assert_awaited_once()

    async def test_sigterm_callback_uses_the_graceful_shutdown_path(self):
        loop = asyncio.get_running_loop()
        callbacks = {}
        def install(sig, callback):
            callbacks[sig] = callback
        with patch.object(loop, "add_signal_handler", side_effect=install), patch.object(loop, "remove_signal_handler"):
            task = asyncio.create_task(runtime.run_bot(self.bot, self.dispatcher, self.engine, self.initialize, self.scheduler))
            await asyncio.wait_for(self.poll_started.wait(), 1)
            self.assertIn(signal.SIGINT, callbacks)
            callbacks[signal.SIGTERM]()
            await asyncio.wait_for(task, 1)
        self.dispatcher.stop_polling.assert_awaited_once()
        self.engine.dispose.assert_awaited_once()

    async def test_one_scheduler_services_both_reminder_types_despite_failure(self):
        with patch.object(main, "send_webinar_reminders", new_callable=AsyncMock, side_effect=SQLAlchemyError("test")) as webinar, patch.object(main, "homework_reminders", new_callable=AsyncMock) as homework, patch.object(main.asyncio, "sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await main.reminder_loop(self.bot)
            webinar.assert_awaited_once_with(self.bot)
            homework.assert_awaited_once_with(self.bot)

    async def test_webinar_notices_and_flags_are_queued_together_without_sending(self):
        webinar = SimpleNamespace(id=1, title="Session", description="Details", meeting_url=None,
            starts_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            reminder_1h_sent=False, reminder_24h_sent=False)
        webinars, students = MagicMock(), MagicMock()
        webinars.scalars.return_value.all.return_value = [webinar]
        students.all.return_value = [(1, "🇬🇧 English")]
        session = AsyncMock()
        session.add = MagicMock()
        session.execute.side_effect = [webinars, students, webinars, students]
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(main, "AsyncSessionLocal", factory):
            await main.send_webinar_reminders(self.bot)
            await main.send_webinar_reminders(self.bot)
        self.assertTrue(webinar.reminder_1h_sent)
        session.add.assert_called_once()
        self.assertEqual(session.add.call_args.args[0].event_key, "webinar:1:reminder_1h:1")
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(session.commit.await_count, 2)


if __name__ == "__main__":
    unittest.main()
