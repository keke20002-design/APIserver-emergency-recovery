param(
    [string]$BackupRoot = (Join-Path (Split-Path $PSScriptRoot -Parent | Split-Path -Parent) 'api_emergency_backup')
)

$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$publicRoot = Join-Path $BackupRoot 'public'
$privateRoot = Join-Path $BackupRoot 'private'

if ((Resolve-Path $sourceRoot).Path -eq [System.IO.Path]::GetFullPath($BackupRoot)) {
    throw 'The backup folder must be outside the APIserver project.'
}

New-Item -ItemType Directory -Force -Path $publicRoot, $privateRoot | Out-Null

$excludedDirs = @(
    '.git', '.venv', '.pytest_cache', '.claude', '.secrets', '__pycache__',
    'api_emergency_backup', 'drafts'
)
$excludedFiles = @(
    '.env', '.env.*', '*.log', '*.db', '*.pyc', '*.pyo', '*.pem', '*.key',
    '*.p12', '*.pfx', 'google-services.json', '*service-account*.json',
    '*credentials*.json', 'startup_err.txt', '*.log.*', '*.png', '*.html',
    'result.json', 'raw.txt', '*_check.txt', 'test_cells.txt', 'daejeon_test.txt',
    'names2.txt', 'gocheok2.txt', 'questions.md', 'test_replay.py'
)

$robocopyArgs = @($sourceRoot, $publicRoot, '/MIR', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XD')
$robocopyArgs += $excludedDirs | ForEach-Object { Join-Path $sourceRoot $_ }
$robocopyArgs += '/XF'
$robocopyArgs += $excludedFiles
& robocopy @robocopyArgs | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "Public backup copy failed (robocopy exit code $LASTEXITCODE)."
}

if (Test-Path (Join-Path $sourceRoot '.env')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot '.env') -Destination (Join-Path $privateRoot '.env') -Force
}
if (Test-Path (Join-Path $sourceRoot '.secrets')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot '.secrets') -Destination $privateRoot -Recurse -Force
}

$forbiddenNames = Get-ChildItem -LiteralPath $publicRoot -Recurse -Force -File | Where-Object {
    $_.Name -eq '.env' -or
    $_.FullName -match '[\\/]\.secrets[\\/]' -or
    $_.Name -match '(?i)(service-account|credentials).*\.json$' -or
    $_.Extension -match '^\.(pem|key|p12|pfx)$'
}
if ($forbiddenNames) {
    throw ('Secret file found in public backup: ' + (($forbiddenNames.FullName) -join ', '))
}

$secretPattern = '(?i)(api[_-]?key|secret|token|password|passwd|private[_-]?key|client[_-]?secret)\s*[:=]\s*["''][A-Za-z0-9_./+\-=]{20,}'
$suspects = Get-ChildItem -LiteralPath $publicRoot -Recurse -File |
    Where-Object { $_.Extension -match '^\.(py|ps1|bat|json|yaml|yml|toml|ini|md|txt)$' -and $_.Name -ne '.env.example' } |
    Select-String -Pattern $secretPattern -ErrorAction SilentlyContinue
if ($suspects) {
    $locations = $suspects | ForEach-Object { '{0}:{1}' -f $_.Path, $_.LineNumber }
    throw ('Possible secret value found in public backup: ' + ($locations -join ', '))
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
Set-Content -LiteralPath (Join-Path $publicRoot 'BACKUP_UPDATED.txt') -Value $stamp -Encoding utf8
Set-Content -LiteralPath (Join-Path $privateRoot 'BACKUP_UPDATED.txt') -Value $stamp -Encoding utf8

Write-Host "Public backup: $publicRoot"
Write-Host "Private backup: $privateRoot"
Write-Host 'Secret scan: passed'
