# Data Backup

This project includes a one-click backup script for the `data/` directory.

## Quick Start

- Double-click `backup_data.bat`
- The archive will be created under `backups/`

## PowerShell Usage

- Full backup:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File .\backup_data.ps1`
- Dry run (size/count preview only):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File .\backup_data.ps1 -DryRun`
- Custom destination:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File .\backup_data.ps1 -DestinationDir D:\my_backups`

## Output Format

- Archive name: `data-backup-YYYYMMDD-HHMMSS.zip`
