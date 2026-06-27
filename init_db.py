import os
import sys

# Ensure config and db can be imported from local directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

def init_database():
    is_sqlite = (config.MYSQL_HOST.lower() == 'na')
    
    if is_sqlite:
        print("Database Mode: Local SQLite Fallback is active.")
        print("Initializing local SQLite schema ('migration.db')...")
        try:
            conn, _ = db.get_db_connection()
            db.init_sqlite_db(conn)
            conn.close()
            print("[SUCCESS] Local SQLite tables (MigrationJobs, MigrationObjects, MigrationEvents) created successfully!")
        except Exception as e:
            print(f"[ERROR] Failed to initialize SQLite database: {e}")
            sys.exit(1)
    else:
        print("Database Mode: Production MySQL is active.")
        print(f"Target Database: {config.MYSQL_DB} on {config.MYSQL_HOST}")
        print("[NOTICE] DDL execution skipped per user instructions.")
        print("[NOTICE] Table creation is managed remotely by your database administrator.")
        print("[NOTICE] Validating database structure using check scripts instead.")

if __name__ == "__main__":
    init_database()
