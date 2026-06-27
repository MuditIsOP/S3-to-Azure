import os
import sys
import boto3
from azure.storage.blob import ContainerClient

# Ensure config and db can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

def fix_failed_transfers():
    print("Starting python-native transfer for failed Unicode/Hindi objects...")
    
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
    
    # Fetch objects marked as failed or having AzCopy errors
    cursor.execute("""
        SELECT ObjectKey, BlobName 
        FROM MigrationObjects 
        WHERE JobId = ? AND Status = 'failed'
    """, (db_job_id,))
    
    failed_objects = cursor.fetchall()
    print(f"Found {len(failed_objects)} failed objects requiring python transfer.")
    
    if len(failed_objects) == 0:
        print("Zero failed objects found. Nothing to transfer!")
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
        
    import urllib.parse
    transferred = 0
    for idx, (key, blob_name) in enumerate(failed_objects):
        # 1. Repair Mojibake if present
        try:
            clean_key = key.encode('latin1').decode('utf-8')
        except Exception:
            clean_key = urllib.parse.unquote(key)
            
        try:
            clean_blob = blob_name.encode('latin1').decode('utf-8')
        except Exception:
            clean_blob = urllib.parse.unquote(blob_name)

        print(f"[{idx+1}/{len(failed_objects)}] Direct transferring: {clean_blob}")
        
        try:
            # Download from S3 stream trying raw key, unquoted key, or repaired key
            s3_resp = None
            for k in [key, clean_key, urllib.parse.unquote(key)]:
                try:
                    s3_resp = s3_client.get_object(Bucket=config.S3_BUCKET_NAME, Key=k)
                    break
                except Exception:
                    continue
                    
            if not s3_resp:
                raise Exception("Could not locate key in S3 with any encoding variant.")
                
            data = s3_resp['Body'].read()
            
            # Upload to Azure Blob using properly quoted UTF-8 path
            azure_target_blob = urllib.parse.quote(clean_blob, safe='/')
            blob_client = container_client.get_blob_client(azure_target_blob)
            blob_client.upload_blob(data, overwrite=True)
            
            # Reset DB status to 'discovered' so verify.py can test and verify it
            cursor.execute("""
                UPDATE MigrationObjects 
                SET Status = 'discovered', LastError = NULL 
                WHERE JobId = ? AND ObjectKey = ?
            """, (db_job_id, key))
            conn.commit()
            transferred += 1
            print(f"  [SUCCESS] Migrated to Azure Blob successfully.")
        except Exception as err:
            print(f"  [ERROR] Transfer failed: {err}")
            
    print(f"\nCompleted direct transfer of {transferred}/{len(failed_objects)} objects.")
    print("Run 'python verify.py' to run MD5 verification on these objects!")
    conn.close()

if __name__ == "__main__":
    fix_failed_transfers()
