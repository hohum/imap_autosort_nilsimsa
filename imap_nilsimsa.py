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
import fnmatch
import hashlib
import imaplib
import logging
import logging.handlers
import math
import os
import random
import re
import select
import statistics
import sys
import time
from typing import Dict, List, Tuple

from rfc5424_logger import RFC5424Formatter
from nilsimsa import Nilsimsa, compare_hexdigests
from openai import OpenAI
from db import DatabaseHelper

try:  # used only when db_backend=mysql; keep import optional
    import mysql.connector  # noqa: F401
except Exception:  # pragma: no cover
    mysql = None  # noqa: F401

# ------------------------------ logging ------------------------------

def setup_logger(
    name: str,
    *,
    log_dir: str = '.',
    logfile: str | None = None,
    enable_syslog: bool = False,
    syslog_address: str = "/dev/log",
    facility: int = 1,
    app_name: str = "imap_nilsimsa",
):
    """Create a file+optional-syslog logger. Idempotent per name.

    Behavior preserved: default filename is YYYYMMDD.log when `logfile` is None.
    """
    logger = logging.getLogger(name)
    log_dir = os.path.expanduser(log_dir)
    os.makedirs(log_dir, exist_ok=True)
    log_filename = os.path.join(
        log_dir,
        logfile if logfile else time.strftime('%Y%m%d', time.localtime()) + '.log',
    )

    if not logger.handlers:  # avoid duplicate handlers
        fmt = RFC5424Formatter(app_name=app_name, facility=facility)
        fh = logging.FileHandler(log_filename)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        if enable_syslog:
            sh = logging.handlers.SysLogHandler(address=syslog_address)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
    logger.setLevel(logging.INFO)
    return logger

# ------------------------------ helpers ------------------------------

class IMAPHelper:
    def __init__(self, config: configparser.ConfigParser):
        self.server = config.get('imap', 'server')
        self.username = config.get('imap', 'username')
        self.password = config.get('imap', 'password')
        self.imap: imaplib.IMAP4_SSL | None = None

    def connect(self) -> imaplib.IMAP4_SSL:
        self.imap = imaplib.IMAP4_SSL(self.server)
        self.imap.login(self.username, self.password)
        return self.imap

    def close(self) -> None:
        if not self.imap:
            return
        # Be tolerant of server state when closing.
        for op in (lambda: self.imap.close(), lambda: self.imap.logout()):  # type: ignore[union-attr]
            try:
                op()
            except Exception:
                pass
        self.imap = None


class HeaderNormalizer:
    @staticmethod
    def normalize(
        mail_txt: str,
        exclude_headers: re.Pattern,
        headers_skip_re: re.Pattern,
        chomp_header: re.Pattern,
        headerIsX: re.Pattern,
        xinclude: List[str],
        dkim_just_d: re.Pattern,
        exclude_received_from_localhost: re.Pattern,
        weight_headers_re: re.Pattern,
        weight_headers_by: int,
    ) -> str:
        """Normalize headers to a stable, content-centric text.

        Behavior preserved; comments clarify intent.
        - Remove weekday/date/id noise; compress folded whitespace.
        - Reduce DKIM-Signature to its `d=` domain when requested.
        - Suppress X-* headers unless explicitly included.
        - Weight certain headers by repeating their text.
        """
        # Strip weekday banners early (cheap pre-pass)
        mail_txt = re.sub(r'(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat).*?([;\n])', r'\1', mail_txt)

        result: list[str] = []
        msg = email.message_from_string(mail_txt)
        for header in sorted(set(msg.keys())):
            if exclude_headers.search(header) or headers_skip_re.search(header):
                continue
            # Drop most X- headers unless explicitly kept
            if headerIsX.search(header) and header not in xinclude:
                continue

            for value in msg.get_all(header, []):
                # Unfold header lines and preserve bytes via backslash escapes
                value = chomp_header.sub(' ', value.encode('ascii', 'backslashreplace').decode()) + "\n"

                if header in {'Received', 'X-Received'}:
                    # Remove amavis noise and local Received lines
                    if re.search(r'port 10024', value):
                        continue
                    if header == 'Received' and exclude_received_from_localhost.search(value):
                        continue
                    # Trim typical volatile bits
                    value = re.sub(r' id \S+', '', value)
                    value = re.sub(r' (Sun|Mon|Tue|Wed|Thu|Fri|Sat),', '', value)
                    value = re.sub(r' (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', '', value)
                    value = re.sub(r' \d{4}-\d{2}-\d{2}', '', value)
                    value = re.sub(r' \d{2}:\d{2}:\d{2}(\.\d+)*', '', value)
                    value = re.sub(r' ( [A-Z]{3,4} )*m=\+\d+\.\d+', '', value)
                    value = re.sub(r' \+\d{4}( (\([A-Z]{3,4}\)))*', '', value)
                    value = re.sub(r' \(.*?\) by ', ' by ', value)
                    add = f"{header}: {value}"
                elif header == 'DKIM-Signature':
                    add = f"{header}: {dkim_just_d.sub(r'\\1', value)}"
                else:
                    add = f"{header}: {value}"

                # Header weighting: exact same effect as original (string repetition)
                if weight_headers_re.search(header):
                    add += add * weight_headers_by
                result.append(add)
        return ''.join(result)


