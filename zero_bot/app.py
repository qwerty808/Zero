from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import time, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .db import Database
from .ui import (
    BTN_ADD_HABIT,
    BTN_ADD_TASK,
    BTN_CANCEL,
    BTN_HABITS,
    BTN_HELP,
    BTN_LIST_ALL,
    BTN_STATS,
    BTN_STATS_SHORT,
    BTN_SUMMARY,
    BTN_TASKS,
    items_keyboard,
    items_text,
    main_menu,
    stats_detailed_text,
    stats_short_text,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("zero-bot")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

HELP_TEXT = """
Единый трекер задач/привычек (Items).

Быстро:
  /menu — меню кнопками
  /addtask «текст» — item: task (once)
  /addhabit «название» — item: habit (daily)
  /items — все активные items
  /tasks — активные задачи (once)
  /habits — активные привычки
  /done «id» — отметить выполненным (для текущего периода)
  /del «id» — удалить (деактивировать)
  /remind «id» HH:MM — напоминание (UTC) на уровне item
  /remindoff «id» — выключить напоминание у item
  /stats — подробная статистика
  /stats_short — краткая статистика
  /cancel — отменить ввод
""".strip()


@dataclass(frozen=True)
class State:
    db: Database
    db_lock: asyncio.Lock


AWAITING_KEY = "awaiting"
AWAIT_TASK = "task"
AWAIT_HABIT = "habit"


def _parse_hhmm_utc(hhmm: str) -> time | None:
    m = TIME_RE.match(hhmm.strip())
    if not m:
        return None
    return time(hour=int(m.group(1)), minute=int(m.group(2)), tzinfo=timezone.utc)


async def _db_call(state: State, fn, *args, **kwargs):
    async with state.db_lock:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _user_ids(update: Update) -> tuple[int, int]:
    tg_id = update.effective_user.id  # type: ignore[union-attr]
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    return int(tg_id), int(chat_id)


def _job_name(telegram_id: int, hhmm: str) -> str:
    return f"itemreminder:{telegram_id}:{hhmm}"


def _remove_jobs_for(app: Application, telegram_id: int) -> None:
    jq = app.job_queue
    if jq is None:
        return
    prefix = f"itemreminder:{telegram_id}:"
    for j in jq.jobs():
        if j.name and j.name.startswith(prefix):
            j.schedule_removal()


def _schedule_all_reminders(app: Application, buckets: list[tuple[int, int, str]]) -> None:
    jq = app.job_queue
    if jq is None:
        return
    for telegram_id, chat_id, hhmm in buckets:
        when = _parse_hhmm_utc(hhmm)
        if when is None:
            continue
        jq.run_daily(
            item_reminder_job,
            time=when,
            name=_job_name(telegram_id, hhmm),
            data={"telegram_id": telegram_id, "chat_id": chat_id, "hhmm": hhmm},
        )


async def item_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    state: State = app.bot_data["state"]
    data = context.job.data  # type: ignore[union-attr]
    telegram_id = int(data["telegram_id"])
    chat_id = int(data["chat_id"])
    hhmm = str(data["hhmm"])

    due = await _db_call(state, state.db.due_items_for_reminder, telegram_id, hhmm)
    if not due:
        return
    text = items_text(due, f"⏰ Напоминание ({hhmm} UTC):")
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=items_keyboard(due))


