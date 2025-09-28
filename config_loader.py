f"""
Refactor rules
   - Strictly keep functionality identical,
   - reduce duplication/lines.
   - maintrin code clarify modifying/adding/deleting comments to aid in maintainability.
   - remove overkill error handling.
"""
#!/usr/bin/env python3
import configparser
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

@dataclass(frozen=True)
class DBConfig:
    backend: str
    host: str
    user: str
    password: str
    name: str
    autocommit: bool

@dataclass(frozen=True)
class AppConfig:
    version: str
    maintenance: bool
    reconsider_after: int
    log_dir: str
    logfile: Optional[str]
    todo_folder: str
    new_folder: str
    imap_folders: List[str]
    imap_server: str
    imap_username: str
    imap_password: str
    openai_api_key: Optional[str]
    sender_skip_llm: List[str]
    threshold: int
    min_score: int
    min_average: float
    weight_headers: List[str]
    headers_skip: List[str]
    weight_headers_by: int
    xinclude: List[str]
    archive_folder: Optional[str]
    archive_after: int
    trash_folder: Optional[str]
    just_delete: Optional[List[str]]
    enable_syslog: bool = False
    syslog_address: str = "/dev/log"
    db: DBConfig

def _cfg_list(cfg: configparser.ConfigParser, section: str, key: str) -> List[str]:
    if not cfg.has_option(section, key):
        return []
    return [x.strip() for x in cfg.get(section, key).split(',') if x.strip()]

def resolve_config_path(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    search_order = [
        lambda: Path.home() / ".config/imap_nilsimsa/imap_nilsimsa.ini",
        lambda: "imap_nilsimsa.ini",
        lambda: "/etc/imap_nilsimsa.ini",
    ]
    for f in (f() for f in search_order):
        if f and Path(f).is_file():
            return str(f)
    raise SystemExit("No config file found.")

def load_app_config(path: str) -> AppConfig:
    cfg = configparser.ConfigParser()
    cfg.read(path)
    log_dir = (cfg.get("general", "log_dir", fallback=None)
               or cfg.get("general", "logdir", fallback=None)
               or cfg.get("general", "logpath", fallback="logs")).strip().rstrip("/")

    backend = cfg.get("database", "db_backend", fallback="mysql").lower()
    if backend == "sqlite":
        db = DBConfig(
            backend="sqlite",
            host="localhost",
            user="",
            password="",
            name=cfg.get("sqlite", "db", fallback="var/lib/imap_autosort/imap_autosort.sqlite"),
            autocommit=cfg.getboolean("sqlite", "autocommit", fallback=True),
        )
    else:
        db = DBConfig(
            backend="mysql",
            host=cfg.get("mysql", "host", fallback="localhost"),
            user=(cfg.get("mysql", "user", fallback="imap_nilsimsa") or "imap_nilsimsa").strip(),
            password=(cfg.get("mysql", "password", fallback="") or "").strip(),
            name=cfg.get("mysql", "db", fallback="imap_nilsimsa"),
            autocommit=cfg.getboolean("database", "autocommit", fallback=True),
        )

    return AppConfig(
        version=cfg.get("general", "version", fallback="1.2.0b"),
        maintenance=cfg.getboolean("general", "maintenance", fallback=False),
        reconsider_after=cfg.getint("general", "reconsider_after", fallback=3600),
        log_dir=log_dir,
        logfile=cfg.get("general", "logfile", fallback=None),
        enable_syslog=cfg.getboolean("general", "enable_syslog", fallback=False),
        syslog_address=cfg.get("general", "syslog_address", fallback="/dev/log"),
        todo_folder=cfg.get("imap", "todo"),
        new_folder=cfg.get("imap", "new"),
        imap_folders=_cfg_list(cfg, "imap", "folders"),
        imap_server=cfg.get("imap", "server"),
        imap_username=cfg.get("imap", "username"),
        imap_password=cfg.get("imap", "password"),
        openai_api_key=(cfg.get("openai", "api_key", fallback=None) or "").strip() or None,
        sender_skip_llm=_cfg_list(cfg, "openai", "sender_skip_llm"),
        threshold=cfg.getint("nilsimsa", "threshold", fallback=50),
        min_score=cfg.getint("nilsimsa", "min_score", fallback=100),
        min_average=cfg.getfloat("nilsimsa", "min_average", fallback=0.0),
        weight_headers=_cfg_list(cfg, "nilsimsa", "weight_headers"),
        headers_skip=_cfg_list(cfg, "nilsimsa", "headers_skip"),
        weight_headers_by=cfg.getint("nilsimsa", "weight_headers_by", fallback=1),
        xinclude=_cfg_list(cfg, "nilsimsa", "xinclude"),
        archive_folder=cfg.get("archive", "folder", fallback=None),
        archive_after=cfg.getint("archive", "after", fallback=0),
        trash_folder=cfg.get("archive", "trash", fallback=None),
        just_delete=_cfg_list(cfg, "archive", "justdelete") or None,
        db=db,
    )
