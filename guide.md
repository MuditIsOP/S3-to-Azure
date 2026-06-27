# Linux VM Deployment & Operations Guide

This guide describes how to deploy, configure, test, and monitor the S3-to-Azure migration orchestrator on your Linux-based Azure VM.

---

## 1. Installation on Linux VM

Run the following commands on your Linux VM terminal to set up the environment.

### A. Install Native AzCopy
```bash
wget -O azcopy_v10.tar.gz https://aka.ms/downloadazcopy-v10-linux
tar -xf azcopy_v10.tar.gz
sudo cp ./azcopy_linux_amd64_*/azcopy /usr/bin/
rm -rf azcopy_v10.tar.gz azcopy_linux_amd64_*
```
*Verify by running `azcopy --version`.*

### B. Clone & Set Up Python
Navigate to the directory where you copied this project and run:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### C. Update Production Configuration
Edit `.env` (using `nano .env`) and set the real MySQL host address:
```ini
# Replace 'na' with the database endpoint
MYSQL_HOST=sasoneazdb.mysql.database.azure.com
```

---

## 2. How to Test with Exactly 1 File (End-to-End Test)

Before running the full migration of 177,297 objects, it is highly recommended to run a test job with a single file to confirm that permissions, MySQL writes, AzCopy commands, and MD5 verifications are completely functional.

### Step 1: Run Inventory with Limit 1
Run `inventory.py` using the `--limit-objects 1` argument. This forces S3 listing to stop after the first file and registers only **1 object** in the database under your job:
```bash
python inventory.py --limit-objects 1
```

### Step 2: Run Transfer
Start the transfer. Because only 1 object exists in the database status, AzCopy will run a copy for this job:
```bash
python transfer.py
```

### Step 3: Run Hashing Verification
Run the hashing validator. It will locate the single cataloged object and verify it against Azure Blob Storage:
```bash
python verify.py
```

### Step 4: Run Reconciliation
Compile the final report and check for freeze violations (it will report warnings about other objects, which is expected since we limited our inventory run to 1):
```bash
python reconcile.py
```

---

## 3. How to Monitor Progress, Speeds, and Logs

### A. Phase 1 Transfer Progress (`transfer.py`)
While `transfer.py` is running, it polls progress in the background every 30 seconds and outputs statistics directly to the terminal and `orchestrator.log`:
```text
2026-06-26 18:26:00 - INFO - Progress: Status=InProgress | Completed=15200 | Failed=0 | Skipped=0 | Bytes=25124000000
```

#### Detailed Monitoring via Native AzCopy
You can check detailed, real-time metrics (including **transfer speed in Mb/s**, percent complete, and remaining time) directly from AzCopy.
1. Open a second terminal window on the VM.
2. Run the native AzCopy status command:
   ```bash
   azcopy jobs show [your-azcopy-job-guid]
   ```
   *(The `[your-azcopy-job-guid]` is printed by `transfer.py` when it starts and is saved in the database under the `AzCopyJobId` column).*

#### Viewing Failed Transfers
If any individual files fail to copy during the transfer, you can list them using:
```bash
azcopy jobs show [your-azcopy-job-guid] --with-status=Failed
```

#### AzCopy Native Logs
AzCopy keeps its own comprehensive log files at `~/.azcopy/` on Linux. You can view the native log file for the job to debug network issues:
```bash
tail -n 100 ~/.azcopy/[your-azcopy-job-guid].log
```

---

## 4. Phase 2 Verification Progress (`verify.py`)

* **Console Logs**: The verification engine outputs progress sequentially in the terminal:
  ```text
  [145/177297] Verifying: ArtOfLivingmatrimony/CustomerData/12.png (102400 bytes)
    [PASS] ArtOfLivingmatrimony/CustomerData/12.png verified successfully using etag_shortcut.
  ```
* **Database Updates**: Verification commits status updates in batches of 500. You can query the database live to see progress:
  ```sql
  SELECT Status, COUNT(*) FROM MigrationObjects GROUP BY Status;
  ```
