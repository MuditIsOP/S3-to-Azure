# S3 to Azure Blob Storage Migration Orchestrator

A robust, zero-data-loss orchestrator suite designed to migrate massive datasets from Amazon S3 to Azure Blob Storage using server-to-server copy (via AzCopy) and a MySQL database for transaction state tracking, integrity validation, and final audit reporting.

---

## Features & Migration Lifecycle

1. **Phase 0: Inventory (`inventory.py`)**
   * Pages through the S3 source bucket and catalogs all objects in the tracking database.
   * Classifies objects into statuses: `discovered` (ready to copy) or `needs_review` (skipped folders/anomalies).
2. **Phase 1: Transfer (`transfer.py`)**
   * Triggers and monitors `azcopy` copy using an optimized S2S (Server-to-Server) channel.
   * Passes filter regexes to explicitly exclude folder placeholders and unsafe backslash keys.
   * Tracks transfer progress in the database and logs job stats (supports `--resume` and `--dry-run`).
3. **Phase 2: Independent Verification (`verify.py`)**
   * Audits transfer success.
   * Streams objects from both S3 and Azure Blob in sequential chunks (8 MB) to compute MD5 hashes without exhausting VM memory.
   * Integrates an **ETag Shortcut optimization**: uses the S3 ETag directly as the MD5 source hash for standard (non-multipart) uploads, avoiding S3 download reads while still hashing the Azure Blob side.
4. **Phase 3: Reconciliation & Reporting (`reconcile.py`)**
   * Re-lists S3 objects to check for **freeze violations** (any edits/additions made to the bucket after baseline inventory).
   * Generates a final, publication-ready markdown report with job stats, verification counts, anomaly lists, and a completeness verdict (`COMPLETE`, `COMPLETE_WITH_REVIEW`, or `FAILED`).

---

## Directory Structure

```text
├── .env                       # Local environment variables
├── .env.example               # Template environment configuration
├── config.py                  # Validates and loads environment variables
├── db.py                      # Unified database connection wrapper (SQLite <-> MySQL)
├── init_db.py                 # SQLite database schema initializer
├── preflight.py               # Connectivity, disk space, and freeze validation checks
├── inventory.py               # Paginated listing and cataloging (Phase 0)
├── transfer.py                # AzCopy S2S transfer trigger & polling (Phase 1)
├── verify.py                  # Dual-side MD5 hashing integrity verification (Phase 2)
├── reconcile.py               # Freeze auditing and final markdown reporting (Phase 3)
├── schema_mysql.sql           # MySQL DDL script for database administrator
├── requirements.txt           # Python library dependencies
└── README.md                  # Operational documentation (this file)
```

---

## Configuration (`.env`)

Configure the orchestrator using a `.env` file in the root directory.

```ini
# AWS S3 Source Settings
AWS_ACCESS_KEY_ID=your-aws-access-key-id
AWS_SECRET_ACCESS_KEY=your-aws-secret-access-key
AWS_REGION=ap-south-1
S3_BUCKET_NAME=sasones3

# Azure Storage Destination Settings
AZURE_STORAGE_ACCOUNT=sasonestorage
AZURE_CONTAINER_NAME=sasonemediacontainer
AZURE_SAS_TOKEN=your-container-level-sas-token

# State Store Database Settings
# Set MYSQL_HOST=na to run in local SQLite fallback mode (creates 'migration.db')
MYSQL_HOST=sasoneazdb.mysql.database.azure.com
MYSQL_PORT=3306
MYSQL_USER=Azure_Dev_1
MYSQL_PASSWORD=your-password
MYSQL_DB=SASONE

# Orchestrator Run Configurations
MIGRATION_JOB_NAME=s3-to-azure-prod-final
VERIFY_SAMPLE_OR_FULL=full
REPORT_DIR=./reports
LOG_PATH=./orchestrator.log
```

---

## Deployment & Operation Guide

### Step 1: Install Dependencies
On the target machine (Windows or Linux VM), install the required Python packages:
```bash
pip install -r requirements.txt
```

### Step 2: Database Schema Setup
Your database engineer must execute the DDL queries inside `schema_mysql.sql` against the `SASONE` MySQL database. 
* *Note: The scripts will not alter or run DDL statements against your production MySQL server, ensuring safety.*

### Step 3: Run Pre-Flight Validation
Ensure the network interfaces, AWS/Azure access credentials, local disk space, and MySQL database connection are fully healthy:
```bash
python preflight.py
```
*You will be prompted to type `yes` to confirm that the S3 source bucket is frozen before the validation summary passes.*

### Step 4: Run Inventory Cataloging (Phase 0)
Authoritatively inventory the S3 bucket:
```bash
python inventory.py
```
* **Testing option**: To dry-run list only the first 100 objects, use `python inventory.py --limit-objects 100`.

### Step 5: Execute AzCopy Transfer (Phase 1)
Start the server-to-server transfer:
```bash
python transfer.py
```
* **Dry-Run**: Validate the constructed AzCopy commands without copying files by running `python transfer.py --dry-run`.
* **Resuming**: If the process halts or is aborted, resume the active AzCopy job by running `python transfer.py --resume`.

### Step 6: Verify Content Integrity (Phase 2)
Perform the sequential MD5 validation check:
```bash
python verify.py
```
This logs the specific verification method used (`etag_shortcut` vs. `full_dual_hash`) per object.

### Step 7: Reconcile and Export Report (Phase 3)
Perform the final freeze audit and generate the completeness verdict:
```bash
python reconcile.py
```
The final report will be exported to `./reports/migration_report_[job_uuid].md`.

---

## Operational Anomaly Handling

* **Folder Placeholders**: Virtual directories (zero-byte keys ending with `/`) are skipped by design during transfer. These are allowed to remain under `needs_review` in the database and will **not** block a final `COMPLETE` verdict.
* **Backslash Keys**: Keys containing the backslash character (`\`) are skipped during transfer. They are flagged as `needs_review` and must be inspected/resolved manually. **Any unresolved backslash keys will restrict the final verdict to `COMPLETE_WITH_REVIEW` instead of `COMPLETE`.**
* **Logs**: Detailed execution events are stored in the database table `MigrationEvents` and in the local log file `orchestrator.log`.
