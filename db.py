from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _today_iso() -> str:
    return date.today().isoformat()


def _parse_iso_date(d: str) -> date:
    # Expect YYYY-MM-DD
    return date.fromisoformat(d)


@dataclass(frozen=True)
class HabitView:
    id: int
    name: str
    streak: int
    done_today: bool
    last_done: str | None


@dataclass(frozen=True)
class TaskView:
    id: int
    text: str
    is_done: bool
    created_at: str
    done_at: str | None


class Database:
    def __init__(self, db_path: str = "data.db") -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self.init()

    def close(self) -> None:
        self._conn.close()

    def init(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              telegram_id INTEGER NOT NULL UNIQUE,
              chat_id INTEGER NOT NULL,
              reminder_time TEXT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS habits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS habit_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              habit_id INTEGER NOT NULL,
              done_date TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(habit_id, done_date),
              FOREIGN KEY(habit_id) REFERENCES habits(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              text TEXT NOT NULL,
              is_done INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              done_at TEXT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_habits_user_id ON habits(user_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_user_id_done ON tasks(user_id, is_done);
            CREATE INDEX IF NOT EXISTS idx_habit_logs_habit_date ON habit_logs(habit_id, done_date);
            """
        )
        self._conn.commit()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def _executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        cur = self._conn.cursor()
        cur.executemany(sql, [tuple(p) for p in seq_of_params])
        return cur

    def get_or_create_user(self, telegram_id: int, chat_id: int) -> int:
        cur = self._execute(
            """
            INSERT INTO users (telegram_id, chat_id, reminder_time, created_at)
            VALUES (?, ?, NULL, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET chat_id=excluded.chat_id
            """,
            (telegram_id, chat_id, _utc_now_iso()),
        )
        self._conn.commit()

        # SQLite's rowcount with ON CONFLICT is not reliable for id; select instead.
        row = self._execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        assert row is not None
        return int(row["id"])

    def set_reminder_time(self, telegram_id: int, time_hhmm: str | None) -> None:
        self._execute(
            "UPDATE users SET reminder_time=? WHERE telegram_id=?",
            (time_hhmm, telegram_id),
        )
        self._conn.commit()

    def get_reminder_time(self, telegram_id: int) -> str | None:
        row = self._execute(
            "SELECT reminder_time FROM users WHERE telegram_id=?",
            (telegram_id,),
        ).fetchone()
        if row is None:
            return None
        return row["reminder_time"]

    def users_with_reminders(self) -> list[tuple[int, int, str]]:
        rows = self._execute(
            "SELECT telegram_id, chat_id, reminder_time FROM users WHERE reminder_time IS NOT NULL"
        ).fetchall()
        out: list[tuple[int, int, str]] = []
        for r in rows:
            if r["reminder_time"] is None:
                continue
            out.append((int(r["telegram_id"]), int(r["chat_id"]), str(r["reminder_time"])))
        return out

    def add_habit(self, telegram_id: int, chat_id: int, name: str) -> int:
        user_id = self.get_or_create_user(telegram_id, chat_id)
        cur = self._execute(
            "INSERT INTO habits (user_id, name, is_active, created_at) VALUES (?, ?, 1, ?)",
            (user_id, name.strip(), _utc_now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def deactivate_habit(self, telegram_id: int, habit_id: int) -> bool:
        cur = self._execute(
            """
            UPDATE habits
            SET is_active=0
            WHERE id=?
              AND user_id=(SELECT id FROM users WHERE telegram_id=?)
            """,
            (habit_id, telegram_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def _habit_done_dates(self, habit_id: int) -> set[date]:
        rows = self._execute(
            "SELECT done_date FROM habit_logs WHERE habit_id=?",
            (habit_id,),
        ).fetchall()
        return {_parse_iso_date(str(r["done_date"])) for r in rows}

    @staticmethod
    def _compute_streak(done_dates: set[date], today: date) -> int:
        streak = 0
        d = today
        while d in done_dates:
            streak += 1
            d = d - timedelta(days=1)
        return streak

    def list_habits(self, telegram_id: int) -> list[HabitView]:
        rows = self._execute(
            """
            SELECT h.id, h.name
            FROM habits h
            JOIN users u ON u.id=h.user_id
            WHERE u.telegram_id=? AND h.is_active=1
            ORDER BY h.id ASC
            """,
            (telegram_id,),
        ).fetchall()

        today = date.today()
        today_iso = today.isoformat()
        out: list[HabitView] = []
        for r in rows:
            hid = int(r["id"])
            dates = self._habit_done_dates(hid)
            last_done = max(dates).isoformat() if dates else None
            out.append(
                HabitView(
                    id=hid,
                    name=str(r["name"]),
                    streak=self._compute_streak(dates, today),
                    done_today=today in dates,
                    last_done=last_done,
                )
            )
        return out

    def mark_habit_done_today(self, telegram_id: int, habit_id: int) -> bool:
        # Verify ownership
        row = self._execute(
            """
            SELECT h.id
            FROM habits h
            JOIN users u ON u.id=h.user_id
            WHERE h.id=? AND u.telegram_id=? AND h.is_active=1
            """,
            (habit_id, telegram_id),
        ).fetchone()
        if row is None:
            return False

        try:
            self._execute(
                "INSERT INTO habit_logs (habit_id, done_date, created_at) VALUES (?, ?, ?)",
                (habit_id, _today_iso(), _utc_now_iso()),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            # already marked for today
            return True

    def add_task(self, telegram_id: int, chat_id: int, text: str) -> int:
        user_id = self.get_or_create_user(telegram_id, chat_id)
        cur = self._execute(
            "INSERT INTO tasks (user_id, text, is_done, created_at, done_at) VALUES (?, ?, 0, ?, NULL)",
            (user_id, text.strip(), _utc_now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_tasks(self, telegram_id: int, include_done: bool = False) -> list[TaskView]:
        if include_done:
            rows = self._execute(
                """
                SELECT t.id, t.text, t.is_done, t.created_at, t.done_at
                FROM tasks t
                JOIN users u ON u.id=t.user_id
                WHERE u.telegram_id=?
                ORDER BY t.is_done ASC, t.id ASC
                """,
                (telegram_id,),
            ).fetchall()
        else:
            rows = self._execute(
                """
                SELECT t.id, t.text, t.is_done, t.created_at, t.done_at
                FROM tasks t
                JOIN users u ON u.id=t.user_id
                WHERE u.telegram_id=? AND t.is_done=0
                ORDER BY t.id ASC
                """,
                (telegram_id,),
            ).fetchall()

        out: list[TaskView] = []
        for r in rows:
            out.append(
                TaskView(
                    id=int(r["id"]),
                    text=str(r["text"]),
                    is_done=bool(int(r["is_done"])),
                    created_at=str(r["created_at"]),
                    done_at=str(r["done_at"]) if r["done_at"] is not None else None,
                )
            )
        return out

    def mark_task_done(self, telegram_id: int, task_id: int) -> bool:
        cur = self._execute(
            """
            UPDATE tasks
            SET is_done=1, done_at=?
            WHERE id=?
              AND user_id=(SELECT id FROM users WHERE telegram_id=?)
            """,
            (_utc_now_iso(), task_id, telegram_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_task(self, telegram_id: int, task_id: int) -> bool:
        cur = self._execute(
            """
            DELETE FROM tasks
            WHERE id=?
              AND user_id=(SELECT id FROM users WHERE telegram_id=?)
            """,
            (task_id, telegram_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def pending_summary(self, telegram_id: int) -> tuple[list[HabitView], list[TaskView]]:
        habits = self.list_habits(telegram_id)
        tasks = self.list_tasks(telegram_id, include_done=False)
        return habits, tasks
