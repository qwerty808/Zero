from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import time, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from db import Database, HabitView, TaskView


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("habit-task-bot")


HELP_TEXT = """
Я бот‑трекер привычек и задач.

Привычки:
  /addhabit «название» — добавить привычку
  /habits — список привычек (с кнопками “✅ Сделано”)
  /habitdone «id» — отметить привычку выполненной сегодня
  /delhabit «id» — удалить (деактивировать) привычку

Задачи:
  /addtask «текст» — добавить задачу
  /tasks — список задач (с кнопками “✅ Готово”)
  /taskdone «id» — закрыть задачу
  /deltask «id» — удалить задачу

Напоминания (время в UTC):
  /setreminder HH:MM — ежедневное напоминание
  /reminderoff — выключить напоминания

Дополнительно:
  /summary — короткая сводка (что осталось на сегодня)
  /help — помощь
""".strip()


TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class BotState:
    db: Database
    db_lock: asyncio.Lock


def _parse_hhmm_utc(hhmm: str) -> time | None:
    m = TIME_RE.match(hhmm.strip())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    return time(hour=hour, minute=minute, tzinfo=timezone.utc)


def _habits_text(habits: list[HabitView]) -> str:
    if not habits:
        return "Привычек пока нет. Добавь: /addhabit <название>"

    lines = ["Твои привычки:"]
    for h in habits:
        done = "✅" if h.done_today else "—"
        last = h.last_done if h.last_done else "никогда"
        lines.append(f"#{h.id} — {h.name} | стрик: {h.streak} | сегодня: {done} | последний: {last}")
    lines.append("")
    lines.append("Отметить: /habitdone <id>  |  Удалить: /delhabit <id>")
    return "\n".join(lines)


def _tasks_text(tasks: list[TaskView]) -> str:
    if not tasks:
        return "Задач пока нет. Добавь: /addtask <текст>"

    lines = ["Твои задачи:"]
    for t in tasks:
        lines.append(f"#{t.id} — {t.text}")
    lines.append("")
    lines.append("Закрыть: /taskdone <id>  |  Удалить: /deltask <id>")
    return "\n".join(lines)


def _habits_keyboard(habits: list[HabitView]) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for h in habits:
        label = f"✅ #{h.id}"
        row.append(InlineKeyboardButton(label, callback_data=f"h:{h.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None


def _tasks_keyboard(tasks: list[TaskView]) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in tasks:
        label = f"✅ #{t.id}"
        row.append(InlineKeyboardButton(label, callback_data=f"t:{t.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None


async def _db_call(state: BotState, fn, *args, **kwargs):
    async with state.db_lock:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _user_ids(update: Update) -> tuple[int, int]:
    tg_id = update.effective_user.id  # type: ignore[union-attr]
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    return int(tg_id), int(chat_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)
    await _db_call(state, state.db.get_or_create_user, telegram_id, chat_id)

    await update.message.reply_text(  # type: ignore[union-attr]
        "Привет! Я помогу вести привычки и задачи.\n\n" + HELP_TEXT,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)  # type: ignore[union-attr]


async def cmd_addhabit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Использование: /addhabit <название>")  # type: ignore[union-attr]
        return

    hid = await _db_call(state, state.db.add_habit, telegram_id, chat_id, name)
    await update.message.reply_text(f"Добавил привычку #{hid}: {name}")  # type: ignore[union-attr]


async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)

    habits = await _db_call(state, state.db.list_habits, telegram_id)
    text = _habits_text(habits)
    kb = _habits_keyboard(habits)
    await update.message.reply_text(text, reply_markup=kb)  # type: ignore[union-attr]


async def cmd_habitdone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)

    if not context.args:
        await update.message.reply_text("Использование: /habitdone <id>")  # type: ignore[union-attr]
        return
    try:
        hid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return

    ok = await _db_call(state, state.db.mark_habit_done_today, telegram_id, hid)
    await update.message.reply_text("Отметил ✅" if ok else "Не нашёл привычку.")  # type: ignore[union-attr]


async def cmd_delhabit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)

    if not context.args:
        await update.message.reply_text("Использование: /delhabit <id>")  # type: ignore[union-attr]
        return
    try:
        hid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return

    ok = await _db_call(state, state.db.deactivate_habit, telegram_id, hid)
    await update.message.reply_text("Удалил." if ok else "Не нашёл привычку.")  # type: ignore[union-attr]


async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /addtask <текст>")  # type: ignore[union-attr]
        return

    tid = await _db_call(state, state.db.add_task, telegram_id, chat_id, text)
    await update.message.reply_text(f"Добавил задачу #{tid}: {text}")  # type: ignore[union-attr]


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)

    tasks = await _db_call(state, state.db.list_tasks, telegram_id, False)
    text = _tasks_text(tasks)
    kb = _tasks_keyboard(tasks)
    await update.message.reply_text(text, reply_markup=kb)  # type: ignore[union-attr]


