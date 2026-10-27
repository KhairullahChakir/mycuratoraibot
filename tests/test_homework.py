"""Offline real-SQL homework tests; no .env, live DB, or Telegram calls."""
import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Message
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


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
from app import homework_handlers as handlers
from app import homework_service as service
from app.database.db import Base
from app.database.migrations import migrate_homework
from app.database.models import Homework, HomeworkAssignment, HomeworkNotification, HomeworkSubmission, Student
from app.homework_texts import TEXTS, texts


class LocalAsyncSession:
    def __init__(self, database, queries):
        self.session = Session(database, expire_on_commit=False)
        self.queries = queries

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.session.close()

    def add(self, obj):
        self.session.add(obj)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def get(self, model, key):
        return self.session.get(model, key)

    async def execute(self, query):
        self.queries.append(query)
        return self.session.execute(query)

    async def scalar(self, query):
        self.queries.append(query)
        return self.session.scalar(query)


class HomeworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = create_engine("sqlite:///:memory:")
        @event.listens_for(self.database, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.database.dispose)
        Base.metadata.create_all(self.database)
        self.queries = []
        self.factory = MagicMock(side_effect=lambda: LocalAsyncSession(self.database, self.queries))
        for module in (service, handlers):
            patcher = patch.object(module, "AsyncSessionLocal", self.factory)
            patcher.start()
            self.addCleanup(patcher.stop)
        with Session(self.database) as session:
            session.add_all([
                Student(telegram_id=1, full_name="English Student", language="🇬🇧 English"),
                Student(telegram_id=2, full_name="Русский студент", language="🇷🇺 Русский"),
                Student(telegram_id=3, full_name=None, language="🇰🇿 Қазақша"),
            ])
            session.commit()
        self.deadline = datetime.now(timezone.utc) + timedelta(hours=12)
        self.ui = handlers.HomeworkHandlers({99}, lambda _: None)
        self.message = MagicMock(spec=Message)
        self.message.from_user = SimpleNamespace(id=1)
        self.message.answer = AsyncMock()
        self.message.text = "Answer"
        self.data = {}
        self.state = AsyncMock()
        self.state.get_data.side_effect = lambda: dict(self.data)
        self.state.update_data.side_effect = lambda **kw: self.data.update(kw)
        self.state.clear.side_effect = lambda: self.data.clear()

    async def create(self, selected=None):
        return (await service.create_homework(99, {99}, "Assignment", "Write an answer", self.deadline, selected))[0]

    def records(self, model):
        with Session(self.database) as session:
            return list(session.scalars(select(model).order_by(model.id)))

    def callback(self, data, actor=1):
        callback = MagicMock(spec=CallbackQuery)
        callback.from_user = SimpleNamespace(id=actor)
        callback.data = data
        callback.message = self.message
        callback.answer = AsyncMock()
        return callback

    async def test_all_and_selected_assignments_are_persisted_and_localized(self):
        homework_id = await self.create()
        with Session(self.database) as session:
            assignments = list(session.scalars(select(HomeworkAssignment)))
            self.assertEqual({a.student_id for a in assignments}, {1, 2, 3})
        messages = self.records(HomeworkNotification)
        self.assertEqual(len(messages), 3)
        self.assertIn("New homework", messages[0].text)
        self.assertIn("Новое задание", messages[1].text)
        self.assertIn("Жаңа тапсырма", messages[2].text)
        self.assertEqual((await service.student_homeworks(1))[1], 1)
        selected = await self.create({2})
        self.assertEqual((await service.student_homeworks(1))[1], 1)
        self.assertEqual((await service.student_homeworks(2))[1], 2)
        self.assertNotEqual(homework_id, selected)

    async def test_invalid_selection_and_deadline_leave_no_partial_records(self):
        for selected in ({999}, set(), {1, 999}):
            with self.assertRaises(service.HomeworkError):
                await self.create(selected)
        with self.assertRaises(service.HomeworkError):
            await service.create_homework(99, {99}, "Test", "Details", datetime.now(timezone.utc) - timedelta(seconds=1))
        self.assertEqual(self.records(Homework), [])
        self.assertEqual(self.records(HomeworkNotification), [])

    async def test_access_is_checked_on_view_and_submit(self):
        homework_id = await self.create({1})
        for action in (service.student_detail(2, homework_id), service.submit_answer(2, homework_id, "Stolen answer")):
            with self.assertRaisesRegex(service.HomeworkError, "missing"):
                await action
        with self.assertRaisesRegex(service.HomeworkError, "profile"):
            await service.student_homeworks(999)
        await self.ui.callback(self.callback(f"hw:submit:{homework_id}", actor=2), self.state)
        self.state.set_state.assert_not_awaited()
        self.assertIn("не назначено", self.message.answer.call_args.args[0])
        self.assertEqual(self.records(HomeworkSubmission), [])

    async def test_submission_pending_review_and_duplicate_prevention(self):
        homework_id = await self.create({1})
        await service.submit_answer(1, homework_id, " My answer ")
        submission = self.records(HomeworkSubmission)[0]
        self.assertEqual((submission.answer, submission.status, submission.revision, submission.completed),
                         ("My answer", "submitted", 1, False))
        with self.assertRaisesRegex(service.HomeworkError, "locked"):
            await service.submit_answer(1, homework_id, "Second answer")
        rows, total, _ = await service.admin_homeworks(99, {99}, pending=True)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0][0].answer, "My answer")
        rows, _, _ = await service.admin_homeworks(99, {99})
        self.assertEqual(tuple(rows[0][1:]), (1, 1, 0, 0))

    async def test_reject_feedback_resubmit_approve_and_stale_reviews(self):
        homework_id = await self.create({1})
        await service.submit_answer(1, homework_id, "First")
        submission = self.records(HomeworkSubmission)[0]
        await service.review_submission(99, {99}, submission.id, 1, "rejected", "Please explain more")
        detail = await service.student_detail(1, homework_id)
        self.assertEqual(detail[1].feedback, "Please explain more")
        self.assertFalse(detail[1].completed)
        await service.submit_answer(1, homework_id, "Improved answer")
        with self.assertRaises(service.HomeworkError):
            await service.review_submission(99, {99}, submission.id, 1, "approved", "Old button")
        await service.review_submission(99, {99}, submission.id, 2, "approved", "Good work")
        submission = self.records(HomeworkSubmission)[0]
        self.assertEqual((submission.status, submission.revision, submission.reviewed_by), ("approved", 2, 99))
        self.assertTrue(submission.completed)
        self.assertIsNotNone(submission.completed_at)
        with self.assertRaises(service.HomeworkError):
            await service.review_submission(99, {99}, submission.id, 2, "rejected", "Replay")
        with self.assertRaises(service.HomeworkError):
            await service.submit_answer(1, homework_id, "Third answer")
        self.assertEqual(len([n for n in self.records(HomeworkNotification) if n.event_key.startswith("review:")]), 2)

    async def test_admin_permissions_on_services_commands_callbacks_and_states(self):
        self.factory.reset_mock()
        with self.assertRaises(PermissionError):
            await service.create_homework(1, {99}, "Title", "Description", self.deadline)
        with self.assertRaises(PermissionError):
            await service.review_submission(1, {99}, 1, 1, "approved", "Nice")
        with self.assertRaises(PermissionError):
            await service.admin_homeworks(1, {99})
        await self.ui.addhomework(self.message, self.state)
        await self.ui.homeworks(self.message)
        await self.ui.submissions(self.message)
        await self.ui.create_input(self.message, self.state)
        await self.ui.feedback_input(self.message, self.state)
        for data in ("hw:active:0", "hw:pending:0", "hw:grade:1:1:approved"):
            callback = self.callback(data)
            await self.ui.callback(callback, self.state)
            callback.answer.assert_awaited_once_with("Not authorized.", show_alert=True)
        self.factory.assert_not_called()
        self.state.set_state.assert_not_awaited()

    async def test_expired_deadline_and_invalid_answers(self):
        homework_id = await self.create({1})
        for answer in (None, " ", "x" * 4001):
            with self.assertRaisesRegex(service.HomeworkError, "invalid_answer"):
                await service.submit_answer(1, homework_id, answer)
        with Session(self.database) as session:
            session.get(Homework, homework_id).deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
        with self.assertRaisesRegex(service.HomeworkError, "overdue"):
            await service.submit_answer(1, homework_id, "Late answer")
        self.assertEqual((await service.admin_homeworks(99, {99}))[1], 0)
        await self.ui.show_student(self.message, 1, homework_id=homework_id)
        self.assertIn("Deadline passed", self.message.answer.call_args.args[0])
        self.assertIsNone(self.message.answer.call_args.kwargs["reply_markup"])

    async def test_reminders_are_persistent_and_not_duplicated(self):
        homework_id = await self.create()
        await service.submit_answer(2, homework_id, "Waiting review")
        await service.queue_reminders()
        await service.queue_reminders()
        reminders = [n for n in self.records(HomeworkNotification) if n.event_key.startswith("reminder:")]
        self.assertEqual({n.student_id for n in reminders}, {1, 3})
        await service.deliver_notifications(AsyncMock())
        await service.queue_reminders()
        self.assertEqual(len([n for n in self.records(HomeworkNotification) if n.event_key.startswith("reminder:")]), 2)
        # Production statement uses a lock restricted to assignment rows.
        query = next(q for q in self.queries if "SKIP LOCKED" in str(q.compile(dialect=postgresql.dialect())) and "homework_assignments" in str(q))
        sql = str(query.compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE OF homework_assignments SKIP LOCKED", sql)

    async def test_reminders_skip_expired_distant_and_approved_work(self):
        homework_id = await self.create({1})
        await service.submit_answer(1, homework_id, "Answer")
        submission = self.records(HomeworkSubmission)[0]
        await service.review_submission(99, {99}, submission.id, 1, "approved", "Good")
        await service.queue_reminders()
        self.deadline = datetime.now(timezone.utc) + timedelta(days=3)
        await self.create({2})
        await service.queue_reminders()
        await service.queue_reminders(datetime.now(timezone.utc) + timedelta(days=4))
        self.assertEqual([n for n in self.records(HomeworkNotification) if n.event_key.startswith("reminder:")], [])

    async def test_delivery_claim_prevents_repeated_or_ambiguous_notifications(self):
        await self.create({1})
        bot = AsyncMock()
        await service.deliver_notifications(bot)
        await service.deliver_notifications(bot)
        bot.send_message.assert_awaited_once()
        self.assertEqual(self.records(HomeworkNotification)[0].status, "sent")
        await self.create({1})
        bot.send_message.side_effect = TelegramForbiddenError(method=SendMessage(chat_id=1, text="Notice"), message="Blocked")
        await service.deliver_notifications(bot)
        count = bot.send_message.await_count
        await service.deliver_notifications(bot)
        self.assertEqual(bot.send_message.await_count, count)
        self.assertEqual(self.records(HomeworkNotification)[-1].status, "failed")

    async def test_interrupted_sending_claim_is_not_retried(self):
        await self.create({1})
        with Session(self.database) as session:
            session.scalar(select(HomeworkNotification)).status = "sending"
            session.commit()
        bot = AsyncMock()
        await service.deliver_notifications(bot)
        bot.send_message.assert_not_awaited()

    async def test_outdated_queued_reminder_is_cancelled_after_submission(self):
        homework_id = await self.create({1})
        await service.queue_reminders()
        await service.submit_answer(1, homework_id, "On time")
        bot = AsyncMock()
        await service.deliver_notifications(bot)
        # The new-assignment notice is sent, the obsolete reminder is not.
        bot.send_message.assert_awaited_once()
        reminder = next(n for n in self.records(HomeworkNotification) if n.event_key.startswith("reminder:"))
        self.assertEqual(reminder.status, "cancelled")

    async def test_queued_webinar_notice_is_cancelled_if_session_has_started(self):
        with Session(self.database) as session:
            session.add(main.Webinar(id=1, title="Past webinar", description="Details",
                starts_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
            session.add(HomeworkNotification(event_key="webinar:1:reminder_1h:1", student_id=1, text="Reminder"))
            session.commit()
        bot = AsyncMock()
        await service.deliver_notifications(bot)
        bot.send_message.assert_not_awaited()
        self.assertEqual(self.records(HomeworkNotification)[0].status, "cancelled")

    async def test_rate_limit_retries_only_the_rejected_chunk(self):
        send = AsyncMock(side_effect=[TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="Notice"), message="Retry", retry_after=1,
        ), None])
        with patch.object(service.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await service.send_with_retry(send, chat_id=1, text="Notice")
            sleep.assert_awaited_once_with(1)
        self.assertEqual(send.await_count, 2)

    async def test_old_completion_button_cannot_write_approval(self):
        homework_id = await self.create({1})
        callback = self.callback("homework:complete")
        with patch.object(main.homework_handlers, "show_student", new_callable=AsyncMock) as show:
            await main.complete_homework(callback)
            show.assert_awaited_once_with(self.message, 1)
        self.assertEqual(self.records(HomeworkSubmission), [])

    async def test_new_commands_precede_state_handlers(self):
        callbacks = [entry.callback.__name__ for entry in main.dp.message.handlers]
        for command in ("addhomework", "homeworks", "submissions", "myhomework", "add_webinar_command",
                        "stats_command", "open_support_command", "students_command"):
            for state_handler in ("create_input", "answer_input", "feedback_input", "registration_name_handler"):
                self.assertLess(callbacks.index(command), callbacks.index(state_handler))

    async def test_long_unicode_homework_and_submission_are_split_safely(self):
        await handlers.send_text(self.message, "\U0001f600" * 12000)
        for call in self.message.answer.call_args_list:
            self.assertLessEqual(len(call.args[0].encode("utf-16-le")) // 2, 4096)
            self.assertIsNone(call.kwargs["parse_mode"])

    async def test_full_creation_and_submission_ui(self):
        self.message.from_user = SimpleNamespace(id=99)
        await self.ui.addhomework(self.message, self.state)
        for state, value in (
            (handlers.CreateHomework.title.state, "New assignment"),
            (handlers.CreateHomework.description.state, "Explain your reasoning"),
            (handlers.CreateHomework.deadline.state, self.deadline.astimezone(service.LOCAL_TZ).strftime("%Y-%m-%d %H:%M")),
            (handlers.CreateHomework.recipients.state, "1,2"),
        ):
            self.state.get_state.return_value = state
            self.message.text = value
            await self.ui.create_input(self.message, self.state)
        homework = self.records(Homework)[0]
        self.assertEqual(homework.title, "New assignment")
        self.message.from_user = SimpleNamespace(id=1)
        await self.ui.callback(self.callback(f"hw:submit:{homework.id}"), self.state)
        self.message.text = "My answer"
        await self.ui.answer_input(self.message, self.state)
        submission = self.records(HomeworkSubmission)[0]
        self.assertEqual(submission.answer, "My answer")
        self.message.from_user = SimpleNamespace(id=99)
        await self.ui.callback(self.callback(f"hw:grade:{submission.id}:1:approved", actor=99), self.state)
        self.message.text = "Well done"
        await self.ui.feedback_input(self.message, self.state)
        self.assertTrue(self.records(HomeworkSubmission)[0].completed)

    async def test_localized_errors_and_database_failures(self):
        self.assertEqual(set(TEXTS), set(main.LANGUAGES))
        for language, messages in TEXTS.items():
            self.assertEqual(set(messages), set(texts(None)))
        self.factory.side_effect = SQLAlchemyError("private connection details")
        await self.ui.show_student(self.message, 1)
        self.assertEqual(self.message.answer.call_args.args[0], texts(None)["db"])
        await self.ui.show_admin(self.message, 99)
        self.assertNotIn("private", self.message.answer.call_args.args[0])


class MigrationTests(unittest.TestCase):
    def test_additive_migration_preserves_legacy_data_and_is_idempotent(self):
        database = create_engine("sqlite:///:memory:")
        self.addCleanup(database.dispose)
        with database.begin() as connection:
            connection.execute(text("CREATE TABLE students (telegram_id BIGINT PRIMARY KEY, language VARCHAR(30), full_name VARCHAR(100), course_name VARCHAR(100), created_at TIMESTAMP NOT NULL)"))
            connection.execute(text("CREATE TABLE homework (id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL, description TEXT NOT NULL, due_date DATE, created_at TIMESTAMP NOT NULL)"))
            connection.execute(text("CREATE TABLE homework_submissions (id INTEGER PRIMARY KEY, student_id BIGINT NOT NULL, homework_id INTEGER NOT NULL, completed BOOLEAN NOT NULL, completed_at TIMESTAMP, UNIQUE(student_id, homework_id))"))
            connection.execute(text("INSERT INTO students VALUES (1, NULL, 'Legacy student', NULL, CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO homework VALUES (1, 'Legacy task', 'Keep instructions', '2026-10-25', CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO homework_submissions VALUES (1, 1, 1, 1, CURRENT_TIMESTAMP)"))
            Base.metadata.create_all(connection)
            migrate_homework(connection)
            migrate_homework(connection)
        with Session(database) as session:
            homework = session.get(Homework, 1)
            self.assertEqual(homework.description, "Keep instructions")
            self.assertEqual(service.utc(homework.deadline).isoformat(), "2026-10-25T18:59:59+00:00")
            submission = session.get(HomeworkSubmission, 1)
            self.assertEqual(submission.status, "approved")
            self.assertIsNone(submission.answer)
            self.assertIsNone(submission.reviewed_by)
            self.assertEqual(len(list(session.scalars(select(HomeworkAssignment)))), 1)
            self.assertEqual(session.execute(text("SELECT count(*) FROM schema_migrations")).scalar(), 1)


if __name__ == "__main__":
    unittest.main()
