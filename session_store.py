"""SQLite-backed session store mapping Telegram user IDs to OpenCode sessions."""

import aiosqlite
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "sessions.db"


@dataclass
class UserSession:
    telegram_user_id: str
    opencode_session_id: str
    created_at: datetime
    updated_at: datetime
    model: Optional[str] = None
    title: Optional[str] = None


class SessionStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._lock = asyncio.Lock()
        self._db: Optional[aiosqlite.Connection] = None

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(str(self.db_path))
            self._db.row_factory = aiosqlite.Row
        return self._db

    async def init(self) -> None:
        """Initialize the database schema."""
        db = await self._get_db()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                telegram_user_id TEXT PRIMARY KEY,
                opencode_session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                model TEXT,
                title TEXT
            )
        """)
        await db.commit()

    async def get_session(self, telegram_user_id: str) -> Optional[UserSession]:
        """Get the OpenCode session ID for a Telegram user."""
        db = await self._get_db()
        async with self._lock:
            async with db.execute(
                "SELECT * FROM sessions WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        return UserSession(
            telegram_user_id=row["telegram_user_id"],
            opencode_session_id=row["opencode_session_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            model=row["model"],
            title=row["title"],
        )

    async def create_session(
        self,
        telegram_user_id: str,
        opencode_session_id: str,
        model: Optional[str] = None,
    ) -> UserSession:
        """Create a new session mapping for a Telegram user."""
        now = datetime.utcnow()
        session = UserSession(
            telegram_user_id=telegram_user_id,
            opencode_session_id=opencode_session_id,
            created_at=now,
            updated_at=now,
            model=model,
        )
        db = await self._get_db()
        async with self._lock:
            await db.execute(
                """
                INSERT OR REPLACE INTO sessions
                (telegram_user_id, opencode_session_id, created_at, updated_at, model, title)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.telegram_user_id,
                    session.opencode_session_id,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.model,
                    session.title,
                ),
            )
            await db.commit()
        return session

    async def update_session(
        self,
        telegram_user_id: str,
        opencode_session_id: Optional[str] = None,
        model: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[UserSession]:
        """Update an existing session."""
        now = datetime.utcnow()
        db = await self._get_db()
        async with self._lock:
            if opencode_session_id is not None:
                await db.execute(
                    """
                    UPDATE sessions
                    SET opencode_session_id = ?, updated_at = ?, model = COALESCE(?, model), title = COALESCE(?, title)
                    WHERE telegram_user_id = ?
                    """,
                    (opencode_session_id, now.isoformat(), model, title, telegram_user_id),
                )
            else:
                await db.execute(
                    """
                    UPDATE sessions
                    SET updated_at = ?, model = COALESCE(?, model), title = COALESCE(?, title)
                    WHERE telegram_user_id = ?
                    """,
                    (now.isoformat(), model, title, telegram_user_id),
                )
            await db.commit()
        return await self.get_session(telegram_user_id)

    async def delete_session(self, telegram_user_id: str) -> bool:
        """Delete a user's session mapping."""
        db = await self._get_db()
        async with self._lock:
            cursor = await db.execute(
                "DELETE FROM sessions WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None