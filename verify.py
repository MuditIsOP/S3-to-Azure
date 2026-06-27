import os
import sys
import hashlib
import json
import datetime
import argparse
import logging
import uuid
import boto3
from botocore.exceptions import ClientError

# Ensure config and db can be imported from local directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

# Set up logging
logger = logging.getLogger("verify")
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
    details_json = json.dumps(details_dict)
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
        SELECT Id, MigrationJobUUID, SourceBucket, DestinationContainer, Status 
        FROM MigrationJobs 
        WHERE Status IN ('paused', 'running') 
        ORDER BY StartedAt DESC
    """)
    row = cursor.fetchone()
    if row:
        return {
            "db_job_id": row[0],
            "job_uuid": row[1],
            "source_bucket": row[2],
            "destination_container": row[3],
            "status": row[4]
        }
    return None

def update_verification_batch(conn, batch_data):
    """Updates object verification statuses in the DB in a single transaction."""
    cursor = conn.cursor()
    update_sql = """
        UPDATE MigrationObjects 
        SET Status = ?, 
            IndependentSourceMD5 = ?, 
            IndependentDestinationMD5 = ?, 
            VerificationMethod = ?, 
            LastError = ?, 
            VerifiedAt = ? 
        WHERE JobId = ? AND ObjectKey = ?
    """
    try:
        cursor.executemany(update_sql, batch_data)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update verification batch: {e}")
        raise e

def verify_migration():
    logger.info("Starting Phase 2 Independent Verification...")
    
    try:
        conn, is_sqlite = db.get_db_connection()
    except Exception as e:
        logger.critical(f"Failed to connect to Database: {e}")
        sys.exit(1)
        
    active_job = get_active_job(conn)
    
    if not active_job:
        logger.error("No active job found in the database. Run inventory.py and transfer.py first.")
        conn.close()
        sys.exit(1)
        
    db_job_id = active_job["db_job_id"]
    job_uuid = active_job["job_uuid"]
    logger.info(f"Using Active Job - DB ID: {db_job_id}, UUID: {job_uuid}")
    
    # 1. Initialize AWS & Azure clients
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION
        )
        
        # Build Azure ContainerClient directly
        token = config.AZURE_SAS_TOKEN.lstrip('?')
        container_url = f"https://{config.AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{config.AZURE_CONTAINER_NAME}?{token}"
        from azure.storage.blob import ContainerClient
        container_client = ContainerClient.from_container_url(container_url)
        
    except Exception as e:
        logger.error(f"Failed to initialize cloud storage clients: {e}")
        conn.close()
        sys.exit(1)
        
    # 2. Retrieve discovered objects to verify
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ObjectKey, BlobName, SizeBytes, S3ETag 
        FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'discovered'
    """, (db_job_id,))
    
    objects_to_verify = cursor.fetchall()
    total_to_verify = len(objects_to_verify)
    logger.info(f"Found {total_to_verify} objects in 'discovered' state requiring verification.")
    
    if total_to_verify == 0:
        logger.info("Zero objects to verify. Exiting.")
        conn.close()
        return

    log_event(conn, db_job_id, "verification_started", {"total_objects_queued": total_to_verify})

    verified_count = 0
    failed_count = 0
    verified_bytes = 0
    
    batch_size = 500
    batch_data = []
    chunk_size = 8 * 1024 * 1024 # 8 MB chunks
    
    try:
        for idx, obj in enumerate(objects_to_verify):
            key = obj[0]
            blob_name = obj[1]
            s3_size = obj[2]
            s3_etag = obj[3]
            
            logger.info(f"[{idx+1}/{total_to_verify}] Verifying: {key} ({s3_size} bytes)")
            
            status = "failed"
            error_msg = None
            source_md5 = None
            dest_md5 = None
            method = None
            
            # Fetch Azure Blob details
            blob_client = container_client.get_blob_client(blob_name)
            
            try:
                if not blob_client.exists():
                    error_msg = "Object does not exist in Azure Blob Storage container"
                    logger.error(f"  [FAIL] {key}: {error_msg}")
                else:
                    props = blob_client.get_blob_properties()
                    azure_size = props.size
                    
                    # 1. Compare Sizes
                    if s3_size != azure_size:
                        error_msg = f"Size mismatch. S3: {s3_size} bytes, Azure: {azure_size} bytes"
                        logger.error(f"  [FAIL] {key}: {error_msg}")
                    else:
                        # Sizes match, proceed to Content-MD5 verification
                        # 2. Determine verification method
                        clean_etag = s3_etag.strip('"')
                        use_etag_shortcut = '-' not in clean_etag and len(clean_etag) == 32
                        
                        hash_azure = hashlib.md5()
                        
                        if use_etag_shortcut:
                            method = "etag_shortcut"
                            # Stream only Azure side
                            with blob_client.download_blob() as downloader:
                                for chunk in downloader.chunks():
                                    hash_azure.update(chunk)
                            source_md5 = bytes.fromhex(clean_etag)
                            dest_md5 = hash_azure.digest()
                        else:
                            method = "full_dual_hash"
                            # Stream both S3 and Azure
                            hash_s3 = hashlib.md5()
                            
                            # Stream S3
                            s3_resp = s3_client.get_object(Bucket=config.S3_BUCKET_NAME, Key=key)
                            with s3_resp['Body'] as s3_stream:
                                while True:
                                    chunk = s3_stream.read(chunk_size)
                                    if not chunk:
                                        break
                                    hash_s3.update(chunk)
                                
                            # Stream Azure
                            with blob_client.download_blob() as downloader:
                                for chunk in downloader.chunks():
                                    hash_azure.update(chunk)
                                
                            source_md5 = hash_s3.digest()
                            dest_md5 = hash_azure.digest()
                            
                        # Compare digests
                        if source_md5 == dest_md5:
                            status = "verified"
                            verified_count += 1
                            verified_bytes += s3_size
                            logger.info(f"  [PASS] {key} verified successfully using {method}.")
                        else:
                            error_msg = f"MD5 mismatch. Hashing method: {method}"
                            logger.error(f"  [FAIL] {key}: {error_msg}")
                            
            except ClientError as e:
                error_msg = f"AWS S3 Client Error: {e}"
                logger.error(f"  [FAIL] {key}: {error_msg}")
            except Exception as e:
                error_msg = f"Unexpected Error: {e}"
                logger.error(f"  [FAIL] {key}: {error_msg}")
                
            if status == "failed":
                failed_count += 1
                
            # Append update row (pass bytes directly; PyMySQL and SQLite map bytes to BINARY/BLOB automatically)
            batch_data.append((
                status,
                source_md5,
                dest_md5,
                method,
                error_msg,
                datetime.datetime.utcnow(),
                db_job_id,
                key
            ))
            
            # Commit batch
            if len(batch_data) >= batch_size:
                update_verification_batch(conn, batch_data)
                batch_data = []
                logger.info(f"Checkpoint: committed {idx+1} verification statuses to DB.")
                
        # Commit final batch
        if batch_data:
            update_verification_batch(conn, batch_data)
            
        # Update active job state counters
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE MigrationJobs 
            SET VerifiedObjects = ?, 
                VerifiedBytes = ?, 
                FailedObjects = ? 
            WHERE Id = ?
        """, (verified_count, verified_bytes, failed_count, db_job_id))
        conn.commit()
        
        log_event(conn, db_job_id, "verification_completed", {
            "verified_objects": verified_count,
            "verified_bytes": verified_bytes,
            "failed_objects": failed_count
        })
        
        logger.info("\n" + "=" * 60)
        logger.info(f" {'PHASE 2 VERIFICATION COMPLETE':^58}")
        logger.info("=" * 60)
        logger.info(f"Verified Objects: {verified_count}")
        logger.info(f"Verified Bytes:   {verified_bytes} bytes ({verified_bytes / (1024**3):.2f} GB)")
        logger.info(f"Failed Objects:   {failed_count}")
        logger.info("=" * 60)
        
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"CRITICAL: Phase 2 verification crashed. Details:\n{tb}")
        log_event(conn, db_job_id, "verification_crashed", {"error": str(e), "traceback": tb})
        conn.close()
        sys.exit(1)
        
    conn.close()

if __name__ == "__main__":
    verify_migration()
