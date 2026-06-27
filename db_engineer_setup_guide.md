# S3 to Azure Blob Storage Migration — Azure SQL Database Setup Guide

Please use this guide to provision the tracking database and configure the necessary tables.

## 1. Database Provisioning Guidelines
* **Type**: Azure SQL Database (Single Database).
* **Region**: **Must be in the same Azure region** as the target Azure Storage Account and the Orchestrator Azure VM (this minimizes query latency for per-object logging).
* **Pricing Tier**: A basic general-purpose tier (e.g., 2 vCores or Standard S1/S2 DTU) is sufficient for tracking 177K objects.

---

## 2. Table Creation Script
Execute the following SQL script to create the tracking tables, foreign keys, and indexes:

```sql
-- =====================================================================
-- S3 -> Azure Blob Storage Migration State Store Schema
-- =====================================================================

-- 1. Create Jobs Tracking Table
CREATE TABLE migration_jobs (
    job_id                  UNIQUEIDENTIFIER PRIMARY KEY,
    job_name                NVARCHAR(200) NOT NULL,
    source_bucket            NVARCHAR(500) NOT NULL,
    destination_container     NVARCHAR(500) NOT NULL,
    azcopy_job_id              NVARCHAR(100) NULL,        -- AzCopy GUID for cross-reference
    status                  NVARCHAR(50) NOT NULL,        -- preflight|running|paused|completed|completed_with_review|failed
    source_frozen_confirmed_at DATETIME2 NULL,
    started_at               DATETIME2 NOT NULL,
    ended_at                 DATETIME2 NULL,
    total_objects             BIGINT NOT NULL DEFAULT 0,
    total_bytes               BIGINT NOT NULL DEFAULT 0,
    verified_objects           BIGINT NOT NULL DEFAULT 0,
    verified_bytes             BIGINT NOT NULL DEFAULT 0,
    failed_objects              BIGINT NOT NULL DEFAULT 0,
    needs_review_objects         BIGINT NOT NULL DEFAULT 0,
    config_snapshot            NVARCHAR(MAX) NULL
);

-- 2. Create Objects Tracking Table
CREATE TABLE migration_objects (
    job_id              UNIQUEIDENTIFIER NOT NULL,
    object_key          NVARCHAR(1024) NOT NULL,
    blob_name           NVARCHAR(1024) NOT NULL,
    size_bytes          BIGINT NOT NULL,
    s3_etag             NVARCHAR(200) NULL,               -- S3 ETag reference
    s3_last_modified      DATETIME2 NULL,
    content_type         NVARCHAR(255) NULL,
    storage_class          NVARCHAR(100) NULL,             -- Glacier / S3 storage class
    azcopy_status          NVARCHAR(50) NULL,               -- Transfer status from AzCopy
    independent_source_md5  VARBINARY(16) NULL,             -- Source MD5 verified independently
    independent_dest_md5     VARBINARY(16) NULL,            -- Destination MD5 verified independently
    status              NVARCHAR(50) NOT NULL DEFAULT 'discovered',
                                                          -- discovered|transferred_by_azcopy|verified|failed|needs_review
    verification_method   NVARCHAR(50) NULL,               -- etag_shortcut | full_dual_hash
    last_error           NVARCHAR(MAX) NULL,
    discovered_at          DATETIME2 NOT NULL,
    verified_at            DATETIME2 NULL,
    PRIMARY KEY (job_id, object_key),
    CONSTRAINT FK_migration_objects_job FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id)
);

-- 3. Create Status Index for fast progress queries
CREATE INDEX ix_migration_objects_status ON migration_objects (job_id, status);

-- 4. Create Migration Audit Log/Events Table
CREATE TABLE migration_events (
    event_id     BIGINT IDENTITY(1,1) PRIMARY KEY,
    job_id       UNIQUEIDENTIFIER NOT NULL,
    object_key    NVARCHAR(1024) NULL,
    event_type    NVARCHAR(100) NOT NULL,
    event_time    DATETIME2 NOT NULL,
    details_json  NVARCHAR(MAX) NULL,
    CONSTRAINT FK_migration_events_job FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id)
);
```

---

## 3. Security & Access Management
* **Credentials**: Please create a dedicated SQL login/user for the migration script rather than using the server admin credentials.
* **Roles**: The migration user requires `db_datareader` and `db_datawriter` permissions on this database.
* **Firewall Rules**:
  * Ensure the database firewall settings have **"Allow Azure services and resources to access this server"** enabled. This is required for the Azure VM to connect.
  * If the migration operator needs to monitor progress from local workstations, please whitelist their specific public IP addresses.

---

## 4. Connection String Template
Once set up, share the connection string using this template:
`Driver={ODBC Driver 18 for SQL Server};Server=tcp:<your-server-name>.database.windows.net,1433;Database=<database-name>;Uid=<migration-username>;Pwd=<password>;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;`
