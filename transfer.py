import os
import sys
import subprocess
import time
import re
import json
import datetime
import argparse
import logging
import uuid

# Ensure config and db can be imported from local directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
import db

# Set up logging
logger = logging.getLogger("transfer")
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
        SELECT Id, MigrationJobUUID, SourceBucket, DestinationContainer, AzCopyJobId, Status 
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
            "azcopy_job_id": row[4],
            "status": row[5]
        }
    return None

def resolve_azcopy_path():
    """Checks local directory and system PATH for azcopy executable."""
    local_azcopy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "azcopy.exe")
    if os.path.exists(local_azcopy):
        return local_azcopy
    return "azcopy"

def parse_azcopy_status(output_text):
    """Parses azcopy jobs show output to retrieve progress metrics."""
    metrics = {
        "status": "InProgress",
        "completed_transfers": 0,
        "failed_transfers": 0,
        "skipped_transfers": 0,
        "total_bytes": 0
    }
    
    # Example fields to look for
    # Job status: Completed / Failed / InProgress
    status_match = re.search(r"Job status:\s*(\w+)", output_text, re.IGNORECASE)
    if status_match:
        metrics["status"] = status_match.group(1)
        
    completed_match = re.search(r"Number of Transfers Completed:\s*(\d+)", output_text)
    if completed_match:
        metrics["completed_transfers"] = int(completed_match.group(1))
        
    failed_match = re.search(r"Number of Transfers Failed:\s*(\d+)", output_text)
    if failed_match:
        metrics["failed_transfers"] = int(failed_match.group(1))
        
    skipped_match = re.search(r"Number of Transfers Skipped:\s*(\d+)", output_text)
    if skipped_match:
        metrics["skipped_transfers"] = int(skipped_match.group(1))
        
    bytes_match = re.search(r"Total Bytes Transferred:\s*([\d,]+)", output_text)
    if bytes_match:
        metrics["total_bytes"] = int(bytes_match.group(1).replace(",", ""))
        
    return metrics

