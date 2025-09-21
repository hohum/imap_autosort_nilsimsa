#!/usr/bin/env python
"""
IMAP AutoSorter (single instance via flock).

Behavior-preserving refactor that reduces repetition and clarifies intent
without changing logic, thresholds, queries, or side-effects.
Double-checked for earlier mistakes (e.g., IMAP credential keys, regex typos,
folder quoting, and function signatures).
"""

import argparse
import configparser
import email
import errno
import fcntl
import hashlib
import imaplib
import logging
import math
import os
import random
import re
import sys
import time
import statistics
from typing import Dict, List, Tuple
from openai import OpenAI
import pprint

import mysql.connector
from nilsimsa import Nilsimsa, compare_hexdigests
import select

def setup_logger(name):
    logger = logging.getLogger(name)
    log_filename = time.strftime("%Y%m%d", time.localtime()) + ".log"
    if not logger.handlers:
        file_handler = logging.FileHandler(log_filename)
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)
    return logger

class DatabaseHelper:
    def __init__(self, mysql_pass, version, logger):
        self.conn = mysql.connector.connect(
            host="localhost", user="imap_nilsimsa", passwd=mysql_pass, db="imap_nilsimsa", autocommit=True
        )
        self.cursor = self.conn.cursor(buffered=True)
        self.logger = logger
        self.version = version
        self._init_schema()

    # Convenience wrappers so callers can either:
    #   self.db.execute("SELECT ...", params); rows = self.db.fetchall()
    # or:
    #   rows = self.db.fetchall("SELECT ...", params)
    def fetchall(self, *args, **kwargs):
        if args or kwargs:
            # args/kwargs contain a query → run it, then fetch
            self.cursor.execute(*args, **kwargs)
        return self.cursor.fetchall()

    def execute(self, *args, **kwargs):
        return self.cursor.execute(*args, **kwargs)


    def _init_schema(self):
        """Connect to MySQL and ensure schema/version – behavior unchanged."""
        try:
            # tables
            self.cursor.execute(
                'CREATE TABLE IF NOT EXISTS nilsimsa ('
                'id INTEGER PRIMARY KEY AUTO_INCREMENT, '
                'added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                'uid INTEGER, folder TEXT, hexdigest TEXT, md5sum TEXT, trimmed_header TEXT)'
            )
            self.cursor.execute('CREATE TABLE IF NOT EXISTS considered (uid INTEGER, considered_when INTEGER)')
            self.cursor.execute('CREATE TABLE IF NOT EXISTS version (version TEXT)')
            # version
            self.cursor.execute('SELECT version FROM version LIMIT 1')
            row = self.cursor.fetchone()
            self.db_version = row[0] if row else None
            if self.db_version != self.version:
                print('Database version mismatch or missing. Rebuilding the nilsimsa table...')
                self.cursor.execute('DROP TABLE IF EXISTS nilsimsa')
                self.cursor.execute(
                    'CREATE TABLE nilsimsa ('
                    'id INTEGER PRIMARY KEY AUTO_INCREMENT, '
                    'added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                    'uid INTEGER, folder TEXT, hexdigest TEXT, md5sum TEXT, trimmed_header TEXT)'
                )
                self.cursor.execute('DELETE FROM version')
                self.cursor.execute("INSERT INTO version (version) VALUES (%s)", (self.version,))
        except mysql.connector.Error as e:
            self.logger.error("Database connection error: %s", e)
            sys.exit("Database connection failed.")

        def fetchall(self, *args, **kwargs):
            if args or kwargs:
                self.cursor.execute(*args, **kwargs)
            return self.cursor.fetchall()

    def execute(self, *args, **kwargs):
        self.cursor.execute(*args, **kwargs)
        self.conn.commit()

    def close(self):
        self.cursor.close()
        self.conn.close()

class IMAPHelper:
    def __init__(self, config):
        self.server = config.get('imap', 'server')
        self.username = config.get('imap', 'username')
        self.password = config.get('imap', 'password')
        self.imap = None

    def connect(self):
        self.imap = imaplib.IMAP4_SSL(self.server)
        self.imap.login(self.username, self.password)
        return self.imap

    def close(self):
        if self.imap:
            try: self.imap.close()
            except Exception: pass
            try: self.imap.logout()
            except Exception: pass

