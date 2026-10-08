[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This validation profile requires Windows PowerShell 7.' }

$repository = Split-Path $PSScriptRoot -Parent
$runId = [guid]::NewGuid().ToString('N')
$resultDirectory = Join-Path $repository "runtime\isolation\$runId"
New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null
$hostOS = (Get-CimInstance Win32_OperatingSystem).Caption
$hostAddresses = @(Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress -Unique)
$configuration = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.wslconfig'
$networkingMode = 'nat'
if (Test-Path -LiteralPath $configuration) {
    $modeLine = Get-Content -LiteralPath $configuration | Where-Object { $_ -match '^\s*networkingMode\s*=' } | Select-Object -Last 1
    if ($modeLine) { $networkingMode = ($modeLine -split '=', 2)[1].Trim().ToLowerInvariant() }
}
if ($networkingMode -ne 'nat') { throw 'Only the WSL2 NAT profile is currently specified.' }

$wslProcess = [System.Diagnostics.ProcessStartInfo]::new()
$wslProcess.FileName = "$env:WINDIR\System32\wsl.exe"
$wslProcess.ArgumentList.Add('--version')
$wslProcess.RedirectStandardOutput = $true
$wslProcess.StandardOutputEncoding = [System.Text.Encoding]::Unicode
$wslProcess.UseShellExecute = $false
$versionProcess = [System.Diagnostics.Process]::Start($wslProcess)
$wslVersionText = $versionProcess.StandardOutput.ReadToEnd()
$versionProcess.WaitForExit()
$wslVersion = [regex]::Match($wslVersionText, '\d+\.\d+\.\d+\.\d+').Value
if ($versionProcess.ExitCode -ne 0 -or -not $wslVersion) { throw 'Unable to identify the installed WSL version.' }

Push-Location $repository
try {
    & docker version --format '{{.Server.Version}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop Linux engine is unavailable.' }
    & docker build -f lab/isolation/Dockerfile --target probe -t huntweave-isolation-probe:p0 .
    if ($LASTEXITCODE -ne 0) { throw 'Probe image build failed.' }
    & docker build -f lab/isolation/Dockerfile --target manager -t huntweave-isolation-manager:p0 .
    if ($LASTEXITCODE -ne 0) { throw 'Lab manager image build failed.' }
    $revision = (& git rev-parse HEAD).Trim()
    $treeDirty = if (& git status --porcelain) { 'true' } else { 'false' }
    # Only this fixed trusted manager gets the socket. No platform secret is passed to the lab.
    & docker run --rm --name "huntweave-isolation-manager-$runId" --network none --read-only `
        --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 --memory 256m `
        --tmpfs '/tmp:size=16m,mode=1777' `
        --mount 'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock' `
        --mount "type=bind,source=$resultDirectory,target=/results" `
        --env HUNTWEAVE_HOST_PLATFORM=windows --env "HUNTWEAVE_HOST_OS=$hostOS" `
        --env "HUNTWEAVE_WSL_NETWORKING=$networkingMode" --env "HUNTWEAVE_WSL_VERSION=$wslVersion" `
        --env "HUNTWEAVE_HOST_ADDRESSES=$($hostAddresses -join ',')" --env "HUNTWEAVE_SOURCE_TREE_DIRTY=$treeDirty" `
        --env "HUNTWEAVE_SOURCE_REVISION=$revision" huntweave-isolation-manager:p0
    $resultCode = $LASTEXITCODE
    Write-Output "Validation report: $resultDirectory\report.json"
    if ($resultCode -ne 0) { throw 'Isolation validation failed; inspect the real report and cleanup status.' }
} finally {
    Pop-Location
}
