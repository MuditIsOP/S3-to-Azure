# Migration Tracking Schema & Permissions

## 1. SQL Schema DDL

```sql
-- 1. Create Jobs Tracking Table
CREATE TABLE migration_jobs (
    job_id                  UNIQUEIDENTIFIER PRIMARY KEY,
    job_name                NVARCHAR(200) NOT NULL,
    source_bucket            NVARCHAR(500) NOT NULL,
    destination_container     NVARCHAR(500) NOT NULL,
    azcopy_job_id              NVARCHAR(100) NULL,        
    status                  NVARCHAR(50) NOT NULL,        
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
    s3_etag             NVARCHAR(200) NULL,               
    s3_last_modified      DATETIME2 NULL,
    content_type         NVARCHAR(255) NULL,
    storage_class          NVARCHAR(100) NULL,             
    azcopy_status          NVARCHAR(50) NULL,               
    independent_source_md5  VARBINARY(16) NULL,             
    independent_dest_md5     VARBINARY(16) NULL,            
    status              NVARCHAR(50) NOT NULL DEFAULT 'discovered',
    verification_method   NVARCHAR(50) NULL,               
    last_error           NVARCHAR(MAX) NULL,
    discovered_at          DATETIME2 NOT NULL,
    verified_at            DATETIME2 NULL,
    PRIMARY KEY (job_id, object_key),
    CONSTRAINT FK_migration_objects_job FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id)
);

-- 3. Create Status Index
CREATE INDEX ix_migration_objects_status ON migration_objects (job_id, status);

-- 4. Create Audit Log Table
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

## 2. Database Permissions

Configure a dedicated SQL Database user for the migration runner script with:
* `db_datareader` (SELECT access)
* `db_datawriter` (INSERT/UPDATE/DELETE access)

*(Ensure the database is located in the target region and whitelists the Azure VM IP / allows Azure resources to connect).*
