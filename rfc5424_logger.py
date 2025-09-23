import logging, logging.handlers, socket, os, re
from datetime import datetime

class RFC5424Formatter(logging.Formatter):
    """RFC 5424 formatter with newline folding to keep entries single-line."""
    _SEVERITY = {
        logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4,
        logging.INFO: 6, logging.DEBUG: 7, logging.NOTSET: 7
    }

    def __init__(self, app_name="imap_nilsimsa", hostname=None, facility=1, structured_data="-"):
        super().__init__(fmt="%(message)s")
        self.app_name = app_name
        self.hostname = hostname or socket.gethostname()
        self.facility = int(facility)
        self.structured_data = structured_data

    @staticmethod
    def _fold(msg: str) -> str:
        # collapse CR/LF runs so receivers won’t split entries
        return re.sub(r"[\r\n]+", "\\n", msg).strip()

    def format(self, record: logging.LogRecord) -> str:
        version = "1"
        ts = datetime.utcfromtimestamp(record.created).isoformat(timespec="milliseconds") + "Z"
        host = self.hostname
        app  = self.app_name
        proc = str(os.getpid())

        # Auto msgid: Class->method from logger hierarchy; fallback to module->function
        _msgid = getattr(record, "msgid", None)
        if not _msgid or _msgid == "-":
            cls = record.name.split(".")[-1] if record.name else (getattr(record, "module", "-") or "-")
            func = getattr(record, "funcName", "-")
            _msgid = f"{cls}->{func}"
        msgid = _msgid
        sd    = getattr(record, "structured_data", self.structured_data) or "-"
        if sd == "-":
            sd = f'[meta@32473 file="{getattr(record,"filename","-")}" line="{getattr(record,"lineno","-")}" pid="{os.getpid()}"]'

        sev   = self._SEVERITY.get(record.levelno, 7)
        pri   = self.facility * 8 + sev
        msg   = self._fold(super().format(record))
        return f"<{pri}>{version} {ts} {host} {app} {proc} {msgid} {sd} {msg}"

def get_logger(name="imap_nilsimsa",
               level=logging.INFO,
               log_path=None,
               app_name="imap_nilsimsa",
               facility=1,
               enable_syslog=False,
               syslog_address="/dev/log"):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    fmt = RFC5424Formatter(app_name=app_name, facility=facility)

    if log_path:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    else:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    if enable_syslog:
        sh = logging.handlers.SysLogHandler(address=syslog_address)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger

