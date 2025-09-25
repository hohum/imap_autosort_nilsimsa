#!/usr/bin/env python3
"""
db_util-v2.py — migrate or sync between MySQL and SQLite using etc/imap_autosort.conf
------------------------------------------------------------------------------------
Usage examples:
  python db_util-v2.py --source=mysql --destination=sqlite --action=migrate
  python db_util-v2.py -s sqlite -d mysql -a sync
  python db_util-v2.py -a sync            # defaults: active -> other
  python db_util-v2.py                    # defaults: active -> other, action=migrate

Behavior:
  - Reads active backend from [database] db_backend in etc/imap_autosort.conf (override with --config).
  - On start, changes working directory to the script’s directory (so relative paths resolve).
  - Error if DESTINATION equals the active backend (to avoid clobbering live DB), exit(2).
  - If --source/--destination not given, assume active -> other (mysql<->sqlite).
  - If --action=sync but the destination DB is missing/empty, automatically perform migrate.
  - migrate works WITHOUT a PK (plain INSERTs). sync uses UPSERT when PK exists; otherwise inserts.
  - Per-table progress bar updates on integer % change; add --verify to compare row counts per table afterwards.
"""
from __future__ import annotations

import sys, argparse, configparser, sqlite3, contextlib, os
from typing import List, Tuple, Dict, Any

# ----- enter the script's directory so relative paths (like etc/...) work -----
try:
    _script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    if _script_dir:
        os.chdir(_script_dir)
except Exception:
    pass

# Optional MySQL connector (only needed when mysql is used)
try:
    import mysql.connector  # type: ignore
    _mysql_available = True
except Exception:
    mysql = None  # type: ignore
    _mysql_available = False

DEFAULT_CONFIG = "etc/imap_autosort.conf"


# ---------- Connection wrapper ----------

class Conn:
    def __init__(self, backend: str, **kw):
        self.backend = backend  # 'mysql' or 'sqlite'
        self.kw = kw
        self.raw = None

    def connect(self):
        if self.backend == "sqlite":
            dbpath = self.kw.get("db") or ":memory:"
            dbpath = os.path.expanduser(dbpath)
            self.raw = sqlite3.connect(dbpath, check_same_thread=False)
            self.raw.isolation_level = None  # autocommit-ish
            self.raw.execute("PRAGMA foreign_keys = ON")
        elif self.backend == "mysql":
            if not _mysql_available:
                raise RuntimeError("mysql-connector-python not installed. Install it to use MySQL.")
            self.raw = mysql.connector.connect(  # type: ignore[name-defined]
                host=self.kw.get("host", "localhost"),
                user=self.kw.get("user", "root"),
                password=self.kw.get("password", ""),
                database=self.kw.get("database") or self.kw.get("db"),
            )
            ac = self.kw.get("autocommit", True)
            try: self.raw.autocommit = ac
            except Exception: pass
        else:
            raise ValueError(f"Unsupported backend: {self.backend}")
        return self

    def cursor(self):
        return self.raw.cursor(buffered=True) if self.backend == "mysql" else self.raw.cursor()

    def execute(self, sql: str, params: tuple = ()):
        if self.backend == "sqlite":
            sql = sql.replace("%s", "?")
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, rows):
        if self.backend == "sqlite":
            sql = sql.replace("%s", "?")
        cur = self.cursor()
        cur.executemany(sql, rows)
        return cur

    def commit(self):
        try: self.raw.commit()
        except Exception: pass

    def close(self):
        with contextlib.suppress(Exception): self.raw.close()


# ---------- Helpers ----------

def list_tables(conn: Conn) -> List[str]:
    if conn.backend == "mysql":
        cur = conn.execute("SHOW TABLES")
        return [r[0] for r in cur.fetchall()]
    else:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        return [r[0] for r in cur.fetchall()]

def describe_columns(conn: Conn, table: str) -> List[Tuple[str, str, bool]]:
    """[(name, type, is_pk)]"""
    out = []
    if conn.backend == "mysql":
        cur = conn.execute(f"DESCRIBE `{table}`")
        for name, coltype, null, key, default, extra in cur.fetchall():
            out.append((name, coltype, key.upper() == "PRI"))
    else:
        cur = conn.execute(f"PRAGMA table_info('{table}')")
        for cid, name, coltype, notnull, dflt, pk in cur.fetchall():
            out.append((name, coltype or "", bool(pk)))
    return out

