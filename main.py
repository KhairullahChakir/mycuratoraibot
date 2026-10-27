
import asyncio
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import BOT_TOKEN
from app.admin_students import register_admin_handlers
from app.homework_handlers import register_homework_handlers
from app.homework_service import homework_worker
from app.homework_texts import texts as homework_texts
from app.database.db import AsyncSessionLocal, init_db, engine
from app.database.models import (
    Student,
    Homework,
    HomeworkSubmission,
    Webinar,
    SupportRequest,
)


# =====================================
# BOT SETUP
# =====================================

dp = Dispatcher(storage=MemoryStorage())

ALMATY_TZ = ZoneInfo("Asia/Almaty")

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}


# =====================================
# CONVERSATION STATES
# =====================================

class RegistrationStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_course = State()


class WebinarStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_start_time = State()
    waiting_for_meeting_url = State()


class SupportStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_reply = State()


# =====================================
# LANGUAGES
# =====================================

LANGUAGES = {
    "🇬🇧 English": {
        "welcome": "Welcome to the Student Assistant Bot! 🎓",
        "choose_language": "Please choose your language:",
        "menu_prompt": "What would you like to do?",
        "homework": "📚 My Homework",
        "webinars": "📅 My Webinars",
        "faq": "❓ FAQ",
        "change_language": "🌐 Change Language",
        "profile": "👤 Register / My Profile",

        "registration_name_prompt": "Please enter your full name:",
        "registration_course_prompt": "Which course are you studying?",
        "registration_success": "✅ Registration completed successfully!",
        "registration_cancelled": "Operation cancelled.",
        "profile_title": "👤 Your Profile",
        "profile_name": "Full name",
        "profile_course": "Course",
        "cancel_hint": "Send /cancel to cancel.",
        "enter_text": "Please send your answer as text.",
        "invalid_name": "Enter a name between 2 and 100 characters.",
        "invalid_course": "Enter a course name between 2 and 100 characters.",

        "webinars_header": "📅 Upcoming Webinars",
        "no_webinars": "There are no upcoming webinars.",
        "webinar_starts": "Starts",
        "webinar_join": "Join",
        "reminder_24h": (
            "⏰ Reminder: your webinar starts within 24 hours!"
        ),
        "reminder_1h": (
            "⏰ Reminder: your webinar starts within 1 hour!"
        ),

        "faq_prompt": "❓ Help Center\n\nChoose a topic:",
        "faq_course": "🎓 Course Access",
        "faq_homework": "📚 Homework Help",
        "faq_webinars": "📅 Joining Webinars",
        "faq_curator": "👩‍🏫 Contact Curator",
        "faq_back": "⬅️ Main Menu",

        "faq_course_answer": (
            "🎓 Course Access\n\n"
            "1. Open the learning platform link provided by your school.\n"
            "2. Sign in using the account details provided by your school.\n"
            "3. If you cannot access your course, contact your curator "
            "through the school's official contact channel."
        ),
        "faq_homework_answer": (
            "📚 Homework Help\n\n"
            "1. Select My Homework from the menu.\n"
            "2. Read the instructions and deadline.\n"
            "3. Complete the assignment.\n"
            "4. Open the assignment and submit your text answer before the deadline.\n"
            "5. Check feedback here. Rejected work can be resubmitted before the deadline."
        ),
        "faq_webinars_answer": (
            "📅 Joining Webinars\n\n"
            "Open My Webinars to check upcoming sessions and meeting links. "
            "Reminders are sent before scheduled webinars."
        ),
        "faq_curator_answer": (
            "👩‍🏫 Contact Curator\n\n"
            "For help, contact your course curator through your school's "
            "official contact details. Direct curator messaging is not "
            "configured in this bot yet."
        ),

        "choose_language_first": "Please choose your language first.",
    },

    "🇷🇺 Русский": {
        "welcome": "Добро пожаловать в бот-помощник студента! 🎓",
        "choose_language": "Пожалуйста, выберите язык:",
        "menu_prompt": "Что вы хотите сделать?",
        "homework": "📚 Мои задания",
        "webinars": "📅 Мои вебинары",
        "faq": "❓ Частые вопросы",
        "change_language": "🌐 Сменить язык",
        "profile": "👤 Регистрация / Профиль",

        "registration_name_prompt": "Введите ваше полное имя:",
        "registration_course_prompt": "Какой курс вы изучаете?",
        "registration_success": "✅ Регистрация успешно завершена!",
        "registration_cancelled": "Действие отменено.",
        "profile_title": "👤 Ваш профиль",
        "profile_name": "Полное имя",
        "profile_course": "Курс",
        "cancel_hint": "Отправьте /cancel для отмены.",
        "enter_text": "Пожалуйста, отправьте ответ текстом.",
        "invalid_name": "Введите имя длиной от 2 до 100 символов.",
        "invalid_course": "Введите название курса длиной от 2 до 100 символов.",

        "webinars_header": "📅 Предстоящие вебинары",
        "no_webinars": "Предстоящих вебинаров пока нет.",
        "webinar_starts": "Начало",
        "webinar_join": "Подключиться",
        "reminder_24h": (
            "⏰ Напоминание: вебинар начнётся в течение 24 часов!"
        ),
        "reminder_1h": (
            "⏰ Напоминание: вебинар начнётся в течение 1 часа!"
        ),

        "faq_prompt": "❓ Центр помощи\n\nВыберите тему:",
        "faq_course": "🎓 Доступ к курсу",
        "faq_homework": "📚 Помощь с заданиями",
        "faq_webinars": "📅 Участие в вебинарах",
        "faq_curator": "👩‍🏫 Связаться с куратором",
        "faq_back": "⬅️ Главное меню",

        "faq_course_answer": (
            "🎓 Доступ к курсу\n\n"
            "1. Откройте ссылку на учебную платформу, предоставленную школой.\n"
            "2. Войдите с помощью выданных вам данных.\n"
            "3. Если доступ не работает, обратитесь к куратору "
            "через официальные контакты школы."
        ),
        "faq_homework_answer": (
            "📚 Помощь с заданиями\n\n"
            "1. Выберите «Мои задания» в меню.\n"
            "2. Прочитайте задание и срок сдачи.\n"
            "3. Выполните работу.\n"
            "4. Откройте задание и отправьте текстовый ответ до срока сдачи.\n"
            "5. Проверьте комментарий. Отклонённую работу можно отправить повторно до срока сдачи."
        ),
        "faq_webinars_answer": (
            "📅 Участие в вебинарах\n\n"
            "Откройте «Мои вебинары», чтобы посмотреть занятия и ссылки. "
            "Напоминания отправляются перед началом запланированных вебинаров."
        ),
        "faq_curator_answer": (
            "👩‍🏫 Связаться с куратором\n\n"
            "Для помощи обратитесь к куратору по официальным контактам школы. "
            "Прямая отправка сообщений куратору через бота пока не настроена."
        ),

        "choose_language_first": "Сначала выберите язык.",
    },

    "🇰🇿 Қазақша": {
        "welcome": "Студент көмекшісі ботына қош келдіңіз! 🎓",
        "choose_language": "Тіліңізді таңдаңыз:",
        "menu_prompt": "Қандай бөлімді таңдайсыз?",
        "homework": "📚 Үй тапсырмаларым",
        "webinars": "📅 Вебинарларым",
        "faq": "❓ Жиі қойылатын сұрақтар",
        "change_language": "🌐 Тілді өзгерту",
        "profile": "👤 Тіркелу / Профиль",

        "registration_name_prompt": "Толық аты-жөніңізді енгізіңіз:",
        "registration_course_prompt": "Қандай курста оқисыз?",
        "registration_success": "✅ Тіркелу сәтті аяқталды!",
        "registration_cancelled": "Әрекет тоқтатылды.",
        "profile_title": "👤 Сіздің профиліңіз",
        "profile_name": "Толық аты-жөні",
        "profile_course": "Курс",
        "cancel_hint": "Бас тарту үшін /cancel жіберіңіз.",
        "enter_text": "Жауабыңызды мәтін түрінде жіберіңіз.",
        "invalid_name": "Аты-жөніңіз 2–100 таңба аралығында болуы керек.",
        "invalid_course": "Курс атауы 2–100 таңба аралығында болуы керек.",

        "webinars_header": "📅 Алдағы вебинарлар",
        "no_webinars": "Әзірге алдағы вебинарлар жоқ.",
        "webinar_starts": "Басталуы",
        "webinar_join": "Қосылу",
        "reminder_24h": (
            "⏰ Еске салу: вебинар 24 сағат ішінде басталады!"
        ),
        "reminder_1h": (
            "⏰ Еске салу: вебинар 1 сағат ішінде басталады!"
        ),

        "faq_prompt": "❓ Көмек орталығы\n\nБөлімді таңдаңыз:",
        "faq_course": "🎓 Курсқа кіру",
        "faq_homework": "📚 Үй тапсырмасына көмек",
        "faq_webinars": "📅 Вебинарларға қатысу",
        "faq_curator": "👩‍🏫 Куратормен байланысу",
        "faq_back": "⬅️ Басты мәзір",

        "faq_course_answer": (
            "🎓 Курсқа кіру\n\n"
            "1. Мектеп берген оқу платформасының сілтемесін ашыңыз.\n"
            "2. Мектеп берген деректермен жүйеге кіріңіз.\n"
            "3. Курсқа кіре алмасаңыз, мектептің ресми байланыс арналары "
            "арқылы кураторыңызға хабарласыңыз."
        ),
        "faq_homework_answer": (
            "📚 Үй тапсырмасына көмек\n\n"
            "1. Мәзірден «Үй тапсырмаларым» бөлімін таңдаңыз.\n"
            "2. Тапсырма нұсқаулығы мен мерзімін оқыңыз.\n"
            "3. Тапсырманы орындаңыз.\n"
            "4. Тапсырманы ашып, мерзіміне дейін мәтіндік жауап жіберіңіз.\n"
            "5. Кері байланысты қараңыз. Қабылданбаған жұмысты мерзіміне дейін қайта жіберуге болады."
        ),
        "faq_webinars_answer": (
            "📅 Вебинарларға қатысу\n\n"
            "Сабақтар мен қосылу сілтемелерін көру үшін "
            "«Вебинарларым» бөлімін ашыңыз. Жоспарланған вебинарларға "
            "дейін еске салу хабарламалары жіберіледі."
        ),
        "faq_curator_answer": (
            "👩‍🏫 Куратормен байланысу\n\n"
            "Көмек алу үшін мектеп берген ресми байланыс деректері арқылы "
            "кураторыңызға хабарласыңыз. Бот арқылы кураторға тікелей "
            "хабарлама жіберу әзірге бапталмаған."
        ),

        "choose_language_first": "Алдымен тілді таңдаңыз.",
    },
}



