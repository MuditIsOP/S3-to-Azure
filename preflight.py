import os
import sys
import shutil
import subprocess
import db
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# Add workspace directory to path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config

def print_banner(title):
    print("\n" + "=" * 60)
    print(f" {title:^58}")
    print("=" * 60)

def check_azcopy():
    print("[*] Checking AzCopy installation...")
    # Check local workspace directory first
    local_azcopy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "azcopy.exe")
    if os.path.exists(local_azcopy):
        cmd = local_azcopy
    else:
        cmd = "azcopy"
        
    try:
        # Run azcopy --version or just azcopy
        result = subprocess.run([cmd, "--version"], capture_output=True, text=True, check=True)
        version = result.stdout.strip()
        print(f"    [PASS] AzCopy is installed. Path: '{cmd}'. Version: {version}")
        return True, cmd
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print("    [FAIL] AzCopy was not found on system PATH or in workspace directory.")
        print(f"           Error detail: {e}")
        return False, None

def check_aws_connectivity(aws_access_key, aws_secret_key, region, bucket_name):
    print(f"[*] Checking S3 bucket '{bucket_name}' connectivity & size...")
    try:
        s3 = boto3.client(
            's3',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=region
        )
        # Verify access first using head_bucket
        s3.head_bucket(Bucket=bucket_name)
        
        # Paginate S3 to sum total objects and size
        total_objects = 0
        total_bytes = 0
        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket_name)
        
        for page in pages:
            if 'Contents' in page:
                total_objects += len(page['Contents'])
                total_bytes += sum(obj['Size'] for obj in page['Contents'])
                
        size_gb = total_bytes / (1024 ** 3)
        print(f"    [PASS] S3 connectivity verified.")
        print(f"           Source Bucket Size: {total_objects:,} objects | {total_bytes:,} bytes ({size_gb:.2f} GB)")
        return True
    except NoCredentialsError:
        print("    [FAIL] AWS credentials not found or invalid.")
        return False
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        print(f"    [FAIL] S3 access failed. Code: {error_code}")
        print(f"           Details: {e}")
        return False
    except Exception as e:
        print(f"    [FAIL] Unexpected error connecting to S3.")
        print(f"           Details: {e}")
        return False

