"""Offline dashboard checks; no .env, PostgreSQL, or Telegram access."""
import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError


config = ModuleType("app.config")
config.BOT_TOKEN = "123456:test"
config.DATABASE_URL = "postgresql+asyncpg://test:test@localhost/test"
previous_config = sys.modules.get("app.config")
sys.modules["app.config"] = config
try:
    main = importlib.import_module("main")
finally:
    if previous_config is None:
        sys.modules.pop("app.config", None)
    else:
        sys.modules["app.config"] = previous_config


class StatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.admins = patch.object(main, "ADMIN_IDS", {123})
        self.admins.start()
        self.addCleanup(self.admins.stop)
        self.factory = MagicMock()
        self.session = AsyncMock()
        self.factory.return_value.__aenter__ = AsyncMock(return_value=self.session)
        self.factory.return_value.__aexit__ = AsyncMock(return_value=False)
        factory_patch = patch.object(main, "AsyncSessionLocal", self.factory)
        factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.message = SimpleNamespace(
            from_user=SimpleNamespace(id=123), answer=AsyncMock(),
        )

    async def test_non_admin_and_missing_sender_do_not_query_database(self):
        for sender in (None, SimpleNamespace(id=999)):
            self.message.from_user = sender
            await main.stats_command(self.message)
            self.assertIn("restricted", self.message.answer.call_args.args[0])
        self.factory.assert_not_called()

    async def run_database_counts(self, populated=False):
        # Execute the actual aggregate query against isolated SQLite tables.
        # PostgreSQL compilation is checked too; no production DB is contacted.
        database = create_engine("sqlite:///:memory:")
        self.addCleanup(database.dispose)
        main.Student.metadata.create_all(database)
        now = datetime.now(timezone.utc)
        if populated:
            with database.begin() as connection:
                connection.execute(main.Student.__table__.insert(), [
                    {"telegram_id": 1, "full_name": "Student", "course_name": "Python"},
                    {"telegram_id": 2, "full_name": None, "course_name": "Python"},
                    {"telegram_id": 3, "full_name": "Student", "course_name": "  "},
                    {"telegram_id": 4, "full_name": "", "course_name": "Python"},
                    {"telegram_id": 5, "full_name": "Student", "course_name": None},
                ])
                connection.execute(main.Homework.__table__.insert(), {
                    "id": 1, "title": "Task", "description": "Task details",
                })
                connection.execute(main.HomeworkSubmission.__table__.insert(), [
                    {"student_id": 1, "homework_id": 1, "completed": True},
                    {"student_id": 2, "homework_id": 1, "completed": True},
                    {"student_id": 3, "homework_id": 1, "completed": False},
                ])
                connection.execute(main.Webinar.__table__.insert(), [
                    {"title": "Past", "description": "Past", "starts_at": now - timedelta(days=1)},
                    {"title": "Future", "description": "Future", "starts_at": now + timedelta(days=1)},
                ])
                connection.execute(main.SupportRequest.__table__.insert(), [
                    {"student_id": 1, "message": "Help", "status": status}
                    for status in ("open", "open", "answered", "closed", "closed", "closed", "other")
                ])

        async def execute(query):
            compiled = query.compile(dialect=postgresql.dialect())
            self.assertIn("count(*)", str(compiled))
            self.assertTrue(any(
                isinstance(value, datetime) and value.tzinfo is not None
                for value in compiled.params.values()
            ))
            with database.connect() as connection:
                counts = dict(connection.execute(query).mappings().one())
            result = MagicMock()
            result.mappings.return_value.one.return_value = counts
            return result

        self.session.execute.side_effect = execute
        await main.stats_command(self.message)
        self.session.execute.assert_awaited_once()
        return self.message.answer.call_args.args[0]

    async def test_empty_tables_show_zero_counts(self):
        response = await self.run_database_counts()
        self.assertEqual(response.count(": 0"), 8)

    async def test_populated_tables_count_only_matching_records(self):
        response = await self.run_database_counts(populated=True)
        for expected in (
            "Total student records: 5", "Completed profiles (name and course): 1",
            "Completed homework submissions: 2", "Total webinars: 2",
            "Upcoming webinars: 1", "Open: 2", "Answered: 1", "Closed: 3",
        ):
            self.assertIn(expected, response)

    async def test_database_errors_do_not_expose_details(self):
        for error in (SQLAlchemyError("private connection details"),
                      OSError("private connection details"), TimeoutError()):
            self.session.execute.side_effect = error
            await main.stats_command(self.message)
            response = self.message.answer.call_args.args[0]
            self.assertIn("try /stats again later", response)
            self.assertNotIn("private", response)

    async def test_stats_and_support_registered_before_conversation_handlers(self):
        handlers = main.dp.message.handlers
        indices = {handler.callback.__name__: i for i, handler in enumerate(handlers)}
        stats = handlers[indices["stats_command"]]
        self.assertEqual(stats.filters[0].callback.commands, ("stats",))
        for command in ("stats_command", "open_support_command"):
            for state_handler in ("webinar_title_handler", "registration_name_handler",
                                  "support_message_handler", "support_reply_handler"):
                self.assertLess(indices[command], indices[state_handler])
        support = handlers[indices["open_support_command"]]
        self.assertEqual(set(support.filters[0].callback.commands), {"support", "opensupport"})


if __name__ == "__main__":
    unittest.main()
