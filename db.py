"""
Refactor of db.py — unified DB helper for MySQL/MariaDB and SQLite
------------------------------------------------------------------
Goals: keep functionality, reduce duplication/lines, clarify with comments,
remove overkill error handling, and add a tiny TDD starter.

Drop-in compatible surface:
- DatabaseHelper.__init__(mysql_pass, version, logger, host, user, db, autocommit, db_backend)
- execute, executemany, fetchall, fetchone, commit, close

Notes:
- Still defaults to MySQL unless db_backend=="sqlite".
- Keeps %s param style at call sites; translated to "?" for SQLite.
"""
from __future__ import annotations

import sys
import contextlib
from typing import Any, Iterable, Optional, Tuple

# Optional MySQL import: library is not required for SQLite runs
try:  # pragma: no cover - exercised only when mysql-connector is present
    import mysql.connector  # type: ignore
    from mysql.connector import Error as MySQLError  # type: ignore
    _mysql_available = True
except Exception:  # pragma: no cover - our SQLite tests won't ship mysql
    mysql = None  # type: ignore
    MySQLError = Exception  # type: ignore
    _mysql_available = False

import sqlite3


class DatabaseHelper:
    """Tiny DB helper with stable API for MySQL/SQLite.

    - Keeps "%s" param style for callers; translated when using SQLite.
    - Initializes a minimal schema and bumps it when version changes.
    """

    _PARAM_MYSQL = "%s"
    _PARAM_SQLITE = "?"

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

        try:
            if self.backend == "sqlite":
                # SQLite file path (or ":memory:") lives in "db"
                sqlite_path = db or ":memory:"
                self.conn = sqlite3.connect(sqlite_path, check_same_thread=False)
                with self.conn:  # sane defaults
                    self.conn.execute("PRAGMA foreign_keys = ON")
                if autocommit:
                    self.conn.isolation_level = None  # autocommit-on
                self.cursor = self.conn.cursor()
            else:
                if not _mysql_available:
                    raise RuntimeError("mysql-connector not available in this environment")
                # Guard against empty user/password (connector may default to OS login otherwise)
                user = (user or "imap_nilsimsa").strip()
                mysql_pass = mysql_pass or ""
                self.conn = mysql.connector.connect(  # type: ignore[name-defined]
                    host=host, user=user, password=mysql_pass, database=db, autocommit=autocommit
                )
                # Older connectors may ignore autocommit arg; try anyway.
                with contextlib.suppress(Exception):
                    self.conn.autocommit = autocommit
                self.cursor = self.conn.cursor(buffered=True)

            # Initialize/upgrade schema after a successful connection
            self._init_schema()
        except (MySQLError, sqlite3.Error, RuntimeError) as e:  # type: ignore[name-defined]
            # Log traceback and exit with a clear one-line error
            self.logger.exception("Database connection/bootstrap error")
            err = f"Database connection failed: {type(e).__name__}: {e}"
            print(err, file=sys.stderr)
            sys.exit(err)

    # --- Public API -------------------------------------------------

    def execute(self, sql: str, params: Optional[Tuple[Any, ...]] = None):
        """Execute one statement and return the cursor."""
        sql = self._translate_sql(sql)
        try:
            self.cursor.execute(sql) if params is None else self.cursor.execute(sql, params)
            return self.cursor
        except (MySQLError, sqlite3.Error) as e:  # type: ignore[name-defined]
            self.logger.exception("DB execute failed: %s", e)
            raise

    def executemany(self, sql: str, seq_of_params: Iterable[Tuple[Any, ...]]):
        """Execute a statement for many parameter sets."""
        return self.cursor.executemany(self._translate_sql(sql), list(seq_of_params))

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

    # --- Internals --------------------------------------------------

    def _translate_sql(self, sql: str) -> str:
        """Translate %s params to ? when talking to SQLite."""
        if self.backend == "sqlite" and isinstance(sql, str):
            return sql.replace(self._PARAM_MYSQL, self._PARAM_SQLITE)
        return sql

    def _nilsimsa_schema_sql(self, id_col: str, ts_col: str) -> str:
        """Single source of truth for the main table definition."""
        return (
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

    def _init_schema(self) -> None:
        """Create/upgrade minimal schema. Recreates main table if version changed."""
        if self.backend == "sqlite":
            id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
            ts_col = "added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        else:
            id_col = "id INTEGER PRIMARY KEY AUTO_INCREMENT"
            ts_col = "added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"

        try:
            # Core tables (idempotent)
            self.execute(self._nilsimsa_schema_sql(id_col, ts_col))
            self.execute("CREATE TABLE IF NOT EXISTS considered (uid INTEGER, considered_when INTEGER)")
            self.execute("CREATE TABLE IF NOT EXISTS version (version TEXT)")

            # Version check
            row = self.fetchone("SELECT version FROM version LIMIT 1")
            db_version = row[0] if row else None

            if db_version != self.version:
                # Recreate main table on version bump and persist version
                self.execute("DROP TABLE IF EXISTS nilsimsa")
                self.execute(self._nilsimsa_schema_sql(id_col, ts_col).replace("IF NOT EXISTS ", ""))
                self.execute("DELETE FROM version")
                self.execute("INSERT INTO version (version) VALUES (%s)", (self.version,))
        except (MySQLError, sqlite3.Error) as e:  # type: ignore[name-defined]
            self.logger.error("Database bootstrap error: %s", e)
            raise