# =====================================
# SUPPORT TEXTS
# =====================================

SUPPORT_TEXTS = {
    "🇬🇧 English": {
        "prompt": (
            "👩‍🏫 Contact Curator\n\n"
            "Describe your question or problem. The curator will receive your message.\n\n"
            "Send /cancel to cancel."
        ),
        "sent": "✅ Your request #{id} was sent to the curator.",
        "reply_prefix": "👩‍🏫 Reply from your curator:\n\n",
        "closed": "✅ Your support request #{id} has been closed.",
        "no_admin": "Support is not configured yet. Please contact your school directly.",
        "reply_prompt": "Replying to support request #{id}. Type your reply, or send /cancel to cancel.",
        "reply_success": "✅ Your reply was delivered to the student.",
        "not_delivered": "Could not deliver the reply. The student may need to open the bot.",
        "saved_not_notified": "Your request #{id} was saved, but the curator could not be notified. Please contact the school directly.",
        "admin_closed": "✅ Support request #{id} is now closed.",
    },
    "🇷🇺 Русский": {
        "prompt": (
            "👩‍🏫 Связаться с куратором\n\n"
            "Опишите свой вопрос или проблему. Куратор получит ваше сообщение.\n\n"
            "Отправьте /cancel для отмены."
        ),
        "sent": "✅ Запрос #{id} отправлен куратору.",
        "reply_prefix": "👩‍🏫 Ответ куратора:\n\n",
        "closed": "✅ Ваш запрос #{id} закрыт.",
        "no_admin": "Поддержка пока не настроена. Обратитесь напрямую в школу.",
        "reply_prompt": "Ответ на запрос #{id}. Введите ответ или отправьте /cancel для отмены.",
        "reply_success": "✅ Ответ отправлен студенту.",
        "not_delivered": "Не удалось отправить ответ. Возможно, студенту нужно открыть бота.",
        "saved_not_notified": "Запрос #{id} сохранён, но куратор не получил уведомление. Обратитесь в школу напрямую.",
        "admin_closed": "✅ Запрос #{id} закрыт.",
    },
    "🇰🇿 Қазақша": {
        "prompt": (
            "👩‍🏫 Куратормен байланысу\n\n"
            "Сұрағыңызды немесе мәселеңізді сипаттаңыз. Куратор хабарламаңызды алады.\n\n"
            "Бас тарту үшін /cancel жіберіңіз."
        ),
        "sent": "✅ #{id} сұранысы кураторға жіберілді.",
        "reply_prefix": "👩‍🏫 Куратордың жауабы:\n\n",
        "closed": "✅ #{id} сұранысы жабылды.",
        "no_admin": "Қолдау қызметі әлі бапталмаған. Мектепке тікелей хабарласыңыз.",
        "reply_prompt": "#{id} сұранысына жауап жазыңыз немесе /cancel жіберіңіз.",
        "reply_success": "✅ Жауап студентке жіберілді.",
        "not_delivered": "Жауап жіберілмеді. Студент ботты ашуы қажет болуы мүмкін.",
        "saved_not_notified": "#{id} сұранысы сақталды, бірақ кураторға хабарланбады. Мектепке тікелей хабарласыңыз.",
        "admin_closed": "✅ #{id} сұранысы жабылды.",
    },
}

