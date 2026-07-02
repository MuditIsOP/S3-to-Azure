import os
import sys
import uuid
import json
import datetime
import argparse
import logging
import boto3
from botocore.exceptions import ClientError
import pymysql

# Set up logging
logger = logging.getLogger("inventory_incremental")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    try:
        # Import config safely
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        import config
        fh = logging.FileHandler(config.LOG_PATH, mode='a', encoding='utf-8')
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception as e:
        print(f"Warning: Could not set up file log handler: {e}", file=sys.stderr)

import config
import db

def log_event(conn, db_job_id, event_type, details_dict, object_key=None):
    details_json = json.dumps(details_dict)
    logger.info(f"Event [{event_type}] | Details: {details_json}")
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO MigrationEvents (MigrationEventsUUID, JobId, ObjectKey, EventType, EventTime, DetailsJson)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (str(uuid.uuid4()), db_job_id, object_key, event_type, datetime.datetime.utcnow(), details_json))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to write event to DB: {e}")

def classify_object(key, size, storage_class):
    reasons = []
    if storage_class in ['GLACIER', 'DEEP_ARCHIVE']:
        reasons.append(f"Storage class '{storage_class}' requires restore before transfer")
    if '\\' in key:
        reasons.append("S3 object key contains backslash characters ('\\')")
    is_placeholder = key.endswith('/') and size == 0
    if reasons:
        return 'needs_review', "; ".join(reasons)
    elif is_placeholder:
        return 'needs_review', "S3 folder placeholder (zero-byte object ending in '/')"
    else:
        return 'discovered', None

