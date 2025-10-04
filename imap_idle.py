import select
import imaplib
import logging

def supports_idle(imap: imaplib.IMAP4_SSL, logger: logging.Logger | None = None) -> bool:
    try:
        typ, data = imap.capability()
        return typ == "OK" and data and (b"IDLE" in b" ".join(data).upper())
    except Exception as e:
        if logger:
            logger.warning("Error checking IMAP capabilities: %s", e)
    return False

def idle_wait(imap: imaplib.IMAP4_SSL, folder: str, timeout: int = 900, logger: logging.Logger | None = None) -> bool:
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
        if logger:
            logger.warning("IMAP IDLE failed: %s", e)
        return False