# =====================================
# MENU ACTIONS
# =====================================

MENU_ACTIONS = (
    "homework",
    "webinars",
    "faq",
    "change_language",
    "profile",
)

MENU_BUTTONS = {
    language[action]
    for language in LANGUAGES.values()
    for action in MENU_ACTIONS
}

FAQ_ACTIONS = (
    "faq_course",
    "faq_homework",
    "faq_webinars",
    "faq_curator",
    "faq_back",
)

FAQ_BUTTONS = {
    language[action]
    for language in LANGUAGES.values()
    for action in FAQ_ACTIONS
}


# =====================================
# DATABASE FUNCTIONS
# =====================================

async def get_saved_language(user_id: int):
    async with AsyncSessionLocal() as session:
        student = await session.get(Student, user_id)
        return student.language if student else None


async def save_student_language(user_id: int, selected_language: str):
    async with AsyncSessionLocal() as session:
        student = await session.get(Student, user_id)

        if student is None:
            session.add(
                Student(
                    telegram_id=user_id,
                    language=selected_language,
                )
            )
        else:
            student.language = selected_language

        await session.commit()


async def get_student_profile(user_id: int):
    async with AsyncSessionLocal() as session:
        student = await session.get(Student, user_id)

        if student is None:
            return None

        return student.full_name, student.course_name


async def save_student_profile(
    user_id: int,
    selected_language: str,
    full_name: str,
    course_name: str,
):
    async with AsyncSessionLocal() as session:
        student = await session.get(Student, user_id)

        if student is None:
            student = Student(
                telegram_id=user_id,
                language=selected_language,
                full_name=full_name,
                course_name=course_name,
            )
            session.add(student)
        else:
            student.language = selected_language
            student.full_name = full_name
            student.course_name = course_name

        await session.commit()





async def get_upcoming_webinars():
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Webinar)
            .where(Webinar.starts_at > now)
            .order_by(Webinar.starts_at)
        )
        return list(result.scalars().all())


async def create_webinar(
    title: str,
    description: str,
    starts_at: datetime,
    meeting_url: str | None,
):
    async with AsyncSessionLocal() as session:
        webinar = Webinar(
            title=title,
            description=description,
            starts_at=starts_at,
            meeting_url=meeting_url,
        )
        session.add(webinar)
        await session.commit()
        await session.refresh(webinar)
        return webinar.id


