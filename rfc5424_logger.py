import logging
from datetime import datetime

class RFC5424Formatter(logging.Formatter):
    def __init__(self, app_name: str = "imap_nilsimsa", facility: int = 1):
        super().__init__()
        self.app_name = app_name
        self.facility = facility  # RFC5424 facility code (e.g., 1=user)

    def format(self, record: logging.LogRecord) -> str:
        # PRI = facility*8 + severity (map Python level to 0–7; default INFO=6)
        sev = min(max(record.levelno // 10, 0), 7)
        pri = self.facility * 8 + sev
        ts = datetime.utcfromtimestamp(record.created).isoformat(timespec="milliseconds") + "Z"
        hostname = getattr(record, "hostname", "-") or "-"
        msg = record.getMessage()
        return f"<{pri}>1 {ts} {hostname} {self.app_name} {record.process} - - {msg}"

