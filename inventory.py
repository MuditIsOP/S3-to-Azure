import os
import sys
import uuid
import json
import datetime
import argparse
import logging
import boto3
from botocore.exceptions import ClientError

# Ensure config and db can be imported from local directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

# Set up logging
logger = logging.getLogger("inventory")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Console logging
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    # File logging
    try:
        fh = logging.FileHandler(config.LOG_PATH, mode='a', encoding='utf-8')
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception as e:
        print(f"Warning: Could not set up file log handler at {config.LOG_PATH}: {e}", file=sys.stderr)

def log_event(conn, db_job_id, event_type, details_dict, object_key=None):
    """Logs migration events to the database and local logger."""
    details_json = json.dumps(details_dict)
    logger.info(f"Event [{event_type}] Key: {object_key} | Details: {details_json}")
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO MigrationEvents (MigrationEventsUUID, JobId, ObjectKey, EventType, EventTime, DetailsJson)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), db_job_id, object_key, event_type, datetime.datetime.utcnow(), details_json))
        conn.commit()
    except Exception as e:
        logger.error(f"CRITICAL: Failed to write event to DB: {e}. Event info: {event_type} - {details_json}")

def classify_object(key, size, storage_class):
    """Classifies S3 object status and checks naming / folder placeholder constraints.
    Returns (status, last_error_message).
    """
    reasons = []
    
    # 1. S3 Storage Class check (Glacier / Deep Archive requires restore)
    if storage_class in ['GLACIER', 'DEEP_ARCHIVE']:
        reasons.append(f"Storage class '{storage_class}' requires restore before transfer")
        
    # 2. Key contains backslashes (flagged for manual review)
    if '\\' in key:
        reasons.append("S3 object key contains backslash characters ('\\')")
        
    # 3. Check if key is a virtual folder placeholder
    is_placeholder = key.endswith('/') and size == 0
    
    if reasons:
        # If it's a folder placeholder and has reasons, list it as skipped/needs_review
        # S3 folder placeholders are explicitly skipped by design.
        return 'needs_review', "; ".join(reasons)
    elif is_placeholder:
        return 'needs_review', "S3 folder placeholder (zero-byte object ending in '/')"
    else:
        return 'discovered', None

