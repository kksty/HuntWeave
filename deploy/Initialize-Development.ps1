[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

$repository = Split-Path $PSScriptRoot -Parent
$secretDirectory = Join-Path $repository 'runtime\secrets'
if (-not (Test-Path -LiteralPath $secretDirectory)) {
    New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
}

foreach ($name in @('access_key', 'runner_token', 'postgres_password', 'app_db_password', 'checkpoint_db_password', 'migrator_db_password')) {
    $filename = Join-Path $secretDirectory $name
    if (Test-Path -LiteralPath $filename) {
        if ((Get-Content -Raw -LiteralPath $filename).Trim().Length -lt 64) {
            throw "Existing secret file is empty or too short: $name"
        }
        continue
    }
    $bytes = New-Object byte[] 32
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    $value = [System.BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    [System.IO.File]::WriteAllText($filename, $value, [System.Text.UTF8Encoding]::new($false))
}
Write-Output 'Development secrets are ready under runtime/secrets; no secret values were printed.'
