from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Literal

ItemKind = Literal["task", "habit"]
Recurrence = Literal["once", "daily", "weekly", "interval"]


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _today() -> date:
    return date.today()


def _today_iso() -> str:
    return _today().isoformat()


def _parse_iso_date(d: str) -> date:
    return date.fromisoformat(d)


def _date_from_iso_datetime_prefix(s: str) -> date:
    # Accept "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SSZ"
    return date.fromisoformat(s[:10])


def _iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


@dataclass(frozen=True)
class ItemView:
    id: int
    kind: ItemKind
    title: str
    recurrence: Recurrence
    interval_days: int | None
    reminder_time: str | None
    is_active: bool
    created_at: str
    completed_at: str | None
    done_in_period: bool
    streak: int
    last_done_key: str | None


class Database:
    """
    Schema v2: unified items + logs.
    - items: tasks and habits unified, with recurrence + per-item reminder time
    - item_logs: "done" markers per recurrence period (day/week/interval bucket/once)
    """

    SCHEMA_VERSION = 2

    def __init__(self, db_path: str = "data.db") -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._init_and_migrate()

    def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def _executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        cur = self._conn.cursor()
        cur.executemany(sql, [tuple(p) for p in seq_of_params])
        return cur

    def _init_meta(self) -> None:
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
              id INTEGER PRIMARY KEY CHECK (id=1),
              version INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        row = self._execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
        if row is None:
            self._execute(
                "INSERT INTO schema_meta (id, version, updated_at) VALUES (1, ?, ?)",
                (0, _utc_now_iso()),
            )
        self._conn.commit()

    def _get_version(self) -> int:
        row = self._execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
        return int(row["version"]) if row else 0

    def _set_version(self, v: int) -> None:
        self._execute("UPDATE schema_meta SET version=?, updated_at=? WHERE id=1", (v, _utc_now_iso()))
        self._conn.commit()

    def _ensure_users_table(self) -> None:
        # Keep users table compatible with existing installs.
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              telegram_id INTEGER NOT NULL UNIQUE,
              chat_id INTEGER NOT NULL,
              reminder_time TEXT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def _ensure_v2_tables(self) -> None:
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              kind TEXT NOT NULL CHECK (kind IN ('task','habit')),
              title TEXT NOT NULL,
              recurrence TEXT NOT NULL CHECK (recurrence IN ('once','daily','weekly','interval')),
              interval_days INTEGER NULL,
              reminder_time TEXT NULL,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              completed_at TEXT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS item_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL,
              period_key TEXT NOT NULL,
              done_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(item_id, period_key),
              FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_items_user_active ON items(user_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_items_user_kind_active ON items(user_id, kind, is_active);
            CREATE INDEX IF NOT EXISTS idx_item_logs_item_key ON item_logs(item_id, period_key);
            """
        )
        self._conn.commit()

    def _table_exists(self, name: str) -> bool:
        row = self._execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def _init_and_migrate(self) -> None:
        self._init_meta()
        self._ensure_users_table()

        current = self._get_version()
        if current >= self.SCHEMA_VERSION:
            self._ensure_v2_tables()
            return

        # If we have legacy tables, migrate them into v2.
        self._ensure_v2_tables()
        has_legacy = self._table_exists("habits") or self._table_exists("tasks")
        if has_legacy:
            self._migrate_from_legacy()
        self._set_version(self.SCHEMA_VERSION)

    def _migrate_from_legacy(self) -> None:
        # Idempotent migration: only migrate if items table is empty.
        row = self._execute("SELECT COUNT(*) AS c FROM items").fetchone()
        if row and int(row["c"]) > 0:
            return

        # Ensure legacy tables exist (they might not).
        has_habits = self._table_exists("habits") and self._table_exists("habit_logs")
        has_tasks = self._table_exists("tasks")

        habit_map: dict[int, int] = {}

        if has_habits:
            # habits -> items(kind=habit, recurrence=daily)
            habit_rows = self._execute(
                "SELECT id, user_id, name, is_active, created_at FROM habits ORDER BY id ASC"
            ).fetchall()
            for r in habit_rows:
                cur = self._execute(
                    """
                    INSERT INTO items
                      (user_id, kind, title, recurrence, interval_days, reminder_time, is_active, created_at, completed_at)
                    VALUES
                      (?, 'habit', ?, 'daily', NULL, NULL, ?, ?, NULL)
                    """,
                    (int(r["user_id"]), str(r["name"]), int(r["is_active"]), str(r["created_at"])),
                )
                habit_map[int(r["id"])] = int(cur.lastrowid)

            # habit_logs -> item_logs(period_key=done_date)
            log_rows = self._execute(
                "SELECT habit_id, done_date, created_at FROM habit_logs ORDER BY id ASC"
            ).fetchall()
            params: list[tuple[int, str, str, str]] = []
            for r in log_rows:
                old_hid = int(r["habit_id"])
                new_item_id = habit_map.get(old_hid)
                if not new_item_id:
                    continue
                done_date = str(r["done_date"])
                created_at = str(r["created_at"])
                params.append((new_item_id, done_date, created_at, created_at))
            if params:
                self._executemany(
                    """
                    INSERT OR IGNORE INTO item_logs (item_id, period_key, done_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    params,
                )

        if has_tasks:
            task_rows = self._execute(
                "SELECT id, user_id, text, is_done, created_at, done_at FROM tasks ORDER BY id ASC"
            ).fetchall()
            for r in task_rows:
                is_done = int(r["is_done"])
                completed_at = str(r["done_at"]) if (is_done and r["done_at"] is not None) else None
                is_active = 0 if is_done else 1
                cur = self._execute(
                    """
                    INSERT INTO items
                      (user_id, kind, title, recurrence, interval_days, reminder_time, is_active, created_at, completed_at)
                    VALUES
                      (?, 'task', ?, 'once', NULL, NULL, ?, ?, ?)
                    """,
                    (
                        int(r["user_id"]),
                        str(r["text"]),
                        is_active,
                        str(r["created_at"]),
                        completed_at,
                    ),
                )
                new_item_id = int(cur.lastrowid)
                if completed_at:
                    self._execute(
                        """
                        INSERT OR IGNORE INTO item_logs (item_id, period_key, done_at, created_at)
                        VALUES (?, 'once', ?, ?)
                        """,
                        (new_item_id, completed_at, completed_at),
                    )

        self._conn.commit()

    # --- Users ---
    def get_or_create_user(self, telegram_id: int, chat_id: int) -> int:
        self._execute(
            """
            INSERT INTO users (telegram_id, chat_id, reminder_time, created_at)
            VALUES (?, ?, NULL, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET chat_id=excluded.chat_id
            """,
            (telegram_id, chat_id, _utc_now_iso()),
        )
        self._conn.commit()
        row = self._execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        assert row is not None
        return int(row["id"])

    def get_chat_id(self, telegram_id: int) -> int | None:
        row = self._execute("SELECT chat_id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        return int(row["chat_id"]) if row else None

    # --- Period keys / due logic ---
    def _period_key_for(self, item_row: sqlite3.Row, today: date) -> str:
        rec = str(item_row["recurrence"])
        if rec == "once":
            return "once"
        if rec == "daily":
            return today.isoformat()
        if rec == "weekly":
            return _iso_week_key(today)
        if rec == "interval":
            interval_days = int(item_row["interval_days"] or 1)
            start = _date_from_iso_datetime_prefix(str(item_row["created_at"]))
            delta = (today - start).days
            if delta < 0:
                delta = 0
            n = delta // max(interval_days, 1)
            period_start = start + timedelta(days=n * max(interval_days, 1))
            return period_start.isoformat()
        # fallback
        return today.isoformat()

    def _streak_for_daily(self, item_id: int, today: date) -> int:
        rows = self._execute(
            "SELECT period_key FROM item_logs WHERE item_id=?",
            (item_id,),
        ).fetchall()
        dates = set()
        for r in rows:
            k = str(r["period_key"])
            if len(k) == 10 and k[4] == "-" and k[7] == "-":
                try:
                    dates.add(_parse_iso_date(k))
                except Exception:
                    pass
        streak = 0
        d = today
        while d in dates:
            streak += 1
            d = d - timedelta(days=1)
        return streak

    # --- Items ---
    def add_item(
        self,
        telegram_id: int,
        chat_id: int,
        *,
        kind: ItemKind,
        title: str,
        recurrence: Recurrence,
        interval_days: int | None = None,
        reminder_time: str | None = None,
    ) -> int:
        user_id = self.get_or_create_user(telegram_id, chat_id)
        cur = self._execute(
            """
            INSERT INTO items
              (user_id, kind, title, recurrence, interval_days, reminder_time, is_active, created_at, completed_at)
            VALUES
              (?, ?, ?, ?, ?, ?, 1, ?, NULL)
            """,
            (user_id, kind, title.strip(), recurrence, interval_days, reminder_time, _utc_now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_items(self, telegram_id: int, *, kind: ItemKind | None = None) -> list[ItemView]:
        today = _today()
        if kind:
            rows = self._execute(
                """
                SELECT i.*
                FROM items i
                JOIN users u ON u.id=i.user_id
                WHERE u.telegram_id=? AND i.is_active=1 AND i.kind=?
                ORDER BY i.id ASC
                """,
                (telegram_id, kind),
            ).fetchall()
        else:
            rows = self._execute(
                """
                SELECT i.*
                FROM items i
                JOIN users u ON u.id=i.user_id
                WHERE u.telegram_id=? AND i.is_active=1
                ORDER BY i.kind ASC, i.id ASC
                """,
                (telegram_id,),
            ).fetchall()

        out: list[ItemView] = []
        for r in rows:
            item_id = int(r["id"])
            period_key = self._period_key_for(r, today)
            done_row = self._execute(
                "SELECT 1 AS x FROM item_logs WHERE item_id=? AND period_key=?",
                (item_id, period_key),
            ).fetchone()
            last_row = self._execute(
                "SELECT MAX(period_key) AS k FROM item_logs WHERE item_id=?",
                (item_id,),
            ).fetchone()
            last_key = str(last_row["k"]) if (last_row and last_row["k"] is not None) else None
            rec = str(r["recurrence"])
            streak = self._streak_for_daily(item_id, today) if rec == "daily" else 0
            out.append(
                ItemView(
                    id=item_id,
                    kind=str(r["kind"]),  # type: ignore[arg-type]
                    title=str(r["title"]),
                    recurrence=str(r["recurrence"]),  # type: ignore[arg-type]
                    interval_days=int(r["interval_days"]) if r["interval_days"] is not None else None,
                    reminder_time=str(r["reminder_time"]) if r["reminder_time"] is not None else None,
                    is_active=bool(int(r["is_active"])),
                    created_at=str(r["created_at"]),
                    completed_at=str(r["completed_at"]) if r["completed_at"] is not None else None,
                    done_in_period=done_row is not None,
                    streak=streak,
                    last_done_key=last_key,
                )
            )
        return out

    def mark_done(self, telegram_id: int, item_id: int) -> bool:
        # Verify ownership + active
        row = self._execute(
            """
            SELECT i.*
            FROM items i
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.id=? AND i.is_active=1
            """,
            (telegram_id, item_id),
        ).fetchone()
        if row is None:
            return False

        today = _today()
        period_key = self._period_key_for(row, today)
        now = _utc_now_iso()
        try:
            self._execute(
                "INSERT INTO item_logs (item_id, period_key, done_at, created_at) VALUES (?, ?, ?, ?)",
                (item_id, period_key, now, now),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # already marked in this period
            return True

        if str(row["recurrence"]) == "once":
            self._execute("UPDATE items SET is_active=0, completed_at=? WHERE id=?", (now, item_id))
            self._conn.commit()
        return True

    def deactivate_item(self, telegram_id: int, item_id: int) -> bool:
        cur = self._execute(
            """
            UPDATE items
            SET is_active=0
            WHERE id=?
              AND user_id=(SELECT id FROM users WHERE telegram_id=?)
            """,
            (item_id, telegram_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # --- Reminders (per item) ---
    def set_item_reminder(self, telegram_id: int, item_id: int, hhmm: str | None) -> bool:
        cur = self._execute(
            """
            UPDATE items
            SET reminder_time=?
            WHERE id=?
              AND user_id=(SELECT id FROM users WHERE telegram_id=?)
            """,
            (hhmm, item_id, telegram_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def reminder_buckets(self) -> list[tuple[int, int, str]]:
        """
        Returns distinct (telegram_id, chat_id, reminder_time) for which there are active items.
        """
        rows = self._execute(
            """
            SELECT DISTINCT u.telegram_id AS telegram_id, u.chat_id AS chat_id, i.reminder_time AS rt
            FROM items i
            JOIN users u ON u.id=i.user_id
            WHERE i.is_active=1 AND i.reminder_time IS NOT NULL
            """
        ).fetchall()
        out: list[tuple[int, int, str]] = []
        for r in rows:
            rt = r["rt"]
            if rt is None:
                continue
            out.append((int(r["telegram_id"]), int(r["chat_id"]), str(rt)))
        return out

    def due_items_for_reminder(self, telegram_id: int, hhmm: str) -> list[ItemView]:
        """
        Active items with reminder_time=hhmm that are due in current period.
        """
        items = self.list_items(telegram_id)
        due: list[ItemView] = []
        for it in items:
            if it.reminder_time != hhmm:
                continue
            if it.recurrence == "once":
                # due if still active (it is)
                due.append(it)
            else:
                if not it.done_in_period:
                    due.append(it)
        return due

    # --- Stats (unified) ---
    def get_stats(self, telegram_id: int) -> dict[str, Any]:
        today = _today()
        start_7 = (today - timedelta(days=6)).isoformat()
        start_30 = (today - timedelta(days=29)).isoformat()

        # Items
        active_rows = self._execute(
            """
            SELECT i.*
            FROM items i
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.is_active=1
            """,
            (telegram_id,),
        ).fetchall()
        active_total = len(active_rows)
        active_tasks = sum(1 for r in active_rows if str(r["kind"]) == "task")
        active_habits = sum(1 for r in active_rows if str(r["kind"]) == "habit")

        # Done today for daily/weekly/interval "period" isn't uniform; we keep "today" metric for daily only.
        daily_habits = self._execute(
            """
            SELECT i.id
            FROM items i
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.is_active=1 AND i.kind='habit' AND i.recurrence='daily'
            """,
            (telegram_id,),
        ).fetchall()
        daily_habit_ids = [int(r["id"]) for r in daily_habits]
        done_today = 0
        if daily_habit_ids:
            placeholders = ",".join(["?"] * len(daily_habit_ids))
            row = self._execute(
                f"""
                SELECT COALESCE(COUNT(*), 0) AS c
                FROM item_logs
                WHERE item_id IN ({placeholders}) AND period_key=?
                """,
                [*daily_habit_ids, today.isoformat()],
            ).fetchone()
            done_today = int(row["c"]) if row else 0

        # Total done logs by kind in windows (daily keys only for habits; tasks are once/period_key='once')
        habit_checks_total_row = self._execute(
            """
            SELECT COALESCE(COUNT(*), 0) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='habit'
            """,
            (telegram_id,),
        ).fetchone()
        habit_checks_total = int(habit_checks_total_row["c"]) if habit_checks_total_row else 0

        habit_checks_7_row = self._execute(
            """
            SELECT COALESCE(COUNT(*), 0) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='habit' AND l.period_key >= ?
              AND length(l.period_key)=10
            """,
            (telegram_id, start_7),
        ).fetchone()
        habit_checks_7 = int(habit_checks_7_row["c"]) if habit_checks_7_row else 0

        habit_checks_30_row = self._execute(
            """
            SELECT COALESCE(COUNT(*), 0) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='habit' AND l.period_key >= ?
              AND length(l.period_key)=10
            """,
            (telegram_id, start_30),
        ).fetchone()
        habit_checks_30 = int(habit_checks_30_row["c"]) if habit_checks_30_row else 0

        tasks_done_total_row = self._execute(
            """
            SELECT COALESCE(COUNT(*), 0) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='task' AND l.period_key='once'
            """,
            (telegram_id,),
        ).fetchone()
        tasks_done_total = int(tasks_done_total_row["c"]) if tasks_done_total_row else 0

        tasks_done_7_row = self._execute(
            """
            SELECT COALESCE(COUNT(*), 0) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='task' AND l.period_key='once'
              AND substr(l.done_at, 1, 10) >= ?
            """,
            (telegram_id, start_7),
        ).fetchone()
        tasks_done_7 = int(tasks_done_7_row["c"]) if tasks_done_7_row else 0

        tasks_done_30_row = self._execute(
            """
            SELECT COALESCE(COUNT(*), 0) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='task' AND l.period_key='once'
              AND substr(l.done_at, 1, 10) >= ?
            """,
            (telegram_id, start_30),
        ).fetchone()
        tasks_done_30 = int(tasks_done_30_row["c"]) if tasks_done_30_row else 0

        # Last 7 days trend (habits: daily period_key; tasks: done_at date)
        habit_daily_rows = self._execute(
            """
            SELECT l.period_key AS d, COUNT(*) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='habit'
              AND length(l.period_key)=10 AND l.period_key >= ?
            GROUP BY l.period_key
            ORDER BY l.period_key ASC
            """,
            (telegram_id, start_7),
        ).fetchall()
        habit_daily_map = {str(r["d"]): int(r["c"]) for r in habit_daily_rows}

        task_daily_rows = self._execute(
            """
            SELECT substr(l.done_at, 1, 10) AS d, COUNT(*) AS c
            FROM item_logs l
            JOIN items i ON i.id=l.item_id
            JOIN users u ON u.id=i.user_id
            WHERE u.telegram_id=? AND i.kind='task' AND l.period_key='once'
              AND substr(l.done_at, 1, 10) >= ?
            GROUP BY substr(l.done_at, 1, 10)
            ORDER BY d ASC
            """,
            (telegram_id, start_7),
        ).fetchall()
        task_daily_map = {str(r["d"]): int(r["c"]) for r in task_daily_rows}

        last7: list[dict[str, Any]] = []
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            last7.append(
                {
                    "date": d,
                    "habit_checks": int(habit_daily_map.get(d, 0)),
                    "tasks_done": int(task_daily_map.get(d, 0)),
                }
            )

        # Per habit summary (daily only shows streak)
        per_habit: list[dict[str, Any]] = []
        for it in self.list_items(telegram_id, kind="habit"):
            per_habit.append(
                {
                    "id": it.id,
                    "name": it.title,
                    "recurrence": it.recurrence,
                    "interval_days": it.interval_days,
                    "done_in_period": it.done_in_period,
                    "streak": it.streak,
                    "last_done_key": it.last_done_key,
                }
            )

        best_streak = max((int(h["streak"]) for h in per_habit), default=0)

        return {
            "active_total": active_total,
            "active_tasks": active_tasks,
            "active_habits": active_habits,
            "habits_done_today": done_today,
            "habits_total_daily": len(daily_habit_ids),
            "habit_checks_total": habit_checks_total,
            "habit_checks_7": habit_checks_7,
            "habit_checks_30": habit_checks_30,
            "tasks_done_total": tasks_done_total,
            "tasks_done_7": tasks_done_7,
            "tasks_done_30": tasks_done_30,
            "best_streak": best_streak,
            "per_habit": per_habit,
            "last7": last7,
        }

