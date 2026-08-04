function Get-BentoRepositoryRoot {
    param([Parameter(Mandatory = $true)][string]$ScriptsDirectory)

    return [System.IO.Path]::GetFullPath((Join-Path $ScriptsDirectory ".."))
}

function Resolve-BentoLauncherPath {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Value
    )

    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Repository $Value))
}

function Get-BentoDisplayPath {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $prefix = $Repository.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($Value.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $Value.Substring($prefix.Length).Replace('\', '/')
    }
    return [System.IO.Path]::GetFileName($Value)
}

function Get-BentoEditorStatus {
    param(
        [Parameter(Mandatory = $true)][string]$HostAddress,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 2
    )

    try {
        $payload = Invoke-RestMethod -Method Get -Uri ("http://{0}:{1}/api/status" -f $HostAddress, $Port) -TimeoutSec $TimeoutSeconds
        $properties = @($payload.PSObject.Properties.Name)
        foreach ($required in @('target', 'revision', 'validation', 'runtimeFingerprint')) {
            if ($properties -notcontains $required) {
                return $null
            }
        }
        if ([string]::IsNullOrWhiteSpace([string]$payload.target) -or
            [string]::IsNullOrWhiteSpace([string]$payload.revision) -or
            [string]::IsNullOrWhiteSpace([string]$payload.validation)) {
            return $null
        }
        return $payload
    }
    catch {
        return $null
    }
}

function Test-BentoStatusTarget {
    param(
        [Parameter(Mandatory = $true)]$Status,
        [Parameter(Mandatory = $true)][string]$ExpectedTarget
    )

    return [string]::Equals([string]$Status.target, $ExpectedTarget, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-BentoPortOpen {
    param(
        [Parameter(Mandatory = $true)][string]$HostAddress,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 500
    )

    $client = New-Object System.Net.Sockets.TcpClient
    $waitHandle = $null
    try {
        $pending = $client.BeginConnect($HostAddress, $Port, $null, $null)
        $waitHandle = $pending.AsyncWaitHandle
        if (-not $waitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $client.EndConnect($pending)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $waitHandle) {
            $waitHandle.Close()
        }
        $client.Close()
    }
}

function Get-BentoProcessSnapshot {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [pscustomobject]@{ Exists = $false; ProcessId = $ProcessId; StartTimeUtc = $null; CommandLine = $null }
    }

    $commandLine = $null
    try {
        $record = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop
        $commandLine = [string]$record.CommandLine
    }
    catch {
        $commandLine = $null
    }

    return [pscustomobject]@{
        Exists = $true
        ProcessId = $ProcessId
        StartTimeUtc = $process.StartTime.ToUniversalTime().ToString('o')
        CommandLine = $commandLine
    }
}

function Test-BentoSessionProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]$Session,
        [Parameter(Mandatory = $true)][string]$Repository
    )

    $snapshot = Get-BentoProcessSnapshot -ProcessId ([int]$Session.pid)
    if (-not $snapshot.Exists) {
        return [pscustomobject]@{ Valid = $false; Exists = $false; Reason = 'process does not exist'; Snapshot = $snapshot }
    }

    try {
        $expectedStart = [System.DateTimeOffset]::Parse([string]$Session.processStartTimeUtc).UtcDateTime
        $actualStart = [System.DateTimeOffset]::Parse([string]$snapshot.StartTimeUtc).UtcDateTime
    }
    catch {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time is invalid'; Snapshot = $snapshot }
    }
    if ([Math]::Abs(($expectedStart - $actualStart).TotalMilliseconds) -gt 100) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process start time does not match'; Snapshot = $snapshot }
    }

    if ([string]::IsNullOrWhiteSpace([string]$snapshot.CommandLine)) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line is unavailable'; Snapshot = $snapshot }
    }
    if ($snapshot.CommandLine -notmatch '(?i)(?:^|\s)-m\s+scripts\.run_bento_work_editor(?:\s|$)') {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line is not the Bento Work editor'; Snapshot = $snapshot }
    }

    $targetMatches = $snapshot.CommandLine.IndexOf([string]$Session.target, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $repositoryMatches = $snapshot.CommandLine.IndexOf($Repository, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not $targetMatches -and -not $repositoryMatches) {
        return [pscustomobject]@{ Valid = $false; Exists = $true; Reason = 'process command line does not match target or repository'; Snapshot = $snapshot }
    }

    return [pscustomobject]@{ Valid = $true; Exists = $true; Reason = 'match'; Snapshot = $snapshot }
}

function Enter-BentoLauncherLock {
    param([Parameter(Mandatory = $true)][string]$Repository)

    $stateDirectory = Join-Path $Repository 'output'
    [System.IO.Directory]::CreateDirectory($stateDirectory) | Out-Null
    $lockPath = Join-Path $stateDirectory 'work-editor-launcher.lock'
    try {
        $stream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        return [pscustomobject]@{ Stream = $stream; Acquired = $true; Path = $lockPath }
    }
    catch [System.IO.IOException] {
        return [pscustomobject]@{ Stream = $null; Acquired = $false; Path = $lockPath }
    }
}

function Exit-BentoLauncherLock {
    param($Handle)

    if ($null -eq $Handle) {
        return
    }
    if ($Handle.Acquired) {
        try { $Handle.Stream.Dispose() } catch { }
        try { Remove-Item -LiteralPath $Handle.Path -Force -ErrorAction SilentlyContinue } catch { }
    }
}

function ConvertTo-BentoProcessArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Argument)

    if ($Argument.Contains('"')) {
        throw 'A process argument unexpectedly contains a double quote.'
    }
    if ($Argument.Length -eq 0 -or $Argument -match '\s') {
        return '"' + $Argument + '"'
    }
    return $Argument
}

function Copy-BentoUrlToClipboard {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        Set-Clipboard -Value $Url -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}
