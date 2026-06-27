# Schema Update Instructions for Database Engineer

To support the verification reporting requirements for the S3 → Azure Blob Storage migration, we need to add a tracking column to the `migration_objects` table.

## Change Description
A new column `verification_method` is added to track the validation path used for each object:
* `etag_shortcut` — Verified using the S3 ETag shortcut (fast path for non-multipart S3 objects).
* `full_dual_hash` — Verified by streaming and hashing the bytes of both the S3 source object and the destination Azure Blob (standard path for multipart S3 objects).

This column provides visibility and auditing capabilities for the final completeness report.

---

## SQL Commands

### 1. Update Existing Tables
If the migration schema has already been deployed, please execute the following `ALTER TABLE` statement against the target database:

```sql
-- Add verification_method column to track hashing method
ALTER TABLE migration_objects 
ADD verification_method NVARCHAR(50) NULL;
```

### 2. Verify Schema Definition
The updated table definition for `migration_objects` should appear as follows in [schema.sql](file:///d:/SAS%20One/Migration/ANtigravity/schema.sql):

```sql
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
    verification_method   NVARCHAR(50) NULL, -- <--- NEW COLUMN
    last_error           NVARCHAR(MAX) NULL,
    discovered_at          DATETIME2 NOT NULL,
    verified_at            DATETIME2 NULL,
    PRIMARY KEY (job_id, object_key),
    FOREIGN KEY (job_id) REFERENCES migration_jobs(job_id)
);
```
