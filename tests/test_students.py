"""Real SQL on isolated SQLite; Telegram and async transport are mocked."""
import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


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
from app import admin_students as admin


class LocalSession:
    """Bridge the production async query calls to a local synchronous session."""
    def __init__(self, database, queries):
        self.session = Session(database)
        self.queries = queries

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.session.close()

    async def scalar(self, query):
        self.queries.append(query)
        return self.session.scalar(query)

    async def execute(self, query):
        self.queries.append(query)
        return self.session.execute(query)

    async def get(self, model, key):
        return self.session.get(model, key)


class StudentManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = create_engine("sqlite:///:memory:")
        self.addCleanup(self.database.dispose)
        main.Student.metadata.create_all(self.database)
        self.queries = []
        self.factory = MagicMock(side_effect=lambda: LocalSession(self.database, self.queries))
        patcher = patch.object(admin, "AsyncSessionLocal", self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stats, self.support = AsyncMock(), AsyncMock()
        self.handlers = admin.StudentAdmin({123}, self.stats, self.support)
        self.message = MagicMock(spec=Message)
        self.message.from_user = SimpleNamespace(id=123)
        self.message.answer = AsyncMock()
        self.message.edit_text = AsyncMock()
        self.state = AsyncMock()
        self.data = {}
        self.state.get_data.side_effect = lambda: self.data.copy()
        self.state.update_data.side_effect = lambda **kw: self.data.update(kw)

    def seed_students(self, count):
        with self.database.begin() as connection:
            connection.execute(main.Student.__table__.insert(), [
                {"telegram_id": n, "full_name": f"Student {n:02d}", "course_name": "Python"}
                for n in range(1, count + 1)
            ])

    def callback(self, data, user_id=123):
        callback = MagicMock(spec=CallbackQuery)
        callback.from_user = SimpleNamespace(id=user_id)
        callback.data = data
        callback.message = self.message
        callback.answer = AsyncMock()
        return callback

    async def test_all_new_commands_reject_non_admin_and_missing_sender(self):
        for sender in (None, SimpleNamespace(id=999)):
            self.message.from_user = sender
            await self.handlers.admin_command(self.message)
            await self.handlers.students_command(self.message)
            await self.handlers.student_command(self.message)
            await self.handlers.findstudent_command(self.message, self.state)
        self.factory.assert_not_called()
        self.state.update_data.assert_not_awaited()
        self.assertEqual(self.message.answer.await_count, 8)

    async def test_unauthorized_callbacks_never_access_data_or_dashboards(self):
        for action in ("students:0", "profile:1", "stats", "support", "webinars", "search:token:0"):
            callback = self.callback("admin:" + action, user_id=999)
            await self.handlers.admin_callback(callback, self.state)
            callback.answer.assert_awaited_once_with("Not authorized.", show_alert=True)
        self.factory.assert_not_called()
        self.stats.assert_not_awaited()
        self.support.assert_not_awaited()
        self.message.answer.assert_not_awaited()

    async def test_existing_support_callbacks_reject_non_admin(self):
        with patch.object(main, "ADMIN_IDS", {123}), patch.object(main, "AsyncSessionLocal") as factory:
            callback = self.callback("support:reply:1", user_id=999)
            await main.support_reply_start(callback, self.state)
            callback.data = "support:close:1"
            await main.support_close_handler(callback, AsyncMock())
            factory.assert_not_called()
            self.assertEqual(callback.answer.await_count, 2)

    async def test_empty_list(self):
        await self.handlers.students_command(self.message)
        text = self.message.answer.call_args.args[0]
        self.assertIn("Total count: 0", text)
        self.assertIn("No students found", text)

    async def test_database_pagination_and_boundaries(self):
        self.seed_students(21)
        for requested, expected, size in ((-1, 0, 10), (0, 0, 10), (1, 1, 10), (2, 2, 1), (999999, 2, 1)):
            students, total, page, last = await admin.get_student_page(requested)
            self.assertEqual((total, page, last, len(students)), (21, expected, 2, size))
            self.assertEqual(students[0].telegram_id, expected * 10 + 1)
        await self.handlers.show_students(self.message, page=1)
        markup = self.message.answer.call_args.kwargs["reply_markup"]
        self.assertEqual(len(markup.inline_keyboard), 11)
        self.assertEqual([b.text for b in markup.inline_keyboard[-1]], ["Previous", "Next"])
        callback = self.callback("admin:students:2")
        await self.handlers.admin_callback(callback, self.state)
        self.assertIn("Page 3 of 3", self.message.edit_text.call_args.args[0])
        self.assertEqual(self.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[-1][0].text, "Previous")

    async def test_case_insensitive_literal_parameterized_search(self):
        names = ["Alice Smith", "ALICE Jones", "100%_Student", "Other"]
        with self.database.begin() as connection:
            connection.execute(main.Student.__table__.insert(), [
                {"telegram_id": i + 1, "full_name": name} for i, name in enumerate(names)
            ])
        students, total, _, _ = await admin.get_student_page(search="aLiCe")
        self.assertEqual(total, 2)
        self.assertEqual([s.telegram_id for s in students], [1, 2])
        self.assertEqual((await admin.get_student_page(search="%_"))[1], 1)
        injection = "' OR 1=1 --"
        self.assertEqual((await admin.get_student_page(search=injection))[1], 0)
        compiled = self.queries[-1].compile(dialect=postgresql.dialect())
        self.assertNotIn(injection, str(compiled))
        self.assertIn(f"%{injection}%", compiled.params.values())

    async def test_search_command_pagination_no_match_and_expired_search(self):
        self.seed_students(12)
        self.message.text = "/findstudent student"
        await self.handlers.findstudent_command(self.message, self.state)
        searches = self.data["admin_student_searches"]
        token = next(iter(searches))
        await self.handlers.admin_callback(self.callback(f"admin:search:{token}:1"), self.state)
        self.assertIn("Page 2 of 2", self.message.edit_text.call_args.args[0])
        self.assertIn("Student search: student", self.message.edit_text.call_args.args[0])
        await self.handlers.admin_callback(self.callback("admin:search:expired:0"), self.state)
        self.assertIn("expired", self.message.answer.call_args.args[0])
        self.message.text = "/findstudent nobody"
        await self.handlers.findstudent_command(self.message, self.state)
        self.assertIn("No matching students", self.message.answer.call_args.args[0])
        self.state.clear.assert_not_awaited()

    async def test_student_details_real_counts_and_missing_fields(self):
        self.seed_students(2)
        with self.database.begin() as connection:
            connection.execute(main.Student.__table__.update().where(main.Student.telegram_id == 1)
                               .values(full_name=None, course_name="  ", language=None))
            connection.execute(main.Homework.__table__.insert(), [
                {"id": i, "title": "Task", "description": "Task"} for i in (1, 2)
            ])
            connection.execute(main.HomeworkSubmission.__table__.insert(), [
                {"student_id": 1, "homework_id": 1, "completed": True},
                {"student_id": 1, "homework_id": 2, "completed": False},
                {"student_id": 2, "homework_id": 1, "completed": True},
            ])
            connection.execute(main.SupportRequest.__table__.insert(), [
                {"student_id": sid, "message": "Private support message", "status": status}
                for sid, status in ((1, "open"), (1, "answered"), (1, "closed"), (2, "open"))
            ])
        self.message.text = "/student 1"
        await self.handlers.student_command(self.message)
        text = self.message.answer.call_args.args[0]
        for expected in ("Telegram ID: 1", "Full name: Not provided", "Course: Not provided",
                         "Selected language: Not provided", "Record created:", "UTC",
                         "Homework submissions recorded: 2", "Completed: 1", "Incomplete submissions: 1",
                         "Support requests total: 3", "Open: 1", "Answered: 1", "Closed: 1"):
            self.assertIn(expected, text)
        self.assertNotIn("Private support message", text)
        await self.handlers.show_profile(self.message, 999)
        self.assertIn("Student not found", self.message.answer.call_args.args[0])

    async def test_profile_button_empty_history_and_invalid_search(self):
        self.seed_students(1)
        await self.handlers.admin_callback(self.callback("admin:profile:1"), self.state)
        text = self.message.answer.call_args.args[0]
        for expected in ("Student 01", "Course: Python", "Homework submissions recorded: 0",
                         "Support requests total: 0", "Open: 0", "Answered: 0", "Closed: 0"):
            self.assertIn(expected, text)
        for text in ("/findstudent", "/findstudent " + "x" * 101):
            self.message.text = text
            await self.handlers.findstudent_command(self.message, self.state)
            self.assertIn("Usage: /findstudent", self.message.answer.call_args.args[0])

    async def test_repeated_and_old_pagination_messages(self):
        self.seed_students(1)
        method = EditMessageText(chat_id=123, message_id=1, text="Students")
        self.message.edit_text.side_effect = TelegramBadRequest(
            method=method, message="Bad Request: message is not modified",
        )
        await self.handlers.admin_callback(self.callback("admin:students:0"), self.state)
        self.message.answer.assert_not_awaited()
        self.message.edit_text.side_effect = TelegramBadRequest(
            method=method, message="Bad Request: message can't be edited",
        )
        await self.handlers.admin_callback(self.callback("admin:students:0"), self.state)
        self.assertIn("Total count: 1", self.message.answer.call_args.args[0])

    async def test_invalid_ids_and_malformed_callbacks(self):
        for value in ("", "-1", "0", "abc", "1 2", "9223372036854775808", "9" * 100, "１２"):
            self.message.text = "/student " + value
            await self.handlers.student_command(self.message)
            self.assertIn("Usage:", self.message.answer.call_args.args[0])
        self.factory.assert_not_called()
        for action in ("profile:bad", "students:-1", "students:abc", "unknown", "profile:0"):
            await self.handlers.admin_callback(self.callback("admin:" + action), self.state)
            self.assertIn("Invalid dashboard", self.message.answer.call_args.args[0])
        self.factory.assert_not_called()

    async def test_database_errors_have_safe_responses(self):
        for error in (SQLAlchemyError("secret"), OSError("secret"), TimeoutError()):
            self.factory.side_effect = error
            await self.handlers.students_command(self.message)
            await self.handlers.show_profile(self.message, 1)
            await self.handlers.show_webinars(self.message)
            self.assertEqual(self.message.answer.call_args.args[0], admin.DATABASE_MESSAGE)

    async def test_dashboard_reuses_stats_and_support_without_message_sender_auth(self):
        # Telegram dashboard messages are authored by the bot; the callback user
        # is the identity that must be checked before calling shared renderers.
        self.message.from_user = SimpleNamespace(id=888)
        await self.handlers.admin_callback(self.callback("admin:stats"), self.state)
        await self.handlers.admin_callback(self.callback("admin:support"), self.state)
        self.stats.assert_awaited_once_with(self.message)
        self.support.assert_awaited_once_with(self.message)
        self.message.from_user = SimpleNamespace(id=123)
        await self.handlers.admin_command(self.message)
        buttons = self.message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual({b.callback_data for row in buttons for b in row},
                         {"admin:students:0", "admin:stats", "admin:support", "admin:webinars"})

    async def test_long_messages_and_webinar_dashboard(self):
        await admin.send_text(self.message, "\U0001f600" * 4000)
        for call in self.message.answer.call_args_list:
            self.assertLessEqual(len(call.args[0].encode("utf-16-le")) // 2, 4096)
        with self.database.begin() as connection:
            connection.execute(main.Webinar.__table__.insert(), [
                {"title": "Future", "description": "Details", "starts_at": datetime.now(timezone.utc) + timedelta(days=1)},
                {"title": "Past", "description": "Details", "starts_at": datetime.now(timezone.utc) - timedelta(days=1)},
            ])
        self.message.answer.reset_mock()
        await self.handlers.admin_callback(self.callback("admin:webinars"), self.state)
        text = "".join(c.args[0] for c in self.message.answer.call_args_list)
        self.assertIn("Total: 2", text)
        self.assertIn("Upcoming: 1", text)
        self.assertIn("Future", text)
        self.assertNotIn("Past", text)

    async def test_commands_registered_before_state_handlers(self):
        names = [h.callback.__name__ for h in main.dp.message.handlers]
        for command in ("admin_command", "students_command", "student_command", "findstudent_command"):
            self.assertLess(names.index(command), names.index("webinar_title_handler"))


if __name__ == "__main__":
    unittest.main()
