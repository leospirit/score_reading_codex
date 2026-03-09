@echo off
setlocal
cd /d %~dp0\..

python tools\pg_analytics_sync.py --report-root data\out --schema-sql analytics_pg\schema.sql %*

endlocal
