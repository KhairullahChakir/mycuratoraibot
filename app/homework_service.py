"""Transactional homework operations and persistent notification delivery."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.database.db import AsyncSessionLocal
from app.database.models import Homework, HomeworkAssignment, HomeworkNotification, HomeworkSubmission, Student
from app.homework_texts import texts


LOCAL_TZ = ZoneInfo("Asia/Almaty")
DB_ERRORS = (SQLAlchemyError, OSError, TimeoutError)


class HomeworkError(ValueError):
    pass


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def deadline_text(value, language=None):
    return utc(value).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M (UTC+05:00)") if value else texts(language)["not_provided"]


def require_admin(user_id, admin_ids):
    if user_id not in admin_ids:
        raise PermissionError("Not authorized")


def queue_notification(session, key, student, message):
    session.add(HomeworkNotification(event_key=key, student_id=student.telegram_id, text=message))


async def send_with_retry(send, *args, **kwargs):
    # Telegram explicitly rejects rate-limited calls; retry only that unsent
    # chunk. Do not retry timeouts or other ambiguous transport failures.
    while True:
        try:
            return await send(*args, **kwargs)
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)


async def create_homework(actor, admin_ids, title, description, deadline, selected=None):
    require_admin(actor, admin_ids)
    title, description = title.strip(), description.strip()
    if not 2 <= len(title) <= 200 or not 1 <= len(description) <= 4000 or utc(deadline) <= datetime.now(timezone.utc):
        raise HomeworkError("Invalid title, description, or future deadline.")
    async with AsyncSessionLocal() as session:
        query = select(Student)
        if selected is not None:
            if not selected or len(selected) > 100:
                raise HomeworkError("Select 1–100 existing students, or use all.")
            query = query.where(Student.telegram_id.in_(selected))
        students = list((await session.execute(query.order_by(Student.telegram_id))).scalars().all())
        if not students or (selected is not None and {s.telegram_id for s in students} != set(selected)):
            raise HomeworkError("No students found, or a selected Telegram ID does not exist.")
        homework = Homework(title=title, description=description, deadline=utc(deadline), due_date=utc(deadline).astimezone(LOCAL_TZ).date())
        session.add(homework)
        await session.flush()
        for student in students:
            session.add(HomeworkAssignment(student_id=student.telegram_id, homework_id=homework.id))
            queue_notification(session, f"assigned:{homework.id}:{student.telegram_id}", student,
                texts(student.language)["new"].format(id=homework.id, title=title, deadline=deadline_text(deadline, student.language)))
        homework_id = homework.id
        await session.commit()
        return homework_id, len(students)


async def student_language(student_id):
    async with AsyncSessionLocal() as session:
        student = await session.get(Student, student_id)
        return student.language if student else None


async def student_homeworks(student_id, page=0):
    async with AsyncSessionLocal() as session:
        if await session.get(Student, student_id) is None:
            raise HomeworkError("profile")
        total = await session.scalar(select(func.count()).select_from(HomeworkAssignment).where(HomeworkAssignment.student_id == student_id))
        page = min(max(0, page), max(0, (total - 1) // 10))
        rows = (await session.execute(
            select(Homework, HomeworkSubmission).join(HomeworkAssignment, HomeworkAssignment.homework_id == Homework.id)
            .outerjoin(HomeworkSubmission, and_(HomeworkSubmission.homework_id == Homework.id, HomeworkSubmission.student_id == student_id))
            .where(HomeworkAssignment.student_id == student_id)
            .order_by(Homework.deadline.desc(), Homework.id.desc()).limit(10).offset(page * 10)
        )).all()
        return rows, total, page


async def assigned_homework(session, student_id, homework_id, lock=False):
    query = select(HomeworkAssignment).where(
        HomeworkAssignment.student_id == student_id, HomeworkAssignment.homework_id == homework_id,
    )
    if lock:
        query = query.with_for_update()
    if (await session.execute(query)).scalar_one_or_none() is None:
        raise HomeworkError("missing")
    homework = await session.get(Homework, homework_id)
    submission = (await session.execute(select(HomeworkSubmission).where(
        HomeworkSubmission.student_id == student_id, HomeworkSubmission.homework_id == homework_id,
    ))).scalar_one_or_none()
    return homework, submission


def can_submit(homework, submission):
    if homework.deadline and utc(homework.deadline) <= datetime.now(timezone.utc):
        raise HomeworkError("overdue")
    if submission and (submission.completed or submission.status not in ("not_submitted", "rejected")):
        raise HomeworkError("locked")


async def student_detail(student_id, homework_id, for_submit=False):
    async with AsyncSessionLocal() as session:
        homework, submission = await assigned_homework(session, student_id, homework_id)
        if for_submit:
            can_submit(homework, submission)
        return homework, submission


async def submit_answer(student_id, homework_id, answer):
    if not answer or not 1 <= len(answer.strip()) <= 4000:
        raise HomeworkError("invalid_answer")
    async with AsyncSessionLocal() as session:
        # Lock the assignment even for the first answer, when no submission exists.
        homework, submission = await assigned_homework(session, student_id, homework_id, lock=True)
        can_submit(homework, submission)
        if submission is None:
            submission = HomeworkSubmission(student_id=student_id, homework_id=homework_id, revision=0)
            session.add(submission)
        submission.answer = answer.strip()
        submission.status = "submitted"
        submission.revision = (submission.revision or 0) + 1
        submission.submitted_at = datetime.now(timezone.utc)
        submission.completed = False
        submission.completed_at = None
        submission.feedback = None
        submission.reviewed_at = None
        submission.reviewed_by = None
        await session.commit()


async def review_submission(actor, admin_ids, submission_id, revision, decision, feedback):
    require_admin(actor, admin_ids)
    if decision not in ("approved", "rejected") or not feedback or not 1 <= len(feedback.strip()) <= 4000:
        raise HomeworkError("Enter feedback of 1–4000 characters.")
    async with AsyncSessionLocal() as session:
        submission = (await session.execute(select(HomeworkSubmission).where(
            HomeworkSubmission.id == submission_id,
        ).with_for_update())).scalar_one_or_none()
        if submission is None or submission.status != "submitted" or submission.revision != revision:
            raise HomeworkError("This submission was already reviewed or changed. Open /submissions again.")
        student = await session.get(Student, submission.student_id)
        homework = await session.get(Homework, submission.homework_id)
        now = datetime.now(timezone.utc)
        submission.status = decision
        submission.feedback = feedback.strip()
        submission.reviewed_by = actor
        submission.reviewed_at = now
        submission.completed = decision == "approved"
        submission.completed_at = now if submission.completed else None
        lang = texts(student.language)
        queue_notification(session, f"review:{submission.id}:{revision}", student,
            lang["review"].format(id=homework.id, title=homework.title, status=lang[decision], feedback=submission.feedback))
        await session.commit()


async def admin_homeworks(actor, admin_ids, page=0, pending=False):
    require_admin(actor, admin_ids)
    async with AsyncSessionLocal() as session:
        if pending:
            condition = HomeworkSubmission.status == "submitted"
            total = await session.scalar(select(func.count()).select_from(HomeworkSubmission).where(condition))
            page = min(max(0, page), max(0, (total - 1) // 10))
            rows = (await session.execute(
                select(HomeworkSubmission, Homework, Student)
                .join(Homework, HomeworkSubmission.homework_id == Homework.id)
                .join(Student, HomeworkSubmission.student_id == Student.telegram_id)
                .where(condition).order_by(HomeworkSubmission.submitted_at, HomeworkSubmission.id)
                .limit(10).offset(page * 10)
            )).all()
        else:
            active = or_(Homework.deadline.is_(None), Homework.deadline > datetime.now(timezone.utc))
            total = await session.scalar(select(func.count()).select_from(Homework).where(active))
            page = min(max(0, page), max(0, (total - 1) // 10))
            assignments = select(func.count()).select_from(HomeworkAssignment).where(HomeworkAssignment.homework_id == Homework.id).correlate(Homework).scalar_subquery()
            def count_status(status):
                return select(func.count()).select_from(HomeworkSubmission).where(
                    HomeworkSubmission.homework_id == Homework.id, HomeworkSubmission.status == status,
                ).correlate(Homework).scalar_subquery()
            rows = (await session.execute(select(Homework, assignments, count_status("submitted"), count_status("approved"), count_status("rejected"))
                .where(active).order_by(Homework.deadline, Homework.id).limit(10).offset(page * 10))).all()
        return rows, total, page


async def queue_reminders(now=None):
    now = now or datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        awaiting_or_complete = select(HomeworkSubmission.id).where(
            HomeworkSubmission.student_id == HomeworkAssignment.student_id,
            HomeworkSubmission.homework_id == HomeworkAssignment.homework_id,
            or_(HomeworkSubmission.completed.is_(True), HomeworkSubmission.status == "submitted"),
        ).correlate(HomeworkAssignment).exists()
        rows = (await session.execute(
            select(HomeworkAssignment, Homework, Student)
            .join(Homework, HomeworkAssignment.homework_id == Homework.id)
            .join(Student, HomeworkAssignment.student_id == Student.telegram_id)
            .where(HomeworkAssignment.reminder_queued.is_(False), Homework.deadline > now,
                   Homework.deadline <= now + timedelta(hours=24), ~awaiting_or_complete)
            .order_by(HomeworkAssignment.homework_id, HomeworkAssignment.student_id)
            .with_for_update(of=HomeworkAssignment, skip_locked=True).limit(100)
        )).all()
        for assignment, homework, student in rows:
            submission = (await session.execute(select(HomeworkSubmission).where(
                HomeworkSubmission.student_id == student.telegram_id, HomeworkSubmission.homework_id == homework.id,
            ))).scalar_one_or_none()
            if submission and (submission.completed or submission.status == "submitted"):
                continue
            assignment.reminder_queued = True
            queue_notification(session, f"reminder:{homework.id}:{student.telegram_id}", student,
                texts(student.language)["reminder"].format(id=homework.id, title=homework.title, deadline=deadline_text(homework.deadline, student.language)))
        await session.commit()


async def deliver_notifications(bot, limit=100):
    for _ in range(limit):
        async with AsyncSessionLocal() as session:
            notification = (await session.execute(select(HomeworkNotification)
                .where(HomeworkNotification.status == "pending").order_by(HomeworkNotification.id)
                .with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
            if notification is None:
                return
            if notification.event_key.startswith("reminder:"):
                homework_id = int(notification.event_key.split(":")[1])
                homework = await session.get(Homework, homework_id)
                submission = (await session.execute(select(HomeworkSubmission).where(
                    HomeworkSubmission.student_id == notification.student_id,
                    HomeworkSubmission.homework_id == homework_id,
                ))).scalar_one_or_none()
                if (homework is None or homework.deadline is None or
                    utc(homework.deadline) <= datetime.now(timezone.utc) or
                    (submission and (submission.completed or submission.status == "submitted"))):
                    notification.status = "cancelled"
                    await session.commit()
                    continue
            notification.status = "sending"
            notification_id, student_id, message = notification.id, notification.student_id, notification.text
            # Commit the claim before contacting Telegram. An interrupted/ambiguous
            # send is never automatically repeated (Telegram has no idempotency key).
            await session.commit()
        status = "sent"
        try:
            for offset in range(0, len(message), 1800):
                await send_with_retry(bot.send_message, chat_id=student_id, text=message[offset:offset + 1800], parse_mode=None)
        except (TelegramAPIError, OSError, TimeoutError):
            status = "failed"
        async with AsyncSessionLocal() as session:
            notification = await session.get(HomeworkNotification, notification_id)
            if notification is not None:
                notification.status = status
                notification.sent_at = datetime.now(timezone.utc) if status == "sent" else None
                await session.commit()


async def homework_worker(bot):
    while True:
        try:
            await queue_reminders()
            await deliver_notifications(bot)
        except asyncio.CancelledError:
            raise
        except DB_ERRORS:
            print("Homework scheduler: database temporarily unavailable.")
        except Exception:
            # Keep the worker alive without logging connection strings or content.
            print("Homework scheduler: notification processing failed.")
        await asyncio.sleep(60)
