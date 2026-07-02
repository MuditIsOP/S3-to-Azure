import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if available
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()

# MySQL config variables for tracking database host
MYSQL_HOST = os.getenv("MYSQL_HOST") or "sasoneazdb.mysql.database.azure.com"
MYSQL_PORT = os.getenv("MYSQL_PORT") or "3306"
MYSQL_USER = os.getenv("MYSQL_USER") or "sasdbadmin"
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD") or "TjlAdks0JHpUYQ=="
MYSQL_DB = "SASONE" # Fixed central tracking database

# Queue table name
REWRITE_QUEUE_TABLE = "AzureUrlRewriteQueue1"

# AWS S3 Source Settings
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sasones3")

# Azure Storage Destination Settings
AZURE_STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT", "sasonestorage")
AZURE_CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME", "sasonemediacontainer")
AZURE_SAS_TOKEN = os.getenv("AZURE_SAS_TOKEN")

MIGRATION_JOB_NAME = os.getenv("MIGRATION_JOB_NAME", "s3-to-azure-prod-final")

REPORT_DIR = os.getenv("REPORT_DIR", "./reports")
LOG_PATH = os.getenv("LOG_PATH", "./orchestrator.log")
VERIFY_SAMPLE_OR_FULL = os.getenv("VERIFY_SAMPLE_OR_FULL", "full").lower()
VERIFY_SAMPLE_PERCENT = int(os.getenv("VERIFY_SAMPLE_PERCENT", "10"))

Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
