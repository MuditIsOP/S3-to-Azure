# S3 → Azure Blob Storage Migration — Production Execution Plan

## 1. Goal

Migrate all objects from the S3 source bucket to the Azure Blob Storage destination container with:

- **Zero tolerated data loss or corruption.** Every object's presence, size, and content integrity must be independently confirmed, not assumed.
- Execution from an **Azure VM**, in the same region as the destination storage account and the state database.
- **AzCopy** as the core transfer engine — not a custom-built streaming client.
- **Azure SQL** as the migration state store, since same-region VM-to-Azure-SQL calls are fast enough to track state at the per-object level without the latency penalty that would exist from a non-Azure machine.
- Full observability: real-time object/byte counts, throughput, ETA, and a final report with an explicit, honest completeness verdict.
- Confirmed numbers going in: **177,286 objects, 417.46 GB**, largest single object **152.42 GB**.

---

## 2. Architecture

```
                  Same Azure region throughout
        ┌─────────────────────────────────────────────┐
        │                                               │
        │   ┌───────────────┐                           │
        │   │  Azure VM     │── azcopy copy ──────────▶ │  (command issued here,
        │   │  (orchestrator)│                          │   bytes do NOT flow
        │   └───────┬───────┘                           │   through this VM)
        │           │                                   │
        │           │ poll job status / push rows       │
        │           ▼                                   │
        │   ┌───────────────┐                            │
        │   │  Azure SQL    │  ← state store, same region │
        │   │  (SASOne DB)  │                             │
        │   └───────────────┘                            │
        │                                                │
        └─────────────────────────────────────────────────┘
                            │
                            │ AzCopy's own server-to-server
                            │ transfer (Put Block From URL)
                            ▼
        ┌─────────────┐                    ┌──────────────┐
        │  AWS S3     │ ◀── pre-signed ────│ Azure Storage│
        │  (source)   │      URL pull       │  servers     │
        └─────────────┘                    └──────────────┘
```

**Key mechanism, stated plainly because it changes how you should think about "where this runs":** AzCopy's S3→Azure copy uses Azure's own `Put Block From URL` API. Azure's storage servers pull bytes directly from S3 using pre-signed URLs — the VM running the `azcopy` command never touches the actual file bytes. The VM's job is to **issue commands, watch job state, and record results** — it is the control plane, not the data plane. This is why running from an Azure VM doesn't speed up the byte transfer itself, but it does put your orchestration layer in the same network neighborhood as Azure SQL, which is what makes per-object state tracking in Azure SQL fast and reasonable here.

---

## 3. Why AzCopy, Not a Custom Transfer Engine

- It is Microsoft's own first-party tool for exactly this migration path, actively maintained.
- Server-side copy (above) — no bandwidth bottleneck on the orchestrating machine.
- Built-in resumability: every run is a **job** with a unique ID, a **plan file** (the full list of files identified for the job) and a **log file**. A killed or failed job resumes with `azcopy jobs resume <job-id>`, which re-attempts only what the plan file shows wasn't already transferred.
- Built-in retry: transient errors (timeouts, server-busy, network blips) are retried automatically, up to 20 attempts, before AzCopy gives up on a given file.
- Built-in per-file integrity check: by default, AzCopy verifies file length matches between source and destination after every single transfer. **This check stays on for this migration, with no exceptions** — see §9.
- Naming differences between S3 and Azure (illegal characters in bucket/container names, periods, consecutive hyphens) are handled automatically by AzCopy; metadata key incompatibilities have a configurable policy (`ExcludeIfInvalid` / `FailIfInvalid` / `RenameIfInvalid`).

Building a custom Python streaming client would mean re-implementing all of the above, with a real chance of being less correct than the first-party tool on day one. The job for everything we build ourselves is **inventory, pre-flight validation, post-transfer independent verification, and reporting** — wrapped around AzCopy, not replacing it.

---

## 4. Non-Negotiable Constraints Given the Risk Tolerance

Stated up front because they shape every section below:

