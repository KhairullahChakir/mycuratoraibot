"""Read-only admin student management, using the existing database schema."""
from datetime import datetime, timezone
from secrets import token_hex

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.database.db import AsyncSessionLocal
from app.database.models import HomeworkSubmission, Student, SupportRequest, Webinar


PAGE_SIZE = 10
DATABASE_ERRORS = (SQLAlchemyError, OSError, TimeoutError)
DATABASE_MESSAGE = "The database is temporarily unavailable. Please try again later."


def provided(value):
    return str(value).strip() if value is not None and str(value).strip() else "Not provided"


def positive_id(value):
    # PostgreSQL BigInteger range; bound length before converting untrusted input.
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise ValueError("Invalid Telegram ID")
    result = int(value)
    if not 0 < result <= 9223372036854775807:
        raise ValueError("Invalid Telegram ID")
    return result


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def send_text(message, text, keyboard=None):
    # At most 3600 UTF-16 units, including when all characters are emoji.
    for offset in range(0, len(text), 1800):
        await message.answer(
            text[offset:offset + 1800], parse_mode=None,
            reply_markup=keyboard if offset + 1800 >= len(text) else None,
        )


async def get_student_page(page=0, search=None):
    conditions = []
    if search is not None:
        # Treat % and _ literally; SQLAlchemy binds the pattern as a parameter.
        pattern = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Student.full_name.ilike(f"%{pattern}%", escape="\\"))
    async with AsyncSessionLocal() as session:
        total = await session.scalar(select(func.count()).select_from(Student).where(*conditions))
        last_page = max(0, (total - 1) // PAGE_SIZE)
        page = min(max(0, page), last_page)
        result = await session.execute(
            select(Student).where(*conditions).order_by(Student.telegram_id)
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
        return list(result.scalars().all()), total, page, last_page


async def get_student_details(student_id):
    async with AsyncSessionLocal() as session:
        student = await session.get(Student, student_id)
        if student is None:
            return None
        homework = await session.execute(
            select(HomeworkSubmission.completed, func.count())
            .where(HomeworkSubmission.student_id == student_id)
            .group_by(HomeworkSubmission.completed)
        )
        support = await session.execute(
            select(SupportRequest.status, func.count())
            .where(SupportRequest.student_id == student_id)
            .group_by(SupportRequest.status)
        )
        return student, dict(homework.all()), dict(support.all())


class StudentAdmin:
    def __init__(self, admin_ids, show_stats, show_support):
        self.admin_ids = admin_ids
        self.show_stats = show_stats
        self.show_support = show_support

    async def authorized(self, event):
        if event.from_user and event.from_user.id in self.admin_ids:
            return True
        if isinstance(event, CallbackQuery):
            await event.answer("Not authorized.", show_alert=True)
        else:
            await event.answer("This command is restricted to bot administrators.")
        return False

    async def admin_command(self, message: Message):
        if not await self.authorized(message):
            return
        await send_text(message, "Admin Dashboard", InlineKeyboardMarkup(inline_keyboard=[
            [button("Students", "admin:students:0"), button("Statistics", "admin:stats")],
            [button("Open Support Requests", "admin:support"), button("Webinars", "admin:webinars")],
        ]))

    async def students_command(self, message: Message):
        if await self.authorized(message):
            await self.show_students(message)

    async def student_command(self, message: Message):
        if not await self.authorized(message):
            return
        args = (message.text or "").split(maxsplit=1)
        try:
            student_id = positive_id(args[1] if len(args) == 2 else "")
        except ValueError:
            await send_text(message, "Usage: /student <telegram_id>. Enter a valid positive Telegram ID.")
            return
        await self.show_profile(message, student_id)

    async def findstudent_command(self, message: Message, state: FSMContext):
        if not await self.authorized(message):
            return
        args = (message.text or "").split(maxsplit=1)
        search = args[1].strip() if len(args) == 2 else ""
        if not search or len(search) > 100:
            await send_text(message, "Usage: /findstudent <name>. Enter 1–100 characters.")
            return
        # Preserve active registration/support state. Keep a bounded history so
        # pagination on earlier search messages still uses the original query.
        searches = dict((await state.get_data()).get("admin_student_searches", {}))
        token = token_hex(4)
        searches[token] = search
        searches = dict(list(searches.items())[-10:])
        await state.update_data(admin_student_searches=searches)
        await self.show_students(message, search=search, token=token)

    async def show_students(self, message, page=0, search=None, token=None, edit=False):
        try:
            students, total, page, last = await get_student_page(page, search)
        except DATABASE_ERRORS:
            await send_text(message, DATABASE_MESSAGE)
            return
        title = "Students" if search is None else f"Student search: {search}"
        lines = [title, f"Total count: {total}", f"Page {page + 1} of {last + 1}"]
        if not students:
            lines.append("No students found." if search is None else "No matching students found.")
        rows = []
        for student in students:
            # Full fields remain available in the profile; keep page text bounded.
            name = provided(student.full_name).replace("\n", " ")
            short_name = name if len(name) <= 40 else name[:39] + "…"
            lines.append(f"{student.telegram_id}: {short_name}")
            rows.append([button(f"View Profile: {student.telegram_id}", f"admin:profile:{student.telegram_id}")])
        prefix = "admin:students" if search is None else f"admin:search:{token}"
        navigation = []
        if page > 0:
            navigation.append(button("Previous", f"{prefix}:{page - 1}"))
        if page < last:
            navigation.append(button("Next", f"{prefix}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        text = "\n".join(lines)
        if edit:
            try:
                await message.edit_text(text, parse_mode=None, reply_markup=keyboard)
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    await send_text(message, text, keyboard)
        else:
            await send_text(message, text, keyboard)

    async def show_profile(self, message, student_id):
        try:
            details = await get_student_details(student_id)
        except DATABASE_ERRORS:
            await send_text(message, DATABASE_MESSAGE)
            return
        if details is None:
            await send_text(message, "Student not found.")
            return
        student, homework, support = details
        created = student.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            created = created.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = (
            f"Student Profile\nTelegram ID: {student.telegram_id}\n"
            f"Full name: {provided(student.full_name)}\n"
            f"Course: {provided(student.course_name)}\n"
            f"Selected language: {provided(student.language)}\n"
            f"Record created: {provided(created)}\n\n"
            f"Homework submissions recorded: {sum(homework.values())}\n"
            f"Completed: {homework.get(True, 0)}\n"
            f"Incomplete submissions: {homework.get(False, 0)}\n\n"
            f"Support requests total: {sum(support.values())}\n"
            f"Open: {support.get('open', 0)}\n"
            f"Answered: {support.get('answered', 0)}\n"
            f"Closed: {support.get('closed', 0)}"
        )
        await send_text(message, text, InlineKeyboardMarkup(inline_keyboard=[
            [button("Students", "admin:students:0")],
        ]))

    async def show_webinars(self, message):
        try:
            async with AsyncSessionLocal() as session:
                total = await session.scalar(select(func.count()).select_from(Webinar))
                result = await session.execute(
                    select(Webinar).where(Webinar.starts_at > datetime.now(timezone.utc))
                    .order_by(Webinar.starts_at, Webinar.id)
                )
                webinars = list(result.scalars().all())
        except DATABASE_ERRORS:
            await send_text(message, DATABASE_MESSAGE)
            return
        await send_text(message, f"Webinars\nTotal: {total}\nUpcoming: {len(webinars)}\n"
                        "Use /addwebinar to create a webinar.")
        if not webinars:
            await send_text(message, "There are no upcoming webinars.")
        for webinar in webinars:
            starts = webinar.starts_at
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=timezone.utc)
            await send_text(message,
                f"Webinar #{webinar.id}: {provided(webinar.title)}\n"
                f"Starts: {starts.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
                f"{provided(webinar.description)}\nMeeting URL: {provided(webinar.meeting_url)}")

    async def admin_callback(self, callback: CallbackQuery, state: FSMContext):
        if not await self.authorized(callback):
            return
        if not isinstance(callback.message, Message):
            await callback.answer("Open /admin again to use this dashboard.", show_alert=True)
            return
        await callback.answer()
        parts = (callback.data or "").split(":")
        try:
            if len(parts) == 2 and parts[1] in ("stats", "support", "webinars"):
                action = {"stats": self.show_stats, "support": self.show_support,
                          "webinars": self.show_webinars}[parts[1]]
                await action(callback.message)
            elif len(parts) == 3 and parts[1] == "profile":
                await self.show_profile(callback.message, positive_id(parts[2]))
            elif ((len(parts) == 3 and parts[1] == "students") or
                  (len(parts) == 4 and parts[1] == "search")):
                raw_page = parts[-1]
                if len(raw_page) > 10 or not raw_page.isascii() or not raw_page.isdecimal():
                    raise ValueError
                search, token = None, None
                if parts[1] == "search":
                    token = parts[2]
                    search = (await state.get_data()).get("admin_student_searches", {}).get(token)
                    if search is None:
                        await send_text(callback.message, "This search has expired. Use /findstudent <name> again.")
                        return
                await self.show_students(callback.message, int(raw_page), search, token, edit=True)
            else:
                raise ValueError
        except ValueError:
            await send_text(callback.message, "Invalid dashboard action or ID. Open /admin again.")
        except DATABASE_ERRORS:
            await send_text(callback.message, DATABASE_MESSAGE)


def register_admin_handlers(dispatcher, admin_ids, show_stats, show_support):
    handlers = StudentAdmin(admin_ids, show_stats, show_support)
    for command, handler in (
        ("admin", handlers.admin_command), ("students", handlers.students_command),
        ("student", handlers.student_command), ("findstudent", handlers.findstudent_command),
    ):
        dispatcher.message.register(handler, Command(command))
    dispatcher.callback_query.register(handlers.admin_callback, F.data.startswith("admin:"))
    return handlers
