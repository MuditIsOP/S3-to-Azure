import os
import sys
import sqlite3
import logging

logger = logging.getLogger("orchestrator.db")

# Import config safely
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config

class MySQLCompatibleCursor:
    """Cursor wrapper that automatically converts SQL parameter placeholders
    from standard '?' (SQLite/SQL Server style) to '%s' (MySQL/PyMySQL style)
    when running against MySQL.
    """
    def __init__(self, cursor, is_mysql):
        self.cursor = cursor
        self.is_mysql = is_mysql

    def execute(self, sql, params=None):
        if self.is_mysql and params is not None:
            # Replace '?' with '%s'. Since our codebase only uses '?' for
            # parameter placeholders and never in literal strings, this is safe.
            sql = sql.replace('?', '%s')
        
        if params is None:
            return self.cursor.execute(sql)
        else:
            return self.cursor.execute(sql, params)

    def executemany(self, sql, seq_of_parameters=None):
        if self.is_mysql and seq_of_parameters:
            sql = sql.replace('?', '%s')
        
        if seq_of_parameters is None:
            return self.cursor.executemany(sql, [])
        else:
            return self.cursor.executemany(sql, seq_of_parameters)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def fetchmany(self, size):
        return self.cursor.fetchmany(size)

    @property
    def lastrowid(self):
        return self.cursor.lastrowid

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def __setattr__(self, name, value):
        # Gracefully swallow 'fast_executemany' setting for MySQL cursors
        if name == 'fast_executemany' and self.is_mysql:
            return
        super().__setattr__(name, value)

    def __getattr__(self, name):
        # Proxy all other attributes to the underlying cursor
        return getattr(self.cursor, name)

    def close(self):
        self.cursor.close()


class MySQLCompatibleConnection:
    """Connection wrapper that returns wrapped cursors."""
    def __init__(self, conn, is_mysql):
        self.conn = conn
        self.is_mysql = is_mysql

    def cursor(self):
        return MySQLCompatibleCursor(self.conn.cursor(), self.is_mysql)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __getattr__(self, name):
        # Proxy other attributes to the underlying connection
        return getattr(self.conn, name)


def get_db_connection():
    """Returns a unified database connection object based on current configuration.
    Returns (conn, is_sqlite)
    """
    is_sqlite = (config.MYSQL_HOST.lower() == 'na')
    
    if is_sqlite:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migration.db")
        conn = sqlite3.connect(db_path)
        # Enable foreign keys in SQLite
        conn.execute("PRAGMA foreign_keys = ON;")
        return MySQLCompatibleConnection(conn, is_mysql=False), True
    else:
        # Import pymysql lazily so local run doesn't strict-fail if it's missing (though it should be installed)
        import pymysql
        conn = pymysql.connect(
            host=config.MYSQL_HOST,
            port=int(config.MYSQL_PORT),
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DB,
            charset='utf8mb4'
        )
        return MySQLCompatibleConnection(conn, is_mysql=True), False


def init_sqlite_db(conn):
    """Initializes the local SQLite database schema matching the MySQL target schema."""
    cursor = conn.cursor()
    
    # 1. MigrationJobs Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS MigrationJobs (
        Id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        MigrationJobUUID        TEXT NOT NULL UNIQUE,
        JobName                 TEXT NOT NULL,
        SourceBucket            TEXT NOT NULL,
        DestinationContainer     TEXT NOT NULL,
        AzCopyJobId              TEXT NULL,
        Status                  TEXT NOT NULL,
        SourceFrozenConfirmedAt TEXT NULL,
        StartedAt               TEXT NOT NULL,
        EndedAt                 TEXT NULL,
        TotalObjects             INTEGER NOT NULL DEFAULT 0,
        TotalBytes               INTEGER NOT NULL DEFAULT 0,
        VerifiedObjects           INTEGER NOT NULL DEFAULT 0,
        VerifiedBytes             INTEGER NOT NULL DEFAULT 0,
        FailedObjects              INTEGER NOT NULL DEFAULT 0,
        NeedsReviewObjects         INTEGER NOT NULL DEFAULT 0,
        ConfigSnapshot            TEXT NULL,
        IsDeleted                 INTEGER NOT NULL DEFAULT 0,
        CreatedDate               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UpdatedDate               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. MigrationObjects Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS MigrationObjects (
        Id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        MigrationObjectsUUID    TEXT NOT NULL UNIQUE,
        JobId                   INTEGER NOT NULL,
        ObjectKey               TEXT NOT NULL,
        BlobName                TEXT NOT NULL,
        SizeBytes               INTEGER NOT NULL,
        S3ETag                  TEXT NULL,
        S3LastModified          TEXT NULL,
        ContentType             TEXT NULL,
        StorageClass            TEXT NULL,
        AzCopyStatus            TEXT NULL,
        IndependentSourceMD5    BLOB NULL,
        IndependentDestinationMD5 BLOB NULL,
        Status                  TEXT NOT NULL DEFAULT 'discovered',
        VerificationMethod      TEXT NULL,
        LastError               TEXT NULL,
        DiscoveredAt            TEXT NOT NULL,
        VerifiedAt              TEXT NULL,
        IsDeleted               INTEGER NOT NULL DEFAULT 0,
        CreatedDate             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UpdatedDate             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (JobId) REFERENCES MigrationJobs(Id) ON DELETE CASCADE
    );
    """)

    # 3. Status Index
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS IDX_MigrationObjects_Status ON MigrationObjects (JobId, Status);
    """)

    # 4. MigrationEvents Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS MigrationEvents (
        Id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        MigrationEventsUUID     TEXT NOT NULL UNIQUE,
        JobId                   INTEGER NOT NULL,
        ObjectKey               TEXT NULL,
        EventType               TEXT NOT NULL,
        EventTime               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        DetailsJson             TEXT NULL,
        IsDeleted               INTEGER NOT NULL DEFAULT 0,
        CreatedDate             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UpdatedDate             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (JobId) REFERENCES MigrationJobs(Id) ON DELETE CASCADE
    );
    """)
    
    conn.commit()

