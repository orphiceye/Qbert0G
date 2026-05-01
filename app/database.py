"""
Database module for QRNG Service.

Handles API key storage, validation, and usage tracking.
Uses SQLite with aiosqlite for async operations.
"""

import secrets
import hashlib
import uuid
from datetime import datetime, date, timedelta
from typing import Optional
import aiosqlite

from .config import get_config


class Database:
    """Async database operations for API keys and usage tracking."""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = get_config().service.database_path
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
    
    async def connect(self) -> None:
        """Open database connection and create tables."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()
        await self._migrate_tables()
        await self._create_bootstrap_admin()
    
    async def disconnect(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
    
    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                name TEXT NOT NULL,
                primary_device_id TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                rate_limit INTEGER,
                daily_byte_limit INTEGER,
                max_bytes_per_request INTEGER,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            );
            
            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
            
            CREATE TABLE IF NOT EXISTS usage_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id TEXT NOT NULL,
                date TEXT NOT NULL,
                requests INTEGER DEFAULT 0,
                bytes_served INTEGER DEFAULT 0,
                FOREIGN KEY (key_id) REFERENCES api_keys(id) ON DELETE CASCADE,
                UNIQUE(key_id, date)
            );
            
            CREATE INDEX IF NOT EXISTS idx_usage_key_date ON usage_records(key_id, date);
        """)
        await self._conn.commit()
    
    async def _migrate_tables(self) -> None:
        """Add columns introduced after initial schema."""
        for column in ("max_bytes_per_request INTEGER",):
            try:
                await self._conn.execute(
                    f"ALTER TABLE api_keys ADD COLUMN {column}"
                )
            except Exception:
                pass  # Column already exists
        await self._conn.commit()

    async def _create_bootstrap_admin(self) -> None:
        """Create bootstrap admin key from config if specified."""
        config = get_config()
        if not config.admin_api_key:
            return
        
        # Check if already exists
        key_hash = self._hash_key(config.admin_api_key)
        cursor = await self._conn.execute(
            "SELECT id FROM api_keys WHERE key_hash = ?",
            (key_hash,)
        )
        if await cursor.fetchone():
            return
        
        # Create bootstrap admin
        key_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        
        await self._conn.execute(
            """INSERT INTO api_keys 
               (id, key_hash, key_prefix, name, primary_device_id, is_admin, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key_id, key_hash, config.admin_api_key[:8], "Bootstrap Admin", "*", 1, 1, now)
        )
        await self._conn.commit()
    
    @staticmethod
    def _hash_key(api_key: str) -> str:
        """Hash an API key for secure storage."""
        return hashlib.sha256(api_key.encode()).hexdigest()
    
    @staticmethod
    def _generate_key() -> str:
        """Generate a new API key."""
        return secrets.token_urlsafe(32)
    
    async def create_api_key(
        self,
        name: str,
        primary_device_id: str,
        is_admin: bool = False,
        rate_limit: Optional[int] = None,
        daily_byte_limit: Optional[int] = None,
        max_bytes_per_request: Optional[int] = None,
    ) -> tuple[str, dict]:
        """
        Create a new API key.

        Returns:
            Tuple of (raw_api_key, key_info_dict)
            The raw key is only returned once at creation time.
        """
        api_key = self._generate_key()
        key_id = str(uuid.uuid4())
        key_hash = self._hash_key(api_key)
        now = datetime.utcnow().isoformat()

        await self._conn.execute(
            """INSERT INTO api_keys
               (id, key_hash, key_prefix, name, primary_device_id, is_admin, enabled,
                rate_limit, daily_byte_limit, max_bytes_per_request, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key_id, key_hash, api_key[:8], name, primary_device_id,
             1 if is_admin else 0, 1, rate_limit, daily_byte_limit,
             max_bytes_per_request, now)
        )
        await self._conn.commit()

        return api_key, {
            "id": key_id,
            "name": name,
            "primary_device_id": primary_device_id,
            "is_admin": is_admin,
            "enabled": True,
            "rate_limit": rate_limit,
            "daily_byte_limit": daily_byte_limit,
            "max_bytes_per_request": max_bytes_per_request,
            "created_at": now,
        }
    
    async def validate_api_key(self, api_key: str) -> Optional[dict]:
        """
        Validate an API key and return its info.
        
        Returns None if key is invalid or disabled.
        """
        key_hash = self._hash_key(api_key)
        
        cursor = await self._conn.execute(
            """SELECT id, name, primary_device_id, is_admin, enabled,
                      rate_limit, daily_byte_limit, max_bytes_per_request,
                      created_at, last_used_at
               FROM api_keys WHERE key_hash = ?""",
            (key_hash,)
        )
        row = await cursor.fetchone()

        if not row:
            return None

        if not row["enabled"]:
            return None

        # Update last used time
        now = datetime.utcnow().isoformat()
        await self._conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (now, row["id"])
        )
        await self._conn.commit()

        return {
            "id": row["id"],
            "name": row["name"],
            "primary_device_id": row["primary_device_id"],
            "is_admin": bool(row["is_admin"]),
            "enabled": bool(row["enabled"]),
            "rate_limit": row["rate_limit"],
            "daily_byte_limit": row["daily_byte_limit"],
            "max_bytes_per_request": row["max_bytes_per_request"],
            "created_at": row["created_at"],
            "last_used_at": now,
        }

    async def get_api_key_by_id(self, key_id: str) -> Optional[dict]:
        """Get API key info by ID (not the actual key)."""
        cursor = await self._conn.execute(
            """SELECT id, key_prefix, name, primary_device_id, is_admin, enabled,
                      rate_limit, daily_byte_limit, max_bytes_per_request,
                      created_at, last_used_at
               FROM api_keys WHERE id = ?""",
            (key_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return None

        return {
            "id": row["id"],
            "key_prefix": row["key_prefix"],
            "name": row["name"],
            "primary_device_id": row["primary_device_id"],
            "is_admin": bool(row["is_admin"]),
            "enabled": bool(row["enabled"]),
            "rate_limit": row["rate_limit"],
            "daily_byte_limit": row["daily_byte_limit"],
            "max_bytes_per_request": row["max_bytes_per_request"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    async def list_api_keys(self) -> list[dict]:
        """List all API keys (without exposing actual keys)."""
        cursor = await self._conn.execute(
            """SELECT id, key_prefix, name, primary_device_id, is_admin, enabled,
                      rate_limit, daily_byte_limit, max_bytes_per_request,
                      created_at, last_used_at
               FROM api_keys ORDER BY created_at DESC"""
        )
        rows = await cursor.fetchall()

        return [
            {
                "id": row["id"],
                "key_prefix": row["key_prefix"],
                "name": row["name"],
                "primary_device_id": row["primary_device_id"],
                "is_admin": bool(row["is_admin"]),
                "enabled": bool(row["enabled"]),
                "rate_limit": row["rate_limit"],
                "daily_byte_limit": row["daily_byte_limit"],
                "max_bytes_per_request": row["max_bytes_per_request"],
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
            }
            for row in rows
        ]
    
    async def update_api_key(
        self,
        key_id: str,
        name: Optional[str] = None,
        primary_device_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        rate_limit: Optional[int] = None,
        daily_byte_limit: Optional[int] = None,
        max_bytes_per_request: Optional[int] = None,
    ) -> bool:
        """Update API key settings. Returns True if key existed."""
        updates = []
        params = []
        
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if primary_device_id is not None:
            updates.append("primary_device_id = ?")
            params.append(primary_device_id)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if rate_limit is not None:
            updates.append("rate_limit = ?")
            params.append(rate_limit)
        if daily_byte_limit is not None:
            updates.append("daily_byte_limit = ?")
            params.append(daily_byte_limit)
        if max_bytes_per_request is not None:
            updates.append("max_bytes_per_request = ?")
            params.append(max_bytes_per_request)

        if not updates:
            return True
        
        params.append(key_id)
        cursor = await self._conn.execute(
            f"UPDATE api_keys SET {', '.join(updates)} WHERE id = ?",
            params
        )
        await self._conn.commit()
        
        return cursor.rowcount > 0
    
    async def delete_api_key(self, key_id: str) -> bool:
        """Delete an API key. Returns True if key existed."""
        cursor = await self._conn.execute(
            "DELETE FROM api_keys WHERE id = ?",
            (key_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0
    
    async def record_usage(self, key_id: str, bytes_served: int) -> None:
        """Record usage for an API key."""
        today = date.today().isoformat()
        
        # Try to update existing record, insert if not exists
        cursor = await self._conn.execute(
            """INSERT INTO usage_records (key_id, date, requests, bytes_served)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(key_id, date) DO UPDATE SET
                   requests = requests + 1,
                   bytes_served = bytes_served + ?""",
            (key_id, today, bytes_served, bytes_served)
        )
        await self._conn.commit()
    
    async def get_usage_today(self, key_id: str) -> dict:
        """Get today's usage for a key."""
        today = date.today().isoformat()
        
        cursor = await self._conn.execute(
            "SELECT requests, bytes_served FROM usage_records WHERE key_id = ? AND date = ?",
            (key_id, today)
        )
        row = await cursor.fetchone()
        
        if not row:
            return {"requests": 0, "bytes_served": 0}
        
        return {"requests": row["requests"], "bytes_served": row["bytes_served"]}
    
    async def get_usage_stats(self, key_id: str, days: int = 7) -> dict:
        """Get usage statistics for a key."""
        # Get key info
        key_info = await self.get_api_key_by_id(key_id)
        if not key_info:
            return None
        
        # Get usage history
        start_date = (date.today() - timedelta(days=days)).isoformat()
        cursor = await self._conn.execute(
            """SELECT date, requests, bytes_served 
               FROM usage_records 
               WHERE key_id = ? AND date >= ?
               ORDER BY date DESC""",
            (key_id, start_date)
        )
        rows = await cursor.fetchall()
        
        # Calculate totals
        total_requests = sum(row["requests"] for row in rows)
        total_bytes = sum(row["bytes_served"] for row in rows)
        
        # Today's usage
        today_usage = await self.get_usage_today(key_id)
        
        return {
            "key_id": key_id,
            "key_name": key_info["name"],
            "primary_device_id": key_info["primary_device_id"],
            "period_days": days,
            "total_requests": total_requests,
            "total_bytes": total_bytes,
            "today_requests": today_usage["requests"],
            "today_bytes": today_usage["bytes_served"],
            "max_bytes_per_request": key_info["max_bytes_per_request"],
            "daily_byte_limit": key_info["daily_byte_limit"],
            "history": [
                {
                    "date": row["date"],
                    "requests": row["requests"],
                    "bytes_served": row["bytes_served"],
                }
                for row in rows
            ],
        }
    
    async def get_aggregated_usage(self, days: int = 7) -> dict:
        """Get aggregated usage across all keys."""
        start_date = (date.today() - timedelta(days=days)).isoformat()
        
        # Daily totals
        cursor = await self._conn.execute(
            """SELECT date, SUM(requests) as requests, SUM(bytes_served) as bytes_served
               FROM usage_records
               WHERE date >= ?
               GROUP BY date
               ORDER BY date DESC""",
            (start_date,)
        )
        daily_rows = await cursor.fetchall()
        
        # By key
        cursor = await self._conn.execute(
            """SELECT k.id, k.name, SUM(u.requests) as requests, SUM(u.bytes_served) as bytes_served
               FROM api_keys k
               LEFT JOIN usage_records u ON k.id = u.key_id AND u.date >= ?
               GROUP BY k.id
               ORDER BY bytes_served DESC""",
            (start_date,)
        )
        key_rows = await cursor.fetchall()
        
        return {
            "period_days": days,
            "daily": [
                {
                    "date": row["date"],
                    "requests": row["requests"],
                    "bytes_served": row["bytes_served"],
                }
                for row in daily_rows
            ],
            "by_key": [
                {
                    "key_id": row["id"],
                    "key_name": row["name"],
                    "requests": row["requests"] or 0,
                    "bytes_served": row["bytes_served"] or 0,
                }
                for row in key_rows
            ],
        }


# Global database instance
_db: Optional[Database] = None


def get_database() -> Database:
    """Get the global database instance."""
    global _db
    if _db is None:
        _db = Database()
    return _db
