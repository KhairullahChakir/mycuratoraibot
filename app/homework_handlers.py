"""aiogram handlers for persistent homework assignments and reviews."""
from datetime import datetime, timezone

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from app.admin_students import button, positive_id, provided
from app.database.db import AsyncSessionLocal
from app.database.models import HomeworkSubmission
from app.homework_texts import texts
from app import homework_service as service


async def send_text(message, text, keyboard=None):
    for offset in range(0, len(text), 1800):
        await service.send_with_retry(
            message.answer, text[offset:offset + 1800], parse_mode=None,
            reply_markup=keyboard if offset + 1800 >= len(text) else None,
        )


class CreateHomework(StatesGroup):
    title = State()
    description = State()
    deadline = State()
    recipients = State()
    feedback = State()


class AnswerHomework(StatesGroup):
    answer = State()


async def language_for(user_id):
    try:
        return await service.student_language(user_id)
    except service.DB_ERRORS:
        return None


def pagination(prefix, page, total, lang):
    row = []
    if page > 0:
        row.append(button(lang["previous"], f"{prefix}:{page - 1}"))
    if (page + 1) * 10 < total:
        row.append(button(lang["next"], f"{prefix}:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


def homework_id(value):
    result = positive_id(value)
    if result > 2147483647:
        raise ValueError("Invalid homework or submission ID")
    return result


class HomeworkHandlers:
    def __init__(self, admin_ids, main_menu):
        self.admin_ids = admin_ids
        self.main_menu = main_menu

    async def authorize(self, event):
        if event.from_user and event.from_user.id in self.admin_ids:
            return True
        if isinstance(event, CallbackQuery):
            await event.answer("Not authorized.", show_alert=True)
        else:
            await event.answer("This command is restricted to bot administrators.")
        return False

    async def addhomework(self, message, state):
        if not await self.authorize(message):
            return
        await state.clear()
        await state.set_state(CreateHomework.title)
        await message.answer("Enter the homework title (2–200 characters). Use /cancel to cancel.", reply_markup=ReplyKeyboardRemove())

    async def create_input(self, message, state):
        if not await self.authorize(message):
            return
        text = (message.text or "").strip()
        current = await state.get_state()
        if not text or text.startswith("/"):
            await message.answer("Please send text, or use /cancel.")
            return
        if current == CreateHomework.title.state:
            if not 2 <= len(text) <= 200:
                await message.answer("The title must contain 2–200 characters.")
                return
            await state.update_data(hw_title=text)
            await state.set_state(CreateHomework.description)
            await message.answer("Enter the instructions (1–4000 characters).")
        elif current == CreateHomework.description.state:
            if len(text) > 4000:
                await message.answer("Limit the instructions to 4000 characters.")
                return
            await state.update_data(hw_description=text)
            await state.set_state(CreateHomework.deadline)
            await message.answer("Enter a future deadline in Almaty time: YYYY-MM-DD HH:MM (UTC+05:00).")
        elif current == CreateHomework.deadline.state:
            try:
                deadline = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=service.LOCAL_TZ)
                if deadline <= datetime.now(service.LOCAL_TZ):
                    raise ValueError
            except ValueError:
                await message.answer("Enter a future deadline using YYYY-MM-DD HH:MM, in Almaty time.")
                return
            await state.update_data(hw_deadline=deadline.astimezone(timezone.utc).isoformat())
            await state.set_state(CreateHomework.recipients)
            await message.answer("Send all to assign to every current student, or comma-separated Telegram IDs (up to 100). Use /students to look up IDs.")
        else:
            data = await state.get_data()
            try:
                selected = None if text.lower() == "all" else {positive_id(item.strip()) for item in text.split(",")}
                homework_id, count = await service.create_homework(
                    message.from_user.id, self.admin_ids, data["hw_title"], data["hw_description"],
                    datetime.fromisoformat(data["hw_deadline"]), selected,
                )
            except (ValueError, KeyError) as error:
                if isinstance(error, service.HomeworkError):
                    await message.answer(str(error))
                else:
                    await message.answer("Enter all or valid comma-separated student IDs. Use /cancel to restart.")
                return
            except service.DB_ERRORS:
                await message.answer(texts(None)["db"])
                return
            await state.clear()
            await message.answer(f"Homework #{homework_id} created and assigned to {count} students. Notifications queued.",
                                 reply_markup=self.main_menu(await language_for(message.from_user.id)))

    async def myhomework(self, message):
        if message.from_user:
            await self.show_student(message, message.from_user.id)

    async def show_student(self, message, student_id, page=0, homework_id=None):
        language = await language_for(student_id)
        lang = texts(language)
        try:
            if homework_id is None:
                rows, total, page = await service.student_homeworks(student_id, page)
                await send_text(message, f"{lang['list']} ({total})" if rows else lang["empty"])
                for homework, submission in rows:
                    status = submission.status if submission else "not_submitted"
                    if submission and submission.completed:
                        status = "approved"
                    await send_text(message, f"#{homework.id}: {homework.title}\n{lang['deadline']}: {service.deadline_text(homework.deadline, language)}\n{lang['status']}: {lang.get(status, status)}",
                        InlineKeyboardMarkup(inline_keyboard=[[button(lang["view"], f"hw:view:{homework.id}")]]))
                keyboard = pagination("hw:list", page, total, lang)
                if keyboard:
                    await send_text(message, f"{page + 1}/{max(1, (total + 9) // 10)}", keyboard)
            else:
                homework, submission = await service.student_detail(student_id, homework_id)
                status = "approved" if submission and submission.completed else submission.status if submission else "not_submitted"
                feedback = submission.feedback if submission else None
                answer = submission.answer if submission else None
                text = (f"#{homework.id}: {homework.title}\n{homework.description}\n\n"
                        f"{lang['deadline']}: {service.deadline_text(homework.deadline, language)}\n"
                        f"{lang['status']}: {lang.get(status, status)}\n"
                        f"{lang['feedback']}: {feedback or lang['not_provided']}\n"
                        f"{lang['answer']}: {answer or lang['not_provided']}")
                keyboard = None
                try:
                    service.can_submit(homework, submission)
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button(lang["submit"], f"hw:submit:{homework.id}")]])
                except service.HomeworkError as error:
                    if str(error) == "overdue":
                        text += f"\n{lang['overdue']}"
                await send_text(message, text, keyboard)
        except service.HomeworkError as error:
            await send_text(message, lang.get(str(error), lang["missing"]))
        except service.DB_ERRORS:
            await send_text(message, lang["db"])

    async def answer_input(self, message, state):
        if not message.from_user:
            return
        language = await language_for(message.from_user.id)
        lang = texts(language)
        data = await state.get_data()
        try:
            homework_id = positive_id(str(data.get("answer_homework_id", "")))
            if (message.text or "").startswith("/"):
                raise service.HomeworkError("invalid_answer")
            await service.submit_answer(message.from_user.id, homework_id, message.text)
        except service.HomeworkError as error:
            await message.answer(lang.get(str(error), lang["invalid"]))
            return
        except ValueError:
            await state.clear()
            await message.answer(lang["invalid"])
            return
        except service.DB_ERRORS:
            await message.answer(lang["db"])
            return
        await state.clear()
        await message.answer(lang["saved"], reply_markup=self.main_menu(language))

    async def homeworks(self, message):
        if await self.authorize(message):
            await self.show_admin(message, message.from_user.id)

    async def submissions(self, message):
        if await self.authorize(message):
            await self.show_admin(message, message.from_user.id, pending=True)

    async def show_admin(self, message, actor, page=0, pending=False):
        try:
            rows, total, page = await service.admin_homeworks(actor, self.admin_ids, page, pending)
        except service.DB_ERRORS:
            await send_text(message, texts(None)["db"])
            return
        title = "Pending submissions" if pending else "Active homework"
        await send_text(message, f"{title}: {total}" + ("\nNo records found." if not rows else ""))
        for row in rows:
            if pending:
                submission, homework, student = row
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    button("Approve", f"hw:grade:{submission.id}:{submission.revision}:approved"),
                    button("Reject", f"hw:grade:{submission.id}:{submission.revision}:rejected"),
                ]])
                await send_text(message,
                    f"Submission #{submission.id} (revision {submission.revision})\nHomework #{homework.id}: {homework.title}\n"
                    f"Student: {provided(student.full_name)} ({student.telegram_id})\n"
                    f"Instructions:\n{homework.description}\nAnswer:\n{provided(submission.answer)}", keyboard)
            else:
                homework, assigned, submitted, approved, rejected = row
                await send_text(message, f"#{homework.id}: {homework.title}\nDeadline: {service.deadline_text(homework.deadline)}\n"
                    f"Assigned: {assigned}; pending review: {submitted}; approved: {approved}; rejected: {rejected}\n"
                    f"Not submitted: {max(0, assigned - submitted - approved - rejected)}")
        keyboard = pagination("hw:pending" if pending else "hw:active", page, total, texts(None))
        if keyboard:
            await send_text(message, f"Page {page + 1}", keyboard)

    async def feedback_input(self, message, state):
        if not await self.authorize(message):
            return
        if (message.text or "").startswith("/"):
            await message.answer("Please enter feedback as text, or use /cancel.")
            return
        data = await state.get_data()
        try:
            await service.review_submission(message.from_user.id, self.admin_ids,
                data["grade_id"], data["grade_revision"], data["grade_decision"], message.text)
        except (service.HomeworkError, KeyError) as error:
            await message.answer(str(error) if isinstance(error, service.HomeworkError) else "Open /submissions again.")
            return
        except service.DB_ERRORS:
            await message.answer(texts(None)["db"])
            return
        await state.clear()
        await message.answer("Review saved. The student notification is queued.",
                             reply_markup=self.main_menu(await language_for(message.from_user.id)))

    async def callback(self, callback, state):
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action in ("grade", "active", "pending") and not await self.authorize(callback):
            return
        if not isinstance(callback.message, Message):
            await callback.answer("Use /start to reopen the bot.", show_alert=True)
            return
        await callback.answer()
        language = await language_for(callback.from_user.id)
        lang = texts(language)
        try:
            if action in ("list", "active", "pending") and len(parts) == 3:
                page = int(parts[2]) if parts[2].isascii() and parts[2].isdecimal() and len(parts[2]) <= 9 else -1
                if page < 0:
                    raise ValueError
                if action == "list":
                    await self.show_student(callback.message, callback.from_user.id, page)
                else:
                    await self.show_admin(callback.message, callback.from_user.id, page, action == "pending")
            elif action in ("view", "submit") and len(parts) == 3:
                assignment_id = homework_id(parts[2])
                if action == "view":
                    await self.show_student(callback.message, callback.from_user.id, homework_id=assignment_id)
                else:
                    await service.student_detail(callback.from_user.id, assignment_id, for_submit=True)
                    await state.clear()
                    await state.update_data(answer_homework_id=assignment_id)
                    await state.set_state(AnswerHomework.answer)
                    await callback.message.answer(lang["prompt"], reply_markup=ReplyKeyboardRemove())
            elif action == "grade" and len(parts) == 5:
                submission_id, revision = homework_id(parts[2]), homework_id(parts[3])
                if parts[4] not in ("approved", "rejected"):
                    raise ValueError
                async with AsyncSessionLocal() as session:
                    submission = await session.get(HomeworkSubmission, submission_id)
                    if submission is None or submission.status != "submitted" or submission.revision != revision:
                        await callback.message.answer("Submission unavailable or changed. Open /submissions again.")
                        return
                await state.clear()
                await state.update_data(grade_id=submission_id, grade_revision=revision, grade_decision=parts[4])
                await state.set_state(CreateHomework.feedback)
                await callback.message.answer(f"Decision: {parts[4]}. Enter feedback (1–4000 characters), or /cancel.", reply_markup=ReplyKeyboardRemove())
            else:
                raise ValueError
        except service.HomeworkError as error:
            await callback.message.answer(lang.get(str(error), lang["invalid"]))
        except ValueError:
            await callback.message.answer(lang["invalid"])
        except service.DB_ERRORS:
            await callback.message.answer(lang["db"])

    def register_states(self, dispatcher):
        for state in (CreateHomework.title, CreateHomework.description, CreateHomework.deadline, CreateHomework.recipients):
            dispatcher.message.register(self.create_input, state)
        dispatcher.message.register(self.feedback_input, CreateHomework.feedback)
        dispatcher.message.register(self.answer_input, AnswerHomework.answer)


def register_homework_handlers(dispatcher, admin_ids, main_menu):
    handlers = HomeworkHandlers(admin_ids, main_menu)
    for command, handler in (("addhomework", handlers.addhomework), ("homeworks", handlers.homeworks),
                             ("submissions", handlers.submissions), ("myhomework", handlers.myhomework)):
        dispatcher.message.register(handler, Command(command))
    dispatcher.callback_query.register(handlers.callback, F.data.startswith("hw:"))
    return handlers
