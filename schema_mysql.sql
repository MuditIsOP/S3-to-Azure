-- MySQL Migration Tracking Schema (schema_mysql.sql)
-- Run this script against your MySQL/MariaDB database to prepare it for migration state tracking.

DROP TABLE IF EXISTS migration_events;
DROP TABLE IF EXISTS migration_objects;
DROP TABLE IF EXISTS migration_jobs;

-- 1. Create Jobs Tracking Table
CREATE TABLE migration_jobs (
    job_id                  VARCHAR(36) PRIMARY KEY,
    job_name                VARCHAR(200) NOT NULL,
    source_bucket            VARCHAR(500) NOT NULL,
    destination_container     VARCHAR(500) NOT NULL,
    azcopy_job_id              VARCHAR(100) NULL,        -- AzCopy GUID for cross-reference
    status                  VARCHAR(50) NOT NULL,         -- preflight|running|paused|completed|completed_with_review|failed
    source_frozen_confirmed_at DATETIME(6) NULL,
    started_at               DATETIME(6) NOT NULL,
    ended_at                 DATETIME(6) NULL,
    total_objects             BIGINT NOT NULL DEFAULT 0,
    total_bytes               BIGINT NOT NULL DEFAULT 0,
    verified_objects           BIGINT NOT NULL DEFAULT 0,
    verified_bytes             BIGINT NOT NULL DEFAULT 0,
    failed_objects              BIGINT NOT NULL DEFAULT 0,
    needs_review_objects         BIGINT NOT NULL DEFAULT 0,
    config_snapshot            JSON NULL                 -- Recommended JSON data type
);

-- 2. Create Objects Tracking Table
CREATE TABLE migration_objects (
    job_id              VARCHAR(36) NOT NULL,
    object_key          VARCHAR(1024) NOT NULL,
    blob_name           VARCHAR(1024) NOT NULL,
    size_bytes          BIGINT NOT NULL,
    s3_etag             VARCHAR(200) NULL,               -- S3 ETag reference
    s3_last_modified      DATETIME(6) NULL,
    content_type         VARCHAR(255) NULL,
    storage_class          VARCHAR(100) NULL,             -- Glacier / S3 storage class
    azcopy_status          VARCHAR(50) NULL,               -- Transfer status from AzCopy
    independent_source_md5  BINARY(16) NULL,               -- Source MD5 verified independently (exact 16 bytes)
    independent_dest_md5     BINARY(16) NULL,              -- Destination MD5 verified independently (exact 16 bytes)
    status              VARCHAR(50) NOT NULL DEFAULT 'discovered',
                                                          -- discovered|transferred_by_azcopy|verified|failed|needs_review
    verification_method   VARCHAR(50) NULL,               -- etag_shortcut | full_dual_hash
    last_error           TEXT NULL,                        -- Long error details
    discovered_at          DATETIME(6) NOT NULL,
    verified_at            DATETIME(6) NULL,
    PRIMARY KEY (job_id, object_key(255)),                -- MySQL limit on index prefix length for long columns
    CONSTRAINT FK_migration_objects_job FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id) ON DELETE CASCADE
);

-- 3. Create Status Index for fast progress queries
CREATE INDEX ix_migration_objects_status ON migration_objects (job_id, status);

-- 4. Create Migration Audit Log/Events Table
CREATE TABLE migration_events (
    event_id     BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id       VARCHAR(36) NOT NULL,
    object_key    VARCHAR(1024) NULL,
    event_type    VARCHAR(100) NOT NULL,
    event_time    DATETIME(6) NOT NULL,
    details_json  JSON NULL,                               -- Recommended JSON data type
    CONSTRAINT FK_migration_events_job FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id) ON DELETE CASCADE
);