def mysql_to_sqlite_type(mysql_type: str) -> str:
    t = (mysql_type or "").lower()
    if "int" in t: return "INTEGER"
    if any(x in t for x in ("decimal", "numeric", "double", "float", "real")): return "REAL"
    if "blob" in t or "binary" in t: return "BLOB"
    if "text" in t or "char" in t or "json" in t: return "TEXT"
    if "date" in t or "time" in t or "year" in t: return "TEXT"
    return "TEXT"

def sqlite_to_mysql_type(sqlite_type: str) -> str:
    t = (sqlite_type or "").lower()
    if "int" in t: return "BIGINT"
    if any(x in t for x in ("real", "floa", "doub")): return "DOUBLE"
    if "blob" in t: return "BLOB"
    if "text" in t or "char" in t or t == "": return "TEXT"
    return "TEXT"

def ensure_table_on_target(src: Conn, dst: Conn, table: str, pk_name: str):
    src_cols = describe_columns(src, table)
    if not src_cols:
        raise RuntimeError(f"Source table {table} has no columns")
    dst_tables = set(list_tables(dst))
    if table in dst_tables:
        return
    cols_frag = []
    for name, coltype, _ in src_cols:
        if dst.backend == "sqlite":
            mapped = mysql_to_sqlite_type(coltype) if src.backend == "mysql" else (coltype or "TEXT")
        else:
            mapped = sqlite_to_mysql_type(coltype) if src.backend == "sqlite" else (coltype or "TEXT")
        cols_frag.append(f"`{name}` {mapped}")
    src_colnames = {name for name, _, _ in src_cols}
    pk_sql = f",\n  PRIMARY KEY(`{pk_name}`)" if pk_name and pk_name in src_colnames else ""
    create_sql = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(cols_frag) + pk_sql + "\n)"
    dst.execute(create_sql)
    dst.commit()

def select_rows(conn: Conn, table: str, cols: List[str], chunk: int, offset: int):
    col_list = ", ".join(f"`{c}`" if conn.backend == "mysql" else f'"{c}"' for c in cols)
    if conn.backend == "mysql":
        cur = conn.execute(f"SELECT {col_list} FROM `{table}` LIMIT {offset},{chunk}")
    else:
        cur = conn.execute(f"SELECT {col_list} FROM {table} LIMIT {chunk} OFFSET {offset}")
    return cur.fetchall()

def count_rows(conn: Conn, table: str) -> int:
    try:
        if conn.backend == "mysql":
            cur = conn.execute(f"SELECT COUNT(*) FROM `{table}`")
        else:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        r = cur.fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0

def print_progress(prefix: str, done: int, total: int):
    """
    Render a simple, dependency-free progress bar on one line.
    Example: [migrate] nilsimsa: |██████████----------|  50.0%  600/1200
    """
    if total <= 0:
        bar = "-" * 30
        msg = f"\r{prefix}: |{bar}|  --.-%  {done}"
    else:
        pct = (done / total) if total else 0.0
        width = 30
        filled = int(width * pct)
        bar = "█" * filled + "-" * (width - filled)
        msg = f"\r{prefix}: |{bar}| {pct*100:6.1f}%  {done}/{total}"
    sys.stdout.write(msg)
    sys.stdout.flush()

def upsert_rows(dst: Conn, table: str, cols: List[str], pk_name: str, rows: List[tuple]):
    if not rows: return
    placeholders = ", ".join(["%s"] * len(cols))
    if dst.backend == "mysql":
        # Keep existing rows; do not clobber. Skip on UNIQUE/PK conflict.
        col_list = ", ".join(f"`{c}`" for c in cols)
        sql = f"INSERT IGNORE INTO `{table}` ({col_list}) VALUES ({placeholders})"
    else:
        col_list = ", ".join(f'"{c}"' for c in cols)
        updates = ", ".join([f'"{c}"=excluded."{c}"' for c in cols if c != pk_name])
        sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT("{pk_name}") DO UPDATE SET {updates}'
    dst.executemany(sql, rows)
    dst.commit()