1. **The source S3 bucket must be frozen (no new writes, no deletes, no overwrites) for the entire migration window.** AzCopy does not support a source or destination that's actively changing during transfer — if the bucket isn't frozen, results are not trustworthy, full stop. Confirm this with whoever owns write access to the bucket before starting, and get it in writing (a Teams message confirming "no writes to this bucket from [time] to [time]" is enough).
2. **`--check-length` (AzCopy's default post-transfer size verification) is never disabled.** It is the cheapest and most basic transferred-correctly check AzCopy gives you for free; the official optimization guidance explicitly notes it can be turned off for speed — we accept the (small) performance cost and never do this.
3. **Size-only verification (what `--check-length` gives you) is not sufficient on its own for this risk tolerance.** It catches truncation but not silent corruption that happens to preserve byte count. We add an independent MD5-based verification pass on top (§9) — this is the layer that gets us to "effectively zero error," not AzCopy's defaults alone.
4. **Every object must reach a terminal, accounted-for state** — `verified`, `failed` (with a logged reason), or `needs_review`. Nothing is allowed to silently vanish from tracking. The final report's headline number must equal the original inventory count, with every object's fate explained.

---

## 5. Technology and Resource Setup

| Component | Choice |
|---|---|
| Orchestration runtime | Azure VM (Linux recommended — AzCopy has first-class Linux package manager support) |
| VM region | **Same region as the destination storage account and Azure SQL database** — this is what makes per-object Azure SQL writes fast |
| Transfer engine | AzCopy v10 |
| State store | Azure SQL (existing SASOne database) |
| Orchestration/reporting layer | Python 3.11+ (wraps AzCopy via subprocess, polls job status, writes to Azure SQL, generates reports) |
| DB access from Python | `pyodbc` or SQLAlchemy with the ODBC Driver for SQL Server |
| Independent verification hashing | Python (`boto3` for S3 head/get on a sample or full pass, `azure-storage-blob` for Azure-side properties) |

**VM sizing note:** since AzCopy here is control-plane only (not pushing bytes itself), the VM doesn't need to be enormous — but give it enough CPU/memory for AzCopy's own job-tracking overhead at 177K+ files (job tracking has real memory cost; see §10), and enough disk space for log/plan files plus our own verification-sampling temp files. A general-purpose VM (e.g. 4 vCPU / 16GB RAM, adjust based on actual job-tracking memory observed during a test run) is a reasonable starting point — not something to over-provision blindly.

**Setup on the VM:**
```bash
# Install AzCopy (Linux)
# Follow current package-manager install instructions for your distro,
# or download the portable binary and add it to PATH.

# Install Python deps
pip install pyodbc azure-storage-blob boto3 python-dotenv --break-system-packages

# Install ODBC driver for SQL Server (required for pyodbc -> Azure SQL)
# Follow Microsoft's current install instructions for your Linux distro.
```

---

## 6. Configuration

```env
# AWS (source)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=...
S3_BUCKET_NAME=...

# Azure (destination)
AZURE_STORAGE_ACCOUNT=sasonestorage
AZURE_CONTAINER_NAME=sasonemediacontainer
AZURE_SAS_TOKEN=...                       # generated fresh for this job, scoped + expiring (see §7)

# Azure SQL (state store — same region as VM)
AZURE_SQL_CONNECTION_STRING=...
MIGRATION_JOB_NAME=s3-to-azure-prod-final

# Verification
VERIFY_SAMPLE_OR_FULL=full                 # full = every object gets independent MD5 check; sample = statistical subset
VERIFY_SAMPLE_PERCENT=10                   # only used if VERIFY_SAMPLE_OR_FULL=sample

# AzCopy tuning (set deliberately, not left at silent defaults — see §10)
AZCOPY_CONCURRENCY_VALUE=AUTO
AZCOPY_LOG_LOCATION=/var/log/azcopy
AZCOPY_JOB_PLAN_LOCATION=/var/azcopy-plans

REPORT_DIR=./reports
LOG_PATH=./orchestrator.log
```

Given the explicit "zero tolerance" requirement: **default to `VERIFY_SAMPLE_OR_FULL=full`.** A sampled check trades certainty for speed — not appropriate when the stated tolerance is effectively zero. Only switch to sampling if a full independent re-verification pass is shown to be impractically slow after a real test run, and that decision should be made explicitly, not by default.

**Credential handling:**
- AWS access key/secret: used only to generate the pre-signed URLs AzCopy needs; same rotation rule as always — rotate before this final run, since both keys have been shared in chat/Teams previously.
- Azure side: use a **SAS token scoped specifically to this container, with write/create/list permissions only, and an expiry set just past your expected migration window** — not the full account key, even though the account key has also already been shared and exposed. A SAS token limits blast radius if it leaks again and naturally expires.
- Azure SQL: use a connection string with write access scoped to the migration tables only.

---

## 7. Pre-Flight Validation (run before any transfer command, abort on any failure)

1. **Confirm S3 bucket is frozen** — get explicit written confirmation from whoever controls write access (per §4.1). This is a process check, not a technical one, but it's the most important box to tick given the constraint.
2. **AzCopy installed and on PATH** — `azcopy --version`.
3. **AWS credentials valid** — quick `aws s3 ls s3://<bucket> --max-items 1` or equivalent boto3 call.
4. **Azure SAS token valid and has required permissions** — test with `azcopy list` against the destination container.
5. **Azure SQL reachable from this VM** — test connection, confirm migration tables exist (§8) or create them now.
6. **Re-run inventory independently** (you already have a count — 177,286 / 417.46GB — but re-confirm it's current, especially given §4.1's freeze requirement: the count used for the final completeness check must come from *after* the freeze takes effect, not before).
7. **Disk space on VM**: confirm enough free space for AzCopy's log + plan files at this file count (plan files scale with object count, not object size — 177K objects is not large for this purpose, but check anyway) plus space for any local verification work.
8. **Print a pre-flight summary and require explicit confirmation before proceeding**: bucket frozen (Y/N), object count, total size, destination container confirmed empty or appropriately handled if not, SAS token expiry time vs. estimated job duration.

---

## 8. Azure SQL Schema (state store)

```sql
CREATE TABLE migration_jobs (
    job_id                  UNIQUEIDENTIFIER PRIMARY KEY,
    job_name                NVARCHAR(200) NOT NULL,
    source_bucket            NVARCHAR(500) NOT NULL,
    destination_container     NVARCHAR(500) NOT NULL,
    azcopy_job_id              NVARCHAR(100) NULL,        -- AzCopy's own job GUID, for cross-reference
    status                  NVARCHAR(50) NOT NULL,        -- preflight|running|paused|completed|completed_with_review|failed
    source_frozen_confirmed_at DATETIME2 NULL,
    started_at               DATETIME2 NOT NULL,
    ended_at                 DATETIME2 NULL,
    total_objects             BIGINT NOT NULL DEFAULT 0,
    total_bytes               BIGINT NOT NULL DEFAULT 0,
    verified_objects           BIGINT NOT NULL DEFAULT 0,
    verified_bytes             BIGINT NOT NULL DEFAULT 0,
    failed_objects              BIGINT NOT NULL DEFAULT 0,
    needs_review_objects         BIGINT NOT NULL DEFAULT 0,
    config_snapshot            NVARCHAR(MAX) NULL
);

CREATE TABLE migration_objects (
    job_id              UNIQUEIDENTIFIER NOT NULL,
    object_key          NVARCHAR(1024) NOT NULL,
    blob_name           NVARCHAR(1024) NOT NULL,
    size_bytes          BIGINT NOT NULL,
    s3_etag             NVARCHAR(200) NULL,               -- reference only, never compared cross-cloud as MD5
    s3_last_modified      DATETIME2 NULL,
    content_type         NVARCHAR(255) NULL,
    storage_class          NVARCHAR(100) NULL,             -- flags Glacier/restore-required objects
    azcopy_status          NVARCHAR(50) NULL,               -- as reported by `azcopy jobs show`
    independent_source_md5  VARBINARY(16) NULL,             -- computed by OUR verification pass, not AzCopy's
    independent_dest_md5     VARBINARY(16) NULL,
    status              NVARCHAR(50) NOT NULL DEFAULT 'discovered',
                        -- discovered|transferred_by_azcopy|verified|failed|needs_review
    last_error           NVARCHAR(MAX) NULL,
    discovered_at          DATETIME2 NOT NULL,
    verified_at            DATETIME2 NULL,
    PRIMARY KEY (job_id, object_key)
);

CREATE INDEX ix_migration_objects_status ON migration_objects (job_id, status);

CREATE TABLE migration_events (
    event_id     BIGINT IDENTITY(1,1) PRIMARY KEY,
    job_id       UNIQUEIDENTIFIER NOT NULL,
    object_key    NVARCHAR(1024) NULL,
    event_type    NVARCHAR(100) NOT NULL,
    event_time    DATETIME2 NOT NULL,
    details_json  NVARCHAR(MAX) NULL
);
```

Because the VM and Azure SQL are in the same region, this design can safely do per-object writes (one row per object, one update on verification) without the latency problem that would exist running this from outside Azure. Still write in **batches of ~500–1000 using a single multi-row `INSERT`/`MERGE`** during the inventory phase rather than one-row-at-a-time round trips — this isn't required for correctness here, but it's free performance and good practice regardless of network proximity.

---

## 9. Execution Phases

### Phase 0 — Inventory

Same approach as previously established: single-threaded `list_objects_v2` paginated loop against S3, no per-object `HEAD` calls. Capture key, size, ETag, last-modified, storage class. Batch-insert into `migration_objects` with `status='discovered'`.

- Flag any object with a storage class requiring restore (Glacier, Deep Archive) as `needs_review` — these cannot be transferred by AzCopy's direct copy path until restored; decide and execute any needed restores **before** the main transfer run, not during it.
- Validate object keys against Azure naming constraints. AzCopy handles bucket/container-name and metadata-key issues automatically, but run an explicit check here anyway as a second, independent line of defense given the risk tolerance — flag anything genuinely invalid as `needs_review`.

Output: confirmed total object count and byte total — cross-check against the 177,286 / 417.46GB figures already gathered, and treat any discrepancy as a signal to investigate before proceeding (could indicate the bucket wasn't actually frozen yet at first count).

### Phase 1 — Transfer (AzCopy)

```bash
azcopy copy \
  "https://s3.amazonaws.com/<bucket-name>" \
  "https://sasonestorage.blob.core.windows.net/sasonemediacontainer?<SAS-token>" \
  --recursive=true \
  --check-length=true \
  --log-level=INFO \
  --s2s-handle-invalid-metadata=RenameIfInvalid
```

- `--check-length=true` is explicit here even though it's the default — never let it be silently overridden by a later config change.
- `--s2s-handle-invalid-metadata=RenameIfInvalid` preserves data rather than silently dropping incompatible metadata (`ExcludeIfInvalid` is the AzCopy default and would silently lose metadata — not acceptable at this risk tolerance; `RenameIfInvalid` preserves the original key/value as a recoverable renamed entry instead).
- Record the AzCopy-assigned job ID (printed at start, also retrievable via `azcopy jobs list`) into `migration_jobs.azcopy_job_id` immediately.
- The orchestrator Python process polls `azcopy jobs show <job-id>` periodically (e.g. every 30–60s) and updates `migration_jobs` with progress; it does **not** try to track individual per-file progress during this phase from AzCopy's side — AzCopy's own plan/log files are the source of truth for transfer-level status.
- If the process is interrupted: `azcopy jobs resume <azcopy-job-id> --source-sas="..." --destination-sas="..."` (SAS tokens aren't persisted in the plan file for security reasons, so they must be supplied again on resume).

### Phase 2 — Independent Verification (the layer that gets you to near-zero error)

This is the most important phase given the stated risk tolerance, and it is **deliberately separate from and in addition to** AzCopy's own `--check-length` check. AzCopy confirms size matches; it does not give you an independently-computed content hash comparison in the basic copy command. We add that ourselves:

For every object (per `VERIFY_SAMPLE_OR_FULL=full`):
1. Fetch the Azure blob's properties (size, and `Content-MD5` if AzCopy set it — confirm whether it does by default for S3→Azure copies during the test run; if not consistently set, fall back to downloading and hashing).
2. Compute or retrieve the S3 source object's MD5 independently:
   - Non-multipart S3 upload → ETag is a true MD5, usable as a quick reference, but for full rigor at this risk tolerance, prefer computing it fresh from a `GetObject` read where feasible.
   - Multipart S3 upload → ETag is **not** a usable MD5 under any circumstance; must compute independently.
3. Compare. Match → `status='verified'`. Mismatch → `status='failed'`, logged with full detail, queued for manual re-transfer decision.
4. For the single 152.42GB file specifically: full re-hash of both sides means reading 150GB+ twice (once from each side) just for verification — budget real time for this, and consider whether multipart/range-based hashing in parallel chunks is worth implementing for this one object specifically, given its outsized weight in your total dataset.
5. Update Azure SQL per object (or in batches, per §8's guidance).

**On "0.1% error tolerance" concretely:** 177,286 objects × 0.1% = ~177 objects. Given the stated requirement is that you are *not* allowed even this much error, the bar is: **every single object reaches `verified` status, or is explicitly and visibly in `failed`/`needs_review` with a human-readable reason in the final report.** "Probably fine" is not an acceptable terminal state for any object under this plan.

### Phase 3 — Reconciliation

- Final count check: `verified` + `failed` + `needs_review` must exactly equal the Phase 0 inventory count. If it doesn't, something was silently dropped somewhere in the pipeline — find it before declaring completion.
- Final byte check: sum of `verified` bytes should equal total inventory bytes minus any `failed`/`needs_review` bytes, exactly.
- Re-list the S3 bucket one final time and diff against the original Phase 0 inventory — if it's not identical, the freeze (§4.1) was violated during the run, and this must be disclosed in the final report, not hidden.

---

## 10. AzCopy Tuning Notes (set deliberately, documented, not left to chance)

- `AZCOPY_CONCURRENCY_VALUE=AUTO` lets AzCopy self-tune; for server-to-server copies (which this is), Microsoft's own guidance suggests this can be set quite high (above 1000) since the orchestrating machine isn't moving the actual bytes — but validate this with a real benchmark run (`azcopy benchmark`) against your actual destination before committing to a high fixed value blindly.
- **At 177,286 files, stay aware of AzCopy's own job-tracking overhead guidance**: jobs comfortably under 10 million files perform well; this job is two orders of magnitude under that ceiling, so no special splitting is needed here, but it's worth knowing the ceiling exists if this process is ever reused for a much larger bucket later.
- Do not set `--log-level=ERROR` to "reduce noise" for this run — keep it at `INFO` (the default) given the risk tolerance; you want the full activity trail, not just errors, in case something needs to be reconstructed after the fact.
- Do not use `cap-mbps` to throttle — there's no stated reason to deliberately slow this down, and doing so only extends the window during which the "source frozen" assumption needs to hold.

---

## 11. Error Classification

| Type | Examples | Handling |
|---|---|---|
| Retryable (AzCopy handles internally) | timeouts, server-busy, transient network errors | AzCopy retries automatically up to 20 times per file — no action needed from us unless it ultimately fails |
| Transfer failure (surfaces in AzCopy log as `UPLOADFAILED`/`COPYFAILED`) | persistent auth issue, permission denied, object genuinely inaccessible | Pull from `azcopy jobs show <id> --with-status=Failed`, log into `migration_objects.failed`, investigate root cause before any retry |
| Verification mismatch (our Phase 2 catch) | size matches per AzCopy but independent MD5 doesn't | `status='failed'`, treated as seriously as a transfer failure — this is exactly the class of error AzCopy's own defaults could miss |
| Needs review | Glacier/restore-required storage class, invalid naming/metadata flagged in Phase 0 | Never auto-resolved; explicit human decision required, logged with reason |

---

## 12. Final Report

Generated at the end of Phase 3, written to Azure SQL (`migration_jobs` row) and exported as a file:

- Job ID, AzCopy job ID, start/end time, total duration
- Source bucket frozen confirmation (timestamp, who confirmed)
- Total discovered objects/bytes (Phase 0)
- Verified objects/bytes (Phase 2 — independently confirmed, not just AzCopy's length check)
- Failed objects with full error detail
- Needs-review objects with reason
- **Reconciliation result** (Phase 3): does verified+failed+needs_review == total discovered? Does final S3 re-list match original inventory exactly?
- **Explicit completeness verdict**:
  - `COMPLETE` — 100% of discovered objects verified, zero failed, zero needs_review, reconciliation passed cleanly
  - `COMPLETE_WITH_REVIEW` — all verified or explicitly accounted for in needs_review, zero unexplained failures
  - `FAILED` — any object in `failed` status, or reconciliation mismatch detected
  
Given the stated risk tolerance, **only `COMPLETE` should be presented as "migration successful" to anyone outside this process** — `COMPLETE_WITH_REVIEW` means there is unresolved work, and `FAILED` means stop and investigate before telling anyone the migration is done.

---

## 13. Build/Execution Order

1. Provision the Azure VM in the correct region; confirm network access to AWS S3 endpoints, Azure Blob, and Azure SQL.
2. Install AzCopy + Python dependencies + ODBC driver.
3. Create the Azure SQL schema (§8).
4. Write and test the orchestrator's config loading + pre-flight validation (§7) — run it standalone first, confirm every check passes or correctly fails on a deliberately broken input.
5. Confirm bucket freeze with the team — get explicit confirmation before proceeding past this point.
6. Run Phase 0 inventory, cross-check counts against the 177,286/417.46GB figures already known.
7. Test the AzCopy command (§9 Phase 1) against a **small subset first** — use `--include-path` to scope to one folder/prefix, confirm metadata/content-type/naming behavior matches expectations on a small sample before running against the full 417GB.
8. Build and test the independent verification pass (§9 Phase 2) against that same small subset — confirm it correctly flags a deliberately corrupted test object as `failed`.
9. Run the full AzCopy transfer against the entire bucket.
10. Run full independent verification against every transferred object.
11. Run reconciliation (§9 Phase 3).
12. Generate and review the final report. Only proceed to declare the migration complete if the verdict is `COMPLETE`.
13. Rotate both AWS and Azure credentials immediately after this run — they have been exposed in chat/Teams throughout this project and this is the last legitimate use of the current keys.
