param(
    [string]$DestinationDir = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $projectRoot "data"

if (-not (Test-Path -LiteralPath $dataDir -PathType Container)) {
    throw "Data directory not found: $dataDir"
}

if ([string]::IsNullOrWhiteSpace($DestinationDir)) {
    $DestinationDir = Join-Path $projectRoot "backups"
}

if (-not (Test-Path -LiteralPath $DestinationDir -PathType Container)) {
    New-Item -ItemType Directory -Path $DestinationDir -Force | Out-Null
}

$files = Get-ChildItem -LiteralPath $dataDir -Recurse -File -ErrorAction SilentlyContinue
$fileCount = @($files).Count
$totalBytes = ($files | Measure-Object -Property Length -Sum).Sum
if ($null -eq $totalBytes) {
    $totalBytes = 0
}
$totalGb = [math]::Round(($totalBytes / 1GB), 3)

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zipPath = Join-Path $DestinationDir "data-backup-$timestamp.zip"

if ($DryRun) {
    [pscustomobject]@{
        mode = "dry-run"
        project_root = $projectRoot
        data_dir = $dataDir
        destination_dir = $DestinationDir
        file_count = [int]$fileCount
        total_bytes = [int64]$totalBytes
        total_gb = $totalGb
        output_zip = $zipPath
    } | Format-List
    exit 0
}

Write-Host "Backing up data directory..."
Write-Host "Source: $dataDir"
Write-Host "Files: $fileCount"
Write-Host ("Size : {0} GB" -f $totalGb)
Write-Host "Output: $zipPath"

Compress-Archive -Path (Join-Path $dataDir "*") -DestinationPath $zipPath -CompressionLevel Optimal -Force

$zipInfo = Get-Item -LiteralPath $zipPath
$zipMb = [math]::Round(($zipInfo.Length / 1MB), 2)

Write-Host ""
Write-Host "Backup completed successfully."
Write-Host ("Archive size: {0} MB" -f $zipMb)
Write-Host "Archive path: $zipPath"