async def ensure_webinar_columns():
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE webinars "
                "ADD COLUMN IF NOT EXISTS reminder_24h_sent "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE webinars "
                "ADD COLUMN IF NOT EXISTS reminder_1h_sent "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )


# =====================================
# KEYBOARDS
# =====================================

def get_language_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=language) for language in LANGUAGES]
        ],
        resize_keyboard=True,
    )


def get_main_menu_keyboard(language):
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=language["homework"]),
                KeyboardButton(text=language["webinars"]),
            ],
            [
                KeyboardButton(text=language["faq"]),
                KeyboardButton(text=language["change_language"]),
            ],
            [KeyboardButton(text=language["profile"])],
        ],
        resize_keyboard=True,
    )




def get_faq_keyboard(language):
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=language["faq_course"]),
                KeyboardButton(text=language["faq_homework"]),
            ],
            [
                KeyboardButton(text=language["faq_webinars"]),
                KeyboardButton(text=language["faq_curator"]),
            ],
            [KeyboardButton(text=language["faq_back"])],
        ],
        resize_keyboard=True,
    )


def format_profile(language, full_name, course_name):
    return (
        f"{language['profile_title']}\n\n"
        f"{language['profile_name']}: {full_name}\n"
        f"{language['profile_course']}: {course_name}"
    )


def format_webinar(webinar, language, number):
    local_start = webinar.starts_at.astimezone(ALMATY_TZ)

    result = (
        f"{number}. {webinar.title}\n"
        f"{webinar.description}\n"
        f"🕒 {language['webinar_starts']}: "
        f"{local_start.strftime('%d.%m.%Y %H:%M')} (Almaty time)"
    )

    if webinar.meeting_url:
        result += f"\n🔗 {language['webinar_join']}: {webinar.meeting_url}"

    return result


# =====================================
# COMMANDS
# =====================================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    if message.from_user:
        selected_language = await get_saved_language(message.from_user.id)

        if selected_language in LANGUAGES:
            language = LANGUAGES[selected_language]
            await message.answer(
                f"{language['welcome']}\n\n{language['menu_prompt']}",
                reply_markup=get_main_menu_keyboard(language),
            )
            return

    await message.answer(
        "Welcome! / Добро пожаловать! / Қош келдіңіз!\n\n"
        "Please choose your language / Выберите язык / "
        "Тіліңізді таңдаңыз:",
        reply_markup=get_language_keyboard(),
    )


@dp.message(Command("myid"))
async def my_id_handler(message: Message):
    if message.from_user:
        await message.answer(
            f"Your Telegram ID is: {message.from_user.id}\n\n"
            "Add this number to ADMIN_IDS in your .env file "
            "if you should manage webinars."
        )


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()

    selected_language = None
    if message.from_user:
        selected_language = await get_saved_language(message.from_user.id)

    if selected_language in LANGUAGES:
        language = LANGUAGES[selected_language]
        await message.answer(
            language["registration_cancelled"],
            reply_markup=get_main_menu_keyboard(language),
        )
    else:
        await message.answer(
            "Operation cancelled. Please choose your language.",
            reply_markup=get_language_keyboard(),
        )


@dp.message(Command("stats"))
async def stats_command(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is restricted to bot administrators.")
        return

    await show_stats(message)


async def show_stats(message: Message):
    def count_rows(model, *conditions):
        return (
            select(func.count()).select_from(model)
            .where(*conditions).scalar_subquery()
        )

    # Independent counts in one statement avoid multiplying rows across tables.
    # COUNT returns zero even when the underlying table has no records.
    query = select(
        count_rows(Student).label("students"),
        count_rows(
            Student,
            func.trim(Student.full_name) != "",
            func.trim(Student.course_name) != "",
        ).label("profiles"),
        count_rows(
            HomeworkSubmission, HomeworkSubmission.completed.is_(True),
        ).label("homework"),
        count_rows(Webinar).label("webinars"),
        count_rows(
            Webinar, Webinar.starts_at > datetime.now(timezone.utc),
        ).label("upcoming"),
        count_rows(SupportRequest, SupportRequest.status == "open").label("open"),
        count_rows(SupportRequest, SupportRequest.status == "answered").label("answered"),
        count_rows(SupportRequest, SupportRequest.status == "closed").label("closed"),
    )
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(query)
            stats = result.mappings().one()
    except (SQLAlchemyError, OSError, TimeoutError):
        # Do not send or log database exception details: they can contain secrets.
        await message.answer(
            "Statistics are temporarily unavailable because of a database error. "
            "Please try /stats again later."
        )
        return

    await message.answer(
        "Admin Statistics Dashboard\n\n"
        f"Total student records: {stats['students']}\n"
        f"Completed profiles (name and course): {stats['profiles']}\n"
        f"Completed homework submissions: {stats['homework']}\n\n"
        f"Total webinars: {stats['webinars']}\n"
        f"Upcoming webinars: {stats['upcoming']}\n\n"
        "Support requests:\n"
        f"Open: {stats['open']}\n"
        f"Answered: {stats['answered']}\n"
        f"Closed: {stats['closed']}"
    )


@dp.message(Command("support", "opensupport"))
async def open_support_command(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is restricted to bot administrators.")
        return

    await show_open_support(message)


async def show_open_support(message: Message):
    try:
        await send_open_support(message)
    except (SQLAlchemyError, OSError, TimeoutError):
        await message.answer(
            "Support requests are temporarily unavailable. Please try /support again later."
        )


async def send_open_support(message: Message):
    # Select the profile explicitly; async sessions must not lazy-load it.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SupportRequest, Student)
            .outerjoin(Student, SupportRequest.student_id == Student.telegram_id)
            .where(SupportRequest.status == "open")
            .order_by(SupportRequest.created_at, SupportRequest.id)
        )
        requests = result.all()

    if not requests:
        await message.answer(
            "There are no open support requests. "
            "Use /support or /opensupport to check again later."
        )
        return

    await message.answer(f"Open Support Requests: {len(requests)}")
    for request, student in requests:
        details = (
            f"Support request #{request.id}\n\n"
            f"Student: {student.full_name or 'Not provided' if student else 'Not provided'}\n"
            f"Course: {student.course_name or 'Not provided' if student else 'Not provided'}\n"
            f"Telegram ID: {request.student_id}\n"
            f"Language: {student.language or 'Not provided' if student else 'Not provided'}\n\n"
            f"Message:\n{request.message}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"Reply to #{request.id}",
                    callback_data=f"support:reply:{request.id}",
                ),
                InlineKeyboardButton(
                    text=f"Close #{request.id}",
                    callback_data=f"support:close:{request.id}",
                ),
            ]]
        )
        # A valid 4000-character question plus profile can exceed Telegram's
        # limit. 1000 code points also fit within 4096 UTF-16 code units.
        for offset in range(0, len(details), 1000):
            await message.answer(
                details[offset:offset + 1000],
                parse_mode=None,
                reply_markup=keyboard if offset + 1000 >= len(details) else None,
            )