def check_azure_connectivity(storage_account, container_name, sas_token, azcopy_cmd):
    print(f"[*] Checking Azure Blob Storage container '{container_name}' connectivity & size...")
    # Normalize SAS token (strip leading ? if present)
    token = sas_token.lstrip('?')
    container_url = f"https://{storage_account}.blob.core.windows.net/{container_name}?{token}"
    
    cmd = azcopy_cmd if azcopy_cmd else "azcopy"
    
    # 1. Run AzCopy list to check SAS token permissions (keeps AzCopy validation)
    try:
        result = subprocess.run(
            [cmd, "list", container_url], 
            capture_output=True, 
            text=True, 
            timeout=15
        )
        if result.returncode != 0:
            print("    [FAIL] AzCopy list failed to authenticate with destination container.")
            print(f"           Stdout: {result.stdout.strip()}")
            print(f"           Stderr: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print("    [FAIL] AzCopy list timed out trying to reach the Azure storage endpoint.")
        return False
    except Exception as e:
        print(f"    [FAIL] Failed to run AzCopy list for Azure validation.")
        print(f"           Details: {e}")
        return False

    # 2. Use python client to check size and count of container
    try:
        from azure.storage.blob import ContainerClient
        container_client = ContainerClient.from_container_url(container_url)
        
        total_objects = 0
        total_bytes = 0
        blobs = container_client.list_blobs()
        for blob in blobs:
            total_objects += 1
            total_bytes += blob.size
            
        size_gb = total_bytes / (1024 ** 3)
        print("    [PASS] Azure Container connectivity verified.")
        print(f"           Destination Container Size: {total_objects:,} objects | {total_bytes:,} bytes ({size_gb:.2f} GB)")
        return True
    except Exception as e:
        print("    [FAIL] Failed to count blobs using Azure SDK.")
        print(f"           Details: {e}")
        return False

def check_database():
    is_sqlite = (config.MYSQL_HOST.lower() == 'na')
    if is_sqlite:
        print("[*] Checking Database connectivity (SQLite Fallback)...")
    else:
        print(f"[*] Checking MySQL Database connectivity ({config.MYSQL_HOST})...")
        
    conn = None
    try:
        conn, is_sqlite = db.get_db_connection()
        cursor = conn.cursor()
        
        # Verify required tables exist
        required_tables = ['MigrationJobs', 'MigrationObjects', 'MigrationEvents']
        missing_tables = []
        
        for table in required_tables:
            if is_sqlite:
                cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            else:
                cursor.execute(f"SHOW TABLES LIKE '{table}'")
            if cursor.fetchone() is None:
                missing_tables.append(table)
                
        if missing_tables:
            if is_sqlite:
                print("    [PASS] SQLite DB connection verified. Schema tables are currently missing but will be auto-initialized by inventory.py.")
                return True
            else:
                print(f"    [FAIL] MySQL DB connection succeeded, but these tables are missing: {', '.join(missing_tables)}")
                print("           Please ensure your database administrator has initialized the schema.")
                return False
                
        print(f"    [PASS] Database connectivity and schema verified successfully ({'SQLite' if is_sqlite else 'MySQL'}).")
        return True
    except Exception as e:
        print(f"    [FAIL] Failed to connect to the tracking database.")
        print(f"           Error detail: {e}")
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

def check_disk_space():
    print("[*] Checking local VM disk space...")
    try:
        # Check current working directory disk space
        total, used, free = shutil.disk_usage(".")
        free_gb = free / (1024 ** 3)
        print(f"    [PASS] VM free disk space: {free_gb:.2f} GB")
        if free_gb < 10.0:
            print("    [WARN] VM free space is under 10 GB. Ensure logs/plan files do not exhaust disk.")
        return True
    except Exception as e:
        print(f"    [FAIL] Failed to check disk space: {e}")
        return False

def confirm_source_frozen():
    print_banner("SOURCE BUCKET FREEZE CONFIRMATION")
    print("WARNING: AzCopy does not support source buckets that are actively being modified.")
    print("Zero data loss is ONLY guaranteed if the S3 bucket is FROZEN (no writes/deletes/updates).")
    print("Confirm with your team that this freeze has been enacted.")
    
    try:
        user_input = input("\nIs the source S3 bucket frozen? (type 'yes' to confirm): ").strip().lower()
        if user_input == 'yes':
            print("\n[PASS] Source bucket freeze confirmed by operator.")
            return True
        else:
            print("\n[FAIL] Source bucket freeze NOT confirmed. Aborting.")
            return False
    except KeyboardInterrupt:
        print("\n\n[FAIL] Interrupted by operator. Aborting.")
        return False

def main():
    print_banner("S3 -> AZURE BLOB STORAGE MIGRATION PRE-FLIGHT VALIDATION")
    
    # 1. Config Loading Check
    print("[*] Loading and validating config.py...")
    try:
        import config
        print("    [PASS] Configuration environment loaded successfully.")
    except Exception as e:
        print("    [FAIL] Configuration loading failed. Check your .env file.")
        print(f"           Details: {e}")
        sys.exit(1)
        
    checks = {}
    
    # 2. Run Technical Checks
    has_azcopy, azcopy_cmd = check_azcopy()
    checks['azcopy'] = has_azcopy
    checks['aws'] = check_aws_connectivity(
        config.AWS_ACCESS_KEY_ID,
        config.AWS_SECRET_ACCESS_KEY,
        config.AWS_REGION,
        config.S3_BUCKET_NAME
    )
    checks['azure'] = check_azure_connectivity(
        config.AZURE_STORAGE_ACCOUNT,
        config.AZURE_CONTAINER_NAME,
        config.AZURE_SAS_TOKEN,
        azcopy_cmd
    )
    checks['sql'] = check_database()
    checks['disk'] = check_disk_space()
    
    # 3. Prompt for Freeze Confirmation
    checks['frozen'] = confirm_source_frozen()
    
    # 4. Final Verdict
    print_banner("PRE-FLIGHT VALIDATION SUMMARY")
    all_passed = True
    for check_name, status in checks.items():
        label = check_name.upper().ljust(15)
        result_str = "PASSED" if status else "FAILED"
        print(f"{label} : {result_str}")
        if not status:
            all_passed = False
            
    print("-" * 60)
    if all_passed:
        print(" VERDICT: ALL PRE-FLIGHT CHECKS PASSED. Ready to run inventory.")
        sys.exit(0)
    else:
        print(" VERDICT: PRE-FLIGHT CHECKS FAILED. Please resolve failures before proceeding.")
        sys.exit(1)

if __name__ == "__main__":
    main()