async def post_init(app: Application) -> None:
    state: State = app.bot_data["state"]
    buckets = await _db_call(state, state.db.reminder_buckets)
    _schedule_all_reminders(app, buckets)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)
    await _db_call(state, state.db.get_or_create_user, telegram_id, chat_id)
    await update.message.reply_text(  # type: ignore[union-attr]
        "Привет! Я Zero — трекер задач и привычек.\n\n" + HELP_TEXT,
        reply_markup=main_menu(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Меню:", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(AWAITING_KEY, None)
    await update.message.reply_text("Ок, отменил.", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /addtask «текст»")  # type: ignore[union-attr]
        return
    item_id = await _db_call(
        state,
        state.db.add_item,
        telegram_id,
        chat_id,
        kind="task",
        title=text,
        recurrence="once",
    )
    await update.message.reply_text(f"Добавил задачу #{item_id}: {text}", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_addhabit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Использование: /addhabit «название»")  # type: ignore[union-attr]
        return
    item_id = await _db_call(
        state,
        state.db.add_item,
        telegram_id,
        chat_id,
        kind="habit",
        title=name,
        recurrence="daily",
    )
    await update.message.reply_text(f"Добавил привычку #{item_id}: {name}", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    items = await _db_call(state, state.db.list_items, telegram_id)
    await update.message.reply_text(  # type: ignore[union-attr]
        items_text(items, "Все активные items:"),
        reply_markup=items_keyboard(items),
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    items = await _db_call(state, state.db.list_items, telegram_id, kind="task")
    await update.message.reply_text(  # type: ignore[union-attr]
        items_text(items, "Активные задачи:"),
        reply_markup=items_keyboard(items),
    )


async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    items = await _db_call(state, state.db.list_items, telegram_id, kind="habit")
    await update.message.reply_text(  # type: ignore[union-attr]
        items_text(items, "Активные привычки:"),
        reply_markup=items_keyboard(items),
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    if not context.args:
        await update.message.reply_text("Использование: /done «id»")  # type: ignore[union-attr]
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return
    ok = await _db_call(state, state.db.mark_done, telegram_id, item_id)
    await update.message.reply_text("Отмечено ✅" if ok else "Не нашёл item.", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    if not context.args:
        await update.message.reply_text("Использование: /del «id»")  # type: ignore[union-attr]
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return
    ok = await _db_call(state, state.db.deactivate_item, telegram_id, item_id)
    await update.message.reply_text("Удалил." if ok else "Не нашёл item.", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /remind «id» HH:MM (UTC)")  # type: ignore[union-attr]
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return
    hhmm = context.args[1].strip()
    if _parse_hhmm_utc(hhmm) is None:
        await update.message.reply_text("Неверный формат времени. Пример: /remind 12 20:30")  # type: ignore[union-attr]
        return
    ok = await _db_call(state, state.db.set_item_reminder, telegram_id, item_id, hhmm)
    if ok:
        # Reschedule this user's jobs: remove then add all from DB buckets.
        _remove_jobs_for(context.application, telegram_id)
        buckets = await _db_call(state, state.db.reminder_buckets)
        _schedule_all_reminders(context.application, buckets)
    await update.message.reply_text("Ок." if ok else "Не нашёл item.", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_remindoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    if not context.args:
        await update.message.reply_text("Использование: /remindoff «id»")  # type: ignore[union-attr]
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")  # type: ignore[union-attr]
        return
    ok = await _db_call(state, state.db.set_item_reminder, telegram_id, item_id, None)
    if ok:
        _remove_jobs_for(context.application, telegram_id)
        buckets = await _db_call(state, state.db.reminder_buckets)
        _schedule_all_reminders(context.application, buckets)
    await update.message.reply_text("Ок." if ok else "Не нашёл item.", reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)
    await _db_call(state, state.db.get_or_create_user, telegram_id, chat_id)
    stats = await _db_call(state, state.db.get_stats, telegram_id)
    await update.message.reply_text(stats_detailed_text(stats), reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_stats_short(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)
    await _db_call(state, state.db.get_or_create_user, telegram_id, chat_id)
    stats = await _db_call(state, state.db.get_stats, telegram_id)
    await update.message.reply_text(stats_short_text(stats), reply_markup=main_menu())  # type: ignore[union-attr]


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, _chat_id = _user_ids(update)
    items = await _db_call(state, state.db.list_items, telegram_id)
    due = [it for it in items if (it.recurrence == "once" or not it.done_in_period)]
    await update.message.reply_text(  # type: ignore[union-attr]
        items_text(due, "Сводка (что ещё осталось):"),
        reply_markup=items_keyboard(due),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    q = update.callback_query
    if q is None:
        return
    telegram_id = int(q.from_user.id)
    data = (q.data or "").strip()
    if data.startswith("done:"):
        try:
            item_id = int(data.split(":", 1)[1])
        except ValueError:
            await q.answer("Некорректный id.")
            return
        ok = await _db_call(state, state.db.mark_done, telegram_id, item_id)
        await q.answer("✅" if ok else "Не найдено")
        # Best-effort refresh message
        items = await _db_call(state, state.db.list_items, telegram_id)
        try:
            await q.edit_message_text(items_text(items, "Все активные items:"), reply_markup=items_keyboard(items))
        except Exception:
            pass
        return
    await q.answer("Неизвестно.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    telegram_id, chat_id = _user_ids(update)
    text = (update.message.text or "").strip()  # type: ignore[union-attr]

    # Menu buttons
    if text == BTN_ADD_TASK:
        context.user_data[AWAITING_KEY] = AWAIT_TASK
        await update.message.reply_text("Напиши текст задачи одним сообщением. /cancel — отмена", reply_markup=main_menu())  # type: ignore[union-attr]
        return
    if text == BTN_ADD_HABIT:
        context.user_data[AWAITING_KEY] = AWAIT_HABIT
        await update.message.reply_text("Напиши название привычки одним сообщением. /cancel — отмена", reply_markup=main_menu())  # type: ignore[union-attr]
        return
    if text == BTN_TASKS:
        await cmd_tasks(update, context)
        return
    if text == BTN_HABITS:
        await cmd_habits(update, context)
        return
    if text == BTN_LIST_ALL:
        await cmd_items(update, context)
        return
    if text == BTN_SUMMARY:
        await cmd_summary(update, context)
        return
    if text == BTN_STATS:
        await cmd_stats(update, context)
        return
    if text == BTN_STATS_SHORT:
        await cmd_stats_short(update, context)
        return
    if text == BTN_HELP:
        await cmd_help(update, context)
        return
    if text == BTN_CANCEL:
        await cmd_cancel(update, context)
        return

    awaiting = context.user_data.get(AWAITING_KEY)
    if awaiting == AWAIT_TASK:
        payload = text.strip()
        if not payload:
            await update.message.reply_text("Пусто. Напиши текст задачи или /cancel")  # type: ignore[union-attr]
            return
        item_id = await _db_call(
            state,
            state.db.add_item,
            telegram_id,
            chat_id,
            kind="task",
            title=payload,
            recurrence="once",
        )
        context.user_data.pop(AWAITING_KEY, None)
        await update.message.reply_text(f"Добавил задачу #{item_id}: {payload}", reply_markup=main_menu())  # type: ignore[union-attr]
        return

    if awaiting == AWAIT_HABIT:
        payload = text.strip()
        if not payload:
            await update.message.reply_text("Пусто. Напиши название привычки или /cancel")  # type: ignore[union-attr]
            return
        item_id = await _db_call(
            state,
            state.db.add_item,
            telegram_id,
            chat_id,
            kind="habit",
            title=payload,
            recurrence="daily",
        )
        context.user_data.pop(AWAITING_KEY, None)
        await update.message.reply_text(f"Добавил привычку #{item_id}: {payload}", reply_markup=main_menu())  # type: ignore[union-attr]
        return


def build_app(*, token: str, db_path: str) -> Application:
    db = Database(db_path=db_path)
    state = State(db=db, db_lock=asyncio.Lock())

    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.bot_data["state"] = state

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Unified commands
    app.add_handler(CommandHandler("items", cmd_items))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("habits", cmd_habits))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("remindoff", cmd_remindoff))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("stats_short", cmd_stats_short))

    # Backwards compatible aliases
    app.add_handler(CommandHandler("addtask", cmd_addtask))
    app.add_handler(CommandHandler("addhabit", cmd_addhabit))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
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

