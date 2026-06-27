import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if it exists
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# List of required general environment variables
REQUIRED_VARS = [
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION",
    "S3_BUCKET_NAME",
    "AZURE_STORAGE_ACCOUNT",
    "AZURE_CONTAINER_NAME",
    "AZURE_SAS_TOKEN",
    "MIGRATION_JOB_NAME"
]

def check_config():
    """Validates that all required environment variables are set and non-empty.
    Raises ValueError if any configurations are missing.
    """
    missing_vars = []
    for var in REQUIRED_VARS:
        val = os.getenv(var)
        if not val or val.strip() == "":
            missing_vars.append(var)
            
    # Check MySQL specific requirements if MYSQL_HOST is active
    mysql_host = os.getenv("MYSQL_HOST", "na")
    if mysql_host.lower() != "na":
        mysql_reqs = ["MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB"]
        for var in mysql_reqs:
            val = os.getenv(var)
            if not val or val.strip() == "":
                missing_vars.append(var)
                
    if missing_vars:
        error_msg = (
            f"Configuration Error: The following environment variables are missing or empty: "
            f"{', '.join(missing_vars)}. Please check your .env file or environment."
        )
        raise ValueError(error_msg)

# Perform configuration validation
check_config()

# Config variables exposed for external use
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

AZURE_STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT")
AZURE_CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME")
AZURE_SAS_TOKEN = os.getenv("AZURE_SAS_TOKEN")

# MySQL config variables
MYSQL_HOST = os.getenv("MYSQL_HOST", "na")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DB = os.getenv("MYSQL_DB")

MIGRATION_JOB_NAME = os.getenv("MIGRATION_JOB_NAME")

# Optional/Defaulted variables
VERIFY_SAMPLE_OR_FULL = os.getenv("VERIFY_SAMPLE_OR_FULL", "full").lower()
VERIFY_SAMPLE_PERCENT = int(os.getenv("VERIFY_SAMPLE_PERCENT", "10"))
REPORT_DIR = os.getenv("REPORT_DIR", "./reports")
LOG_PATH = os.getenv("LOG_PATH", "./orchestrator.log")

# Build target paths
Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

