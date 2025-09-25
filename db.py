
"""
db.py — unified DB helper for MySQL/MariaDB and SQLite
------------------------------------------------------
Minimal, robust helper that supports explicit `db_backend`
and keeps call sites using %s param style.
"""
from __future__ import annotations

import sys
import contextlib
from typing import Any, Iterable, Optional, Tuple

# Try MySQL connector, but don't require it for SQLite runs
try:
    import mysql.connector  # type: ignore
    from mysql.connector import Error as MySQLError  # type: ignore
    _mysql_available = True
except Exception:
    mysql = None
    MySQLError = Exception  # fallback for typing
    _mysql_available = False

import sqlite3


class DatabaseHelper:
    def __init__(
        self,
        mysql_pass: str,
        version: str,
        logger,
        host: str = "localhost",
        user: str = "imap_nilsimsa",
        db: str = "imap_nilsimsa",
        autocommit: bool = True,
        db_backend: Optional[str] = None,
    ) -> None:
        self.logger = logger
        self.version = version
        self.backend = (db_backend or "mysql").lower()
        self._param_mysql = "%s"
        self._param_sqlite = "?"

        try:
            if self.backend == "sqlite":
                sqlite_path = db or ":memory:"
                self.conn = sqlite3.connect(sqlite_path, check_same_thread=False)
                # sane defaults
                with self.conn:
                    self.conn.execute("PRAGMA foreign_keys = ON")
                if autocommit:
                    self.conn.isolation_level = None  # autocommit
                self.cursor = self.conn.cursor()
            else:
                if not _mysql_available:
                    raise RuntimeError("mysql-connector not available in this environment")
                # guard against empty user/password (connector will default user to OS login)
                user = (user or "imap_nilsimsa").strip()
                mysql_pass = (mysql_pass or "")
                self.conn = mysql.connector.connect(  # type: ignore[name-defined]
                    host=host,
                    user=user,
                    password=mysql_pass,
                    database=db,
                    autocommit=autocommit,
                )
                try:
                    self.conn.autocommit = autocommit
                except Exception:
                    pass
                self.cursor = self.conn.cursor(buffered=True)
            # init schema exactly once after a successful connect
            self._init_schema()
        except (MySQLError, sqlite3.Error, RuntimeError) as e:  # type: ignore[name-defined]
            # Log full traceback to your logger, and surface the exact error to stderr
            self.logger.exception("Database connection/bootstrap error")
            err_msg = f"Database connection failed: {type(e).__name__}: {e}"
            print(err_msg, file=sys.stderr)
            sys.exit(err_msg)

    # --- Public helpers -------------------------------------------------

    def execute(self, sql: str, params: Optional[Tuple[Any, ...]] = None):
        
        """Execute a single statement, with optional params. Returns cursor."""
        sql = self._translate_sql(sql)
        try:
            if params is None:
                self.cursor.execute(sql)
            else:
                self.cursor.execute(sql, params)
            return self.cursor
        except (MySQLError, sqlite3.Error) as e:  # type: ignore[name-defined]
            self.logger.exception("DB execute failed: %s", e)
            raise



    def executemany(self, sql: str, seq_of_params: Iterable[Tuple[Any, ...]]):
        """Execute a statement against multiple parameter sets."""
        sql = self._translate_sql(sql)
        return self.cursor.executemany(sql, list(seq_of_params))

    def fetchall(self, sql: Optional[str] = None, params: Optional[Tuple[Any, ...]] = None):
        if sql is not None:
            self.execute(sql, params)
        return self.cursor.fetchall()

    def fetchone(self, sql: Optional[str] = None, params: Optional[Tuple[Any, ...]] = None):
        if sql is not None:
            self.execute(sql, params)
        return self.cursor.fetchone()

    def commit(self):
        with contextlib.suppress(Exception):
            self.conn.commit()

    def close(self):
        with contextlib.suppress(Exception):
            self.cursor.close()
        with contextlib.suppress(Exception):
            self.conn.close()

    # --- Internal helpers ----------------------------------------------

    def _translate_sql(self, sql: str) -> str:
        if self.backend == "sqlite" and isinstance(sql, str):
            return sql.replace(self._param_mysql, self._param_sqlite)
        return sql

    def _init_schema(self) -> None:
        """Create/upgrade minimal schema. Recreates main table if version changed."""
        try:
            if self.backend == "sqlite":
                id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
                ts_col = "added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
            else:
                id_col = "id INTEGER PRIMARY KEY AUTO_INCREMENT"
                ts_col = "added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"

            # Core tables
            self.execute(
                f"""
                CREATE TABLE IF NOT EXISTS nilsimsa (
                    {id_col},
                    {ts_col},
                    uid INTEGER,
                    folder TEXT,
                    hexdigest TEXT,
                    md5sum TEXT,
                    trimmed_header TEXT,
                    categories TEXT,
                    moved_from TEXT,
                    message_id TEXT
                )
                """
            )
            self.execute("CREATE TABLE IF NOT EXISTS considered (uid INTEGER, considered_when INTEGER)")
            self.execute("CREATE TABLE IF NOT EXISTS version (version TEXT)")

            # Version check
            self.execute("SELECT version FROM version LIMIT 1")
            row = self.cursor.fetchone()
            db_version = row[0] if row else None

            if db_version != self.version:
                self.execute("DROP TABLE IF EXISTS nilsimsa")
                self.execute(
                    f"""
                    CREATE TABLE nilsimsa (
                        {id_col},
                        {ts_col},
                        uid INTEGER,
                        folder TEXT,
                        hexdigest TEXT,
                        md5sum TEXT,
                        trimmed_header TEXT,
                        categories TEXT,
                        moved_from TEXT,
                        message_id TEXT
                    )
                    """
                )
                self.execute("DELETE FROM version")
                self.execute("INSERT INTO version (version) VALUES (%s)", (self.version,))

        except (MySQLError, sqlite3.Error) as e:  # type: ignore[name-defined]
            self.logger.error("Database bootstrap error: %s", e)
            raise