class HeaderNormalizer:
    @staticmethod
    def normalize(mail_txt, exclude_headers, headers_skip_re, chomp_header, headerIsX, xinclude, dkim_just_d, 
                    exclude_received_from_localhost, weight_headers_re, weight_headers_by):
        """Normalize headers to a stable, content-centric text.

        We remove non-signal noise that varies across MTAs: weekdays/dates/ids,
        amavis/mailscanner artifacts, local Received lines, and collapse folded
        whitespace. DKIM is reduced to its domain (d=...), and we suppress most
        X- headers except if explicitly listed in xinclude.
        """
        mail_txt = re.sub(r'(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat).*?([;\n])', r'\1', mail_txt)
        result = ''
        msg = email.message_from_string(mail_txt)
        for header in sorted(set(msg.keys())):
            if exclude_headers.search(header) or headers_skip_re.search(header):
                continue
            for this_header_content in msg.get_all(header):
                # Unfold header lines and preserve bytes via backslash escapes
                this_header_content = chomp_header.sub(' ', this_header_content.encode('ascii', 'backslashreplace').decode())
                this_header_content += "\n"
                # Drop most X- headers unless explicitly kept
                if headerIsX.search(header) and header not in xinclude:
                    continue
                # Received/X-Received cleanup (strip ids, weekdays, month names, times, tzs, and noisy by-clauses)
                if header in ['Received', 'X-Received']:
                    if re.search(r'port 10024', this_header_content):  # amavis noise
                        continue
                    if header == 'Received' and exclude_received_from_localhost.match(this_header_content):
                        continue
                    this_header_content = re.sub(r' id \S+', '', this_header_content)
                    this_header_content = re.sub(r' (Sun|Mon|Tue|Wed|Thu|Fri|Sat),', '', this_header_content)
                    this_header_content = re.sub(r' (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', '', this_header_content)
                    this_header_content = re.sub(r' \d{4}-\d{2}-\d{2}', '', this_header_content)
                    this_header_content = re.sub(r' \d{2}:\d{2}:\d{2}(\.\d+)*', '', this_header_content)
                    this_header_content = re.sub(r' ( [A-Z]{3,4} )*m=\+\d+\.\d+', '', this_header_content)
                    this_header_content = re.sub(r' \+\d{4}( (\([A-Z]{3,4}\)))*', '', this_header_content)
                    this_header_content = re.sub(r' \(.*?\) by ', ' by ', this_header_content)
                    add = header + ': ' + this_header_content
                elif header == 'DKIM-Signature':
                    add = header + ': ' + dkim_just_d.sub(r'\1', this_header_content)
                else:
                    add = header + ': ' + this_header_content
                # Header weighting: exact same effect as original (string repetition)
                if weight_headers_re.search(header):
                    add += add * weight_headers_by
                result += add
        return result