def insert_batch(conn, batch_data):
    cursor = conn.cursor()
    insert_sql = """
        INSERT INTO MigrationObjects (
            MigrationObjectsUUID, JobId, ObjectKey, BlobName, SizeBytes, S3ETag, 
            S3LastModified, ContentType, StorageClass, Status, VerificationMethod, LastError, DiscoveredAt, VerifiedAt
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        cursor.executemany(insert_sql, batch_data)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to insert batch of {len(batch_data)} records: {e}")
        raise e

def run_incremental_inventory():
    logger.info("Starting S3 to MySQL Phase 0 Incremental Inventory...")
    
    # Establish Connection
    try:
        conn, is_sqlite = db.get_db_connection()
        logger.info("Connected to database")
    except Exception as e:
        logger.critical(f"Failed to connect to Database: {e}")
        sys.exit(1)

    cursor = conn.cursor()
    
    # 1. Find the latest previous job to use as a baseline
    cursor.execute("""
        SELECT Id, JobName, StartedAt 
        FROM MigrationJobs 
        WHERE Status IN ('paused', 'completed_with_review', 'completed', 'failed')
        ORDER BY StartedAt DESC LIMIT 1
    """)
    prev_job_row = cursor.fetchone()
    
    verified_cache = {}
    if prev_job_row:
        prev_job_id = prev_job_row[0]
        logger.info(f"Loading verified objects from previous job ID {prev_job_id} ({prev_job_row[1]}) as baseline...")
        
        cursor.execute("""
            SELECT ObjectKey, BlobName, SizeBytes, S3ETag, S3LastModified, ContentType, StorageClass, VerificationMethod
            FROM MigrationObjects 
            WHERE JobId = %s AND Status = 'verified'
        """, (prev_job_id,))
        
        for row in cursor.fetchall():
            verified_cache[row[0]] = {
                'BlobName': row[1],
                'SizeBytes': row[2],
                'S3ETag': row[3],
                'S3LastModified': row[4],
                'ContentType': row[5],
                'StorageClass': row[6],
                'VerificationMethod': row[7]
            }
        logger.info(f"Loaded {len(verified_cache):,} verified objects from previous run baseline.")
    else:
        logger.warning("No previous job found. This will run as a full inventory.")

    # 2. Register new Job
    job_uuid = str(uuid.uuid4())
    job_name = f"{config.MIGRATION_JOB_NAME}-incremental"
    source_bucket = config.S3_BUCKET_NAME
    dest_container = config.AZURE_CONTAINER_NAME
    
    config_snap = {
        "VERIFY_SAMPLE_OR_FULL": config.VERIFY_SAMPLE_OR_FULL,
        "VERIFY_SAMPLE_PERCENT": config.VERIFY_SAMPLE_PERCENT,
        "AWS_REGION": config.AWS_REGION,
        "AZURE_STORAGE_ACCOUNT": config.AZURE_STORAGE_ACCOUNT,
        "is_incremental": True
    }
    
    try:
        cursor.execute("""
            INSERT INTO MigrationJobs (
                MigrationJobUUID, JobName, SourceBucket, DestinationContainer, 
                Status, StartedAt, TotalObjects, TotalBytes, ConfigSnapshot
            ) VALUES (%s, %s, %s, %s, 'running', %s, 0, 0, %s)
        """, (job_uuid, job_name, source_bucket, dest_container, datetime.datetime.utcnow(), json.dumps(config_snap)))
        conn.commit()
        db_job_id = cursor.lastrowid
        logger.info(f"Registered incremental job {job_name} in database. UUID: {job_uuid}, DB ID: {db_job_id}")
    except Exception as e:
        logger.critical(f"Failed to write job entry to database: {e}")
        conn.close()
        sys.exit(1)

    log_event(conn, db_job_id, "inventory_incremental_started", {
        "job_name": job_name,
        "source_bucket": source_bucket,
        "destination_container": dest_container,
        "prev_job_id": prev_job_row[0] if prev_job_row else None
    })

    # 3. Initialize S3 Client
    try:
        s3 = boto3.client(
            's3',
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION
        )
    except Exception as e:
        error_msg = f"Failed to initialize S3 client: {e}"
        logger.error(error_msg)
        log_event(conn, db_job_id, "inventory_incremental_failed", {"error": error_msg})
        cursor.execute("UPDATE MigrationJobs SET Status = 'failed', EndedAt = %s WHERE Id = %s", (datetime.datetime.utcnow(), db_job_id))
        conn.commit()
        conn.close()
        sys.exit(1)

    # 4. Paginate and list S3 bucket
    total_count = 0
    total_bytes = 0
    new_discovered_count = 0
    new_discovered_bytes = 0
    needs_review_count = 0
    discovered_at = datetime.datetime.utcnow()
    batch_size = 1000
    batch_data = []

    try:
        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=source_bucket)
        
        logger.info(f"Listing objects in S3 bucket '{source_bucket}'...")
        
        for page in pages:
            if 'Contents' not in page:
                break
                
            for obj in page['Contents']:
                key = obj['Key']
                size = obj['Size']
                etag = obj['ETag'].strip('"')
                last_modified = obj['LastModified'].astimezone(datetime.timezone.utc).replace(tzinfo=None)
                storage_class = obj.get('StorageClass', 'STANDARD')
                
                # Check if this object was already successfully verified in previous run
                if key in verified_cache and verified_cache[key]['SizeBytes'] == size:
                    # Carry over as already verified
                    cached = verified_cache[key]
                    status = 'verified'
                    verification_method = cached['VerificationMethod']
                    last_error = None
                    verified_time = discovered_at
                else:
                    # New or modified object - needs to be discovered/copied/verified
                    status, last_error = classify_object(key, size, storage_class)
                    verification_method = None
                    verified_time = None
                    if status == 'discovered':
                        new_discovered_count += 1
                        new_discovered_bytes += size
                    elif status == 'needs_review':
                        needs_review_count += 1

                # Append to batch
                batch_data.append((
                    str(uuid.uuid4()), # MigrationObjectsUUID
                    db_job_id,
                    key,
                    key,
                    size,
                    etag,
                    last_modified,
                    obj.get('ContentType', 'application/octet-stream'),
                    storage_class,
                    status,
                    verification_method,
                    last_error,
                    discovered_at,
                    verified_time
                ))
                
                total_count += 1
                total_bytes += size
                
                if len(batch_data) >= batch_size:
                    insert_batch(conn, batch_data)
                    logger.info(f"Cataloged {total_count} objects...")
                    batch_data = []
                    
        if batch_data:
            insert_batch(conn, batch_data)

        # Update Job completion details
        cursor.execute("""
            UPDATE MigrationJobs 
            SET TotalObjects = %, 
                TotalBytes = %, 
                NeedsReviewObjects = %, 
                Status = 'paused', 
                EndedAt = % 
            WHERE Id = %
        """.replace('%', '?'), (total_count, total_bytes, needs_review_count, datetime.datetime.utcnow(), db_job_id))
        conn.commit()
        
        log_event(conn, db_job_id, "inventory_incremental_completed", {
            "total_objects": total_count,
            "total_bytes": total_bytes,
            "new_discovered_objects": new_discovered_count,
            "new_discovered_bytes": new_discovered_bytes,
            "needs_review_objects": needs_review_count
        })
        
        logger.info("\n" + "=" * 60)
        logger.info(f" {'INCREMENTAL INVENTORY COMPLETE':^58}")
        logger.info("=" * 60)
        logger.info(f"Database Job ID:      {db_job_id}")
        logger.info(f"Total Objects in S3:  {total_count:,}")
        logger.info(f"Already Verified:     {total_count - new_discovered_count - needs_review_count:,}")
        logger.info(f"New to Transfer:      {new_discovered_count:,} ({new_discovered_bytes / (1024**2):.2f} MB)")
        logger.info(f"Needs Review:         {needs_review_count:,}")
        logger.info("=" * 60)
        logger.info("Next steps:")
        logger.info("  1. Run 'python transfer.py' to transfer new files.")
        logger.info("  2. Run 'python verify.py' to verify them.")
        logger.info("  3. Run 'python reconcile.py' to generate the report.")
        logger.info("=" * 60)
        
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"CRITICAL: Incremental Inventory failed. Details:\n{tb}")
        try:
            log_event(conn, db_job_id, "inventory_incremental_failed", {"error": str(e), "traceback": tb})
            cursor.execute("UPDATE MigrationJobs SET Status = 'failed', EndedAt = ? WHERE Id = ?", (datetime.datetime.utcnow(), db_job_id))
            conn.commit()
        except Exception as db_err:
            logger.critical(f"Failed to record failure in DB: {db_err}")
        conn.close()
        sys.exit(1)
        
    conn.close()

if __name__ == "__main__":
    run_incremental_inventory()
