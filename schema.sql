-- S3 to Azure Blob Storage Migration Schema
-- Run this script against your Azure SQL Database (SASOne) to prepare it for migration state tracking.

IF OBJECT_ID('dbo.migration_events', 'U') IS NOT NULL
    DROP TABLE dbo.migration_events;

IF OBJECT_ID('dbo.migration_objects', 'U') IS NOT NULL
    DROP TABLE dbo.migration_objects;

IF OBJECT_ID('dbo.migration_jobs', 'U') IS NOT NULL
    DROP TABLE dbo.migration_jobs;

CREATE TABLE migration_jobs (
    job_id                  UNIQUEIDENTIFIER PRIMARY KEY,
    job_name                NVARCHAR(200) NOT NULL,
    source_bucket            NVARCHAR(500) NOT NULL,
    destination_container     NVARCHAR(500) NOT NULL,
    azcopy_job_id              NVARCHAR(100) NULL,        -- AzCopy's own job GUID, for cross-reference
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

CREATE TABLE migration_objects (
    job_id              UNIQUEIDENTIFIER NOT NULL,
    object_key          NVARCHAR(1024) NOT NULL,
    blob_name           NVARCHAR(1024) NOT NULL,
    size_bytes          BIGINT NOT NULL,
    s3_etag             NVARCHAR(200) NULL,               -- reference only, never compared cross-cloud as MD5
    s3_last_modified      DATETIME2 NULL,
    content_type         NVARCHAR(255) NULL,
    storage_class          NVARCHAR(100) NULL,             -- flags Glacier/restore-required objects
    azcopy_status          NVARCHAR(50) NULL,               -- as reported by `azcopy jobs show`
    independent_source_md5  VARBINARY(16) NULL,             -- computed by OUR verification pass, not AzCopy's
    independent_dest_md5     VARBINARY(16) NULL,
    status              NVARCHAR(50) NOT NULL DEFAULT 'discovered',
                        -- discovered|transferred_by_azcopy|verified|failed|needs_review
    verification_method   NVARCHAR(50) NULL,               -- etag_shortcut|full_dual_hash
    last_error           NVARCHAR(MAX) NULL,
    discovered_at          DATETIME2 NOT NULL,
    verified_at            DATETIME2 NULL,
    PRIMARY KEY (job_id, object_key),
    FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id)
);

CREATE INDEX ix_migration_objects_status ON migration_objects (job_id, status);

CREATE TABLE migration_events (
    event_id     BIGINT IDENTITY(1,1) PRIMARY KEY,
    job_id       UNIQUEIDENTIFIER NOT NULL,
    object_key    NVARCHAR(1024) NULL,
    event_type    NVARCHAR(100) NOT NULL,
    event_time    DATETIME2 NOT NULL,
    details_json  NVARCHAR(MAX) NULL,
    FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id)
);