def run_transfer(dry_run=False, resume=False):
    logger.info("Starting Phase 1 Transfer Orchestration...")
    
    try:
        conn, is_sqlite = db.get_db_connection()
    except Exception as e:
        logger.critical(f"Failed to connect to Database: {e}")
        sys.exit(1)
        
    active_job = get_active_job(conn)
    
    if not active_job:
        logger.error("No active or paused job found in the database. Please run inventory.py first.")
        conn.close()
        sys.exit(1)
        
    db_job_id = active_job["db_job_id"]
    job_uuid = active_job["job_uuid"]
    logger.info(f"Using Active Job - DB ID: {db_job_id}, UUID: {job_uuid} ({active_job['status']})")
    
    azcopy_bin = resolve_azcopy_path()
    
    # 1. Exclusion Regex (folder placeholders and backslash keys)
    exclude_regex = r".*/$;.*\\.*"
    
    # 2. Build S3 & Azure Source/Destination URLs
    s3_url = f"https://s3.amazonaws.com/{config.S3_BUCKET_NAME}"
    
    # Normalize SAS token (strip leading ?)
    token = config.AZURE_SAS_TOKEN.lstrip('?')
    azure_url = f"https://{config.AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{config.AZURE_CONTAINER_NAME}?{token}"
    
    # Obfuscated version of Azure URL for safe logging
    obfuscated_azure_url = f"https://{config.AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{config.AZURE_CONTAINER_NAME}?SECRET_SAS_TOKEN"
    
    # 3. Formulate AzCopy command
    cmd_args = []
    
    if resume and active_job["azcopy_job_id"]:
        logger.info(f"Resuming existing AzCopy Job ID: {active_job['azcopy_job_id']}")
        cmd_args = [
            azcopy_bin, "jobs", "resume", active_job["azcopy_job_id"],
            "--source-sas", "", # Not needed for S3 endpoint URL copy typically, but passed if required
            "--destination-sas", token
        ]
        display_args = [
            azcopy_bin, "jobs", "resume", active_job["azcopy_job_id"],
            "--destination-sas", "SECRET_SAS_TOKEN"
        ]
    else:
        cmd_args = [
            azcopy_bin, "copy", s3_url, azure_url,
            "--recursive=true",
            "--check-length=true",
            "--log-level=INFO",
            "--s2s-handle-invalid-metadata=RenameIfInvalid",
            "--exclude-regex", exclude_regex
        ]
        display_args = [
            azcopy_bin, "copy", s3_url, obfuscated_azure_url,
            "--recursive=true",
            "--check-length=true",
            "--log-level=INFO",
            "--s2s-handle-invalid-metadata=RenameIfInvalid",
            "--exclude-regex", exclude_regex
        ]
        
    logger.info(f"Command (Obfuscated): {' '.join(display_args)}")
    
    if dry_run:
        logger.info("DRY-RUN mode active. Skipping execution.")
        conn.close()
        return
        
    # Setup AWS Credentials in Environment for AzCopy to use
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = config.AWS_ACCESS_KEY_ID
    env["AWS_SECRET_ACCESS_KEY"] = config.AWS_SECRET_ACCESS_KEY
    
    log_event(conn, db_job_id, "transfer_started", {
        "resume_flag": resume,
        "azcopy_job_id": active_job["azcopy_job_id"] if resume else "new"
    })
    
    # Set status to running in DB
    cursor = conn.cursor()
    cursor.execute("UPDATE MigrationJobs SET Status = 'running' WHERE Id = ?", (db_job_id,))
    conn.commit()
    
    # 4. Start AzCopy process (redirecting stdout to a log file to avoid pipe buffer deadlocks)
    azcopy_stdout_path = os.path.join(os.path.dirname(config.LOG_PATH), "azcopy_stdout.log")
    try:
        log_file = open(azcopy_stdout_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            cmd_args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=env
        )
    except Exception as e:
        logger.error(f"Failed to start AzCopy process: {e}")
        log_event(conn, db_job_id, "transfer_failed", {"error": str(e)})
        cursor.execute("UPDATE MigrationJobs SET Status = 'failed' WHERE Id = ?", (db_job_id,))
        conn.commit()
        conn.close()
        sys.exit(1)

    # 5. Extract AzCopy Job ID from output log file (wait 2s for AzCopy startup header)
    time.sleep(2)
    azcopy_job_id = active_job["azcopy_job_id"]
    
    try:
        if os.path.exists(azcopy_stdout_path):
            with open(azcopy_stdout_path, "r", encoding="utf-8") as lf:
                # Read startup header (first 50 lines) to find JobId
                for _ in range(50):
                    line = lf.readline()
                    if not line:
                        break
                    job_id_match = re.search(r"JobId:\s*([a-f0-9\-]+)", line, re.IGNORECASE)
                    if job_id_match:
                        extracted_id = job_id_match.group(1).strip()
                        if not resume:
                            azcopy_job_id = extracted_id
                            logger.info(f"Extracted AzCopy Job ID: {azcopy_job_id}")
                            cursor.execute("UPDATE MigrationJobs SET AzCopyJobId = ? WHERE Id = ?", (azcopy_job_id, db_job_id))
                            conn.commit()
                            log_event(conn, db_job_id, "azcopy_job_registered", {"azcopy_job_id": azcopy_job_id})
                        else:
                            logger.info(f"Confirmed resumed AzCopy Job ID: {extracted_id}")
                        break
    except Exception as lf_err:
        logger.error(f"Failed to read AzCopy stdout log file for Job ID: {lf_err}")

    # Close parent handle to log file
    try:
        log_file.close()
    except Exception:
        pass

    # Wait for process or transition to polling loop
    logger.info("AzCopy transfer is active. Starting progress polling loop (every 30s)...")
    
    while process.poll() is None:
        time.sleep(30)
        
        if azcopy_job_id:
            try:
                show_result = subprocess.run(
                    [azcopy_bin, "jobs", "show", azcopy_job_id],
                    capture_output=True,
                    text=True,
                    check=True
                )
                metrics = parse_azcopy_status(show_result.stdout)
                
                # Log progress event
                log_event(conn, db_job_id, "transfer_progress", metrics)
                
                # We can update job totals or details if needed
                logger.info(
                    f"Progress: Status={metrics['status']} | "
                    f"Completed={metrics['completed_transfers']} | "
                    f"Failed={metrics['failed_transfers']} | "
                    f"Skipped={metrics['skipped_transfers']} | "
                    f"Bytes={metrics['total_bytes']}"
                )
            except Exception as show_err:
                logger.warning(f"Failed to query AzCopy job status: {show_err}")

    # Process ended
    rc = process.returncode
    logger.info(f"AzCopy process exited with return code: {rc}")
    
    # Perform a final jobs show query to get final statistics
    final_status = "Failed"
    if azcopy_job_id:
        try:
            show_result = subprocess.run(
                [azcopy_bin, "jobs", "show", azcopy_job_id],
                capture_output=True,
                text=True,
                check=True
            )
            metrics = parse_azcopy_status(show_result.stdout)
            final_status = metrics["status"]
            
            # Write final log event
            log_event(conn, db_job_id, "transfer_finished", {
                "exit_code": rc,
                "azcopy_status": final_status,
                "metrics": metrics
            })
            
            # Update job state
            # If AzCopy was successful or completed with warnings, we set job status to paused
            # (which means transfer complete, ready for Phase 2 Verification).
            # If it failed completely, set status to failed.
            if final_status in ["Completed", "CompletedWithErrors", "CompletedWithSkipped", "CompletedWithErrorsAndSkipped", "Success"]:
                db_status = "paused"
                logger.info("AzCopy transfer completed successfully. Ready for verification.")
            else:
                db_status = "failed"
                logger.error(f"AzCopy transfer failed with status: {final_status}")
                
            cursor.execute("UPDATE MigrationJobs SET Status = ? WHERE Id = ?", (db_status, db_job_id))
            conn.commit()
            
        except Exception as final_err:
            logger.error(f"Failed to record final stats: {final_err}")
            
    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run S3 to Azure Blob Storage Phase 1 Transfer Orchestration.")
    parser.add_argument("--dry-run", action="store_true", help="Obfuscate credentials and show commands without executing")
    parser.add_argument("--resume", action="store_true", help="Resume the active AzCopy job GUID found in the DB")
    args = parser.parse_args()
    
    run_transfer(dry_run=args.dry_run, resume=args.resume)
