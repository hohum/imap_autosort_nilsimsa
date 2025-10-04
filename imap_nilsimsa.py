#!/usr/bin/env python
"""
IMAP AutoSorter (single instance via flock)
===========================================

Purpose:
  Sort mail into folders using Nilsimsa similarity over normalized headers.
  This module owns orchestration: IMAP access, scoring, movement, and logging.

Contributor notes & LLM guardrails (read BEFORE changes)
-------------------------------------------------------
1) DB/schema coordination
   - Changes to database schemas REQUIRE: (a) updating `db.py`, and (b) a
     migration or explicit compatibility check before any destructive action.
   - Never drop or recreate tables on a version mismatch without:
       * checking the current on-disk/in-DB version,
       * writing a migration path or creating a backup first,
       * logging the decision and outcome clearly.
   - Keep MySQL/SQLite parity: types, keys, and uniqueness constraints must
     remain semantically equivalent (see db.py notes).

2) Semantic Versioning policy (avoid version sprawl)
   - Use MAJOR.MINOR.PATCH strings consistently across configs, logs, and the
     `version` table in the DB.
   - PATCH: bugfixes only; no DDL; no behavior changes that break automation.
   - MINOR: backward-compatible additions/tunables; if DB columns/indexes are
     added, they must be nullable or have safe defaults; include an automatic,
     online migration.
   - MAJOR: backward-incompatible changes (key/DDL/meaning changes). Avoid if
     at all possible. If unavoidable:
       • provide a one-shot migration tool with dry-run + backup,
       • document rollback steps,
       • gate rollout behind an explicit flag/environment switch.
   - Do not reserve or skip versions; keep a short, linear history with clear
     notes for each bump.

3) Public API stability
   - Do not break CLI args, function signatures, or return shapes without a
     deprecation period and clear logs. Add, don’t replace, where possible.

   **CLI & UX invariants (must NOT change silently):**
   - `--config` stays optional; default resolution is implemented by the caller.
   - Default flag meanings (`--dry-run`, `--debug`, `--quiet`) remain unchanged.

4) Logging & observability
   - Prefer structured, greppable one-liners; include message-id where useful.
   - Avoid noisy begin/end banners; log class->method context via child loggers.

5) Error handling
   - Fail fast on unrecoverable bootstrap errors; otherwise prefer raising to
     let the caller decide (systemd, cron, or daemon mode can restart/alert).

6) LLM calls (classification)
   - Respect `sender_skip_llm` globs. All prompts must be idempotent, bounded
     in size, and safe to execute repeatedly. Log only short summaries; never
     log full email content.

7) Performance & concurrency
   - Keep IMAP round-trips minimal. Any change that adds per-message fetches
     must be justified. Single-process lock is acquired before side effects.

8) Refactor
   - Strictly keep functionality identical,
   - reduce duplication/lines.
   - maintrin code clarify modifying/adding/deleting comments to aid in maintainability.
   - remove overkill error handling.

This block is intentional prose for both humans and LLMs. Do not remove.
"""

# ------------------------------ stdlib & deps ------------------------------
import argparse
import configparser
import email
import errno
import fcntl
import hashlib
import imaplib
import logging
import logging.handlers
import os
import sys
import random
import re
import time
from typing import Dict, List, Tuple

from nilsimsa import Nilsimsa, compare_hexdigests
from llm import LLMClassifier
from db import DatabaseHelper
from header_normalizer import normalize_header
from sorter_engine import decide_winner
from logging_setup import setup_logger
from imap_helper import IMAPHelper
from imap_idle import supports_idle as imap_supports_idle, idle_wait as imap_idle_wait
from imap_utils import parse_uid_set, extract_copyuid

try:  # used only when db_backend=mysql; keep import optional
    import mysql.connector  # noqa: F401
except Exception:  # pragma: no cover
    mysql = None  # noqa: F401

# ------------------------------ helpers ------------------------------

