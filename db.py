"""Async SQLite persistence layer for the bot (presence + giveaways).

The database file lives under DATA_DIR (mounted as a Docker volume, see
docker-compose.yml) so data survives container recreation/updates.
"""
import os

import aiosqlite

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "bot.sqlite3")


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._create_tables()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _create_tables(self):
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS presence (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT,
                activity_type TEXT,
                activity_text TEXT
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER,
                host_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                winners INTEGER NOT NULL,
                color INTEGER NOT NULL,
                end_ts INTEGER NOT NULL
            )
            """
        )
        await self._conn.commit()

    # --- Presence ---

    async def get_presence(self):
        async with self._conn.execute(
            "SELECT status, activity_type, activity_text FROM presence WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_presence(self, status: str, activity_type: str | None, activity_text: str | None):
        await self._conn.execute(
            """
            INSERT INTO presence (id, status, activity_type, activity_text)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                activity_type = excluded.activity_type,
                activity_text = excluded.activity_text
            """,
            (status, activity_type, activity_text),
        )
        await self._conn.commit()

    # --- Giveaways ---

    async def add_giveaway(
        self,
        message_id: int,
        channel_id: int,
        guild_id: int | None,
        host_id: int,
        prize: str,
        winners: int,
        color: int,
        end_ts: int,
    ):
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO giveaways
                (message_id, channel_id, guild_id, host_id, prize, winners, color, end_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, channel_id, guild_id, host_id, prize, winners, color, end_ts),
        )
        await self._conn.commit()

    async def remove_giveaway(self, message_id: int):
        await self._conn.execute("DELETE FROM giveaways WHERE message_id = ?", (message_id,))
        await self._conn.commit()

    async def get_active_giveaways(self):
        async with self._conn.execute(
            "SELECT message_id, channel_id, guild_id, host_id, prize, winners, color, end_ts FROM giveaways"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]