def insert_rows(dst: Conn, table: str, cols: List[str], rows: List[tuple]):
    """Plain INSERTs (no PK needed)."""
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(cols))
    if dst.backend == "mysql":
        col_list = ", ".join(f"`{c}`" for c in cols)
        sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"
    else:
        col_list = ", ".join(f'"{c}"' for c in cols)
        sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'
    dst.executemany(sql, rows)
    dst.commit()

def clone_table(src: Conn, dst: Conn, table: str, pk_name: str, chunk_size: int = 5000, show_progress: bool = True):
    cols = [c for c, _, _ in describe_columns(src, table)]
    if not cols: raise RuntimeError(f"Table {table} has no columns")
    ensure_table_on_target(src, dst, table, pk_name)
    dst.execute(f'DELETE FROM "{table}"' if dst.backend == "sqlite" else f"DELETE FROM `{table}`")
    dst.commit()
    total = count_rows(src, table) if show_progress else 0
    offset = 0
    copied = 0
    prefix = f"[migrate] {table}"
    last_shown = -1  # last integer % emitted
    if show_progress:
        print_progress(prefix, copied, total)  # show 0%
    while True:
        rows = select_rows(src, table, cols, chunk_size, offset)
        if not rows:
            break
        insert_rows(dst, table, cols, rows)
        offset += len(rows)
        copied += len(rows)
        if show_progress:
            if total > 0:
                pct = int((copied * 100) / total)
                if pct != last_shown:
                    last_shown = pct
                    print_progress(prefix, copied, total)
            else:
                print_progress(prefix, copied, total)
    if show_progress:
        sys.stdout.write("\n"); sys.stdout.flush()

def sync_table(src: Conn, dst: Conn, table: str, pk_name: str, chunk_size: int = 5000, show_progress: bool = True):
    cols = [c for c, _, _ in describe_columns(src, table)]
    ensure_table_on_target(src, dst, table, pk_name)
    has_pk = pk_name in cols if pk_name else False
    total = count_rows(src, table) if show_progress else 0
    offset = 0
    copied = 0
    prefix = f"[sync] {table}"
    last_shown = -1
    if show_progress:
        print_progress(prefix, copied, total)
    while True:
        rows = select_rows(src, table, cols, chunk_size, offset)
        if not rows:
            break
        if (dst.backend == "mysql") or has_pk:
            upsert_rows(dst, table, cols, pk_name, rows)
        else:
            print(f"[sync:{table}] no PK '{pk_name}' present — inserting without dedupe")
            insert_rows(dst, table, cols, rows)
        offset += len(rows)
        copied += len(rows)
        if show_progress:
            if total > 0:
                pct = int((copied * 100) / total)
                if pct != last_shown:
                    last_shown = pct
                    print_progress(prefix, copied, total)
            else:
                print_progress(prefix, copied, total)
    if show_progress:
        sys.stdout.write("\n"); sys.stdout.flush()


# ---------- Verification (optional) ----------
def row_counts(conn: Conn, tables: list[str]) -> dict[str, int]:
    out = {}
    for t in tables:
        try:
            out[t] = count_rows(conn, t)
        except Exception:
            out[t] = -1
    return out

def verify_copy(src_params: Dict[str, Any], dst_params: Dict[str, Any], tables: list[str]) -> None:
    s, d = Conn(**src_params).connect(), Conn(**dst_params).connect()
    try:
        sc = row_counts(s, tables)
        dc = row_counts(d, tables)
        print("\nVerification (row counts):")
        ok_all = True
        for t in tables:
            s_n, d_n = sc.get(t, -1), dc.get(t, -1)
            status = "OK" if s_n == d_n and s_n >= 0 else "MISMATCH"
            if status != "OK": ok_all = False
            print(f"  {t:20s}  src={s_n:8d}  dst={d_n:8d}  [{status}]")
        if ok_all:
            print("Verification: ALL TABLES MATCH.")
        else:
            print("Verification: differences detected (see above).")
    finally:
        s.close(); d.close()


# ---------- Config & CLI ----------

def read_main_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    with open(path, "r", encoding="utf-8") as f:
        cfg.read_file(f)
    return cfg