class IMAPAutoSorter:
    """Sort emails into folders by Nilsimsa similarity of headers.

    Concurrency: we acquire an exclusive flock *in the constructor* so only one
    instance runs at a time. If the lock is already held, this process exits.
    """

    def __init__(self, config_path: str):
        # Acquire the flock immediately (before any other side effects)
        self.lockfile_path = "/tmp/imap_autosync_lock_in_class"
        self.lock_fd = None
        self._ensure_single_instance()

        # Load config
        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        # General
        self.version = self.config.get("general", "version", fallback="1.2.0b")
        self.maintenance = self.config.getboolean("general", "maintenance", fallback=False)
        self.reconsider_after = self.config.getint("general", "reconsider_after", fallback=3600)
        # Compat: accept log_dir/logdir/logpath; strip trailing slashes
        log_dir = (
            self.config.get('general', 'log_dir', fallback=None)
            or self.config.get('general', 'logdir', fallback=None)
            or self.config.get('general', 'logpath', fallback='logs')
        ).strip().rstrip('/')
        self.logfile = self.config.get('general', 'logfile', fallback=None)

        # IMAP folders & lists
        self.todo_folder = self.config.get("imap", "todo")
        self.new_folder = self.config.get("imap", "new")
        self.imap_folders = self._get_list("imap", "folders")
        # For LLMClassifier
        self.sender_skip_llm = self._get_list("openai", "sender_skip_llm")
        # LLM classifier (shared in llm.py)
        api_key = self.config.get("openai", "api_key", fallback=None)
        self.llm = LLMClassifier(api_key, self.sender_skip_llm, logger=None)

        # Nilsimsa thresholds & knobs
        self.threshold = self.config.getint("nilsimsa", "threshold", fallback=50)
        self.min_score = self.config.getint("nilsimsa", "min_score", fallback=100)
        self.min_average = self.config.getfloat("nilsimsa", "min_average", fallback=0)
        self.min_over = self.config.getfloat("nilsimsa", "min_over", fallback=1)
        self.weight_headers = self._get_list("nilsimsa", "weight_headers")
        self.headers_skip = self._get_list("nilsimsa", "headers_skip")
        self.weight_headers_by = self.config.getint("nilsimsa", "weight_headers_by", fallback=1)
        self.xinclude = self._get_list("nilsimsa", "xinclude")
        self.sender_skip_llm = self._get_list("openai", "sender_skip_llm")

        # Archive
        self.archive_folder = self.config.get("archive", "folder", fallback=None)
        self.archive_after = self.config.getint("archive", "after", fallback=0)
        self.just_delete = self._get_list("archive", "justdelete") if self.config.has_option("archive", "justdelete") else None
        self.trash_folder = self.config.get("archive", "trash", fallback=None)

        # Regexes (kept same semantics; precompiled for clarity/speed)
        self.exclude_headers = re.compile(r"^(Date|Message-ID|X-.*Mailscanner.*|X-Amavis-.*|X-Spam-.*|X-Virus-.*|ARC-.*)$", re.I)
        self.no_dates_received = re.compile(r";\s+.*$", re.M | re.I)
        # Extract just the d= token from DKIM-Signature values
        self.dkim_just_d = re.compile(r"(?is)\A.*?\b(d=[^;\s]+).*\Z")
        self.chomp_header = re.compile(r"[\r\n]+\s*", re.M)
        self.exclude_received_from_localhost = re.compile(r"^from\s+(localhost|marcsnet\.com)\s+", re.I)
        weight_headers_pattern = r"^(" + "|".join(self.weight_headers) + r")$" if self.weight_headers else r"^$"
        self.weight_headers_re = re.compile(weight_headers_pattern, re.I)
        headers_skip_pattern = r"^(" + "|".join(self.headers_skip) + r")$" if self.headers_skip else r"^$"
        self.headers_skip_re = re.compile(headers_skip_pattern, re.I)
        self.headerIsX = re.compile(r"^x-", re.I)

        # Logger (base + child)
        base_logger = setup_logger(
            "imap_nilsimsa",
            log_dir=log_dir,
            logfile=self.logfile,
            enable_syslog=self.config.getboolean('general', 'enable_syslog', fallback=False),
            syslog_address=self.config.get('general', 'syslog_address', fallback='/dev/log'),
        )
        self.logger = base_logger.getChild(self.__class__.__name__)

        # Database config & helper
        db_backend = self.config.get("database", "db_backend", fallback="mysql").lower()
        if db_backend == "sqlite":
            db_name = self.config.get("sqlite", "db", fallback="var/lib/imap_autosort/imap_autosort.sqlite")
            db_host = "localhost"
            db_user = ""
            autocommit = self.config.getboolean("sqlite", "autocommit", fallback=True)
            mysql_pass = ""
        else:
            db_name = self.config.get("mysql", "db", fallback="imap_nilsimsa")
            db_host = self.config.get("mysql", "host", fallback="localhost")
            db_user = (self.config.get("mysql", "user", fallback="imap_nilsimsa") or "imap_nilsimsa").strip()
            mysql_pass = (self.config.get("mysql", "password", fallback="") or "").strip()
            autocommit = self.config.getboolean("database", "autocommit", fallback=True)

        self.db = DatabaseHelper(
            mysql_pass=mysql_pass,
            version=self.version,
            logger=base_logger.getChild("DatabaseHelper"),
            host=db_host,
            user=db_user,
            db=db_name,               # file path when sqlite; schema name when mysql
            autocommit=autocommit,
            db_backend=db_backend,
        )
        self.imap_helper = IMAPHelper(self.config)

    # ------------------------------ misc utils ------------------------------

    def _parse_uid_set(self, s: str) -> List[int]:
        out: List[int] = []
        s = (s or '').strip()
        if not s:
            return out
        for part in s.replace(',', ' ').split():
            if ':' in part:
                a, b = map(int, part.split(':', 1))
                out.extend(range(min(a, b), max(a, b) + 1))
            else:
                out.append(int(part))
        return out

    def _extract_copyuid(self, result):
        typ, data = result or (None, None)
        pieces: list[str] = []
        for d in (data or []):
            if isinstance(d, (bytes, bytearray)):
                pieces.append(d.decode('utf-8', 'ignore'))
            elif isinstance(d, tuple) and len(d) > 1 and isinstance(d[1], (bytes, bytearray)):
                pieces.append(d[1].decode('utf-8', 'ignore'))
            elif isinstance(d, str):
                pieces.append(d)
        joined = ' '.join(pieces)
        m = re.search(r'\[(COPYUID|APPENDUID)\s+(\d+)\s+([^\s]+)\s+([^\]]+)\]', joined)
        if not m:
            return None
        uidvalidity = int(m.group(2))
        src = self._parse_uid_set(m.group(3))
        dst = self._parse_uid_set(m.group(4))
        return uidvalidity, src, dst

    def _get_list(self, section: str, key: str) -> List[str]:
        """Parse comma-separated config option into a trimmed list."""
        if not self.config.has_option(section, key):
            return []
        return [x.strip() for x in self.config.get(section, key).split(',') if x.strip()]

    def _ensure_single_instance(self) -> None:
        """Exclusive flock; exit if already locked (unchanged UX)."""
        try:
            self.lock_fd = open(self.lockfile_path, "w")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                print(f"Another instance is already running. Exiting. (Lock: {self.lockfile_path})")
                sys.exit(0)
            raise

    # ------------------------------ status ------------------------------

    @staticmethod
    def status(current: int, total: int, message: str = '') -> None:
        """One-line progress bar (identical effect to original)."""
        if total <= 1:
            return
        percent = int(100 * current / (total - 1) + 0.5)
        num_equals = int(percent / 2)
        sys.stdout.write("%s [%-50s] %3d%% %d/%d\r" % (message, '=' * num_equals, percent, current + 1, total))
        if current == (total - 1):
            print("")
        sys.stdout.flush()

    # ------------------------------ core: sync & distance ------------------------------

    def sync_and_distance(
        self,
        imap: imaplib.IMAP4_SSL,
        folder: str,
        source_hexdigest: str,
        dry_run: bool = False,
        debug: bool = False,
        quiet: bool = False,
    ) -> List[int]:
        """Sync DB with IMAP for *folder* and compute distances to source_hexdigest.

        Preserves behavior:
          • Only (SEEN) messages are considered
          • Duplicate header detection via md5sum across folders
          • DB rows with missing UIDs on IMAP are pruned
        """
        if not quiet:
            print("Analyzing folder %s" % folder)
        distances: List[int] = []

        # Load cached rows for this folder
        mail_db: Dict[str, str] = {}
        self.db.execute("SELECT uid, hexdigest FROM nilsimsa WHERE folder = %s", (folder,))
        for uid, hx in self.db.fetchall():
            mail_db[str(uid)] = str(hx)

        # Live IMAP UIDs (read-write select so expunged are gone)
        imap.select('"%s"' % folder, readonly=False)
        result, data = imap.uid('search', None, "(SEEN)")
        email_uids = data[0].decode().split() if data and data[0] else []
        message_count = len(email_uids)

        for i, email_uid in enumerate(email_uids):
            if not quiet:
                self.status(i, message_count, 'Comparing ')
            if debug:
                print("Folder: %s, email_uid: %s" % (folder, email_uid))

            if email_uid not in mail_db:
                # Not in DB → normalize header and derive md5 over trimmed header
                res_fetch, data_fetch = imap.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
                raw_header = data_fetch[0][1].decode('utf-8', 'backslashreplace') if data_fetch and data_fetch[0] else ''
                trimmed_header = normalize_header(
                    mail_txt=raw_header,
                    exclude_headers=self.exclude_headers,
                    headers_skip_re=self.headers_skip_re,
                    chomp_header=self.chomp_header,
                    headerIsX=self.headerIsX,
                    xinclude=self.xinclude,
                    dkim_just_d=self.dkim_just_d,
                    exclude_received_from_localhost=self.exclude_received_from_localhost,
                    weight_headers_re=self.weight_headers_re,
                    weight_headers_by=self.weight_headers_by,
                )
                md5sum = hashlib.md5(trimmed_header.encode('utf-8')).hexdigest()
                # Look up any rows with this md5 (same normalized header)
                self.db.execute("SELECT id, uid, folder, categories, hexdigest FROM nilsimsa WHERE md5sum = %s", (md5sum,))
                md5_rows = self.db.fetchall()
                if not md5_rows:
                    msg = email.message_from_string(raw_header)
                    cats = '[{"cta":"Notice LLM classification never done"},{"label":["Unclassified:1.00"]}]'
                    try:
                        target_hexdigest = Nilsimsa(f"X-LLM-Categories: {cats}\n{trimmed_header}").hexdigest()
                    except Exception as e:
                        self.logger.error("Failed to compute Nilsimsa hash: %s", e)
                        self.logger.error(trimmed_header)
                        imap.uid('MOVE', email_uid, 'INBOX.autosort.problem')
                        continue
                    if not dry_run:
                        self.db.execute(
                            "INSERT INTO nilsimsa (uid, folder, hexdigest, md5sum, trimmed_header, categories) VALUES (%s, %s, %s, %s, %s, %s)",
                            (email_uid, folder, target_hexdigest, md5sum, trimmed_header, cats),
                        )
                else:
                    if len(md5_rows) == 1:
                        prev_id, prev_uid, prev_folder, prev_cats, prev_hex = md5_rows[0]
                        if not dry_run:
                            try:
                                self.db.execute(
                                    "UPDATE nilsimsa SET uid=%s, folder=%s, moved_from=%s WHERE id=%s",
                                    (email_uid, folder, prev_folder or '', prev_id),
                                )
                            except Exception as e:
                                self.logger.error("Move-update failed: %s", e)
                        cats = prev_cats or ''
                        if not cats:
                            msg = email.message_from_string(raw_header)
                            cats = '[{"cta":"Notice LLM classification never done"},{"label":["Unclassified:1.00"]}]'
                            try:
                                target_hexdigest = Nilsimsa(f"X-LLM-Categories: {cats}\n{trimmed_header}").hexdigest()
                            except Exception as e:
                                self.logger.error("Failed to compute Nilsimsa hash: %s", e)
                                self.logger.error(trimmed_header)
                                continue
                            if not dry_run:
                                try:
                                    self.db.execute(
                                        "UPDATE nilsimsa SET categories=%s, hexdigest=%s WHERE id=%s",
                                        (cats, target_hexdigest, prev_id),
                                    )
                                except Exception as e:
                                    self.logger.error("Post-move categories update failed: %s", e)
                        else:
                            try:
                                target_hexdigest = Nilsimsa(f"X-LLM-Categories: {cats}\n{trimmed_header}").hexdigest()
                            except Exception as e:
                                self.logger.error("Failed to compute Nilsimsa hash: %s", e)
                                self.logger.error(trimmed_header)
                                continue
                    else:
                        chosen = next((c for (_id, _uid, _folder, c, _hex) in md5_rows if c and ('Unclassified' not in c)), None)
                        if not chosen:
                            msg = email.message_from_string(raw_header)
                            chosen, _ = self.llm._classify_email(
                                f"From: {msg.get('From','')}\nSubject: {msg.get('Subject','')}"
                            )
                        cats = chosen
                        try:
                            target_hexdigest = Nilsimsa(f"X-LLM-Categories: {cats}\n{trimmed_header}").hexdigest()
                        except Exception as e:
                            self.logger.error("Failed to compute Nilsimsa hash: %s", e)
                            self.logger.error(trimmed_header)
                            continue
                        if not dry_run:
                            self.db.execute(
                                "INSERT INTO nilsimsa (uid, folder, hexdigest, md5sum, trimmed_header, categories) VALUES (%s, %s, %s, %s, %s, %s)",
                                (email_uid, folder, target_hexdigest, md5sum, trimmed_header, cats),
                            )
            else:
                if debug:
                    print("Email UID %s found in DB" % email_uid)
                target_hexdigest = mail_db[email_uid]
                del mail_db[email_uid]

            try:
                distance = compare_hexdigests(source_hexdigest, target_hexdigest)
            except Exception as e:
                self.logger.error("Failed to compute distance: %s", e)
                continue

            if debug:
                print("Distance between source and %s: %s" % (target_hexdigest, distance))
            distances.append(distance)

        # Prune DB rows for UIDs no longer in the IMAP folder
        if mail_db:
            self.logger.info(f"{len(mail_db)} records for cleanup in DB folder[{folder}]")
        for email_uid in list(mail_db.keys()):
            if not quiet:
                self.status(0, len(mail_db), 'Deleting moved messages ')
            if not dry_run:
                self.db.execute("DELETE FROM nilsimsa WHERE uid = %s AND folder = %s", (email_uid, folder))
            else:
                print("Dry run: would have deleted DB entry for UID: %s, folder: %s" % (email_uid, folder))

        return distances

    # ------------------------------ todo / autosort ------------------------------

    def todo_count(self, imap: imaplib.IMAP4_SSL) -> int:
        imap.select(self.todo_folder, readonly=False)
        resp, data = imap.search(None, 'UNSEEN')
        return len(data[0].split()) if data and data[0] else 0

    def autosort_inbox(
        self,
        imap: imaplib.IMAP4_SSL,
        dry_run: bool = False,
        debug: bool = False,
        quiet: bool = False,
    ) -> None:
        while self.todo_count(imap):
            imap.select(self.todo_folder, readonly=False)
            result, data = imap.uid('search', None, "(UNSEEN)")
            if not (data and data[0]):
                break
            email_uids = [str(x) for x in data[0].decode().split()]

            for email_uid in email_uids:
                print("----- Considering message: %s" % email_uid)
                imap.select(self.todo_folder, readonly=False)
                res_fetch, data_fetch = imap.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
                try:
                    raw_header = data_fetch[0][1].decode('utf-8', 'backslashreplace')
                except Exception:
                    sys.exit("Error: email_uid: %s has no data" % email_uid)

                msg = email.message_from_string(raw_header)
                print("---------- Source: subject: %s" % msg['Subject'])
                message_id = (msg.get('Message-ID', '') or '').strip()
                trimmed_header = normalize_header(
                    mail_txt=raw_header,
                    exclude_headers=self.exclude_headers,
                    headers_skip_re=self.headers_skip_re,
                    chomp_header=self.chomp_header,
                    headerIsX=self.headerIsX,
                    xinclude=self.xinclude,
                    dkim_just_d=self.dkim_just_d,
                    exclude_received_from_localhost=self.exclude_received_from_localhost,
                    weight_headers_re=self.weight_headers_re,
                    weight_headers_by=self.weight_headers_by,
                )

                self.logger.info("* New message from: %s, Message-ID: %s", msg['From'], message_id)
                self.logger.info(trimmed_header)

                cats, is_suss = self.llm._classify_email(
                    f"From: {msg.get('From','')}\nSubject: {msg.get('Subject','')}"
                )
                try:
                    m = re.findall(r'"(?:Spam|Phishing Suspected):(\d+\.\d{2})"', cats)
                    if m and max(map(float, m)) >= 0.10:
                        imap.uid('STORE', email_uid, '+FLAGS', '($label1)')
                except Exception:
                    pass

                try:
                    source_hexdigest = Nilsimsa(f"X-LLM-Categories: {cats}\n{trimmed_header}").hexdigest()
                except Exception as e:
                    self.logger.error("Cannot compute Nilsimsa hash: %s", e)
                    imap.uid('COPY', email_uid, 'INBOX.autosort.problem')
                    imap.uid('STORE', email_uid, '+FLAGS', '(\\Deleted)')
                    imap.expunge()
                    continue

                # Cache distances once and use engine to decide winner
                dist_cache = {
                    f: self.sync_and_distance(imap, f, source_hexdigest, dry_run, debug, quiet)
                    for f in self.imap_folders
                }
                tie_ratio_gap = getattr(self, "tie_ratio_gap", 0.10)
                winning_folder, winning_score = decide_winner(
                    dist_cache,
                    base_threshold=self.threshold,
                    min_score=self.min_score,
                    min_average=self.min_average,
                    tie_ratio_gap=tie_ratio_gap,
                    logger=self.logger,
                    debug=debug,
                    quiet=quiet,
                )
                if not winning_folder:
                    winning_folder, winning_score = self.new_folder, 0.0

                if not dry_run:
                    print("* Moving message to %s" % winning_folder)
                    imap.select(self.todo_folder, readonly=False)
                    typ, data = imap.uid('MOVE', email_uid, '"%s"' % winning_folder)
                    if typ == 'OK':
                        dst_uid = None
                        info = extract_copyuid((typ, data)) or extract_copyuid(('OK', getattr(imap, 'untagged_responses', {}).get('OK', [])))
                        if info:
                            _uidv, src_uids, dst_uids = info
                            try:
                                dst_uid = dst_uids[src_uids.index(int(email_uid))]
                            except Exception:
                                dst_uid = None

                        md5sum = hashlib.md5(trimmed_header.encode('utf-8')).hexdigest()
                        self.db.execute(
                            "INSERT INTO nilsimsa (uid, folder, hexdigest, md5sum, trimmed_header, categories, moved_from, message_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (dst_uid, winning_folder, source_hexdigest, md5sum, trimmed_header, cats, self.todo_folder, message_id),
                        )
                        self.logger.info("Moved email %s to %s (dst UID: %s)", email_uid, winning_folder, dst_uid)
                    else:
                        self.logger.error("MOVE failed for %s -> %s", email_uid, winning_folder)
                else:
                    print("Dry run: would have moved %s to folder %s" % (email_uid, winning_folder))

    # ------------------------------ archive ------------------------------

    def archive_emails(self, imap: imaplib.IMAP4_SSL, dry_run: bool = False) -> None:
        if not self.archive_folder or self.archive_after <= 0:
            return

        seconds_threshold = self.archive_after * 24 * 60 * 60
        for folder in self.imap_folders:
            print("Checking %s for messages to archive older than %d seconds" % (folder, seconds_threshold))
            try:
                imap.select('"%s"' % folder, readonly=False)
                result, data = imap.uid('search', None, "(SEEN OLDER %d)" % seconds_threshold)
            except Exception as e:
                self.logger.error("Error selecting folder %s: %s", folder, e)
                continue

            email_uids = (data[0].decode().split() if (data and data[0]) else [])
            if email_uids:
                n = len(email_uids)
                print(f"Found {n} emails to consider for archiving in folder {folder}")
                self.logger.info("Found %d emails in %s for archive", n, folder)

            for email_uid in email_uids:
                target_folder = self.trash_folder if (self.just_delete and folder in self.just_delete) else self.archive_folder
                if not dry_run:
                    result_copy = imap.uid('COPY', email_uid, '"%s"' % target_folder)
                    if result_copy[0] == 'OK':
                        imap.uid('STORE', email_uid, '+FLAGS', '(\\Deleted)')
                        imap.expunge()
                else:
                    print("Dry run: message %s from folder %s would be archived to %s" % (email_uid, folder, target_folder))

    # ------------------------------ housekeeping ------------------------------

    def prune_considered(self) -> None:
        now = int(time.time())
        delete_older_than = now - self.reconsider_after - random.randint(0, self.reconsider_after)
        self.db.execute("DELETE FROM considered WHERE considered_when < %s", (delete_older_than,))

    def _imap_connect(self):
        return self.imap_helper.connect()

    def _process_core(self, imap, dry_run=False, debug=False, quiet=False):
        self.prune_considered()
        print("Archiving messages")
        try:
            self.archive_emails(imap, dry_run)
        except Exception as e:
            self.logger.error("Archiving error: %s", e)
        print("Sorting mail")
        self.autosort_inbox(imap, dry_run, debug, quiet)

    # ------------------------------ execution modes ------------------------------

    def process(self, dry_run: bool = False, debug: bool = False, quiet: bool = False) -> None:
        print("\n-----\nProcessing at %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
        imap = self._imap_connect()
        try:
            self._process_core(imap, dry_run, debug, quiet)
        finally:
            self.imap_helper.close()

    def supports_idle(self, imap: imaplib.IMAP4_SSL) -> bool:
        try:
            typ, data = imap.capability()
            return typ == "OK" and data and (b"IDLE" in b" ".join(data).upper())
        except Exception as e:
            if hasattr(self, "logger") and self.logger:
                self.logger.warning("Error checking IMAP capabilities: %s", e)
        return False

    def idle_wait(self, imap: imaplib.IMAP4_SSL, folder: str, timeout: int = 900) -> bool:
        try:
            imap.select(folder, readonly=False)
            if not hasattr(imap, 'sock'):
                return False
            imap.send(b'IDLE\r\n')
            r, _, _ = select.select([imap.sock], [], [], timeout)
            if r:
                _ = imap.sock.recv(4096)
                imap.send(b'DONE\r\n')
                imap._get_response()
                return True
            imap.send(b'DONE\r\n')
            imap._get_response()
            return False
        except Exception as e:
            if hasattr(self, "logger") and self.logger:
                self.logger.warning("IMAP IDLE failed: %s", e)
            return False

    def idle_or_poll(self, imap: imaplib.IMAP4_SSL, folder: str, poll_interval: int = 60, idle_timeout: int = 900) -> None:
        if imap_supports_idle(imap, self.logger):
            while True:
                if self.todo_count(imap) > 0:
                    break
                self.logger.info("Waiting for new mail using IMAP IDLE...")
                if not imap_idle_wait(imap, self.todo_folder, timeout=idle_timeout, logger=self.logger):
                    self.logger.info("IMAP IDLE: no new mail.")
                    break
        else:
            while True:
                if self.todo_count(imap) > 0:
                    break
                self.logger.info("Waiting for new mail (polling every %ds)...", poll_interval)
                time.sleep(poll_interval)

    def process_with_idle(self, dry_run=False, debug=False, quiet=False, loop=False, idle_timeout=900, poll_interval=60):
        print("\n-----\nProcessing at %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
        imap = self._imap_connect()
        try:
            while True:
                self._process_core(imap, dry_run, debug, quiet)
                self.idle_or_poll(imap, self.todo_folder, poll_interval=poll_interval, idle_timeout=idle_timeout)
                if not loop:
                    break
        finally:
            self.imap_helper.close()


# ------------------------------ CLI ------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="IMAP AutoSorter using Nilsimsa hashing (single instance via flock in class)."
    )
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress informational output")
    parser.add_argument("-l", "--loop", type=float, default=0.0, help="Loop delay in seconds (if > 0, script repeats)")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without moving emails")
    parser.add_argument("--config", type=str, default="etc/imap_autosort.conf", help="Path to configuration file")
    parser.add_argument("--daemon", action="store_true", help="Run as a background daemon (requires python-daemon)")
    args = parser.parse_args()

    if os.path.dirname(sys.argv[0]):
        os.chdir(os.path.dirname(sys.argv[0]))

    sorter = IMAPAutoSorter(args.config)  # flock acquired here

    if sorter.maintenance:
        sys.exit('Under Maintenance')

    if args.daemon:
        try:
            import daemon  # type: ignore
        except ImportError:
            sys.exit("python-daemon is required for --daemon mode. Install with: pip install python-daemon")
        with daemon.DaemonContext():
            sorter.process_with_idle(
                dry_run=args.dry_run,
                debug=args.debug,
                quiet=args.quiet,
                loop=True,
                idle_timeout=int(args.loop) if args.loop > 0 else 900,
                poll_interval=60,
            )
    elif args.loop and args.loop > 0:
        sorter.process_with_idle(
            dry_run=args.dry_run,
            debug=args.debug,
            quiet=args.quiet,
            loop=True,
            idle_timeout=int(args.loop),
            poll_interval=60,
        )
    else:
        sorter.process(dry_run=args.dry_run, debug=args.debug, quiet=args.quiet)


if __name__ == "__main__":
    main()
