import os
import sys
import json
import datetime
import argparse
import logging
import uuid
import boto3

# Ensure config and db can be imported from local directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

# Set up logging
logger = logging.getLogger("reconcile")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    try:
        fh = logging.FileHandler(config.LOG_PATH, mode='a', encoding='utf-8')
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception as e:
        print(f"Warning: Could not set up file log handler at {config.LOG_PATH}: {e}", file=sys.stderr)

def log_event(conn, db_job_id, event_type, details_dict, object_key=None):
    """Logs migration events to the database and local logger."""
    details_json = json.dumps(details_dict, default=str)
    logger.info(f"Event [{event_type}] | Details: {details_json}")
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO MigrationEvents (MigrationEventsUUID, JobId, ObjectKey, EventType, EventTime, DetailsJson)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), db_job_id, object_key, event_type, datetime.datetime.utcnow(), details_json))
        conn.commit()
    except Exception as e:
        logger.error(f"CRITICAL: Failed to write event to DB: {e}")

def get_active_job(conn):
    """Retrieves the most recent running or paused job from the DB."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT Id, MigrationJobUUID, JobName, SourceBucket, DestinationContainer, AzCopyJobId, Status, StartedAt, TotalObjects, TotalBytes 
        FROM MigrationJobs 
        ORDER BY StartedAt DESC
    """)
    row = cursor.fetchone()
    if row:
        return {
            "db_job_id": row[0],
            "job_uuid": row[1],
            "job_name": row[2],
            "source_bucket": row[3],
            "destination_container": row[4],
            "azcopy_job_id": row[5],
            "status": row[6],
            "started_at": row[7],
            "total_objects": row[8],
            "total_bytes": row[9]
        }
    return None

def check_freeze_violations(source_bucket, db_job_id, conn):
    """Checks S3 bucket for changes since Phase 0 inventory."""
    logger.info("Performing final freeze check against S3...")
    violations = []
    
    try:
        cursor = conn.cursor()
        # Optimize: Fetch all cataloged objects into memory to avoid per-object DB roundtrips
        logger.info("Loading cataloged object metadata from database for freeze validation...")
        cursor.execute("""
            SELECT ObjectKey, SizeBytes FROM MigrationObjects 
            WHERE JobId = ?
        """, (db_job_id,))
        cataloged_objects = {row[0]: row[1] for row in cursor.fetchall()}
        logger.info(f"Loaded {len(cataloged_objects)} objects from database state store.")

        s3 = boto3.client(
            's3',
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION
        )
        paginator = s3.get_paginator('list_objects_v2')
        page_iterator = paginator.paginate(Bucket=source_bucket)
        
        for page in page_iterator:
            if 'Contents' not in page:
                break
                
            for obj in page['Contents']:
                key = obj['Key']
                size = obj['Size']
                
                # S3 folder placeholders are excluded by design and might not be cataloged,
                # so skip warning about them if they are virtual directories.
                if key.endswith('/') and size == 0:
                    continue
                
                if key not in cataloged_objects:
                    violations.append(f"[NEW OBJECT] Key: {key} (added after freeze)")
                elif cataloged_objects[key] != size:
                    violations.append(f"[MODIFIED OBJECT] Key: {key} (size changed from {cataloged_objects[key]} to {size})")
                    
    except Exception as e:
        logger.error(f"Failed to perform freeze check against S3: {e}")
        violations.append(f"[CHECK FAILED] Error: {e}")
        
    return violations