async def cmd_taskdone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)

    if not context.args:
        await update.message.reply_text("Использование: /taskdone <id>")  # type: ignore[union-attr]
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return

    ok = await _db_call(state, state.db.mark_task_done, telegram_id, tid)
    await update.message.reply_text("Готово ✅" if ok else "Не нашёл задачу.")  # type: ignore[union-attr]


async def cmd_deltask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)

    if not context.args:
        await update.message.reply_text("Использование: /deltask <id>")  # type: ignore[union-attr]
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return

    ok = await _db_call(state, state.db.delete_task, telegram_id, tid)
    await update.message.reply_text("Удалил." if ok else "Не нашёл задачу.")  # type: ignore[union-attr]


def _reminder_job_name(telegram_id: int) -> str:
    return f"reminder:{telegram_id}"


def _remove_existing_reminder_jobs(app: Application, telegram_id: int) -> None:
    jq = app.job_queue
    if jq is None:
        return
    for j in jq.get_jobs_by_name(_reminder_job_name(telegram_id)):
        j.schedule_removal()


def _schedule_reminder_job(app: Application, telegram_id: int, chat_id: int, hhmm: str) -> bool:
    jq = app.job_queue
    if jq is None:
        return False
    when = _parse_hhmm_utc(hhmm)
    if when is None:
        return False
    _remove_existing_reminder_jobs(app, telegram_id)
    jq.run_daily(
        reminder_job,
        time=when,
        name=_reminder_job_name(telegram_id),
        data={"telegram_id": telegram_id, "chat_id": chat_id},
    )
    return True


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    state: BotState = app.bot_data["state"]
    data = context.job.data  # type: ignore[union-attr]
    telegram_id = int(data["telegram_id"])
    chat_id = int(data["chat_id"])

    habits, tasks = await _db_call(state, state.db.pending_summary, telegram_id)
    pending_habits = [h for h in habits if not h.done_today]

    if not pending_habits and not tasks:
        return

    parts: list[str] = ["Напоминание на сегодня:"]
    if pending_habits:
        parts.append("")
        parts.append("Привычки (ещё не отмечены):")
        for h in pending_habits:
            parts.append(f"- #{h.id} {h.name}")
    if tasks:
        parts.append("")
        parts.append("Задачи (открытые):")
        for t in tasks:
            parts.append(f"- #{t.id} {t.text}")
    parts.append("")
    parts.append("Быстрые команды: /habits и /tasks")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(parts))


async def cmd_setreminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)

    if not context.args:
        await update.message.reply_text("Использование: /setreminder HH:MM (в UTC)")  # type: ignore[union-attr]
        return

    hhmm = context.args[0].strip()
    if _parse_hhmm_utc(hhmm) is None:
        await update.message.reply_text("Неверный формат времени. Пример: /setreminder 20:30")  # type: ignore[union-attr]
        return

    await _db_call(state, state.db.get_or_create_user, telegram_id, chat_id)
    await _db_call(state, state.db.set_reminder_time, telegram_id, hhmm)
    ok = _schedule_reminder_job(context.application, telegram_id, chat_id, hhmm)
    await update.message.reply_text("Ок, буду напоминать ежедневно (UTC)." if ok else "Не смог поставить напоминание.")  # type: ignore[union-attr]


