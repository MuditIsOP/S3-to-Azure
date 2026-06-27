import os
import sys
import boto3
from azure.storage.blob import ContainerClient, ContentSettings

# Ensure config and db can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

def fix_failed_transfers():
    print("Starting python-native transfer for remaining failed objects...")
    
    try:
        conn, is_sqlite = db.get_db_connection()
    except Exception as e:
        print(f"Failed to connect to DB: {e}")
        sys.exit(1)
        
    cursor = conn.cursor()
    cursor.execute("""
        SELECT Id FROM MigrationJobs 
        WHERE Status IN ('paused', 'running', 'completed_with_review', 'failed') 
        ORDER BY StartedAt DESC
    """)
    job_row = cursor.fetchone()
    if not job_row:
        print("No active job found in DB.")
        conn.close()
        sys.exit(1)
        
    db_job_id = job_row[0]
    
    # Fetch objects marked as failed
    cursor.execute("""
        SELECT ObjectKey, BlobName 
        FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'failed'
    """, (db_job_id,))
    
    failed_objects = cursor.fetchall()
    print(f"Found {len(failed_objects)} failed objects requiring transfer.")
    
    if len(failed_objects) == 0:
        print("Zero failed objects found. All files are already migrated!")
        conn.close()
        return
        
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION
        )
        
        token = config.AZURE_SAS_TOKEN.lstrip('?')
        container_url = f"https://{config.AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{config.AZURE_CONTAINER_NAME}?{token}"
        container_client = ContainerClient.from_container_url(container_url)
    except Exception as e:
        print(f"Failed to initialize storage clients: {e}")
        conn.close()
        sys.exit(1)
        
    transferred = 0
    for idx, (key, blob_name) in enumerate(failed_objects):
        # Repair Mojibake encoding for clean Hindi blob path
        try:
            clean_blob = blob_name.encode('latin1').decode('utf-8')
        except Exception:
            clean_blob = blob_name

        print(f"[{idx+1}/{len(failed_objects)}] Migrating: {clean_blob}")
        
        try:
            # 1. Download from S3 using exact key stored in S3
            s3_resp = s3_client.get_object(Bucket=config.S3_BUCKET_NAME, Key=key)
            data = s3_resp['Body'].read()
            content_type = s3_resp.get('ContentType', 'application/octet-stream')
            
            # 2. Upload directly to Azure Blob using exact clean Hindi string (matching our test success)
            blob_client = container_client.get_blob_client(clean_blob)
            cnt_settings = ContentSettings(content_type=content_type)
            blob_client.upload_blob(data, overwrite=True, content_settings=cnt_settings)
            
            # 3. Update DB BlobName to clean_blob and Status to 'discovered' for verification
            cursor.execute("""
                UPDATE MigrationObjects 
                SET BlobName = ?, Status = 'discovered', LastError = NULL 
                WHERE JobId = ? AND ObjectKey = ?
            """, (clean_blob, db_job_id, key))
            conn.commit()
            transferred += 1
            print(f"  [SUCCESS] Migrated with metadata successfully.")
        except Exception as err:
            print(f"  [ERROR] Transfer failed: {err}")
            
    print(f"\nCompleted migration of {transferred}/{len(failed_objects)} objects.")
    print("Run 'python verify.py' to verify all objects!")
    conn.close()

if __name__ == "__main__":
    fix_failed_transfers()