# ------------------------------ main engine ------------------------------

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

        # OpenAI client (optional)
        api_key = (self.config.get("openai", "api_key", fallback=None) or '').strip()
        self.client = None
        if api_key:
            try:
                self.client = OpenAI(api_key=api_key)
            except Exception as e:
                self.client = None
                # logger not ready yet; ignore

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
        self.dkim_just_d = re.compile(r"^.*;\s*(d=[^;]+);.*$", re.M)
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

    # ------------------------------ header normalization ------------------------------

    def return_header(self, mail_txt: str) -> str:
        return HeaderNormalizer.normalize(
            mail_txt,
            self.exclude_headers,
            self.headers_skip_re,
            self.chomp_header,
            self.headerIsX,
            self.xinclude,
            self.dkim_just_d,
            self.exclude_received_from_localhost,
            self.weight_headers_re,
            self.weight_headers_by,
        )

    # ------------------------------ core: sync & distance ------------------------------

    def _classify_email(self, from_addr: str, subject: str) -> str:
        """Call OpenAI unless sender matches configured globs; log short result."""
        if any(fnmatch.fnmatch((from_addr or "").lower(), pat.lower()) for pat in self.sender_skip_llm):
            if self.logger:
                self.logger.info("LLM skipped for sender %s (sender_skip_llm matched)", from_addr)
            return '[{"cta":"Sender skipped"},{"label":["SenderSkipped:1.00"]}]'

        if not self.client:
            return '[{"cta":"Notice LLM not configured"},{"label":["Unclassified:1.00"]}]'

        prompt = (
            f"From: {from_addr}\nSubject: {subject}\n\n"
            "Return ONLY one plain-text JSON-like string: "
            "'[{""cta"": ""...""}, {""label"": [""X:0.00"", ""Y:0.00"", ""Z:0.00"", ""A:0.00"", ""B:0.00""]}]'\n"
            "Rules: CTA 3–10 words, imperative; labels ≥5 noun phrases with probs summing to 1.00."
        )
        try:
            response = self.client.chat.completions.create(
                model="gpt-5-mini",
                messages=[
                    {"role": "system", "content": "You are an email intent detector."},
                    {"role": "user", "content": prompt},
                ],
                timeout=60,
            )
            result = (response.choices[0].message.content or "").strip()
            if self.logger:
                self.logger.info("ChatGPT API response: %s", result)
            return result
        except Exception as e:  # keep tolerant; produce a deterministic fallback
            if self.logger:
                self.logger.error("GPT classification error: %s", e)
            return '[{"cta":"Notice LLM classification error"},{"label":["Unclassified:1.00"]}]'

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
                trimmed_header = self.return_header(raw_header)
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
                            chosen = self._classify_email(msg.get('From',''), msg.get('Subject',''))
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

    # ------------------------------ scoring ------------------------------

    def score_folder(
        self,
        folder: str,
        distances: List[int],
        threshold: int,
        debug: bool = False,
        quiet: bool = False,
    ) -> Tuple[float, float]:
        """Score a folder from distances over *threshold*; semantics unchanged."""
        over_threshold = [x for x in distances if x > threshold]
        if not over_threshold:
            if not quiet:
                print("n/a: %s nothing over threshold %s" % (folder, threshold))
            return 0.0, -1.0

        scores = [100 * (x - threshold) / (128 - threshold) for x in over_threshold]
        total_score = sum(scores)
        scored_count = len(scores)
        average = total_score / scored_count if scored_count else 0.0
        if scored_count > 1:
            total_score *= math.log10(scored_count)

        # Summaries (over-threshold only)
        n_over = len(over_threshold)
        ot_sorted = sorted(over_threshold)
        ot_min, ot_max = ot_sorted[0], ot_sorted[-1]
        ot_mean = sum(over_threshold) / n_over
        ot_var = sum((v - ot_mean) ** 2 for v in over_threshold) / max(1, n_over - 1)
        ot_std = ot_var ** 0.5
        idx = lambda p: int(p * (n_over - 1))
        ot_p90, ot_p95, ot_p99 = ot_sorted[idx(0.90)], ot_sorted[idx(0.95)], ot_sorted[idx(0.99)]

        # Longest run of consecutive over-threshold values in the original order.
        run = best_run = 0
        for v in distances:
            if v >= threshold:
                run += 1
                best_run = max(best_run, run)
            else:
                run = 0

        very_hi_cut = max(threshold + 15, 90)
        very_hi = sum(1 for v in over_threshold if v >= very_hi_cut)
        self.logger.info(
            ("Dist[%s] ≥%d: %d vals, mean %.1f±%.1f, span %d–%d, p90/95/99=%d/%d/%d, "
             "%d very-high (≥%d); longest ≥%d run=%d; total_score=%.1f avg=%.1f"),
            folder, threshold, n_over, ot_mean, ot_std, ot_min, ot_max,
            ot_p90, ot_p95, ot_p99, very_hi, very_hi_cut, threshold, best_run, total_score, average
        )

        sc_sorted = sorted(scores)
        sc_min, sc_max = sc_sorted[0], sc_sorted[-1]
        sc_mean = sum(scores) / n_over
        sc_var = sum((s - sc_mean) ** 2 for s in scores) / max(1, n_over - 1)
        sc_std = sc_var ** 0.5
        spct = lambda p: sc_sorted[int(p * (n_over - 1))]
        sc_p90, sc_p95, sc_p99 = spct(0.90), spct(0.95), spct(0.99)
        sc_very_cut = 95
        sc_very = sum(1 for s in scores if s >= sc_very_cut)
        self.logger.info(
            ("Score[%s] ≥%d: %d vals, mean %.1f±%.1f, span %.0f–%.0f, "
             "p90/95/99=%.0f/%.0f/%.0f, %d very-high (≥%d); total_score=%.1f avg=%.1f"),
            folder, threshold, n_over, sc_mean, sc_std, sc_min, sc_max,
            sc_p90, sc_p95, sc_p99, sc_very, sc_very_cut, total_score, average
        )

        if not quiet:
            print(average)
        return total_score, average

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
                trimmed_header = self.return_header(raw_header)
                self.logger.info("* New message from: %s, Message-ID: %s", msg['From'], message_id)
                self.logger.info(trimmed_header)

                cats = self._classify_email(msg['From'], msg['Subject'])
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

                # Cache distances once per folder (threshold-independent)
                dist_cache = {f: self.sync_and_distance(imap, f, source_hexdigest, dry_run, debug, quiet)
                              for f in self.imap_folders}

                base_T = self.threshold
                tie_ratio_gap = getattr(self, "tie_ratio_gap", 0.10)
                T = base_T
                winning_folder, winning_score = self.new_folder, 0.0

                while True:
                    stats: dict[str, Tuple[float, float]] = {}
                    sum_av = 0.0
                    for f, d in dist_cache.items():
                        sc, av = self.score_folder(f, d, T, debug, quiet)
                        stats[f] = (sc, av)
                        sum_av += max(0.0, av)

                    if sum_av <= 0.0:
                        self.logger.info("T=%d | no over-threshold signal; skipping ladder", T)
                        self.logger.info("RESOLVE @T=%d | no folder clears minimums; using new_folder", T)
                        break

                    ranked = sorted(stats.items(), key=lambda it: (it[1][1], it[1][0]), reverse=True)
                    lead_f, (lead_sc, lead_av) = ranked[0]
                    runner = ranked[1] if len(ranked) > 1 else None

                    r1 = (lead_av / sum_av) if sum_av > 0 else 0.0
                    r2 = ((runner[1][1] / sum_av) if (sum_av > 0 and runner) else 0.0)
                    ratio_gap = r1 - r2
                    self.logger.info("T=%d | leader=%s av=%.2f sc=%.2f | r1=%.3f r2=%.3f gap=%.3f",
                                     T, lead_f, lead_av, lead_sc, r1, r2, ratio_gap)

                    if (not runner) or (ratio_gap >= tie_ratio_gap) or (T >= 125):
                        if lead_sc > self.min_score and lead_av > self.min_average:
                            winning_folder, winning_score = lead_f, lead_sc
                            self.logger.info(
                                "RESOLVE @T=%d | winner=%s av=%.2f sc=%.2f (gap>=%.3f or no runner)",
                                T, winning_folder, lead_av, lead_sc, tie_ratio_gap,
                            )
                        else:
                            self.logger.info("RESOLVE @T=%d | no folder clears minimums; using new_folder", T)
                        break
                    else:
                        T += 5
                        self.logger.info("LADDER (ratio gap %.3f < %.3f) → raise T to %d", ratio_gap, tie_ratio_gap, T)

                if not dry_run:
                    print("* Moving message to %s" % winning_folder)
                    imap.select(self.todo_folder, readonly=False)
                    typ, data = imap.uid('MOVE', email_uid, '"%s"' % winning_folder)
                    if typ == 'OK':
                        dst_uid = None
                        info = self._extract_copyuid((typ, data)) or self._extract_copyuid(('OK', getattr(imap, 'untagged_responses', {}).get('OK', [])))
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
        if self.supports_idle(imap):
            while True:
                if self.todo_count(imap) > 0:
                    break
                self.logger.info("Waiting for new mail using IMAP IDLE...")
                if self.idle_wait(imap, folder, timeout=idle_timeout):
                    self.logger.info("IMAP IDLE: new mail detected.")
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
