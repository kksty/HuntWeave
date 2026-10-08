[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$composeFile = Join-Path $PSScriptRoot 'compose.yaml'

function Invoke-Compose {
    param([string[]]$CommandArgs)
    & docker compose -f $composeFile @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "Compose command failed: $($CommandArgs[0])" }
}

Invoke-Compose -CommandArgs @('up', '-d', '--wait', '--wait-timeout', '90')

$missingKeyProbe = @'
from fastapi.testclient import TestClient
from huntweave.api.app import create_app
client = TestClient(create_app())
for path in ('/', '/api/v1/system/capabilities', '/docs', '/openapi.json', '/evidence/x'):
    response = client.get(path)
    assert response.status_code == 503, (path, response.status_code)
    assert response.json()['reason_code'] == 'access_key_missing'
assert client.get('/health/live').status_code == 200
print('PASS: missing-key image blocks all business resources')
'@
Invoke-Compose -CommandArgs @('run', '--rm', '-T', '--no-deps', '-e', 'HUNTWEAVE_ACCESS_KEY_FILE=/tmp/absent-key', 'app', 'python', '-c', $missingKeyProbe)

$secretProbe = @'
import os
from pathlib import Path
assert os.getuid() == 10001
assert not Path('/run/secrets/access_key').exists()
assert not Path('/var/run/docker.sock').exists()
assert not Path('/opt/huntweave/.agents').exists()
print('PASS: Runner is non-root and has no global key, Docker socket or development skills')
'@
Invoke-Compose -CommandArgs @('exec', '-T', 'runner', 'python', '-c', $secretProbe)

$readOnlyProbe = @'
from pathlib import Path
try:
    Path('/evidence/.startup-write-probe').write_text('probe')
except OSError as error:
    assert error.errno == 30, error
else:
    raise AssertionError('app evidence archive is writable')
print('PASS: app evidence archive is read-only')
'@
Invoke-Compose -CommandArgs @('exec', '-T', 'app', 'python', '-c', $readOnlyProbe)

# Scope all fault injection to this Compose project's app and Runner services.
try {
    Invoke-Compose -CommandArgs @('stop', 'runner')
    & docker compose -f $composeFile exec -T app python -m huntweave.api.healthcheck
    if ($LASTEXITCODE -eq 0) { throw 'Readiness passed while Runner was stopped.' }
    Write-Output 'PASS: app readiness rejects an unavailable Runner'
} finally {
    Invoke-Compose -CommandArgs @('up', '-d', '--wait', '--wait-timeout', '90')
}

$appId = (& docker compose -f $composeFile ps -q app).Trim()
if (-not $appId) { throw 'App container is missing.' }
$before = [int](& docker inspect --format '{{.RestartCount}}' $appId)
$processProbe = @'
import os
import signal
from pathlib import Path
matches = []
for folder in Path('/proc').iterdir():
    if not folder.name.isdigit():
        continue
    try:
        parts = (folder / 'cmdline').read_bytes().split(b'\0')
    except (FileNotFoundError, PermissionError):
        continue
    if b'huntweave.api.agentd' in parts:
        matches.append(int(folder.name))
assert len(matches) == 1, matches
os.kill(matches[0], signal.SIGTERM)
print('Injected: agentd exit')
'@
Invoke-Compose -CommandArgs @('exec', '-T', 'app', 'python', '-c', $processProbe)
$deadline = (Get-Date).AddSeconds(40)
$restarted = $false
while ((Get-Date) -lt $deadline) {
    $count = [int](& docker inspect --format '{{.RestartCount}}' $appId)
    $health = & docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' $appId
    if ($count -gt $before -and $health -eq 'healthy') { $restarted = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $restarted) { throw 'Supervisor did not restart and recover after agentd exited.' }
Write-Output 'PASS: agentd exit stops the service; restart completes migrations and restores health'
Invoke-Compose -CommandArgs @('ps')