register_admin_handlers(dp, ADMIN_IDS, show_stats, show_open_support)
homework_handlers = register_homework_handlers(
    dp, ADMIN_IDS,
    lambda language: get_main_menu_keyboard(LANGUAGES.get(language, LANGUAGES["🇬🇧 English"])),
)


@dp.message(Command("addwebinar"))
async def add_webinar_command(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer(
            "This command is restricted to bot administrators."
        )
        return

    await state.clear()
    await state.set_state(WebinarStates.waiting_for_title)
    await message.answer(
        "➕ Create a webinar\n\nEnter the webinar title "
        "(up to 200 characters).\nSend /cancel to stop."
    )


homework_handlers.register_states(dp)


# =====================================
# WEBINAR CREATION: ADMIN STATES
# =====================================

@dp.message(WebinarStates.waiting_for_title)
async def webinar_title_handler(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is restricted to bot administrators.")
        return
    if not message.text:
        await message.answer("Please send the webinar title as text.")
        return

    title = message.text.strip()

    if not 2 <= len(title) <= 200:
        await message.answer("The title must contain 2–200 characters.")
        return

    await state.update_data(webinar_title=title)
    await state.set_state(WebinarStates.waiting_for_description)
    await message.answer("Enter a description for the webinar.")


@dp.message(WebinarStates.waiting_for_description)
async def webinar_description_handler(
    message: Message,
    state: FSMContext,
):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is restricted to bot administrators.")
        return
    if not message.text:
        await message.answer("Please send the description as text.")
        return

    description = message.text.strip()

    if not 1 <= len(description) <= 4000:
        await message.answer("The description must contain 1–4000 characters.")
        return

    await state.update_data(webinar_description=description)
    await state.set_state(WebinarStates.waiting_for_start_time)

    await message.answer(
        "Enter the start date and time in Almaty time:\n\n"
        "YYYY-MM-DD HH:MM\n"
        "Example: 2026-11-15 18:30"
    )


@dp.message(WebinarStates.waiting_for_start_time)
async def webinar_start_handler(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is restricted to bot administrators.")
        return
    if not message.text:
        await message.answer("Send the date and time as text.")
        return

    try:
        local_start = datetime.strptime(
            message.text.strip(),
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=ALMATY_TZ)
    except ValueError:
        await message.answer(
            "Invalid format. Use YYYY-MM-DD HH:MM, "
            "for example 2026-11-15 18:30."
        )
        return

    if local_start <= datetime.now(ALMATY_TZ):
        await message.answer(
            "The start time must be in the future. Enter it again."
        )
        return

    await state.update_data(starts_at=local_start.isoformat())
    await state.set_state(WebinarStates.waiting_for_meeting_url)

    await message.answer(
        "Enter the meeting URL (must start with https:// or http://).\n"
        "Send a single hyphen - if no meeting link is available."
    )


@dp.message(WebinarStates.waiting_for_meeting_url)
async def webinar_url_handler(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is restricted to bot administrators.")
        return
    if not message.text:
        await message.answer("Send a URL as text, or send - for no link.")
        return

    url = message.text.strip()

    if url == "-":
        meeting_url = None
    elif (
        url.startswith("https://") or url.startswith("http://")
    ) and len(url) <= 500:
        meeting_url = url
    else:
        await message.answer(
            "Please enter a valid http:// or https:// URL, "
            "or send - if there is no link."
        )
        return

    data = await state.get_data()

    try:
        starts_at = datetime.fromisoformat(data["starts_at"])
        webinar_id = await create_webinar(
            title=data["webinar_title"],
            description=data["webinar_description"],
            starts_at=starts_at,
            meeting_url=meeting_url,
        )
    except Exception as error:
        print(f"Could not create webinar: {error}")
        await message.answer(
            "There was a database error while creating the webinar. "
            "Check the terminal, then try again with /addwebinar."
        )
        await state.clear()
        return

    await state.clear()

    selected_language = await get_saved_language(message.from_user.id)
    language = LANGUAGES.get(selected_language, LANGUAGES["🇬🇧 English"])

    await message.answer(
        f"✅ Webinar created successfully!\n"
        f"Webinar ID: {webinar_id}\n"
        f"Title: {data['webinar_title']}\n"
        f"Start: {starts_at.astimezone(ALMATY_TZ).strftime('%d.%m.%Y %H:%M')} "
        f"(Almaty time)\n\n"
        "Students can now see it in My Webinars.",
        reply_markup=get_main_menu_keyboard(language),
    )


# =====================================
# STUDENT REGISTRATION
# =====================================

@dp.message(RegistrationStates.waiting_for_name)
async def registration_name_handler(
    message: Message,
    state: FSMContext,
):
    if not message.from_user:
        return

    selected_language = await get_saved_language(message.from_user.id)
    language = LANGUAGES.get(selected_language, LANGUAGES["🇬🇧 English"])

    if not message.text:
        await message.answer(language["enter_text"])
        return

    full_name = message.text.strip()

    if not 2 <= len(full_name) <= 100:
        await message.answer(language["invalid_name"])
        return

    await state.update_data(full_name=full_name)
    await state.set_state(RegistrationStates.waiting_for_course)

    await message.answer(
        language["registration_course_prompt"]
        + "\n"
        + language["cancel_hint"],
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(RegistrationStates.waiting_for_course)
async def registration_course_handler(
    message: Message,
    state: FSMContext,
):
    if not message.from_user:
        return

    user_id = message.from_user.id
    selected_language = await get_saved_language(user_id)
    language = LANGUAGES.get(selected_language, LANGUAGES["🇬🇧 English"])

    if not message.text:
        await message.answer(language["enter_text"])
        return

    course_name = message.text.strip()

    if not 2 <= len(course_name) <= 100:
        await message.answer(language["invalid_course"])
        return

    data = await state.get_data()
    full_name = data.get("full_name")

    if not full_name:
        await state.clear()
        await state.set_state(RegistrationStates.waiting_for_name)
        await message.answer(language["registration_name_prompt"])
        return

    await save_student_profile(
        user_id=user_id,
        selected_language=selected_language,
        full_name=full_name,
        course_name=course_name,
    )

    await state.clear()
    await message.answer(
        f"{language['registration_success']}\n\n"
        f"{format_profile(language, full_name, course_name)}\n\n"
        f"{language['menu_prompt']}",
        reply_markup=get_main_menu_keyboard(language),
    )


# =====================================
# STUDENT SUPPORT REQUESTS
# =====================================

@dp.message(SupportStates.waiting_for_message)
async def support_message_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if not message.from_user:
        return

    if not message.text or not message.text.strip():
        await message.answer("Please send your question as text.")
        return

    request_text = message.text.strip()
    if len(request_text) > 4000:
        await message.answer("Please limit your message to 4000 characters.")
        return

    user_id = message.from_user.id
    student_language = await get_saved_language(user_id)
    texts = SUPPORT_TEXTS.get(
        student_language, SUPPORT_TEXTS["🇬🇧 English"]
    )

    if not ADMIN_IDS:
        await state.clear()
        await message.answer(
            texts["no_admin"],
            reply_markup=get_main_menu_keyboard(
                LANGUAGES.get(student_language, LANGUAGES["🇬🇧 English"])
            ),
        )
        return

    # Store the request and make sure the student record exists.
    async with AsyncSessionLocal() as session:
        student = await session.get(Student, user_id)
        if student is None:
            student = Student(
                telegram_id=user_id,
                language=student_language or "🇬🇧 English",
            )
            session.add(student)
            await session.flush()

        request = SupportRequest(
            student_id=user_id,
            message=request_text,
            status="open",
        )
        session.add(request)
        await session.flush()
        request_id = request.id

        full_name = student.full_name or message.from_user.full_name
        course_name = student.course_name or "Not provided"
        student_language = student.language or "🇬🇧 English"
        await session.commit()

    texts = SUPPORT_TEXTS.get(
        student_language, SUPPORT_TEXTS["🇬🇧 English"]
    )
    notification = (
        f"🆘 New support request #{request_id}\n\n"
        f"Student: {full_name}\n"
        f"Course: {course_name}\n"
        f"Telegram ID: {user_id}\n"
        f"Language: {student_language}\n\n"
        f"Message:\n{request_text}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✉️ Reply to #{request_id}",
                    callback_data=f"support:reply:{request_id}",
                ),
                InlineKeyboardButton(
                    text=f"✅ Close #{request_id}",
                    callback_data=f"support:close:{request_id}",
                ),
            ]
        ]
    )

    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=notification,
                reply_markup=keyboard,
            )
            delivered += 1
        except TelegramAPIError as error:
            print(
                f"Could not notify admin {admin_id} about support request "
                f"#{request_id}: {error}"
            )

    await state.clear()
    menu_language = LANGUAGES.get(
        student_language, LANGUAGES["🇬🇧 English"]
    )

    if delivered:
        await message.answer(
            texts["sent"].format(id=request_id),
            reply_markup=get_main_menu_keyboard(menu_language),
        )
    else:
        await message.answer(
            texts["saved_not_notified"].format(id=request_id),
            reply_markup=get_main_menu_keyboard(menu_language),
        )


@dp.callback_query(F.data.startswith("support:reply:"))
async def support_reply_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Not authorized.", show_alert=True)
        return

    try:
        request_id = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid request ID.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        request = await session.get(SupportRequest, request_id)
        if request is None:
            await callback.answer("Request not found.", show_alert=True)
            return
        if request.status == "closed":
            await callback.answer("This request is closed.", show_alert=True)
            return

    await state.clear()
    await state.update_data(support_request_id=request_id)
    await state.set_state(SupportStates.waiting_for_reply)

    selected_language = await get_saved_language(callback.from_user.id)
    texts = SUPPORT_TEXTS.get(
        selected_language, SUPPORT_TEXTS["🇬🇧 English"]
    )
    await callback.answer()

    if isinstance(callback.message, Message):
        await callback.message.answer(
            texts["reply_prompt"].format(id=request_id)
        )


@dp.message(SupportStates.waiting_for_reply)
async def support_reply_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    if not message.text or not message.text.strip():
        await message.answer("Please send your reply as text.")
        return

    reply_text = message.text.strip()
    if len(reply_text) > 4000:
        await message.answer("Please limit your reply to 4000 characters.")
        return

    data = await state.get_data()
    request_id = data.get("support_request_id")
    if not request_id:
        await state.clear()
        await message.answer("No support request is selected.")
        return

    async with AsyncSessionLocal() as session:
        request = await session.get(SupportRequest, request_id)
        if request is None or request.status == "closed":
            await state.clear()
            await message.answer("This support request is unavailable or closed.")
            return
        student_id = request.student_id

    student_language = await get_saved_language(student_id)
    student_texts = SUPPORT_TEXTS.get(
        student_language, SUPPORT_TEXTS["🇬🇧 English"]
    )

    try:
        await bot.send_message(
            chat_id=student_id,
            text=student_texts["reply_prefix"] + reply_text,
        )
    except TelegramAPIError as error:
        print(f"Could not deliver reply for request #{request_id}: {error}")
        await message.answer(student_texts["not_delivered"])
        return

    async with AsyncSessionLocal() as session:
        request = await session.get(SupportRequest, request_id)
        if request is not None:
            request.response = reply_text
            request.status = "answered"
            request.answered_at = datetime.now(timezone.utc)
            await session.commit()

    await state.clear()
    admin_language = await get_saved_language(message.from_user.id)
    admin_texts = SUPPORT_TEXTS.get(
        admin_language, SUPPORT_TEXTS["🇬🇧 English"]
    )
    await message.answer(admin_texts["reply_success"])


@dp.callback_query(F.data.startswith("support:close:"))
async def support_close_handler(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Not authorized.", show_alert=True)
        return

    try:
        request_id = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid request ID.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        request = await session.get(SupportRequest, request_id)
        if request is None:
            await callback.answer("Request not found.", show_alert=True)
            return
        if request.status == "closed":
            await callback.answer("This request is already closed.", show_alert=True)
            return

        student_id = request.student_id
        request.status = "closed"
        await session.commit()

    student_language = await get_saved_language(student_id)
    student_texts = SUPPORT_TEXTS.get(
        student_language, SUPPORT_TEXTS["🇬🇧 English"]
    )

    try:
        await bot.send_message(
            chat_id=student_id,
            text=student_texts["closed"].format(id=request_id),
        )
    except TelegramAPIError as error:
        print(f"Could not notify student about closed request #{request_id}: {error}")

    await callback.answer("Support request closed.")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(f"✅ Support request #{request_id} closed.")
        except TelegramAPIError as error:
            print(f"Could not update admin message: {error}")

# =====================================
# LANGUAGE SELECTION
# =====================================

@dp.message(lambda message: message.text in LANGUAGES)
async def language_handler(message: Message):
    if not message.from_user:
        return

    selected_language = message.text
    await save_student_language(message.from_user.id, selected_language)

    language = LANGUAGES[selected_language]
    await message.answer(
        f"{language['welcome']}\n\n{language['menu_prompt']}",
        reply_markup=get_main_menu_keyboard(language),
    )


# =====================================
# STUDENT MENU
# =====================================

@dp.message(lambda message: message.text in MENU_BUTTONS)
async def menu_handler(message: Message, state: FSMContext):
    if not message.from_user:
        return

    user_id = message.from_user.id
    try:
        selected_language = await get_saved_language(user_id)
    except (SQLAlchemyError, OSError, TimeoutError):
        language_key = next((key for key, value in LANGUAGES.items() if value["homework"] == message.text), None)
        await message.answer(homework_texts(language_key)["db"])
        return

    if selected_language not in LANGUAGES:
        await message.answer(
            "Please choose your language first.",
            reply_markup=get_language_keyboard(),
        )
        return

    language = LANGUAGES[selected_language]

    action = next(
        (
            item for item in MENU_ACTIONS
            if message.text == language[item]
        ),
        None,
    )

    if action == "change_language":
        await state.clear()
        await message.answer(
            language["choose_language"],
            reply_markup=get_language_keyboard(),
        )

    elif action == "profile":
        profile = await get_student_profile(user_id)

        if profile and profile[0] and profile[1]:
            await message.answer(
                format_profile(language, profile[0], profile[1]),
                reply_markup=get_main_menu_keyboard(language),
            )
        else:
            await state.clear()
            await state.set_state(RegistrationStates.waiting_for_name)
            await message.answer(
                language["registration_name_prompt"]
                + "\n"
                + language["cancel_hint"],
                reply_markup=ReplyKeyboardRemove(),
            )

    elif action == "homework":
        await homework_handlers.show_student(message, user_id)

    elif action == "webinars":
        webinars = await get_upcoming_webinars()

        if not webinars:
            await message.answer(language["no_webinars"])
            return

        sections = [language["webinars_header"]]

        for index, webinar in enumerate(webinars, start=1):
            sections.append("")
            sections.append(format_webinar(webinar, language, index))

        await message.answer("\n".join(sections))

    elif action == "faq":
        await message.answer(
            language["faq_prompt"],
            reply_markup=get_faq_keyboard(language),
        )




# =====================================
# FAQ
# =====================================

@dp.message(lambda message: message.text in FAQ_BUTTONS)
async def faq_handler(message: Message, state: FSMContext):
    if not message.from_user:
        return

    selected_language = await get_saved_language(message.from_user.id)

    if selected_language not in LANGUAGES:
        await message.answer(
            "Please choose your language first.",
            reply_markup=get_language_keyboard(),
        )
        return

    language = LANGUAGES[selected_language]

    action = next(
        (
            item for item in FAQ_ACTIONS
            if message.text == language[item]
        ),
        None,
    )

    if action == "faq_back":
        await state.clear()
        await message.answer(
            language["menu_prompt"],
            reply_markup=get_main_menu_keyboard(language),
        )
        return

    if action == "faq_curator":
        support_texts = SUPPORT_TEXTS.get(
            selected_language, SUPPORT_TEXTS["🇬🇧 English"]
        )

        if not ADMIN_IDS:
            await message.answer(
                support_texts["no_admin"],
                reply_markup=get_faq_keyboard(language),
            )
            return

        await state.clear()
        await state.set_state(SupportStates.waiting_for_message)
        await message.answer(
            support_texts["prompt"],
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if action:
        await message.answer(
            f"{language[action + '_answer']}\n\n"
            f"{language['faq_prompt']}",
            reply_markup=get_faq_keyboard(language),
        )


# =====================================
# LEGACY HOMEWORK BUTTONS
# =====================================

@dp.callback_query(F.data == "homework:complete")
async def complete_homework(callback: CallbackQuery):
    # Old sample buttons cannot bypass submission and administrator review.
    await callback.answer()
    if isinstance(callback.message, Message):
        await homework_handlers.show_student(callback.message, callback.from_user.id)


# =====================================
# AUTOMATIC WEBINAR REMINDERS
# =====================================

async def reminder_loop(bot: Bot):
    while True:
        try:
            now = datetime.now(timezone.utc)

            async with AsyncSessionLocal() as session:
                webinar_result = await session.execute(
                    select(Webinar).where(Webinar.starts_at > now)
                )
                webinars = list(webinar_result.scalars().all())

                student_result = await session.execute(
                    select(Student.telegram_id, Student.language)
                )
                students = list(student_result.all())

                for webinar in webinars:
                    start_time = webinar.starts_at

                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=timezone.utc)

                    time_left = start_time - now

                    # Send the 1-hour reminder only during the final hour.
                    if (
                        not webinar.reminder_1h_sent
                        and timedelta(0) < time_left <= timedelta(hours=1)
                    ):
                        reminder_key = "reminder_1h"
                        flag_name = "reminder_1h_sent"

                    # Send the 24-hour reminder only between 23h55m and 24h
                    # before the webinar. This stops short-notice webinars
                    # from getting both reminders at once.
                    elif (
                        not webinar.reminder_24h_sent
                        and timedelta(hours=23, minutes=55)
                        <= time_left
                        <= timedelta(hours=24)
                    ):
                        reminder_key = "reminder_24h"
                        flag_name = "reminder_24h_sent"

                    else:
                        continue

                    local_start = start_time.astimezone(ALMATY_TZ)

                    for student_id, student_language in students:
                        language = LANGUAGES.get(
                            student_language,
                            LANGUAGES["🇬🇧 English"],
                        )

                        notification = (
                            f"{language[reminder_key]}\n\n"
                            f"📅 {webinar.title}\n"
                            f"{webinar.description}\n"
                            f"🕒 {local_start.strftime('%d.%m.%Y %H:%M')} "
                            "(Almaty time)"
                        )

                        if webinar.meeting_url:
                            notification += f"\n🔗 {webinar.meeting_url}"

                        try:
                            await bot.send_message(
                                chat_id=student_id,
                                text=notification,
                            )
                        except TelegramAPIError as error:
                            print(
                                f"Could not send webinar reminder to "
                                f"student {student_id}: {error}"
                            )

                    # Mark the reminder after processing all students.
                    setattr(webinar, flag_name, True)

                await session.commit()

        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Webinar reminder scheduler error: {error}")

        await asyncio.sleep(60)


# =====================================
# START BOT
# =====================================

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing. Check your .env file.")

    await init_db()
    await ensure_webinar_columns()

    bot = Bot(token=BOT_TOKEN)
    reminder_task = asyncio.create_task(reminder_loop(bot))
    homework_task = asyncio.create_task(homework_worker(bot))

    print("Bot is running!")

    if not ADMIN_IDS:
        print(
            "Warning: ADMIN_IDS is empty. "
            "Set it in .env to allow webinar creation."
        )

    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        homework_task.cancel()

        try:
            await reminder_task
        except asyncio.CancelledError:
            pass

        try:
            await homework_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
