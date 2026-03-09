param(
    [string]$TaskName = "SpeechAnalyticsPgSyncDaily",
    [string]$RunAt = "01:30",
    [string]$PythonExe = "python"
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$scriptPath = Join-Path $repoRoot "tools\pg_analytics_sync.py"

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$scriptPath`" --report-root data/out --schema-sql analytics_pg/schema.sql"
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Write-Host "Registered task: $TaskName at $RunAt"
