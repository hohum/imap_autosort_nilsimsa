# db_helper.py
import sys
import mysql.connector

class DatabaseHelper:
    def __init__(self, mysql_pass, version, logger,
                 host="localhost", user="imap_nilsimsa", db="imap_nilsimsa", autocommit=True):
        self.logger = logger
        self.version = version
        try:
            self.conn = mysql.connector.connect(
                host=host, user=user, passwd=mysql_pass, db=db, autocommit=autocommit
            )
            self.cursor = self.conn.cursor(buffered=True)
            self._init_schema()
        except mysql.connector.Error as e:
            self.logger.error("Database connection error: %s", e)
            sys.exit("Database connection failed.")

    def execute(self, *args, **kwargs):
        return self.cursor.execute(*args, **kwargs)

    def fetchall(self, *args, **kwargs):
        # supports both: rows = db.fetchall("SELECT ...", params)
        # and: db.execute("SELECT ...", params); rows = db.fetchall()
        if args or kwargs:
            self.cursor.execute(*args, **kwargs)
        return self.cursor.fetchall()

    def close(self):
        try: self.cursor.close()
        finally:
            try: self.conn.close()
            except Exception: pass

    def _init_schema(self):
        try:
            self.cursor.execute(
                'CREATE TABLE IF NOT EXISTS nilsimsa ('
                'id INTEGER PRIMARY KEY AUTO_INCREMENT, '
                'added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                'uid INTEGER, folder TEXT, hexdigest TEXT, md5sum TEXT, trimmed_header TEXT)'
            )
            self.cursor.execute('CREATE TABLE IF NOT EXISTS considered (uid INTEGER, considered_when INTEGER)')
            self.cursor.execute('CREATE TABLE IF NOT EXISTS version (version TEXT)')
            self.cursor.execute('SELECT version FROM version LIMIT 1')
            row = self.cursor.fetchone()
            db_version = row[0] if row else None
            if db_version != self.version:
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
            self.logger.error("Database bootstrap error: %s", e)
            sys.exit("Database connection failed.")
