# Register native GitHub issue dependencies for the P2 slices.
#
# Policy: docs/agents/p2-execution-batches.md section 5 - only hard and partial
# gates become native `blocked_by` links; soft references stay in the issue text.
# Registering the missing links is the step the interrupted publication left out.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/link-issue-dependencies.ps1 -PlanDir <dir>
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/link-issue-dependencies.ps1 -PlanDir <dir> -Apply
#
# <dir> must contain catalog.json and mapping.json (output of the publish step).
# The script is idempotent: existing edges are skipped, only missing ones are
# created, and every hard gate is re-read at the end to verify.
#
# NOTE: this file is deliberately ASCII-only. Windows PowerShell 5.1 reads .ps1
# files as ANSI when they carry no BOM, so non-ASCII text here would be decoded
# wrongly and would break the parser mid-file.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PlanDir,
    [string]$Repo = 'kksty/HuntWeave',
    [switch]$Apply,
    [int]$ThrottleMs = 900
)

$ErrorActionPreference = 'Stop'

# Read the blocked_by set of one issue as an int array.
# Do not wrap the ConvertFrom-Json output in @(): when the endpoint returns an
# array, @() makes PowerShell treat the whole array as one element and the
# ForEach-Object below then sees Object[] instead of one dependency per item.
function Get-BlockedBy {
    param([int]$Issue)
    $json = gh api "repos/$Repo/issues/$Issue/dependencies/blocked_by?per_page=100" | Out-String
    if ($LASTEXITCODE -ne 0) { throw "cannot read dependencies of #$Issue" }
    $parsed = $json | ConvertFrom-Json
    $result = @()
    foreach ($item in $parsed) { $result += [int]$item.number }
    return $result
}

