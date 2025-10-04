import imaplib
import configparser

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
        for op in (lambda: self.imap.close(), lambda: self.imap.logout()):  # type: ignore[union-attr]
            try:
                op()
            except Exception:
                pass
        self.imap = None