def load_connection_params(cfg: configparser.ConfigParser, backend: str) -> Dict[str, Any]:
    if backend == "mysql":
        return {
            "backend": "mysql",
            "host": cfg.get("mysql", "host", fallback="localhost"),
            "user": cfg.get("mysql", "user", fallback="imap_nilsimsa"),
            "password": cfg.get("mysql", "password", fallback=""),
            "database": cfg.get("mysql", "db", fallback="imap_nilsimsa"),
            "autocommit": cfg.getboolean("database", "autocommit", fallback=True),
        }
    elif backend == "sqlite":
        dbpath = cfg.get("sqlite", "db", fallback="var/lib/imap_autosort/imap_autosort.sqlite")
        return {
            "backend": "sqlite",
            "db": dbpath,
            "autocommit": cfg.getboolean("sqlite", "autocommit", fallback=True),
        }
    else:
        raise ValueError(f"Unsupported backend: {backend}")

def other_backend(name: str) -> str:
    return "sqlite" if name == "mysql" else "mysql"

def dest_exists(dst_params: Dict[str, Any]) -> bool:
    if dst_params["backend"] == "sqlite":
        p = dst_params.get("db") or ""
        return os.path.exists(p) and os.path.getsize(p) > 0
    else:
        try:
            conn = Conn(**dst_params).connect()
            tabs = list_tables(conn)
            conn.close()
            return len(tabs) > 0
        except Exception:
            return False

def main():
    ap = argparse.ArgumentParser(description="Migrate or sync between MySQL and SQLite using etc/imap_autosort.conf")
    ap.add_argument("-s", "--source", choices=["mysql", "sqlite"], help="Source DB backend")
    ap.add_argument("-d", "--destination", choices=["mysql", "sqlite"], help="Destination DB backend")
    ap.add_argument("-a", "--action", choices=["migrate", "sync"], default="migrate", help="Action to perform")
    ap.add_argument("--tables", help="Comma-separated table list (default: all from source)")
    ap.add_argument("--pk", help="Primary key overrides, e.g. 'nilsimsa:id,considered:uid,version:rowid'")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help=f"Config path (default: {DEFAULT_CONFIG})")
    ap.add_argument("--no-progress", dest="progress", action="store_false", help="Disable per-table progress")
    ap.add_argument("--verify", action="store_true", help="After completion, compare row counts per table")
    args = ap.parse_args()

    cfg = read_main_config(args.config)
    active = cfg.get("database", "db_backend", fallback="mysql").lower()

    # Defaults: if not provided, assume active -> other
    src = args.source or active
    dst = args.destination or other_backend(src)

    # Error if destination equals the active backend
    if dst == active:
        print(f"ERROR: destination '{dst}' equals active backend '{active}'. Refusing to clobber live DB.", file=sys.stderr)
        sys.exit(2)

    if src == dst:
        print("ERROR: source and destination must be different.", file=sys.stderr)
        sys.exit(2)

    src_params = load_connection_params(cfg, src)
    dst_params = load_connection_params(cfg, dst)

    # Build table list
    if args.tables:
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    else:
        sconn = Conn(**src_params).connect()
        tables = list_tables(sconn)
        sconn.close()

    # PK overrides
    pk_map: Dict[str, str] = {}
    if args.pk:
        for part in args.pk.split(","):
            t, pk = [x.strip() for x in part.split(":", 1)]
            pk_map[t] = pk
    def get_pk(t): return pk_map.get(t, "id")

    # If action is sync but destination doesn't exist, migrate instead
    action = args.action
    if action == "sync" and not dest_exists(dst_params):
        print("Destination appears empty/missing — performing full migrate instead of sync.")
        action = "migrate"

    # Connect
    sconn = Conn(**src_params).connect()
    dconn = Conn(**dst_params).connect()

    try:
        if action == "migrate":
            for tbl in tables:
                print(f"[migrate] {src} -> {dst} table {tbl}")
                clone_table(sconn, dconn, tbl, get_pk(tbl), show_progress=args.progress)
        else:
            for tbl in tables:
                print(f"[sync] {src} -> {dst} table {tbl}")
                sync_table(sconn, dconn, tbl, get_pk(tbl), show_progress=args.progress)
        print("Done.")
        if args.verify:
            verify_copy(src_params, dst_params, tables)
    finally:
        sconn.close()
        dconn.close()


if __name__ == "__main__":
    main()
