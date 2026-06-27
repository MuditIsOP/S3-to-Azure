import os
import sys
import hashlib
import json
import datetime
import argparse
import logging
import uuid
import concurrent.futures
import queue
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
    """Updates object verification statuses in the DB using a single high-performance bulk UPDATE query by integer Primary Key Id."""
    if not batch_data:
        return
    cursor = conn.cursor()
    try:
        # Check if SQLite wrapper is active
        is_sqlite = hasattr(conn, 'is_sqlite') and conn.is_sqlite
        
        if is_sqlite:
            update_sql = """
                UPDATE MigrationObjects 
                SET Status = ?, IndependentSourceMD5 = ?, IndependentDestinationMD5 = ?, 
                    VerificationMethod = ?, LastError = ?, VerifiedAt = ? 
                WHERE Id = ?
            """
            cursor.executemany(update_sql, batch_data)
        else:
            # High-Performance MySQL Bulk UPDATE Query using Integer Primary Key Id
            # batch_data item format: (status, source_md5, dest_md5, method, error_msg, verified_at, obj_db_id)
            status_cases, status_params = [], []
            src_md5_cases, src_md5_params = [], []
            dst_md5_cases, dst_md5_params = [], []
            method_cases, method_params = [], []
            err_cases, err_params = [], []
            vtime_cases, vtime_params = [], []
            ids = []
            
            for row in batch_data:
                status, s_md5, d_md5, method, err, vtime, obj_id = row
                ids.append(obj_id)
                
                status_cases.append("WHEN %s THEN %s")
                status_params.extend([obj_id, status])
                
                src_md5_cases.append("WHEN %s THEN %s")
                src_md5_params.extend([obj_id, s_md5])
                
                dst_md5_cases.append("WHEN %s THEN %s")
                dst_md5_params.extend([obj_id, d_md5])
                
                method_cases.append("WHEN %s THEN %s")
                method_params.extend([obj_id, method])
                
                err_cases.append("WHEN %s THEN %s")
                err_params.extend([obj_id, err])
                
                vtime_cases.append("WHEN %s THEN %s")
                vtime_params.extend([obj_id, vtime])
                
            id_placeholders = ','.join(['%s'] * len(ids))
            params = status_params + src_md5_params + dst_md5_params + method_params + err_params + vtime_params + ids
            
            bulk_update_sql = f"""
                UPDATE MigrationObjects 
                SET 
                    Status = CASE Id {' '.join(status_cases)} END,
                    IndependentSourceMD5 = CASE Id {' '.join(src_md5_cases)} END,
                    IndependentDestinationMD5 = CASE Id {' '.join(dst_md5_cases)} END,
                    VerificationMethod = CASE Id {' '.join(method_cases)} END,
                    LastError = CASE Id {' '.join(err_cases)} END,
                    VerifiedAt = CASE Id {' '.join(vtime_cases)} END
                WHERE Id IN ({id_placeholders})
            """
            cursor.execute(bulk_update_sql, params)
            
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"Failed to update verification batch: {e}")
        raise e

def verify_single_object(obj_data, db_job_id):
    """Worker function to verify a single object using thread-local cloud clients."""
    idx, total_to_verify, obj_id, key, blob_name, s3_size, s3_etag = obj_data
    
    status = "failed"
    error_msg = None
    source_md5 = None
    dest_md5 = None
    method = None
    chunk_size = 8 * 1024 * 1024 # 8 MB
    
    # Repair Mojibake for Azure lookup if needed
    try:
        clean_blob = blob_name.encode('latin1').decode('utf-8')
    except Exception:
        clean_blob = blob_name
        
    try:
        # Create thread-isolated clients
        s3_client = boto3.client(
            's3',
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION
        )
        token = config.AZURE_SAS_TOKEN.lstrip('?')
        container_url = f"https://{config.AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{config.AZURE_CONTAINER_NAME}?{token}"
        from azure.storage.blob import ContainerClient
        container_client = ContainerClient.from_container_url(container_url)
        blob_client = container_client.get_blob_client(clean_blob)
        
        if not blob_client.exists():
            error_msg = "Object does not exist in Azure Blob Storage container"
        else:
            props = blob_client.get_blob_properties()
            azure_size = props.size
            
            if s3_size != azure_size:
                error_msg = f"Size mismatch. S3: {s3_size} bytes, Azure: {azure_size} bytes"
            else:
                clean_etag = s3_etag.strip('"')
                use_etag_shortcut = '-' not in clean_etag and len(clean_etag) == 32
                hash_azure = hashlib.md5()
                
                if use_etag_shortcut:
                    method = "etag_shortcut"
                    downloader = blob_client.download_blob()
                    for chunk in downloader.chunks():
                        hash_azure.update(chunk)
                    source_md5 = bytes.fromhex(clean_etag)
                    dest_md5 = hash_azure.digest()
                else:
                    method = "full_dual_hash"
                    hash_s3 = hashlib.md5()
                    s3_resp = s3_client.get_object(Bucket=config.S3_BUCKET_NAME, Key=key)
                    with s3_resp['Body'] as s3_stream:
                        while True:
                            chunk = s3_stream.read(chunk_size)
                            if not chunk:
                                break
                            hash_s3.update(chunk)
                    downloader = blob_client.download_blob()
                    for chunk in downloader.chunks():
                        hash_azure.update(chunk)
                    source_md5 = hash_s3.digest()
                    dest_md5 = hash_azure.digest()
                    
                if source_md5 == dest_md5:
                    status = "verified"
                else:
                    error_msg = f"MD5 mismatch. Hashing method: {method}"
    except ClientError as e:
        error_msg = f"AWS S3 Client Error: {e}"
    except Exception as e:
        error_msg = f"Unexpected Error: {e}"
        
    db_row = (status, source_md5, dest_md5, method, error_msg, datetime.datetime.utcnow(), obj_id)
    return idx, status, s3_size, key, method, error_msg, db_row