def insert_batch(conn, batch_data):
    """Inserts a batch of object metadata into MigrationObjects."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT INTO MigrationObjects (
            MigrationObjectsUUID, JobId, ObjectKey, BlobName, SizeBytes, S3ETag, 
            S3LastModified, ContentType, StorageClass, Status, LastError, DiscoveredAt
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    try:
        cursor.executemany(insert_sql, batch_data)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to insert batch of {len(batch_data)} records: {e}")
        raise e

def run_inventory(limit=None):
    logger.info("Starting S3 to MySQL Phase 0 Inventory...")
    
    # Generate unique Job ID and collect config snapshot
    job_uuid = str(uuid.uuid4())
    job_name = config.MIGRATION_JOB_NAME
    source_bucket = config.S3_BUCKET_NAME
    dest_container = config.AZURE_CONTAINER_NAME
    
    config_snap = {
        "VERIFY_SAMPLE_OR_FULL": config.VERIFY_SAMPLE_OR_FULL,
        "VERIFY_SAMPLE_PERCENT": config.VERIFY_SAMPLE_PERCENT,
        "AWS_REGION": config.AWS_REGION,
        "AZURE_STORAGE_ACCOUNT": config.AZURE_STORAGE_ACCOUNT
    }
    
    # Establish Connection
    try:
        conn, is_sqlite = db.get_db_connection()
        if is_sqlite:
            db.init_sqlite_db(conn)
            logger.info("Connected to local SQLite database (initialized tables if missing)")
        else:
            logger.info(f"Connected to MySQL database ({config.MYSQL_HOST})")
    except Exception as e:
        logger.critical(f"Failed to connect to Database: {e}")
        sys.exit(1)
        
    # Write Job Record to database
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO MigrationJobs (
                MigrationJobUUID, JobName, SourceBucket, DestinationContainer, 
                Status, StartedAt, TotalObjects, TotalBytes, ConfigSnapshot
            ) VALUES (?, ?, ?, ?, 'running', ?, 0, 0, ?)
        """, (job_uuid, job_name, source_bucket, dest_container, datetime.datetime.utcnow(), json.dumps(config_snap)))
        conn.commit()
        db_job_id = cursor.lastrowid
        logger.info(f"Registered job {job_name} in database. UUID: {job_uuid}, DB ID: {db_job_id}")
    except Exception as e:
        logger.critical(f"Failed to write job entry to database: {e}")
        conn.close()
        sys.exit(1)

    log_event(conn, db_job_id, "inventory_started", {
        "job_name": job_name,
        "source_bucket": source_bucket,
        "destination_container": dest_container,
        "limit_objects_flag": limit
    })

    # Initialize S3 Client
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
        log_event(conn, db_job_id, "inventory_failed", {"error": error_msg})
        cursor.execute("UPDATE MigrationJobs SET Status = 'failed', EndedAt = ? WHERE Id = ?", (datetime.datetime.utcnow(), db_job_id))
        conn.commit()
        conn.close()
        sys.exit(1)

    # Paginate and list S3 bucket
    total_count = 0
    total_bytes = 0
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
                logger.info("S3 bucket is empty.")
                break
                
            for obj in page['Contents']:
                key = obj['Key']
                size = obj['Size']
                etag = obj['ETag'].strip('"') # Strip wrapping double quotes
                last_modified = obj['LastModified'].astimezone(datetime.timezone.utc).replace(tzinfo=None)
                storage_class = obj.get('StorageClass', 'STANDARD')
                
                # Classify object
                status, last_error = classify_object(key, size, storage_class)
                if status == 'needs_review':
                    needs_review_count += 1
                
                # Append to batch
                batch_data.append((
                    str(uuid.uuid4()), # MigrationObjectsUUID
                    db_job_id,
                    key,
                    key, # blob_name is key by default
                    size,
                    etag,
                    last_modified,
                    obj.get('ContentType', 'application/octet-stream'),
                    storage_class,
                    status,
                    last_error,
                    discovered_at
                ))
                
                total_count += 1
                total_bytes += size
                
                # Check batch threshold
                if len(batch_data) >= batch_size:
                    insert_batch(conn, batch_data)
                    logger.info(f"Buffered insertion completed: {total_count} objects cataloged.")
                    batch_data = []
                    
                # Handle limit-objects flag for testing
                if limit and total_count >= limit:
                    logger.info(f"Reached limit-objects threshold of {limit}. Stopping listing.")
                    break
                    
            if limit and total_count >= limit:
                break
                
        # Insert remaining records in buffer
        if batch_data:
            insert_batch(conn, batch_data)
            logger.info(f"Buffered insertion completed: {total_count} objects cataloged (final buffer).")

        # Update Job completion details
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE MigrationJobs 
            SET TotalObjects = ?, 
                TotalBytes = ?, 
                NeedsReviewObjects = ?, 
                Status = 'paused', 
                EndedAt = ? 
            WHERE Id = ?
        """, (total_count, total_bytes, needs_review_count, datetime.datetime.utcnow(), db_job_id))
        conn.commit()
        
        log_event(conn, db_job_id, "inventory_completed", {
            "total_objects": total_count,
            "total_bytes": total_bytes,
            "needs_review_objects": needs_review_count
        })
        
        logger.info("\n" + "=" * 60)
        logger.info(f" {'PHASE 0 INVENTORY COMPLETE':^58}")
        logger.info("=" * 60)
        logger.info(f"Job UUID:             {job_uuid}")
        logger.info(f"Database Job ID:      {db_job_id}")
        logger.info(f"Total Objects:        {total_count}")
        logger.info(f"Total Bytes:          {total_bytes} bytes ({total_bytes / (1024**3):.2f} GB)")
        logger.info(f"Needs Review Objects: {needs_review_count}")
        logger.info("=" * 60)
        
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"CRITICAL: Phase 0 Inventory failed. Details:\n{tb}")
        
        # Attempt logging error to DB
        try:
            log_event(conn, db_job_id, "inventory_failed", {"error": str(e), "traceback": tb})
            cursor = conn.cursor()
            cursor.execute("UPDATE MigrationJobs SET Status = 'failed', EndedAt = ? WHERE Id = ?", (datetime.datetime.utcnow(), db_job_id))
            conn.commit()
        except Exception as db_err:
            logger.critical(f"Failed to record failure in DB: {db_err}")
            
        conn.close()
        sys.exit(1)
        
    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run S3 to MySQL Phase 0 Inventory.")
    parser.add_argument("--limit-objects", type=int, default=None, help="Limit number of objects listed (for testing)")
    args = parser.parse_args()
    
    run_inventory(limit=args.limit_objects)