# --- 1. Read the plan (explicit UTF-8: Windows PowerShell 5.1 Get-Content defaults
#        to ANSI, which corrupts the UTF-8 Chinese and breaks JSON parsing) ---
$catalogPath = Join-Path $PlanDir 'catalog.json'
$mappingPath = Join-Path $PlanDir 'mapping.json'
$catalog = [System.IO.File]::ReadAllText($catalogPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$mapping = [System.IO.File]::ReadAllText($mappingPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json

$number = @{}
foreach ($e in $catalog.existing) { $number[$e.key] = [int]$e.number }
foreach ($p in $mapping.PSObject.Properties) { $number[$p.Name] = [int]$p.Value.number }
$number['e20'] = 20

# --- 2. Soft references: never written as native blocked_by (see section 5) ---
$SOFT = @{
    'G0'  = @('e21')                  # #21 is the P1 capacity-release acceptance
    'M1'  = @('e21')                  # maintenance ticket, unrelated to capacity
    'M3'  = @('e21')                  # its diagnosis precedes #21's capacity result
    'M4'  = @('e21')                  # maintenance ticket, unrelated to capacity
    'E1'  = @('e21', 'e32')           # pooling can use the existing P1 baseline
    'X2'  = @('e32')                  # tool-baseline call cites #32 data
    'e30' = @('e32')                  # batch import cites #32 data
    'F1'  = @('E2')                   # partial gate on retention/cursor semantics
}

# --- 3. Build the desired hard-gate edge set ---
$desired = New-Object System.Collections.ArrayList
$softSkipped = New-Object System.Collections.ArrayList

$sources = @()
foreach ($t in $catalog.tickets)  { $sources += [pscustomobject]@{ key = $t.key; deps = @($t.deps) } }
foreach ($e in $catalog.existing) { $sources += [pscustomobject]@{ key = $e.key; deps = @($e.deps) } }

foreach ($s in $sources) {
    if (-not $number.ContainsKey($s.key)) { throw "plan has no issue number for $($s.key)" }
    $skip = @()
    if ($SOFT.ContainsKey($s.key)) { $skip = @($SOFT[$s.key]) }
    foreach ($d in $s.deps) {
        if ($d -eq 'e20') { continue }                       # #20 is closed
        if (-not $number.ContainsKey($d)) { throw "dependency $($s.key) -> $d has no issue number" }
        if ($skip -contains $d) {
            [void]$softSkipped.Add([pscustomobject]@{ issue = $number[$s.key]; blocked_by = $number[$d] })
            continue
        }
        [void]$desired.Add([pscustomobject]@{ issue = $number[$s.key]; blocked_by = $number[$d]; key = $s.key; dep = $d })
    }
}

Write-Output ("desired hard-gate edges: {0}; soft references (not linked): {1}" -f $desired.Count, $softSkipped.Count)

# --- 4. Current state: one paginated read gives every issue's blocked_by count ---
# No @(...) wrapper: wrapping a pipeline that returns one array makes PowerShell
# treat the whole array as a single element, so the loop below would see one
# Object[] instead of one issue per iteration.
$raw = gh api "repos/$Repo/issues?state=all&per_page=100" --paginate | Out-String
if ($LASTEXITCODE -ne 0) { throw 'cannot inventory issues' }
$all = $raw | ConvertFrom-Json

$count = @{}
foreach ($i in $all) {
    if ($i.PSObject.Properties.Name -contains 'issue_dependencies_summary') {
        $count[[int]$i.number] = [int]$i.issue_dependencies_summary.blocked_by
    }
}
Write-Output ("read dependency counters for {0} issues" -f $count.Count)

# A zero counter proves there is no edge. A non-zero counter only proves "some
# edge", so fetch the real set for those issues (never skip a whole issue on a count).
$have = @{}
foreach ($n in $count.Keys) { if ($count[$n] -gt 0) { $have[$n] = @() } }
foreach ($n in @($have.Keys)) {
    $have[$n] = Get-BlockedBy -Issue $n
    Start-Sleep -Milliseconds 200
}
foreach ($n in $count.Keys) { if (-not $have.ContainsKey($n)) { $have[$n] = @() } }

# --- 5. Idempotent write ---
$created = 0
$skipped = 0
foreach ($d in $desired) {
    if (@($have[$d.issue]) -contains $d.blocked_by) { $skipped++; continue }
    if (-not $Apply) {
        Write-Output ("DRY-RUN  LINK #{0} <- #{1}   ({2} <- {3})" -f $d.issue, $d.blocked_by, $d.key, $d.dep)
        continue
    }
    $depId = (gh api "repos/$Repo/issues/$($d.blocked_by)" | ConvertFrom-Json).id
    gh api --method POST "repos/$Repo/issues/$($d.issue)/dependencies/blocked_by" -F "issue_id=$depId" --silent | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "write failed: #$($d.issue) <- #$($d.blocked_by)" }
    Write-Output ("LINK #{0} <- #{1}" -f $d.issue, $d.blocked_by)
    $created++
    Start-Sleep -Milliseconds $ThrottleMs
}
Write-Output ("write done: created {0}, already present (skipped) {1}" -f $created, $skipped)

if (-not $Apply) { Write-Output 'DRY-RUN finished: nothing written. Re-run with -Apply.'; return }

# --- 6. Verify every hard gate by reading it back ---
# Only *missing* edges are a failure. An edge that exists on GitHub but is not in
# the plan is reported as extra: it may predate this plan (e.g. an edge onto an
# already-closed ticket) and dropping it is a judgement call, not a repair.
$missing = @()
$extra = @()
foreach ($n in ($desired | ForEach-Object { $_.issue } | Sort-Object -Unique)) {
    $expect = @($desired | Where-Object { $_.issue -eq $n } | ForEach-Object { $_.blocked_by } | Sort-Object)
    $got = @(Get-BlockedBy -Issue $n | Sort-Object)
    $absent = @($expect | Where-Object { $got -notcontains $_ })
    $surplus = @($got | Where-Object { $expect -notcontains $_ })
    if ($absent.Count) { $missing += ("  #{0} missing[{1}]" -f $n, ($absent -join ',')) }
    if ($surplus.Count) { $extra += ("  #{0} extra[{1}]" -f $n, ($surplus -join ',')) }
}
if ($extra.Count) { Write-Output 'NOTE - extra edges present on GitHub (not in plan, left as-is):'; $extra | ForEach-Object { Write-Output $_ } }
if ($missing.Count) {
    Write-Output 'VERIFY FAILED - missing gates:'
    $missing | ForEach-Object { Write-Output $_ }
    exit 1
}
Write-Output ("VERIFY OK: all {0} hard-gate edges present, 0 missing." -f $desired.Count)