def verify_migration():
    parser = argparse.ArgumentParser(description="Phase 2 MD5 Verification Engine")
    parser.add_argument("--workers", type=int, default=40, help="Number of parallel worker threads (default: 40)")
    args = parser.parse_args()
    max_workers = args.workers

    logger.info(f"Starting Phase 2 Independent Verification ({max_workers} Parallel Workers)...")
    
    try:
        conn, is_sqlite = db.get_db_connection()
    except Exception as e:
        logger.critical(f"Failed to connect to Database: {e}")
        sys.exit(1)
        
    active_job = get_active_job(conn)
    if not active_job:
        logger.error("No active job found in the database.")
        conn.close()
        sys.exit(1)
        
    db_job_id = active_job["db_job_id"]
    job_uuid = active_job["job_uuid"]
    logger.info(f"Using Active Job - DB ID: {db_job_id}, UUID: {job_uuid}")
    
    cursor = conn.cursor()
    cursor.execute("""
        SELECT Id, ObjectKey, BlobName, SizeBytes, S3ETag 
        FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'discovered'
    """, (db_job_id,))
    
    objects_to_verify = cursor.fetchall()
    total_to_verify = len(objects_to_verify)
    logger.info(f"Found {total_to_verify:,} objects in 'discovered' state requiring verification.")
    
    if total_to_verify == 0:
        logger.info("Zero objects to verify. Exiting.")
        conn.close()
        return

    log_event(conn, db_job_id, "verification_started", {"total_objects_queued": total_to_verify, "workers": max_workers})

    verified_count = 0
    failed_count = 0
    verified_bytes = 0
    batch_size = 500
    batch_data = []
    
    # Prepare work queue payloads with Id
    tasks = [
        (idx+1, total_to_verify, obj[0], obj[1], obj[2], obj[3], obj[4])
        for idx, obj in enumerate(objects_to_verify)
    ]
    
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(verify_single_object, task, db_job_id): task
                for task in tasks
            }
            
            completed_in_run = 0
            for future in concurrent.futures.as_completed(future_to_task):
                completed_in_run += 1
                try:
                    idx, status, s3_size, key, method, error_msg, db_row = future.result()
                    
                    if status == "verified":
                        verified_count += 1
                        verified_bytes += s3_size
                        if completed_in_run % 10 == 0:
                            logger.info(f"[{completed_in_run:,}/{total_to_verify:,}] [PASS] {key} verified using {method}.")
                    else:
                        failed_count += 1
                        logger.error(f"[{completed_in_run:,}/{total_to_verify:,}] [FAIL] {key}: {error_msg}")
                        
                    batch_data.append(db_row)
                    
                    if len(batch_data) >= batch_size:
                        update_verification_batch(conn, batch_data)
                        batch_data = []
                        logger.info(f"Checkpoint: committed {completed_in_run:,} verification statuses to DB.")
                        
                except Exception as exc:
                    logger.error(f"Worker thread exception: {exc}")
                    
        if batch_data:
            update_verification_batch(conn, batch_data)
            
        # Recalculate final totals in DB
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(SizeBytes) FROM MigrationObjects WHERE JobId = ? AND Status = 'verified'", (db_job_id,))
        final_verified = cursor.fetchone()
        v_objs = final_verified[0] or 0
        v_bytes = final_verified[1] or 0
        
        cursor.execute("SELECT COUNT(*) FROM MigrationObjects WHERE JobId = ? AND Status = 'failed'", (db_job_id,))
        f_objs = cursor.fetchone()[0] or 0
        
        cursor.execute("""
            UPDATE MigrationJobs 
            SET VerifiedObjects = ?, VerifiedBytes = ?, FailedObjects = ? 
            WHERE Id = ?
        """, (v_objs, v_bytes, f_objs, db_job_id))
        conn.commit()
        
        log_event(conn, db_job_id, "verification_completed", {
            "verified_objects": v_objs,
            "verified_bytes": v_bytes,
            "failed_objects": f_objs
        })
        
        logger.info("\n" + "=" * 60)
        logger.info(f" {'PHASE 2 VERIFICATION COMPLETE':^58}")
        logger.info("=" * 60)
        logger.info(f"Total Verified Objects: {v_objs:,}")
        logger.info(f"Total Verified Bytes:   {v_bytes:,} bytes ({v_bytes / (1024**3):.2f} GB)")
        logger.info(f"Total Failed Objects:   {f_objs:,}")
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