def run_reconciliation():
    logger.info("Starting Phase 3 Reconciliation and Reporting...")
    
    try:
        conn, is_sqlite = db.get_db_connection()
    except Exception as e:
        logger.critical(f"Failed to connect to Database: {e}")
        sys.exit(1)
        
    active_job = get_active_job(conn)
    
    if not active_job:
        logger.error("No active job found in the database. Run inventory first.")
        conn.close()
        sys.exit(1)
        
    db_job_id = active_job["db_job_id"]
    job_uuid = active_job["job_uuid"]
    job_name = active_job["job_name"]
    started_at = active_job["started_at"]
    
    # 1. Fetch Object Status Counts
    cursor = conn.cursor()
    cursor.execute("""
        SELECT Status, COUNT(*), SUM(SizeBytes) 
        FROM MigrationObjects 
        WHERE JobId = ? 
        GROUP BY Status
    """, (db_job_id,))
    
    status_rows = cursor.fetchall()
    status_counts = {row[0]: row[1] for row in status_rows}
    status_sizes = {row[0]: row[2] or 0 for row in status_rows}
    
    discovered_count = status_counts.get("discovered", 0)
    verified_count = status_counts.get("verified", 0)
    failed_count = status_counts.get("failed", 0)
    needs_review_count = status_counts.get("needs_review", 0)
    
    discovered_size = status_sizes.get("discovered", 0)
    verified_size = status_sizes.get("verified", 0)
    failed_size = status_sizes.get("failed", 0)
    needs_review_size = status_sizes.get("needs_review", 0)
    
    # 2. Fetch Verification Methods Breakdown
    cursor.execute("""
        SELECT VerificationMethod, COUNT(*) 
        FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'verified' 
        GROUP BY VerificationMethod
    """, (db_job_id,))
    
    method_rows = cursor.fetchall()
    methods = {row[0]: row[1] for row in method_rows}
    
    etag_shortcut_count = methods.get("etag_shortcut", 0)
    full_dual_hash_count = methods.get("full_dual_hash", 0)
    
    # 3. Check for specific unresolved review items
    # Check folder placeholders (these are allowed to be skipped and remain in needs_review)
    cursor.execute("""
        SELECT COUNT(*) FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'needs_review' AND LastError LIKE ?
    """, (db_job_id, '%folder placeholder%'))
    folder_placeholders_count = cursor.fetchone()[0]
    
    # Check backslash keys (these must be resolved!)
    cursor.execute("""
        SELECT ObjectKey, LastError FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'needs_review' AND LastError LIKE ?
    """, (db_job_id, '%backslash%'))
    backslash_keys = cursor.fetchall()
    backslash_count = len(backslash_keys)
    
    # Any other needs_review anomalies
    unresolved_review_count = needs_review_count - folder_placeholders_count
    
    # 4. Check for freeze violations
    violations = check_freeze_violations(active_job["source_bucket"], db_job_id, conn)
    has_violations = len(violations) > 0
    
    # 5. Determine Completeness Verdict
    # Verdict rules:
    # COMPLETE: 100% of files verified, 0 failures, 0 unresolved needs_review (excluding folder placeholders), 0 freeze violations
    # COMPLETE_WITH_REVIEW: All files accounted for, but there are unresolved items in needs_review (like backslashes) or failed items
    # FAILED: Size/MD5 mismatches (failed objects > 0) or S3 freeze violations.
    
    if failed_count > 0 or has_violations:
        verdict = "FAILED"
        job_status = "failed"
    elif unresolved_review_count > 0 or discovered_count > 0:
        verdict = "COMPLETE_WITH_REVIEW"
        job_status = "completed_with_review"
    else:
        verdict = "COMPLETE"
        job_status = "completed"
        
    ended_at = datetime.datetime.utcnow()
    # Normalize datetime format
    if isinstance(started_at, str):
        # SQLITE stores dates as string sometimes
        try:
            started_at = datetime.datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            started_at = datetime.datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            
    duration = ended_at - started_at if isinstance(started_at, datetime.datetime) else datetime.timedelta(0)
    
    # 6. Generate Markdown Report
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    report_filename = f"migration_report_{job_uuid}.md"
    report_path = os.path.join(config.REPORT_DIR, report_filename)
    
    # Fetch failures list for report
    cursor.execute("""
        SELECT ObjectKey, SizeBytes, LastError 
        FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'failed'
    """, (db_job_id,))
    failed_objects = cursor.fetchall()
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"# S3 → Azure Blob Storage Migration Final Report\n\n")
        f.write(f"## Executive Summary\n\n")
        f.write(f"* **Job Name**: `{job_name}`\n")
        f.write(f"* **Job UUID**: `{job_uuid}`\n")
        f.write(f"* **Database Job ID**: `{db_job_id}`\n")
        f.write(f"* **AzCopy Job ID**: `{active_job['azcopy_job_id']}`\n")
        f.write(f"* **Start Time**: `{started_at}`\n")
        f.write(f"* **End Time**: `{ended_at}`\n")
        f.write(f"* **Duration**: `{duration}`\n")
        f.write(f"* **Completeness Verdict**: **`{verdict}`**\n\n")
        
        f.write(f"## Reconciliation Table\n\n")
        f.write(f"| Object Status | File Count | Total Bytes | Percentage |\n")
        f.write(f"|---|---|---|---|\n")
        total_job_objects = active_job['total_objects'] if active_job['total_objects'] > 0 else 1
        f.write(f"| **Verified (Success)** | {verified_count:,} | {verified_size:,} | {verified_count / total_job_objects * 100:.2f}% |\n")
        f.write(f"| **Failed (Mismatches)** | {failed_count:,} | {failed_size:,} | {failed_count / total_job_objects * 100:.2f}% |\n")
        f.write(f"| **Needs Review (Anomalies)** | {needs_review_count:,} | {needs_review_size:,} | {needs_review_count / total_job_objects * 100:.2f}% |\n")
        f.write(f"| **Unprocessed (Discovered)** | {discovered_count:,} | {discovered_size:,} | {discovered_count / total_job_objects * 100:.2f}% |\n")
        f.write(f"| **Total Discovered** | **{active_job['total_objects']:,}** | **{active_job['total_bytes']:,}** | **100.00%** |\n\n")
        
        f.write(f"## Verification Method Audit\n\n")
        f.write(f"* **Verified via S3 ETag Shortcut (`etag_shortcut`)**: {etag_shortcut_count:,} objects\n")
        f.write(f"* **Verified via Dual-Side Hashing (`full_dual_hash`)**: {full_dual_hash_count:,} objects\n\n")
        
        f.write(f"## Anomalies & Issues Audit\n\n")
        
        f.write(f"### S3 Folder Placeholders (Excluded by Design)\n")
        f.write(f"* **Skipped Count**: {folder_placeholders_count:,} folder placeholders\n")
        f.write(f"* **Status**: Ignored during copy, excluded from completeness blocks.\n\n")
        
        f.write(f"### Backslash-Containing S3 Keys (Resolution Required)\n")
        f.write(f"* **Flagged Count**: {backslash_count:,} objects\n")
        if backslash_count > 0:
            f.write(f"* **Status**: **UNRESOLVED** (Blocks `COMPLETE` verdict)\n")
            f.write(f"* **Flagged Keys**:\n")
            for idx, key_row in enumerate(backslash_keys):
                f.write(f"  {idx+1}. `{key_row[0]}` (Reason: {key_row[1]})\n")
        else:
            f.write(f"* **Status**: Resolved / None remaining.\n")
        f.write(f"\n")
        
        f.write(f"### Integrity Failures / Mismatches\n")
        if failed_count > 0:
            f.write(f"* **Failures List**:\n")
            for idx, fail_row in enumerate(failed_objects):
                f.write(f"  {idx+1}. `{fail_row[0]}` ({fail_row[1]} bytes) — Error: {fail_row[2]}\n")
        else:
            f.write(f"* **Status**: Zero integrity failures detected.\n")
        f.write(f"\n")
        
        f.write(f"### S3 Freeze Violations\n")
        if has_violations:
            f.write(f"* **Violations List**:\n")
            for violation in violations:
                f.write(f"  * {violation}\n")
        else:
            f.write(f"* **Status**: Zero freeze violations detected.\n")
        f.write(f"\n")
        
    logger.info(f"Reconciliation Report successfully exported to {report_path}")
    
    # 7. Update active job details in DB
    cursor.execute("""
        UPDATE MigrationJobs 
        SET Status = ?, 
            EndedAt = ? 
        WHERE Id = ?
    """, (job_status, ended_at, db_job_id))
    conn.commit()
    
    log_event(conn, db_job_id, "reconciliation_completed", {
        "verdict": verdict,
        "report_path": report_path,
        "unresolved_review_items": unresolved_review_count,
        "freeze_violations": len(violations)
    })
    
    logger.info("=" * 60)
    logger.info(f" RECONCILIATION VERDICT: {verdict}")
    logger.info("=" * 60)
    
    conn.close()

if __name__ == "__main__":
    run_reconciliation()
