# imap_autosort_nilsimsa

IMAP AutoSorter is a Python tool that automatically sorts your email into IMAP folders based on **Nilsimsa similarity hashing of message headers**.  
It can also use optional **LLM-based classification (OpenAI API)** for semantic hints. The goal is to reduce manual inbox triage by learning from the structure of your existing folders and message flow.

---

## Features

- **Automatic email sorting**  
  Compares headers of new/unread messages against stored Nilsimsa digests of existing folders, then moves messages to the best match.  

- **Self-maintaining database**  
  Uses MySQL/MariaDB to cache normalized headers, hashes, and classifications.  

- **Optional OpenAI classification**  
  If enabled, emails can be enriched with lightweight labels/CTAs before similarity comparison.  

- **Archiving**  
  Can automatically move or delete old messages according to config.  

- **Single-instance lock**  
  Ensures only one sorter runs at a time.  

- **Flexible logging**  
  Structured logging in [RFC 5424 format](https://datatracker.ietf.org/doc/html/rfc5424) with syslog or file rotation.

---

## Installation

### Requirements

- **Linux** or another Unix-like OS (uses `flock` and syslog conventions).  
- **Python 3.9+** (tested up to Python 3.13).  
- **MySQL/MariaDB server** running locally or accessible over the network.  
- IMAP server with SSL (tested with Dovecot).  

### Python dependencies

Install via pip:

```bash
pip install mysql-connector-python openai python-daemon
```

### Database setup

The sorter will initialize its schema automatically if the configured database exists. Create a database and user first:

```sql
CREATE DATABASE imap_nilsimsa CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'imap_nilsimsa'@'localhost' IDENTIFIED BY 'yourpassword';
GRANT ALL PRIVILEGES ON imap_nilsimsa.* TO 'imap_nilsimsa'@'localhost';
```

### Configuration

Copy the sample config and edit it:

```bash
cp imap_autosort.conf.sample imap_autosort.conf
nano imap_autosort.conf
```

Key sections include:

- `[imap]` — IMAP server, credentials, folder names.  
- `[mysql]` — Database password.  
- `[nilsimsa]` — Thresholds and tuning knobs.  
- `[openai]` — API key and sender skip rules (optional).  
- `[archive]` — Folder and retention policy for old mail.  

---

## Usage

Run the sorter manually:

```bash
python3 imap_nilsimsa.py --config imap_autosort.conf
```

Options:

- `--dry-run` — simulate without moving mail.  
- `--debug` — extra debug output.  
- `--quiet` — suppress progress bars/info.  
- `--loop SECONDS` — repeat every N seconds.  
- `--daemon` — run as background process (requires `python-daemon`).  

Example (daemon mode with IDLE support):

```bash
python3 imap_nilsimsa.py --config imap_autosort.conf --daemon
```

Logs are written daily (`YYYYMMDD.log`) and/or sent to syslog, depending on config.

---

## Target Audience

This project is aimed at **Linux/Python-proficient email users or sysadmins** who:

- Host their own IMAP servers (e.g., Dovecot, Cyrus).  
- Want automated but transparent mail sorting.  
- Are comfortable editing config files and running Python scripts with databases.  
- May want to experiment with lightweight machine learning (OpenAI API) but can run fully without it.  

It is **not** a plug-and-play desktop app — it’s intended for technical users who like fine-grained control over how their mail is handled.


---

## Developer / Contributor Notes

### Code structure

- **`imap_nilsimsa.py`** — main entry point; IMAP connection, header normalization, Nilsimsa scoring, autosort logic, and CLI.  
- **`db.py`** — database helper class, schema initialization, query helpers.  
- **`rfc5424_logger.py`** — structured logger formatter (RFC 5424) with optional syslog support.  
- **`imap_autosort.conf.sample`** — example configuration file.  

### Database schema

- **`nilsimsa`** — stores UID, folder, Nilsimsa hex digest, md5sum of trimmed headers, categories (from LLM), and message ID.  
- **`considered`** — prevents reprocessing of recently seen messages.  
- **`version`** — tracks DB schema version, upgraded automatically on mismatch.  

### Development guidelines

- Preserve behavior when refactoring: thresholds, similarity scoring, logging output, and DB schema must remain stable.  
- Child loggers inherit from the base RFC 5424 logger; use `logger.getChild()` consistently.  
- Avoid breaking config compatibility (`imap_autosort.conf`).  

### Running locally

For rapid testing with a non-production mailbox:

```bash
python3 imap_nilsimsa.py --config test.conf --dry-run --debug
```

### Contributions

Pull requests are welcome for:

- Bug fixes and performance improvements.  
- Enhanced header normalization or noise suppression.  
- Additional logging backends or metrics collection.  
- Improved OpenAI classification prompt design.  

Please ensure new code paths are **idempotent** and **do not alter scoring math** unless explicitly documented.  

---

---

## Architecture Overview

```text
                 ┌──────────────────────────────┐
                 │          IMAP Server         │
                 │  (e.g., Dovecot via SSL)     │
                 └──────────────┬───────────────┘
                                │ UID FETCH (headers), SEARCH, MOVE/COPY
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────────┐
│                         IMAP AutoSorter (Python)                      │
│                                                                       │
│  ┌───────────────────────────┐     ┌───────────────────────────────┐  │
│  │ Header Normalizer         │     │ Nilsimsa Engine               │  │
│  │ - drop noisy fields       │     │ - hexdigests for headers      │  │
│  │ - collapse whitespace     │     │ - distance compare (-127..128)│  │
│  └──────────────┬────────────┘     └───────────────┬───────────────┘  │
│                 │                                    distances        │
│                 ▼                                                    │
│        ┌────────────────┐                                 ┌──────────┐ │
│        │ Optional LLM   │  categories/labels              │ Scoring  │ │
│        │ (OpenAI)       ├────────────────────────────────►│ & Select │ │
│        │ - sender skip  │                                 │ folder   │ │
│        └───────┬────────┘                                 └────┬─────┘ │
│                │ categories embedded into normalized header      │ move/copy
│                ▼                                                ▼        │
│  ┌───────────────────────────┐                    ┌─────────────────────┐ │
│  │ Database Helper (MySQL)   │◄───────────────────┤ IMAP MOVE/COPY      │ │
│  │ - cache md5/hex/headers   │   upsert/cleanup   │ + UID mapping       │ │
│  │ - considered/version      │                    └─────────────────────┘ │
│  └───────────────────────────┘                                            │
│                                                                       │
│  Daemon/Loop mode → IDLE or poll for new mail                         │
│  RFC5424 logging → file and/or syslog                                 │
└───────────────────────────────────────────────────────────────────────┘

                                │
                                │ (optional policy)
                                ▼
                     ┌──────────────────────────────┐
                     │  Archive / Trash Folders     │
                     │ (time-based retention)       │
                     └──────────────────────────────┘
```

### Processing Flow (TL;DR)
1. **Fetch new mail** from TODO folder (UNSEEN).  
2. **Normalize headers** (stable text; removes noisy bits).  
3. **(Optional) LLM** adds lightweight categories / CTA unless sender is skipped.  
4. **Compute Nilsimsa digest** and **compare** to cached per-folder digests.  
5. **Score folders** (over-threshold only), apply tie-break via raising threshold.  
6. **Move message** to winning folder; record to DB (md5 / hex / categories / message-id).  
7. **Prune** DB entries for UIDs that no longer exist; **Archive** older mail if enabled.  
8. **Wait** via IMAP IDLE or poll, then repeat.
