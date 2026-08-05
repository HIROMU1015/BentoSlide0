[CmdletBinding()]
param([ValidateRange(1, 120)][int]$ShutdownTimeoutSeconds = 10)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
Set-Location -LiteralPath $repository
$stateDirectory = Join-Path $repository 'output'
$pidPath = Join-Path $stateDirectory 'html-preview.pid'
$sessionPath = Join-Path $stateDirectory 'html-preview-session.json'
$logPath = Join-Path $stateDirectory 'html-preview.log'
$lockHandle = $null

function Get-HtmlPreviewStatus {
    param([int]$Port)
    try {
        $payload = Invoke-RestMethod -Method Get -Uri ("http://127.0.0.1:{0}/api/status" -f $Port) -TimeoutSec 2
        if ([string]$payload.format -ne 'bento/html-preview-status/v1') { return $null }
        return $payload
    }
    catch { return $null }
}

function Test-HtmlPreviewSessionIdentity {
    param($Session)
    $snapshot = Get-BentoProcessSnapshot -ProcessId ([int]$Session.pid)
    if (-not $snapshot.Exists) { return [pscustomobject]@{ Valid = $false; Exists = $false; Reason = 'process does not exist' } }
    try {
        $expected = [System.DateTimeOffset]::Parse([string]$Session.processStartTimeUtc).UtcDateTime
        $actual = [System.DateTimeOffset]::Parse([string]$snapshot.StartTimeUtc).UtcDateTime
    }
    catch { return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time is invalid' } }
    if ([Math]::Abs(($expected - $actual).TotalMilliseconds) -gt 100) { return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time does not match' } }
    if ([string]::IsNullOrWhiteSpace([string]$snapshot.CommandLine) -or $snapshot.CommandLine -notmatch '(?i)(?:^|\s)-m\s+scripts\.run_html_preview(?:\s|$)') {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line is not the HTML preview' }
    }
    if ($snapshot.CommandLine.IndexOf($repository, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process repository does not match' }
    }
    return [pscustomobject]@{ Valid = $true; Exists = $true; Reason = 'match' }
}

function Clear-CurrentUrl {
    try {
        $python = Find-BentoLauncherPython -Repository $repository -RequiredImports @('yaml', 'jsonschema', 'scripts.deck_workflow')
        & $python.Executable -m scripts.deck_workflow --root $repository clear-current-url | Out-Null
    }
    catch { Write-Host 'Could not clear preview.currentUrl; inspect deck.yaml.' -ForegroundColor Yellow }
}

try {
    $lockHandle = Enter-BentoFileLock -Repository $repository -Name 'html-preview-launcher.lock'
    if (-not $lockHandle.Acquired) { throw 'Another HTML preview launcher is already working. Try again shortly.' }
    if (-not (Test-Path -LiteralPath $sessionPath -PathType Leaf)) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Clear-CurrentUrl
        Write-Host 'BentoSlide HTML preview is already stopped.'
        exit 0
    }
    try { $session = Get-Content -LiteralPath $sessionPath -Raw -Encoding utf8 | ConvertFrom-Json }
    catch { throw "Cannot read the HTML preview session safely: $sessionPath" }
    if ([string]$session.format -ne 'bento/html-preview-session/v1') { throw "Unknown HTML preview session format: $($session.format)" }
    if (-not [string]::Equals([string]$session.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Session repository mismatch; no process was stopped.' }
    if ([string]$session.host -ne '127.0.0.1' -or [int]$session.port -lt 1 -or [int]$session.port -gt 65535) { throw 'Unsafe session host/port; no process was stopped.' }
    if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) { throw 'PID file is missing; no process was stopped.' }
    $recordedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw -Encoding ascii).Trim(), [ref]$recordedPid) -or $recordedPid -ne [int]$session.pid) {
        throw 'PID and session mismatch; no process was stopped.'
    }
    $identity = Test-HtmlPreviewSessionIdentity -Session $session
    if (-not $identity.Exists) {
        Remove-Item -LiteralPath $pidPath,$sessionPath -Force -ErrorAction SilentlyContinue
        Clear-CurrentUrl
        Write-Host 'HTML preview was already stopped; stale state was removed.'
        exit 0
    }
    if (-not $identity.Valid) { throw "Preview PID validation failed; no process was stopped: $($identity.Reason)" }
    $status = Get-HtmlPreviewStatus -Port ([int]$session.port)
    if ($null -eq $status -or -not [string]::Equals([string]$status.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Preview status does not match the recorded repository; no process was stopped.'
    }
    $finalIdentity = Test-HtmlPreviewSessionIdentity -Session $session
    if (-not $finalIdentity.Valid) { throw "Final PID validation failed; no process was stopped: $($finalIdentity.Reason)" }
    Stop-Process -Id $recordedPid -Force -ErrorAction Stop
    $deadline = [System.DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 200
        $exists = $null -ne (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)
        $open = Test-BentoPortOpen -HostAddress '127.0.0.1' -Port ([int]$session.port) -TimeoutMilliseconds 200
        if (-not $exists -and -not $open) { break }
    } while ([System.DateTime]::UtcNow -lt $deadline)
    if ($exists -or $open) { throw 'Preview process or port did not stop; session state was retained.' }
    Remove-Item -LiteralPath $pidPath,$sessionPath -Force
    Clear-CurrentUrl
    if (Test-Path -LiteralPath $logPath) { Add-Content -LiteralPath $logPath -Value @("stoppedAt=$([System.DateTimeOffset]::Now.ToString('o'))", "pid=$recordedPid", 'status=stopped') -Encoding utf8 }
    Write-Host 'BentoSlide HTML preview stopped. Chapter files and logs were retained.'
    exit 0
}
catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
finally { Exit-BentoLauncherLock -Handle $lockHandle }