class IMAPAutoSorter:
    """Sort emails into folders by Nilsimsa similarity of headers.

    Concurrency: we acquire an exclusive flock *in the constructor* so only one
    instance runs at a time. If the lock is already held, this process exits.
    """

    # ------------------------------ init ------------------------------
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

        # IMAP folders & lists
        self.todo_folder = self.config.get("imap", "todo")
        self.new_folder = self.config.get("imap", "new")
        self.imap_folders = self._get_list("imap", "folders")
        
        # openai api key
        api_key = self.config.get("openai", "api_key", fallback=None)
        self.client = None
        if api_key:
            api_key = api_key.strip()
            if api_key:
                try:
                    self.client = OpenAI(api_key=api_key)
                except Exception as e:
                    self.client = None
                    if hasattr(self, "logger") and self.logger:
                        self.logger.error("OpenAI client init failed: %s", e)

        # Nilsimsa thresholds & knobs
        self.threshold = self.config.getint("nilsimsa", "threshold", fallback=50)
        self.min_score = self.config.getint("nilsimsa", "min_score", fallback=100)
        self.min_average = self.config.getfloat("nilsimsa", "min_average", fallback=0)
        self.min_over = self.config.getfloat("nilsimsa", "min_over", fallback=1)
        self.weight_headers = self._get_list("nilsimsa", "weight_headers")
        self.headers_skip = self._get_list("nilsimsa", "headers_skip")
        self.weight_headers_by = self.config.getint("nilsimsa", "weight_headers_by", fallback=1)
        self.xinclude = self._get_list("nilsimsa", "xinclude")

        # MySQL
        self.mysql_pass = self.config.get("mysql", "password")

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

        # Logging — daily filename like original, avoid duplicate handlers
        self.logger = setup_logger("imap_nilsimsa")

        # DB
        self.db = DatabaseHelper(self.mysql_pass, self.version, self.logger)
        self.imap_helper = IMAPHelper(self.config)

    # ------------------------------ small helpers ------------------------------
    
    def _parse_uid_set(self, s: str):
        out = []; s = (s or '').strip()
        if not s: return out
        for part in s.replace(',', ' ').split():
            if ':' in part:
                a, b = part.split(':', 1); a, b = int(a), int(b)
                out.extend(range(min(a, b), max(a, b) + 1))
            else:
                out.append(int(part))
        return out


    def _extract_copyuid(self, result):
        import re
        typ, data = result or (None, None)
        pieces = []
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
        """Parse comma-separated config option into a trimmed list ("a, b" -> ["a","b"])."""
        if not self.config.has_option(section, key):
            return []
        return [x.strip() for x in self.config.get(section, key).split(',') if x.strip()]

    def _ensure_single_instance(self) -> None:
        """Exclusive flock; exit if already locked."""
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
        """One-line progress bar identical in effect to original."""
        if total <= 1:
            return
        percent = int(100 * current / (total - 1) + 0.5)
        num_equals = int(percent / 2)
        sys.stdout.write("%s [%-50s] %3d%% %d/%d\r" % (message, '=' * num_equals, percent, current + 1, total))
        if current == (total - 1):
            print("")
        sys.stdout.flush()

    def _classify_email(self, from_addr: str, subject: str):
        prompt = f"""
From: {from_addr}
Subject: {subject}

Return ONLY one plain-text JSON-like string:
'[{{"cta":"..."}},{{"label":["X:0.00","Y:0.00","Z:0.00","A:0.00","B:0.00"]}}]'

Rules:
- CTA: 3–10 words, imperative, generic, dictionary words only excluding "now" or similar; meaningful for automation; including a generic but relevant domain noun if obvious (e.g., ‘Review military aircraft discussion thread’).
- Use From/Subject + domain for inference; prefer abstract action (don’t parrot topic words/brands unless essential for safety/finance).
- Labels: ≥5 noun phrases, sorted desc; include "Spam" and/or "Phishing Suspected" only if very confident.
- Probabilities: 2 decimals, sum=1.00 (adjust last value if needed).
- Output must be exactly one string, no extra text.

Guidance:
- detect distinctive signals — including subtle role phrases — and adaptively generalize them into brand-agnostic concepts; capture oddities that differentiate the message; avoid proper nouns/department names and fixed keyword lists; do not prioritize any field (e.g., “photo desk” ⇒ “photo”).
- Some emails are internal notifications from my own systems (e.g. Macrodroid, fail2ban).
"""
        try:
            response = self.client.chat.completions.create(
                model="gpt-5-mini",
                messages=[
                    {"role": "system", "content": (
                            "You are an email intent detector."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                timeout=60
            )
            result = (response.choices[0].message.content or "").strip()
            if self.logger:
                self.logger.info("ChatGPT API response: %s", result)
            return result
        except Exception as e:
            if self.logger:
                self.logger.error("GPT classification error: %s", e)
            return '[{"cta":"Notice LLM classisication error"},{"label":["Unclassified:1.00"]}]'

    def status(self, current: int, total: int, message: str = '') -> None:
        """One-line progress bar identical in effect to original."""
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
        return HeaderNormalizer.normalize(mail_txt, self.exclude_headers, self.headers_skip_re, self.chomp_header, self.headerIsX,
                                            self.xinclude, self.dkim_just_d, self.exclude_received_from_localhost, self.weight_headers_re, 
                                            self.weight_headers_by)

    # ------------------------------ core: sync & distance ------------------------------
    def sync_and_distance(self, imap: imaplib.IMAP4_SSL, folder: str, source_hexdigest: str,
                          dry_run: bool = False, debug: bool = False, quiet: bool = False) -> List[int]:
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
                    # No md5sum entry → treat as new. Classify, compute hexdigest over categories+trimmed_header, insert full row.
                    msg = email.message_from_string(raw_header)
                    # Maybe later we can reclassify all older mail, but for now hard set
                    # cats = self._classify_email(msg.get('From',''), msg.get('Subject',''))
                    cats = '[{"cta":"Notice LLM classisication never done"},{"label":["Unclassified:1.00"]}]'
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
                    # md5sum exists. If exactly one row → moved; else (>=2) → unknown; in both cases ensure consistent categories.
                    if len(md5_rows) == 1:
                        prev_id, prev_uid, prev_folder, prev_cats, prev_hex = md5_rows[0]
                        # Update DB to reflect IMAP state (uid, folder, moved_from)
                        if not dry_run:
                            try:
                                self.db.execute(
                                    "UPDATE nilsimsa SET uid=%s, folder=%s, moved_from=%s WHERE id=%s",
                                    (email_uid, folder, prev_folder or '', prev_id),
                                )
                            except Exception as e:
                                if self.logger: self.logger.error("Move-update failed: %s", e)
                        # Choose categories: reuse if present, else classify once
                        cats = prev_cats or ''
                        if (not cats): # or ('Unclassified' in cats):
                            msg = email.message_from_string(raw_header)
                            # Maybe later we can reclassify all older mail, but for now hard set
                            # cats = self._classify_email(msg.get('From',''), msg.get('Subject',''))
                            cats = '[{"cta":"Notice LLM classisication never done"},{"label":["Unclassified:1.00"]}]'
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
                                    if self.logger: self.logger.error("Post-move categories update failed: %s", e)
                        else:
                            # Categories already present; compute hexdigest for in-memory distance only
                            try:
                                target_hexdigest = Nilsimsa(f"X-LLM-Categories: {cats}\n{trimmed_header}").hexdigest()
                            except Exception as e:
                                self.logger.error("Failed to compute Nilsimsa hash: %s", e)
                                self.logger.error(trimmed_header)
                                continue
                    else:
                        # Multiple md5sum rows → unknown; reuse any existing non-empty/non-Unclassified categories if possible
                        chosen = None
                        for (_id, _uid, _folder, _cats, _hex) in md5_rows:
                            if _cats and ('Unclassified' not in _cats):
                                chosen = _cats
                                break
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
                # Already in DB: reuse existing hex and mark as seen for pruning step
                if debug:
                    print("Email UID %s found in DB" % email_uid)
                target_hexdigest = mail_db[email_uid]
                del mail_db[email_uid]

            # Distance against the *source* hexdigest (unchanged)
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
            print(f"{len(mail_db)} records for cleanup in DB folder[{folder}]")
        for email_uid in list(mail_db.keys()):
            if not quiet:
                self.status(0, len(mail_db), 'Deleting moved messages ')
            if not dry_run:
                self.db.execute("DELETE FROM nilsimsa WHERE uid = %s AND folder = %s", (email_uid, folder))
            else:
                print("Dry run: would have deleted DB entry for UID: %s, folder: %s" % (email_uid, folder))

        return distances

    # ------------------------------ scoring ------------------------------
    def score_folder(self, folder: str, distances: List[int], threshold: int,
                     debug: bool = False, quiet: bool = False) -> Tuple[float, float]:
        """Score a folder from distances over *threshold*; semantics unchanged."""
        over_threshold = [x for x in distances if x > threshold]
        if not over_threshold:
            return 0.0, 0.0

        scores = [100 * (x - threshold) / (128 - threshold) for x in over_threshold]
        total_score = sum(scores)
        scored_count = len(scores)
        average = 0.0

        if scored_count >= self.min_over:
            average = total_score / scored_count
            total_score *= math.log10(scored_count) if scored_count > 1 else 1

            # Summarize ONLY the over-threshold values (no under-threshold data).
            n_over = len(over_threshold)
            ot_sorted = sorted(over_threshold)
            ot_min = ot_sorted[0]
            ot_max = ot_sorted[-1]
            # use population stdev for stability on small n; switch to sample if you prefer
            ot_mean = sum(over_threshold) / n_over
            ot_var = sum((v - ot_mean) ** 2 for v in over_threshold) / max(1, n_over - 1)
            ot_std = ot_var ** 0.5
            def pct(p):
                i = int(p * (n_over - 1))
                return ot_sorted[i]
            ot_p90, ot_p95, ot_p99 = pct(0.90), pct(0.95), pct(0.99)

            # Longest run of consecutive over-threshold values in the original order.
            run = best_run = 0
            for v in distances:
                if v >= threshold:
                    run += 1
                    if run > best_run:
                        best_run = run
                else:
                    run = 0

            # (Optional) very-high bucket entirely above threshold as a quick “tail heat” signal
            very_hi_cut = max(threshold + 15, 90)
            very_hi = sum(1 for v in over_threshold if v >= very_hi_cut)

            # One-liner: compact stats + readable narrative, strictly about over-threshold.
            self.logger.info(
                ("Dist[%s] ≥%d: %d vals, mean %.1f±%.1f, span %d–%d, p90/95/99=%d/%d/%d, "
                 "%d very-high (≥%d); longest ≥%d run=%d; total_score=%.1f avg=%.1f"),
                folder, threshold, n_over, ot_mean, ot_std, ot_min, ot_max,
                ot_p90, ot_p95, ot_p99, very_hi, very_hi_cut, threshold, best_run, total_score, average
            )
            
            # One-liner (SCORES): mirror the distance summary for the score distribution (over-threshold only).
            sc_sorted = sorted(scores)
            sc_min = sc_sorted[0]
            sc_max = sc_sorted[-1]
            sc_mean = sum(scores) / n_over
            sc_var = sum((s - sc_mean) ** 2 for s in scores) / max(1, n_over - 1)
            sc_std = sc_var ** 0.5
            def spct(p: float) -> float:
                i = int(p * (n_over - 1))
                return sc_sorted[i]
            sc_p90, sc_p95, sc_p99 = spct(0.90), spct(0.95), spct(0.99)
            sc_very_cut = 95  # fixed “very-high” score bucket
            sc_very = sum(1 for s in scores if s >= sc_very_cut)
            self.logger.info(
                ("Score[%s] ≥%d: %d vals, mean %.1f±%.1f, span %.0f–%.0f, "
                 "p90/95/99=%.0f/%.0f/%.0f, %d very-high (≥%d); total_score=%.1f avg=%.1f"),
                folder, threshold, n_over, sc_mean, sc_std, sc_min, sc_max,
                sc_p90, sc_p95, sc_p99, sc_very, sc_very_cut, total_score, average
            )
            
            if not quiet:
                print(average)
        else:
            if not quiet:
                print("n/a: %s nothing over threshold %s" % (folder, threshold))
            average = -1.0

        return total_score, average

    # ------------------------------ todo / autosort ------------------------------
    def todo_count(self, imap: imaplib.IMAP4_SSL) -> int:
        """Return count of UNSEEN in TODO folder (unchanged query)."""
        imap.select(self.todo_folder, readonly=False)
        resp, data = imap.search(None, 'UNSEEN')
        return len(data[0].split()) if data and data[0] else 0

    def autosort_inbox(self, imap: imaplib.IMAP4_SSL, dry_run: bool = False,
                       debug: bool = False, quiet: bool = False) -> None:
        """Process UNSEEN in TODO: compute source hexdigest, score per folder, move/copy."""
        while self.todo_count(imap):
            imap.select(self.todo_folder, readonly=False)
            result, data = imap.uid('search', None, "(UNSEEN)")
            if not (data and data[0]):
                break
            email_uids = [str(x) for x in data[0].decode().split()]

            for email_uid in email_uids:
                print("----- Considering message: %s" % email_uid)
                self.logger.info("---------- Considering message: %s" % email_uid)
                imap.select(self.todo_folder, readonly=False)
                res_fetch, data_fetch = imap.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
                try:
                    raw_header = data_fetch[0][1].decode('utf-8', 'backslashreplace')
                except Exception:
                    sys.exit("Error: email_uid: %s has no data" % email_uid)
                
                msg = email.message_from_string(raw_header)
                print("---------- Source: subject: %s" % msg['Subject'])
                trimmed_header = self.return_header(raw_header)
                self.logger.info("From: %s", msg['From'])
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

                winning_folder = self.new_folder
                winning_score = 0.0
                
                # --- Ratio-as-tie-trigger ladder (winner still decided by avg->score) ---
                base_T = self.threshold
                tie_ratio_gap = getattr(self, "tie_ratio_gap", 0.10)  # if (r1 - r2) < this => tie → raise T

                # Cache distances once per folder (threshold-independent)
                dist_cache = {}
                for f in self.imap_folders:
                    dist_cache[f] = self.sync_and_distance(imap, f, source_hexdigest, dry_run, debug, quiet)

                T = base_T
                winning_folder, winning_score = self.new_folder, 0.0
                while True:
                    # Score all folders at a shared threshold T
                    stats = {}  # f -> (score, avg)
                    sum_av = 0.0
                    for f, d in dist_cache.items():
                        sc, av = self.score_folder(f, d, T, debug, quiet)
                        stats[f] = (sc, av)
                        sum_av += max(0.0, av)                    

                    # Early stop: no over-threshold signal in any folder → don't ladder
                    if sum_av <= 0.0:
                        self.logger.info("T=%d | no over-threshold signal; skipping ladder", T)
                        self.logger.info("RESOLVE @T=%d | no folder clears minimums; using new_folder", T)
                        break

                    # Rank by (avg, then score)
                    ranked = sorted(stats.items(), key=lambda it: (it[1][1], it[1][0]), reverse=True)
                    lead_f, (lead_sc, lead_av) = ranked[0]
                    runner = ranked[1] if len(ranked) > 1 else None

                    # Compute top-2 ratio gap of averages
                    r1 = (lead_av / sum_av) if sum_av > 0 else 0.0
                    r2 = ((runner[1][1] / sum_av) if (sum_av > 0 and runner) else 0.0)
                    ratio_gap = r1 - r2
                    self.logger.info("T=%d | leader=%s av=%.2f sc=%.2f | r1=%.3f r2=%.3f gap=%.3f",
                                     T, lead_f, lead_av, lead_sc, r1, r2, ratio_gap)

                    # If clearly separated by ratio, decide now; else ladder up
                    if (not runner) or (ratio_gap >= tie_ratio_gap) or (T >= 125):
                        if lead_sc > self.min_score and lead_av > self.min_average:
                            winning_folder, winning_score = lead_f, lead_sc
                            self.logger.info("RESOLVE @T=%d | winner=%s av=%.2f sc=%.2f (gap>=%.3f or no runner)",
                                             T, winning_folder, lead_av, lead_sc, tie_ratio_gap)
                        else:
                            self.logger.info("RESOLVE @T=%d | no folder clears minimums; using new_folder", T)
                        break
                    else:
                        T += 5  # tie by ratio → raise threshold and re-evaluate
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
                            try: dst_uid = dst_uids[src_uids.index(int(email_uid))]  # map src->dst
                            except Exception: 
                                dst_uid = None

                        # --- DB upsert to reflect move (md5 on trimmed_header; hexdigest on categories+trimmed_header) ---
                        md5sum = hashlib.md5(trimmed_header.encode('utf-8')).hexdigest()
                        message_id = (msg.get('Message-ID','') or '').strip()                    
                        self.db.execute(
                            "INSERT INTO nilsimsa (uid, folder, hexdigest, md5sum, trimmed_header, categories, moved_from, message_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (dst_uid, winning_folder, source_hexdigest, md5sum, trimmed_header, cats, self.todo_folder, message_id)
                        )
                        self.logger.info("Moved email %s to %s (dst UID: %s)", email_uid, winning_folder, dst_uid)
                    else:
                        self.logger.error("MOVE failed for %s -> %s", email_uid, winning_folder)
                else:
                    print("Dry run: would have moved %s to folder %s" % (email_uid, winning_folder))

    # ------------------------------ archive ------------------------------
    def archive_emails(self, imap: imaplib.IMAP4_SSL, dry_run: bool = False) -> None:
        self.logger.info(f"---------- Begin Archive check")
        """Archive or delete old emails according to config (unchanged logic)."""
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

            if (payload := (data[0] if data and data[0] else None)):
                email_uids = payload.decode().split()
                n = len(email_uids)
                print(f"Found {n} emails to consider for archiving in folder {folder}")
                self.logger.info("Found %d emails in %s for archive", n, folder)
            else:
                email_uids = []

            for email_uid in email_uids:
                target_folder = self.trash_folder if (self.just_delete and folder in self.just_delete) else self.archive_folder
                if not dry_run:
                    result_copy = imap.uid('COPY', email_uid, '"%s"' % target_folder)
                    if result_copy[0] == 'OK':
                        imap.uid('STORE', email_uid, '+FLAGS', '(\\Deleted)')
                        imap.expunge()
                else:
                    print("Dry run: message %s from folder %s would be archived to %s" % (email_uid, folder, target_folder))
        self.logger.info(f"---------- End Archive check")

    # ------------------------------ housekeeping ------------------------------
    def prune_considered(self) -> None:
        """Delete old rows from 'considered' to avoid reprocessing."""
        now = int(time.time())
        delete_older_than = now - self.reconsider_after - random.randint(0, self.reconsider_after)
        self.db.execute("DELETE FROM considered WHERE considered_when < %s", (delete_older_than,))

    def _imap_connect(self):
        return self.imap_helper.connect()

    def _process_core(self, imap, dry_run=False, debug=False, quiet=False):
        """Core logic for archiving and sorting mail, shared by process/process_with_idle."""
        self.prune_considered()
        print("Archiving messages")
        try:
            self.archive_emails(imap, dry_run)
        except Exception as e:
            self.logger.error("Archiving error: %s", e)

        print("Sorting mail")
        self.autosort_inbox(imap, dry_run, debug, quiet)

    def process(self, dry_run: bool = False, debug: bool = False, quiet: bool = False) -> None:
        self.logger.info(f"## ----- Begin Script run")
        print("\n-----\nProcessing at %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
        imap = self._imap_connect()
        try:
            self._process_core(imap, dry_run, debug, quiet)
        finally:
            self.imap_helper.close()
            self.logger.info(f"## ----- End Script run")

    def process_with_idle(self, dry_run=False, debug=False, quiet=False, loop=False, idle_timeout=900, poll_interval=60):
        self.logger.info(f"## ----- Begin Script run (IDLE/poll mode)")
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
            self.logger.info(f"## ----- End Script run")

    def supports_idle(self, imap: imaplib.IMAP4_SSL) -> bool:
        """Check if the IMAP server supports the IDLE extension."""
        try:
            typ, data = imap.capability()
            if typ == "OK" and data:
                caps = b" ".join(data).upper()
                return b"IDLE" in caps
        except Exception as e:
            if hasattr(self, "logger") and self.logger:
                self.logger.warning("Error checking IMAP capabilities: %s", e)
        return False

    def idle_wait(self, imap: imaplib.IMAP4_SSL, folder: str, timeout: int = 900) -> bool:
        """
        Enter IMAP IDLE mode and wait for a new message or timeout.
        Returns True if new mail is detected, False if timeout.
        """
        try:
            imap.select(folder, readonly=False)
            if not hasattr(imap, 'sock'):
                return False
            imap.send(b'IDLE\r\n')
            r, _, _ = select.select([imap.sock], [], [], timeout)
            if r:
                resp = imap.sock.recv(4096)
                imap.send(b'DONE\r\n')
                imap._get_response()
                return True
            else:
                imap.send(b'DONE\r\n')
                imap._get_response()
                return False
        except Exception as e:
            if hasattr(self, "logger") and self.logger:
                self.logger.warning("IMAP IDLE failed: %s", e)
            return False

    def idle_or_poll(self, imap: imaplib.IMAP4_SSL, folder: str, poll_interval: int = 60, idle_timeout: int = 900) -> None:
        """
        Wait for new mail using IDLE if supported, else poll every poll_interval seconds.
        Only returns when new mail is detected.
        """
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

# ------------------------------ CLI ------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="IMAP AutoSorter using Nilsimsa hashing (single instance via flock in class).")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress informational output")
    parser.add_argument("-l", "--loop", type=float, default=0.0, help="Loop delay in seconds (if > 0, script repeats)")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without moving emails")
    parser.add_argument("--config", type=str, default="imap_autosort.conf", help="Path to configuration file")
    parser.add_argument("--daemon", action="store_true", help="Run as a background daemon (requires python-daemon)")
    args = parser.parse_args()

    # Optionally change directory to the script location
    if os.path.dirname(sys.argv[0]):
        os.chdir(os.path.dirname(sys.argv[0]))

    sorter = IMAPAutoSorter(args.config)  # flock acquired here

    if sorter.maintenance:
        sys.exit('Under Maintenance')

    # Use IDLE/polling only if daemon or loop mode
    if args.daemon:
        try:
            import daemon
        except ImportError:
            sys.exit("python-daemon is required for --daemon mode. Install with: pip install python-daemon")
        with daemon.DaemonContext():
            sorter.process_with_idle(
                dry_run=args.dry_run, debug=args.debug, quiet=args.quiet,
                loop=True, idle_timeout=int(args.loop) if args.loop > 0 else 900, poll_interval=60
            )
    elif args.loop and args.loop > 0:
        sorter.process_with_idle(
            dry_run=args.dry_run, debug=args.debug, quiet=args.quiet,
            loop=True, idle_timeout=int(args.loop), poll_interval=60
        )
    else:
        sorter.process(dry_run=args.dry_run, debug=args.debug, quiet=args.quiet)

if __name__ == "__main__":
    main()
