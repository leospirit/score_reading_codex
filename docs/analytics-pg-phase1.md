# PG Analytics Phase 1 (Sidecar)

This phase is sidecar-only and does not change scoring/report behavior.

## What it does

- Reads report JSON files from `data/out/**/web_*.json`
- Upserts normalized rows into PostgreSQL analytics tables
- Tracks file ingest state to skip unchanged files
- Writes job run summary into `analytics_job_run`

## Files

- SQL schema: `analytics_pg/schema.sql`
- Python sync entry: `tools/pg_analytics_sync.py`
- Backend module: `score_reading/src/analytics/pg_sync.py`
- Windows runner: `tools/run_pg_analytics_sync.bat`
- Windows scheduled-task helper: `tools/register_pg_analytics_task.ps1`

## Env

Set in `.env` or shell:

```env
PG_ANALYTICS_DSN=postgresql://user:password@127.0.0.1:5432/speech_analytics
```

## Usage

Dry run (no DB writes):

```bash
python tools/pg_analytics_sync.py --dry-run
```

Real sync:

```bash
python tools/pg_analytics_sync.py
```

Limit first N files:

```bash
python tools/pg_analytics_sync.py --limit 200
```

## Windows scheduled task

```powershell
powershell -ExecutionPolicy Bypass -File tools/register_pg_analytics_task.ps1 -TaskName "SpeechAnalyticsPgSyncDaily" -RunAt "01:30"
```

Then verify in Task Scheduler and ensure `PG_ANALYTICS_DSN` is available in that runtime environment.
