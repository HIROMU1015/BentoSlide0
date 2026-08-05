[CmdletBinding()]
param(
    [ValidateRange(0, 65535)][int]$Port = 0,
    [ValidateRange(1, 300)][int]$StartupTimeoutSeconds = 20,
    [switch]$NoClipboard
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
Set-Location -LiteralPath $repository
$hostAddress = '127.0.0.1'
$stateDirectory = Join-Path $repository 'output'
$pidPath = Join-Path $stateDirectory 'html-preview.pid'
$sessionPath = Join-Path $stateDirectory 'html-preview-session.json'
$logPath = Join-Path $stateDirectory 'html-preview.log'
$stdoutLogPath = Join-Path $stateDirectory 'html-preview.stdout.log'
$errorLogPath = Join-Path $stateDirectory 'html-preview.error.log'
$lockHandle = $null
$startedProcess = $null

function Get-HtmlPreviewStatus {
    param([Parameter(Mandatory = $true)][int]$StatusPort, [int]$TimeoutSeconds = 2)
    try {
        $payload = Invoke-RestMethod -Method Get -Uri ("http://127.0.0.1:{0}/api/status" -f $StatusPort) -TimeoutSec $TimeoutSeconds
        if ([string]$payload.format -ne 'bento/html-preview-status/v1' -or
            -not [string]::Equals([string]$payload.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
        return $payload
    }
    catch { return $null }
}

function Test-HtmlPreviewSessionIdentity {
    param([Parameter(Mandatory = $true)]$Session)
    $snapshot = Get-BentoProcessSnapshot -ProcessId ([int]$Session.pid)
    if (-not $snapshot.Exists) { return [pscustomobject]@{ Valid = $false; Exists = $false; Reason = 'process does not exist'; Snapshot = $snapshot } }
    try {
        $expected = [System.DateTimeOffset]::Parse([string]$Session.processStartTimeUtc).UtcDateTime
        $actual = [System.DateTimeOffset]::Parse([string]$snapshot.StartTimeUtc).UtcDateTime
    }
    catch { return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time is invalid'; Snapshot = $snapshot } }
    if ([Math]::Abs(($expected - $actual).TotalMilliseconds) -gt 100) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time does not match'; Snapshot = $snapshot }
    }
    if ([string]::IsNullOrWhiteSpace([string]$snapshot.CommandLine) -or
        $snapshot.CommandLine -notmatch '(?i)(?:^|\s)-m\s+scripts\.run_html_preview(?:\s|$)') {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line is not the HTML preview'; Snapshot = $snapshot }
    }
    if ($snapshot.CommandLine.IndexOf($repository, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process repository does not match'; Snapshot = $snapshot }
    }
    return [pscustomobject]@{ Valid = $true; Exists = $true; Reason = 'match'; Snapshot = $snapshot }
}

function Add-HtmlPreviewLog {
    param([Parameter(Mandatory = $true)][string[]]$Lines)
    Add-Content -LiteralPath $logPath -Value $Lines -Encoding utf8
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $repository 'scripts\run_html_preview.py') -PathType Leaf)) {
        throw 'scripts/run_html_preview.py was not found.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repository 'deck.yaml') -PathType Leaf)) {
        throw 'deck.yaml was not found.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repository 'chapters') -PathType Container)) {
        throw 'chapters/ was not found.'
    }
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $lockHandle = Enter-BentoFileLock -Repository $repository -Name 'html-preview-launcher.lock'
    if (-not $lockHandle.Acquired) { throw 'Another HTML preview launcher is already working. Try again in a few seconds.' }

    $python = Find-BentoLauncherPython -Repository $repository -RequiredImports @('bento_converter', 'yaml', 'jsonschema', 'scripts.deck_workflow')
    if ($Port -eq 0) {
        $stateJson = & $python.Executable -m scripts.deck_workflow --root $repository status --json 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Cannot read deck workflow state: $($stateJson -join ' ')" }
        $deck = ($stateJson -join "`n") | ConvertFrom-Json
        $Port = [int]$deck.preview.htmlPort
    }
    if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid HTML preview port: $Port" }
    $url = "http://${hostAddress}:$Port/"

    $session = $null
    if (Test-Path -LiteralPath $sessionPath -PathType Leaf) {
        try { $session = Get-Content -LiteralPath $sessionPath -Raw -Encoding utf8 | ConvertFrom-Json }
        catch { throw "Cannot read the existing HTML preview session: $sessionPath" }
        if ([string]$session.format -ne 'bento/html-preview-session/v1') { throw "Unknown HTML preview session format: $($session.format)" }
        if (-not [string]::Equals([string]$session.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'The existing HTML preview session belongs to another repository.'
        }
    }

    $status = Get-HtmlPreviewStatus -StatusPort $Port
    if ($null -ne $status) {
        if ($null -eq $session) { throw 'An untracked HTML preview is already using this port; it will not be stopped or adopted.' }
        if ([int]$session.port -ne $Port) { throw 'The recorded HTML preview session uses a different port.' }
        $identity = Test-HtmlPreviewSessionIdentity -Session $session
        if (-not $identity.Valid) { throw "The existing preview PID cannot be verified safely: $($identity.Reason)" }
        & $python.Executable -m scripts.deck_workflow --root $repository set-current-url --url $url | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Cannot update preview.currentUrl.' }
        if (-not $NoClipboard) { Copy-BentoUrlToClipboard -Url $url | Out-Null }
        Write-Host "BentoSlide HTML preview is already running.`n$url"
        exit 0
    }
    if (Test-BentoPortOpen -HostAddress $hostAddress -Port $Port) {
        throw "Port $Port is used by another service. It will not be stopped. Try start_html_preview.cmd -Port 4174"
    }
    if ($null -ne $session) {
        $identity = Test-HtmlPreviewSessionIdentity -Session $session
        if ($identity.Exists) { throw "The recorded preview process exists but status is unavailable: $($identity.Reason)" }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $sessionPath -Force -ErrorAction SilentlyContinue
    }

    foreach ($path in @($logPath, $stdoutLogPath, $errorLogPath)) {
        $previous = $path + '.previous.log'
        if (Test-Path -LiteralPath $previous) { Remove-Item -LiteralPath $previous -Force }
        if (Test-Path -LiteralPath $path) { Move-Item -LiteralPath $path -Destination $previous -Force }
    }
    Set-Content -LiteralPath $logPath -Value @(
        "startedAt=$([System.DateTimeOffset]::Now.ToString('o'))", "python=$($python.Executable)",
        "repository=$repository", "host=$hostAddress", "port=$Port", "url=$url", 'status=starting'
    ) -Encoding utf8

    $arguments = @('-u', '-m', 'scripts.run_html_preview', '--root', $repository, '--host', $hostAddress, '--port', [string]$Port)
    $argumentLine = ($arguments | ForEach-Object { ConvertTo-BentoProcessArgument -Argument ([string]$_) }) -join ' '
    $previousPythonUtf8 = $env:PYTHONUTF8
    $previousPythonIoEncoding = $env:PYTHONIOENCODING
    $env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
    try {
        $startedProcess = Start-Process -FilePath $python.Executable -ArgumentList $argumentLine -WorkingDirectory $repository `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutLogPath -RedirectStandardError $errorLogPath
    }
    finally {
        if ($null -eq $previousPythonUtf8) { Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue } else { $env:PYTHONUTF8 = $previousPythonUtf8 }
        if ($null -eq $previousPythonIoEncoding) { Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue } else { $env:PYTHONIOENCODING = $previousPythonIoEncoding }
    }

    $deadline = [System.DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $startedStatus = $null
    do {
        Start-Sleep -Milliseconds 200
        $startedProcess.Refresh()
        if ($startedProcess.HasExited) { break }
        $startedStatus = Get-HtmlPreviewStatus -StatusPort $Port -TimeoutSeconds 1
        if ($null -ne $startedStatus) { break }
    } while ([System.DateTime]::UtcNow -lt $deadline)
    if ($null -eq $startedStatus) { throw "HTML preview did not become ready within $StartupTimeoutSeconds seconds." }

    $snapshot = Get-BentoProcessSnapshot -ProcessId $startedProcess.Id
    if (-not $snapshot.Exists -or [string]::IsNullOrWhiteSpace([string]$snapshot.StartTimeUtc)) { throw 'Cannot capture the HTML preview process identity.' }
    $record = [ordered]@{
        format = 'bento/html-preview-session/v1'; pid = $startedProcess.Id; startedAt = [System.DateTimeOffset]::Now.ToString('o')
        processStartTimeUtc = $snapshot.StartTimeUtc; repository = $repository; python = $python.Executable
        host = $hostAddress; port = $Port; url = $url
    }
    $temporary = $sessionPath + '.tmp'
    $record | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $sessionPath -Force
    Set-Content -LiteralPath $pidPath -Value ([string]$startedProcess.Id) -Encoding ascii
    & $python.Executable -m scripts.deck_workflow --root $repository set-current-url --url $url | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot update preview.currentUrl.' }
    Add-HtmlPreviewLog -Lines @("pid=$($startedProcess.Id)", 'status=started')
    if (-not $NoClipboard -and -not (Copy-BentoUrlToClipboard -Url $url)) { Write-Host 'Clipboard copy failed; the preview remains running.' -ForegroundColor Yellow }
    Write-Host "BentoSlide HTML preview started.`n$url`nOpen or reload this URL in the ChatGPT Work browser."
    exit 0
}
catch {
    if ($null -ne $startedProcess) {
        try { $startedProcess.Refresh(); if (-not $startedProcess.HasExited) { Stop-Process -Id $startedProcess.Id -Force -ErrorAction SilentlyContinue } } catch { }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $sessionPath -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $logPath) { Add-HtmlPreviewLog -Lines @("failedAt=$([System.DateTimeOffset]::Now.ToString('o'))", "error=$($_.Exception.Message)", 'status=failed') }
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (Test-Path -LiteralPath $errorLogPath) { Get-Content -LiteralPath $errorLogPath -Tail 20 -ErrorAction SilentlyContinue }
    exit 1
}
finally { Exit-BentoLauncherLock -Handle $lockHandle }
