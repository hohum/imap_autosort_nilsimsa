from __future__ import annotations

import sys
import contextlib
from typing import Any, Iterable, Optional, Tuple

# Optional MySQL import
try:  # pragma: no cover
    import mysql.connector  # type: ignore
    from mysql.connector import Error as MySQLError  # type: ignore
    _mysql_available = True
except Exception:  # pragma: no cover
    mysql = None  # type: ignore
    MySQLError = Exception  # type: ignore
    _mysql_available = False

import sqlite3


class DatabaseHelper:
    """Tiny DB helper with stable API for MySQL/SQLite.

    Public surface (drop-in compatible):
      - __init__(mysql_pass, version, logger, host, user, db, autocommit, db_backend, allow_destructive=False)
      - execute, executemany, fetchall, fetchone, commit, close
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
        allow_destructive: bool = False,
    ) -> None:
        self.logger = logger
        self.version = version
        self.backend = (db_backend or "mysql").lower()
        self.allow_destructive = allow_destructive

        try:
            if self.backend == "sqlite":
                sqlite_path = db or ":memory:"
                self.conn = sqlite3.connect(sqlite_path, check_same_thread=False)
                with self.conn:
                    self.conn.execute("PRAGMA foreign_keys = ON")
                if autocommit:
                    self.conn.isolation_level = None  # autocommit-on
                self.cursor = self.conn.cursor()
            else:
                if not _mysql_available:
                    raise RuntimeError("mysql-connector not available in this environment")
                user = (user or "imap_nilsimsa").strip()
                mysql_pass = mysql_pass or ""
                self.conn = mysql.connector.connect(  # type: ignore[name-defined]
                    host=host, user=user, password=mysql_pass, database=db, autocommit=autocommit
                )
                with contextlib.suppress(Exception):
                    self.conn.autocommit = autocommit
                self.cursor = self.conn.cursor(buffered=True)

            self._init_schema()
        except (MySQLError, sqlite3.Error, RuntimeError) as e:  # type: ignore[name-defined]
            self.logger.exception("Database connection/bootstrap error")
            err = f"Database connection failed: {type(e).__name__}: {e}"
            print(err, file=sys.stderr)
            sys.exit(err)

    # --- Public API -------------------------------------------------

    def execute(self, sql: str, params: Optional[Tuple[Any, ...]] = None):
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
        """For sqlite backend, translate MySQL-style %s placeholders to '?'.
        Skips %s that appear inside single-quoted string literals (handles doubled quotes '').
        """
        if self.backend != 'sqlite':
            return sql
        out = []
        in_single = False
        i = 0
        L = len(sql)
        while i < L:
            ch = sql[i]
            if ch == "'":
                if in_single:
                    # doubled single-quote -> literal quote, stay in string
                    if i + 1 < L and sql[i+1] == "'":
                        out.append("''")
                        i += 2
                        continue
                    in_single = False
                else:
                    in_single = True
                out.append(ch)
                i += 1
                continue
            if (not in_single) and ch == '%' and i + 1 < L and sql[i+1] == 's':
                out.append('?')
                i += 2
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    def _nilsimsa_schema_sql(self, id_col: str, ts_col: str) -> str:
        return (
            f"""                CREATE TABLE IF NOT EXISTS nilsimsa (
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

    def _ensure_core_tables(self, id_col: str, ts_col: str) -> None:
        self.execute(self._nilsimsa_schema_sql(id_col, ts_col))
        self.execute("CREATE TABLE IF NOT EXISTS considered (uid INTEGER, considered_when INTEGER)")
        self.execute("CREATE TABLE IF NOT EXISTS version (version TEXT)")

    def _init_schema(self) -> None:
        """Create/upgrade minimal schema. On version mismatch:
        - if allow_destructive=True: drop & recreate main table, set version
        - else: ensure tables exist, set version if empty, log mismatch and continue
        """
        if self.backend == "sqlite":
            id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
            ts_col = "added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        else:
            id_col = "id INTEGER PRIMARY KEY AUTO_INCREMENT"
            ts_col = "added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"

        try:
            self._ensure_core_tables(id_col, ts_col)

            row = self.fetchone("SELECT version FROM version LIMIT 1")
            db_version = row[0] if row else None

            if db_version == self.version:
                return

            if db_version is None:
                # first run: set version
                self.execute("INSERT INTO version (version) VALUES (%s)", (self.version,))
                return

            # version mismatch
            if self.allow_destructive:
                self.logger.warning("Schema version change %s -> %s: destructive rebuild enabled", db_version, self.version)
                self.execute("DROP TABLE IF EXISTS nilsimsa")
                self.execute(self._nilsimsa_schema_sql(id_col, ts_col).replace("IF NOT EXISTS ", ""))
                self.execute("DELETE FROM version")
                self.execute("INSERT INTO version (version) VALUES (%s)", (self.version,))
            else:
                self.logger.warning("Schema version mismatch %s != %s (non-destructive mode) — continuing without rebuild", db_version, self.version)
        except (MySQLError, sqlite3.Error) as e:  # type: ignore[name-defined]
            self.logger.error("Database bootstrap error: %s", e)
            raise
