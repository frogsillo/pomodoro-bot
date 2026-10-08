"""Capa de persistencia con SQLite (sqlite3 + asyncio.to_thread)."""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._create_tables(conn)
            return conn
        self._conn = await asyncio.to_thread(_open)
        log.info("SQLite lista en %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                sound_filename TEXT
            );
            CREATE TABLE IF NOT EXISTS active_sessions (
                guild_id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                text_channel_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                remaining_seconds REAL NOT NULL,
                deadline_at TEXT,
                paused INTEGER NOT NULL DEFAULT 0,
                pomodoro_count INTEGER NOT NULL,
                total_work_done INTEGER NOT NULL,
                session_started_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS completed_pomodoros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                pomodoros_completed INTEGER NOT NULL,
                work_seconds INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pomodoros_user_date
                ON completed_pomodoros(user_id, completed_at);
            """
        )
        conn.commit()

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            def _run() -> None:
                assert self._conn is not None
                self._conn.execute(sql, params)
                self._conn.commit()
            await asyncio.to_thread(_run)

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        async with self._lock:
            def _run() -> Optional[sqlite3.Row]:
                assert self._conn is not None
                return self._conn.execute(sql, params).fetchone()
            return await asyncio.to_thread(_run)

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        async with self._lock:
            def _run() -> List[sqlite3.Row]:
                assert self._conn is not None
                return self._conn.execute(sql, params).fetchall()
            return await asyncio.to_thread(_run)

    # -------- config por guild --------
    async def get_guild_sound(self, guild_id: int) -> Optional[str]:
        row = await self._fetchone("SELECT sound_filename FROM guild_config WHERE guild_id=?", (guild_id,))
        return row["sound_filename"] if row else None

    async def set_guild_sound(self, guild_id: int, filename: Optional[str]) -> None:
        await self._execute(
            """INSERT INTO guild_config (guild_id, sound_filename) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET sound_filename=excluded.sound_filename""",
            (guild_id, filename),
        )

    # -------- sesiones activas --------
    async def save_active_session(self, data: Dict[str, Any]) -> None:
        await self._execute(
            """INSERT INTO active_sessions
               (guild_id, owner_id, voice_channel_id, text_channel_id,
                phase, remaining_seconds, deadline_at, paused,
                pomodoro_count, total_work_done, session_started_at)
               VALUES (:guild_id, :owner_id, :voice_channel_id, :text_channel_id,
                       :phase, :remaining_seconds, :deadline_at, :paused,
                       :pomodoro_count, :total_work_done, :session_started_at)
               ON CONFLICT(guild_id) DO UPDATE SET
                 owner_id=excluded.owner_id,
                 voice_channel_id=excluded.voice_channel_id,
                 text_channel_id=excluded.text_channel_id,
                 phase=excluded.phase,
                 remaining_seconds=excluded.remaining_seconds,
                 deadline_at=excluded.deadline_at,
                 paused=excluded.paused,
                 pomodoro_count=excluded.pomodoro_count,
                 total_work_done=excluded.total_work_done""",
            data,
        )

    async def delete_active_session(self, guild_id: int) -> None:
        await self._execute("DELETE FROM active_sessions WHERE guild_id=?", (guild_id,))

    async def load_active_sessions(self) -> List[sqlite3.Row]:
        return await self._fetchall("SELECT * FROM active_sessions")

    # -------- registro --------
    async def record_pomodoro(self, user_id: int, guild_id: int, duration_seconds: int) -> None:
        await self._execute(
            """INSERT INTO completed_pomodoros (user_id, guild_id, completed_at, duration_seconds)
               VALUES (?, ?, ?, ?)""",
            (user_id, guild_id, iso_utc(datetime.now(timezone.utc)), int(duration_seconds)),
        )

    async def record_session(self, guild_id: int, user_id: int, started_at: datetime,
                             ended_at: datetime, pomodoros_completed: int, work_seconds: int) -> None:
        await self._execute(
            """INSERT INTO session_history
               (guild_id, user_id, started_at, ended_at, pomodoros_completed, work_seconds)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, iso_utc(started_at), iso_utc(ended_at),
             int(pomodoros_completed), int(work_seconds)),
        )

    # -------- estadísticas --------
    async def get_stats(self, user_id: int) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())

        today = await self._fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration_seconds),0) AS secs "
            "FROM completed_pomodoros WHERE user_id=? AND completed_at>=?",
            (user_id, iso_utc(today_start)),
        )
        week = await self._fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration_seconds),0) AS secs "
            "FROM completed_pomodoros WHERE user_id=? AND completed_at>=?",
            (user_id, iso_utc(week_start)),
        )
        total = await self._fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration_seconds),0) AS secs "
            "FROM completed_pomodoros WHERE user_id=?",
            (user_id,),
        )
        sessions = await self._fetchone(
            "SELECT COUNT(*) AS n FROM session_history WHERE user_id=?", (user_id,)
        )

        rows = await self._fetchall(
            "SELECT DISTINCT substr(completed_at,1,10) AS day "
            "FROM completed_pomodoros WHERE user_id=? ORDER BY day DESC",
            (user_id,),
        )
        days = [r["day"] for r in rows]

        from datetime import date as _date
        streak = 0
        if days:
            today_d = today_start.date()
            yesterday_d = today_d - timedelta(days=1)
            first = _date.fromisoformat(days[0])
            start = today_d if first == today_d else (yesterday_d if first == yesterday_d else None)
            if start is not None:
                current = start
                for d in days:
                    if _date.fromisoformat(d) == current:
                        streak += 1
                        current -= timedelta(days=1)
                    else:
                        break

        total_secs = int(total["secs"] or 0)
        avg = (total_secs / len(days)) if days else 0.0

        return {
            "today_count": int(today["n"] or 0),
            "today_seconds": int(today["secs"] or 0),
            "week_count": int(week["n"] or 0),
            "week_seconds": int(week["secs"] or 0),
            "total_count": int(total["n"] or 0),
            "total_seconds": total_secs,
            "sessions": int(sessions["n"] or 0),
            "streak": streak,
            "avg_daily_seconds": avg,
        }