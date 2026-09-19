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
                end_ts INTEGER NOT NULL,
                reward TEXT,
                banner_url TEXT,
                picture_url TEXT
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triggers (
                guild_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                response TEXT NOT NULL,
                reaction TEXT,
                cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, word)
            )
            """
        )
        # Migration: add new giveaway columns if this DB was created before they existed.
        # SQLite's ADD COLUMN is a no-op-safe operation we guard with a try/except
        # so existing deployments keep their data instead of needing a manual fix.
        for column, ddl in (
            ("reward", "ALTER TABLE giveaways ADD COLUMN reward TEXT"),
            ("banner_url", "ALTER TABLE giveaways ADD COLUMN banner_url TEXT"),
            ("picture_url", "ALTER TABLE giveaways ADD COLUMN picture_url TEXT"),
        ):
            try:
                await self._conn.execute(ddl)
            except aiosqlite.OperationalError:
                pass  # column already exists
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
        reward: str | None = None,
        banner_url: str | None = None,
        picture_url: str | None = None,
    ):
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO giveaways
                (message_id, channel_id, guild_id, host_id, prize, winners, color, end_ts,
                 reward, banner_url, picture_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, channel_id, guild_id, host_id, prize, winners, color, end_ts,
                reward, banner_url, picture_url,
            ),
        )
        await self._conn.commit()

    async def remove_giveaway(self, message_id: int):
        await self._conn.execute("DELETE FROM giveaways WHERE message_id = ?", (message_id,))
        await self._conn.commit()

    async def get_active_giveaways(self):
        async with self._conn.execute(
            """
            SELECT message_id, channel_id, guild_id, host_id, prize, winners, color, end_ts,
                   reward, banner_url, picture_url
            FROM giveaways
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # --- Triggers (auto-responder) ---

    async def upsert_trigger(
        self,
        guild_id: int,
        word: str,
        response: str,
        reaction: str | None,
        cooldown_seconds: int,
    ):
        await self._conn.execute(
            """
            INSERT INTO triggers (guild_id, word, response, reaction, cooldown_seconds)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, word) DO UPDATE SET
                response = excluded.response,
                reaction = excluded.reaction,
                cooldown_seconds = excluded.cooldown_seconds
            """,
            (guild_id, word.lower(), response, reaction, cooldown_seconds),
        )
        await self._conn.commit()

    async def remove_trigger(self, guild_id: int, word: str):
        await self._conn.execute(
            "DELETE FROM triggers WHERE guild_id = ? AND word = ?", (guild_id, word.lower())
        )
        await self._conn.commit()

    async def get_triggers(self, guild_id: int | None = None):
        if guild_id is None:
            query = "SELECT guild_id, word, response, reaction, cooldown_seconds FROM triggers"
            params = ()
        else:
            query = (
                "SELECT guild_id, word, response, reaction, cooldown_seconds "
                "FROM triggers WHERE guild_id = ?"
            )
            params = (guild_id,)
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]