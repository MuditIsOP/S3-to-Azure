import os
import sys
import datetime
import logging
import uuid
from azure.storage.blob import ContainerClient

# Ensure config and db can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

def quick_verify_migration():
    print("Starting Fast Size & Metadata Verification (Non-hash audit)...")
    
    try:
        conn, is_sqlite = db.get_db_connection()
    except Exception as e:
        print(f"Failed to connect to DB: {e}")
        sys.exit(1)
        
    cursor = conn.cursor()
    cursor.execute("""
        SELECT Id, MigrationJobUUID 
        FROM MigrationJobs 
        WHERE Status IN ('paused', 'running') 
        ORDER BY StartedAt DESC
    """)
    job_row = cursor.fetchone()
    if not job_row:
        print("No active job found in DB.")
        conn.close()
        sys.exit(1)
        
    db_job_id = job_row[0]
    job_uuid = job_row[1]
    print(f"Using Active Job ID: {db_job_id}")
    
    # Initialize Azure ContainerClient
    try:
        token = config.AZURE_SAS_TOKEN.lstrip('?')
        container_url = f"https://{config.AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{config.AZURE_CONTAINER_NAME}?{token}"
        container_client = ContainerClient.from_container_url(container_url)
    except Exception as e:
        print(f"Failed to initialize Azure client: {e}")
        conn.close()
        sys.exit(1)
        
    # Query objects needing verification
    cursor.execute("""
        SELECT ObjectKey, BlobName, SizeBytes 
        FROM MigrationObjects 
        WHERE JobId = ? AND Status IN ('discovered', 'failed')
    """, (db_job_id,))
    
    objects_to_verify = cursor.fetchall()
    total_to_verify = len(objects_to_verify)
    print(f"Found {total_to_verify:,} objects to audit...")
    
    if total_to_verify == 0:
        print("Zero objects left to verify! All objects are already verified.")
        conn.close()
        return

    verified_count = 0
    failed_count = 0
    verified_bytes = 0
    batch_size = 1000
    batch_data = []
    
    update_sql = """
        UPDATE MigrationObjects 
        SET Status = ?, 
            VerificationMethod = ?, 
            LastError = ?, 
            VerifiedAt = ? 
        WHERE JobId = ? AND ObjectKey = ?
    """
    
    for idx, obj in enumerate(objects_to_verify):
        key = obj[0]
        blob_name = obj[1]
        s3_size = obj[2]
        
        status = "failed"
        error_msg = None
        method = "size_and_metadata"
        
        # Repair Mojibake for Azure lookup if necessary
        try:
            clean_blob = blob_name.encode('latin1').decode('utf-8')
        except Exception:
            clean_blob = blob_name
            
        try:
            blob_client = container_client.get_blob_client(clean_blob)
            if not blob_client.exists():
                error_msg = "Object does not exist in Azure Blob Storage"
            else:
                props = blob_client.get_blob_properties()
                azure_size = props.size
                if s3_size != azure_size:
                    error_msg = f"Size mismatch. S3: {s3_size}, Azure: {azure_size}"
                else:
                    status = "verified"
                    verified_count += 1
                    verified_bytes += s3_size
        except Exception as err:
            error_msg = f"Azure lookup error: {err}"
            
        if status == "failed":
            failed_count += 1
            
        batch_data.append((
            status,
            method,
            error_msg,
            datetime.datetime.utcnow(),
            db_job_id,
            key
        ))
        
        if len(batch_data) >= batch_size:
            cursor.executemany(update_sql, batch_data)
            conn.commit()
            batch_data = []
            print(f"Audited [{idx+1:,}/{total_to_verify:,}] objects...")
            
    if batch_data:
        cursor.executemany(update_sql, batch_data)
        conn.commit()
        
    # Get overall counts for job update
    cursor.execute("SELECT COUNT(*), SUM(SizeBytes) FROM MigrationObjects WHERE JobId = ? AND Status = 'verified'", (db_job_id,))
    final_verified = cursor.fetchone()
    total_v_objects = final_verified[0] or 0
    total_v_bytes = final_verified[1] or 0
    
    cursor.execute("SELECT COUNT(*) FROM MigrationObjects WHERE JobId = ? AND Status = 'failed'", (db_job_id,))
    total_f_objects = cursor.fetchone()[0] or 0
    
    cursor.execute("""
        UPDATE MigrationJobs 
        SET VerifiedObjects = ?, VerifiedBytes = ?, FailedObjects = ? 
        WHERE Id = ?
    """, (total_v_objects, total_v_bytes, total_f_objects, db_job_id))
    conn.commit()
    
    print("\n" + "=" * 60)
    print(f" {'FAST SIZE & METADATA VERIFICATION COMPLETE':^58}")
    print("=" * 60)
    print(f"Newly Verified in Run: {verified_count:,}")
    print(f"Total Verified Objects: {total_v_objects:,}")
    print(f"Total Verified Bytes:   {total_v_bytes:,} bytes ({total_v_bytes / (1024**3):.2f} GB)")
    print(f"Total Failed Objects:   {total_f_objects:,}")
    print("=" * 60)
    conn.close()

if __name__ == "__main__":
    quick_verify_migration()
