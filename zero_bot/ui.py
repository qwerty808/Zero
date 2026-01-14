from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from .db import ItemView


BTN_ADD_TASK = "➕ Задача"
BTN_ADD_HABIT = "➕ Привычка"
BTN_LIST_ALL = "📋 Всё"
BTN_TASKS = "📋 Задачи"
BTN_HABITS = "🔥 Привычки"
BTN_SUMMARY = "📌 Сводка"
BTN_STATS = "📊 Статистика"
BTN_STATS_SHORT = "📊 Кратко"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "❌ Отмена"


def main_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_ADD_TASK), KeyboardButton(BTN_ADD_HABIT)],
        [KeyboardButton(BTN_TASKS), KeyboardButton(BTN_HABITS)],
        [KeyboardButton(BTN_LIST_ALL), KeyboardButton(BTN_SUMMARY)],
        [KeyboardButton(BTN_STATS), KeyboardButton(BTN_STATS_SHORT)],
        [KeyboardButton(BTN_HELP), KeyboardButton(BTN_CANCEL)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


def items_keyboard(items: list[ItemView]) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for it in items:
        label = f"✅ #{it.id}"
        row.append(InlineKeyboardButton(label, callback_data=f"done:{it.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None


def _spark_bar(value: int, max_value: int, width: int = 10) -> str:
    if max_value <= 0:
        return "▱" * width
    filled = int(round((value / max_value) * width))
    filled = max(0, min(width, filled))
    return ("▰" * filled) + ("▱" * (width - filled))


def stats_short_text(stats: dict) -> str:
    h_done = int(stats["habits_done_today"])
    h_total_daily = int(stats["habits_total_daily"])
    total_active = int(stats["active_total"])
    active_tasks = int(stats["active_tasks"])
    active_habits = int(stats["active_habits"])
    h7 = int(stats["habit_checks_7"])
    t7 = int(stats["tasks_done_7"])
    t_total_done = int(stats["tasks_done_total"])
    best_streak = int(stats["best_streak"])
    return "\n".join(
        [
            "Краткая статистика:",
            f"- Активных: {total_active} (🔥 {active_habits} / 📋 {active_tasks})",
            f"- Привычки daily сегодня: {h_done}/{h_total_daily} | лучший стрик: {best_streak}",
            f"- За 7 дней: отметок привычек {h7} | закрыто задач {t7}",
            f"- Задач закрыто всего: {t_total_done}",
        ]
    )


def stats_detailed_text(stats: dict) -> str:
    total_active = int(stats["active_total"])
    active_tasks = int(stats["active_tasks"])
    active_habits = int(stats["active_habits"])
    h_done = int(stats["habits_done_today"])
    h_total_daily = int(stats["habits_total_daily"])
    h_total_checks = int(stats["habit_checks_total"])
    h7 = int(stats["habit_checks_7"])
    h30 = int(stats["habit_checks_30"])
    t_total_done = int(stats["tasks_done_total"])
    t7 = int(stats["tasks_done_7"])
    t30 = int(stats["tasks_done_30"])

    last7 = list(stats["last7"])
    max_h = max((int(x["habit_checks"]) for x in last7), default=0)
    max_t = max((int(x["tasks_done"]) for x in last7), default=0)

    lines: list[str] = []
    lines.append("📊 Подробная статистика (унифицированная)")
    lines.append("")
    lines.append(f"Активные items: {total_active} (🔥 {active_habits} / 📋 {active_tasks})")
    lines.append("")
    lines.append("Привычки:")
    lines.append(f"- Daily сегодня: {h_done}/{h_total_daily}")
    lines.append(f"- Отметок всего: {h_total_checks}")
    lines.append(f"- Отметок за 7 дней: {h7} | за 30 дней: {h30}")
    lines.append("")
    lines.append("Задачи (once):")
    lines.append(f"- Закрыто всего: {t_total_done}")
    lines.append(f"- Закрыто за 7 дней: {t7} | за 30 дней: {t30}")
    lines.append("")
    lines.append("Тренд (7 дней):")
    for d in last7:
        day = str(d["date"])[5:]  # MM-DD
        hc = int(d["habit_checks"])
        td = int(d["tasks_done"])
        lines.append(f"{day}  H {hc:>2} {_spark_bar(hc, max_h)}   T {td:>2} {_spark_bar(td, max_t)}")

    per_habit = list(stats["per_habit"])
    if per_habit:
        lines.append("")
        lines.append("По привычкам:")
        for h in per_habit:
            rec = str(h["recurrence"])
            rec_label = rec
            if rec == "interval":
                rec_label = f"interval:{h['interval_days']}"
            mark = "✅" if bool(h["done_in_period"]) else "—"
            last = h["last_done_key"] or "никогда"
            lines.append(f"{mark} #{h['id']} {h['name']} | {rec_label} | стрик {h['streak']} | последний {last}")

    return "\n".join(lines)


def items_text(items: list[ItemView], title: str) -> str:
    if not items:
        return f"{title}\n\nПусто."
    lines = [title]
    for it in items:
        kind = "🔥" if it.kind == "habit" else "📋"
        rec = it.recurrence
        if rec == "interval" and it.interval_days:
            rec = f"interval:{it.interval_days}"
        done = "✅" if it.done_in_period else "—"
        extra = ""
        if it.kind == "habit" and it.recurrence == "daily":
            extra = f" | стрик {it.streak}"
        if it.reminder_time:
            extra += f" | ⏰ {it.reminder_time}"
        lines.append(f"{done} #{it.id} {kind} {it.title} [{rec}]{extra}")
    lines.append("")
    lines.append("Отметить: /done «id» | Удалить: /del «id» | Напомнить: /remind «id» HH:MM | /remindoff «id»")
    return "\n".join(lines)

