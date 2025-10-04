import os, time, logging, logging.handlers
from rfc5424_logger import RFC5424Formatter

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
    logger = logging.getLogger(name)
    log_dir = os.path.expanduser(log_dir)
    os.makedirs(log_dir, exist_ok=True)
    log_filename = os.path.join(
        log_dir,
        logfile if logfile else time.strftime('%Y%m%d', time.localtime()) + '.log',
    )
    if not logger.handlers:
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