async def cmd_reminderoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    await _db_call(state, state.db.set_reminder_time, telegram_id, None)
    _remove_existing_reminder_jobs(context.application, telegram_id)
    await update.message.reply_text("Напоминания выключены.")  # type: ignore[union-attr]


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    habits, tasks = await _db_call(state, state.db.pending_summary, telegram_id)
    pending_habits = [h for h in habits if not h.done_today]

    parts: list[str] = ["Сводка:"]
    parts.append(f"- Привычек осталось: {len(pending_habits)}")
    parts.append(f"- Открытых задач: {len(tasks)}")
    if pending_habits:
        parts.append("")
        parts.append("Привычки:")
        for h in pending_habits:
            parts.append(f"- #{h.id} {h.name}")
    if tasks:
        parts.append("")
        parts.append("Задачи:")
        for t in tasks:
            parts.append(f"- #{t.id} {t.text}")

    await update.message.reply_text("\n".join(parts))  # type: ignore[union-attr]


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    query = update.callback_query
    if query is None:
        return

    telegram_id = int(query.from_user.id)
    data = (query.data or "").strip()

    if data.startswith("h:"):
        try:
            hid = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректный id.")
            return
        ok = await _db_call(state, state.db.mark_habit_done_today, telegram_id, hid)
        await query.answer("Отмечено ✅" if ok else "Не нашёл привычку.")
        habits = await _db_call(state, state.db.list_habits, telegram_id)
        text = _habits_text(habits)
        kb = _habits_keyboard(habits)
        try:
            await query.edit_message_text(text=text, reply_markup=kb)
        except Exception:
            # If edit fails (e.g. message too old), just send a new message
            await query.message.reply_text(text, reply_markup=kb)  # type: ignore[union-attr]
        return

    if data.startswith("t:"):
        try:
            tid = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректный id.")
            return
        ok = await _db_call(state, state.db.mark_task_done, telegram_id, tid)
        await query.answer("Готово ✅" if ok else "Не нашёл задачу.")
        tasks = await _db_call(state, state.db.list_tasks, telegram_id, False)
        text = _tasks_text(tasks)
        kb = _tasks_keyboard(tasks)
        try:
            await query.edit_message_text(text=text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)  # type: ignore[union-attr]
        return

    await query.answer("Неизвестное действие.")


async def post_init(app: Application) -> None:
    state: BotState = app.bot_data["state"]
    users = await _db_call(state, state.db.users_with_reminders)
    for telegram_id, chat_id, hhmm in users:
        _schedule_reminder_job(app, telegram_id, chat_id, hhmm)


def build_app(token: str, db_path: str) -> Application:
    db = Database(db_path=db_path)
    state = BotState(db=db, db_lock=asyncio.Lock())

    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.bot_data["state"] = state

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    app.add_handler(CommandHandler("addhabit", cmd_addhabit))
    app.add_handler(CommandHandler("habits", cmd_habits))
    app.add_handler(CommandHandler("habitdone", cmd_habitdone))
    app.add_handler(CommandHandler("delhabit", cmd_delhabit))

    app.add_handler(CommandHandler("addtask", cmd_addtask))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("taskdone", cmd_taskdone))
    app.add_handler(CommandHandler("deltask", cmd_deltask))

    app.add_handler(CommandHandler("setreminder", cmd_setreminder))
    app.add_handler(CommandHandler("reminderoff", cmd_reminderoff))
    app.add_handler(CommandHandler("summary", cmd_summary))

    app.add_handler(CallbackQueryHandler(on_callback))
    return app


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is required. Put it into environment or .env")

    db_path = os.getenv("DB_PATH", "data.db")
    app = build_app(token=token, db_path=db_path)
    logger.info("Bot started. DB=%s", db_path)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
