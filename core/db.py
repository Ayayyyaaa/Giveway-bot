"""Accès à la base de données SQLite (persistante, montée en volume Docker)."""
from __future__ import annotations

import aiosqlite
from typing import Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS sticky_config (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    threshold INTEGER NOT NULL DEFAULT 5,
    last_message_id INTEGER,
    counter INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_presence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT,
    activity_type TEXT,
    activity_text TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def set_sticky(self, channel_id, guild_id, text, threshold, message_id):
        await self._conn.execute(
            "INSERT INTO sticky_config (channel_id, guild_id, text, threshold, last_message_id, counter) "
            "VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "guild_id=excluded.guild_id, text=excluded.text, threshold=excluded.threshold, "
            "last_message_id=excluded.last_message_id, counter=0",
            (channel_id, guild_id, text, threshold, message_id),
        )
        await self._conn.commit()

    async def get_sticky(self, channel_id):
        cur = await self._conn.execute("SELECT * FROM sticky_config WHERE channel_id = ?", (channel_id,))
        return await cur.fetchone()

    async def remove_sticky(self, channel_id):
        row = await self.get_sticky(channel_id)
        await self._conn.execute("DELETE FROM sticky_config WHERE channel_id = ?", (channel_id,))
        await self._conn.commit()
        return row

    async def increment_counter(self, channel_id):
        await self._conn.execute(
            "UPDATE sticky_config SET counter = counter + 1 WHERE channel_id = ?", (channel_id,)
        )
        await self._conn.commit()
        row = await self.get_sticky(channel_id)
        return row["counter"] if row else 0

    async def reset_counter_and_message(self, channel_id, new_message_id):
        await self._conn.execute(
            "UPDATE sticky_config SET counter = 0, last_message_id = ? WHERE channel_id = ?",
            (new_message_id, channel_id),
        )
        await self._conn.commit()

    async def get_all_sticky(self):
        cur = await self._conn.execute("SELECT * FROM sticky_config")
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # Présence du bot (statut + activité), pour la réappliquer au redémarrage
    # ------------------------------------------------------------------
    async def set_presence(self, status, activity_type, activity_text):
        await self._conn.execute(
            "INSERT INTO bot_presence (id, status, activity_type, activity_text) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "activity_type=excluded.activity_type, activity_text=excluded.activity_text",
            (status, activity_type, activity_text),
        )
        await self._conn.commit()

    async def get_presence(self):
        cur = await self._conn.execute("SELECT * FROM bot_presence WHERE id = 1")
        return await cur.fetchone()