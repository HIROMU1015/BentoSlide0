[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$Source = 'output\presentation.generated.bento.html',
    [string]$Target = 'output\presentation.final.bento.html',
    [string]$Registry = 'output\diagnostics\merged-registry.json',
    [ValidateRange(1, 300)][int]$StartupTimeoutSeconds = 20,
    [switch]$NoClipboard
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
$hostAddress = '127.0.0.1'
$stateDirectory = Join-Path $repository 'output'
$pidPath = Join-Path $stateDirectory 'work-editor.pid'
$sessionPath = Join-Path $stateDirectory 'work-editor-session.json'
$logPath = Join-Path $stateDirectory 'work-editor.log'
$stdoutLogPath = Join-Path $stateDirectory 'work-editor.stdout.log'
$errorLogPath = Join-Path $stateDirectory 'work-editor.error.log'
$lockHandle = $null
$startedProcess = $null

function Find-BentoPython {
    param([Parameter(Mandatory = $true)][string]$Repository)

    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($relative in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe', 'env\Scripts\python.exe')) {
        $path = Join-Path $Repository $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $candidates.Add([pscustomobject]@{ Command = $path; Prefix = @(); Label = $path })
        }
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        $candidates.Add([pscustomobject]@{ Command = $py.Source; Prefix = @('-3'); Label = ($py.Source + ' -3') })
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $candidates.Add([pscustomobject]@{ Command = $python.Source; Prefix = @(); Label = $python.Source })
    }

    $attempts = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $candidates) {
        Push-Location $Repository
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = & $candidate.Command @($candidate.Prefix) -c "import bento_converter, sys; print(sys.executable)" 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
            Pop-Location
        }
        if ($exitCode -eq 0) {
            $executable = [string]($output | Select-Object -Last 1)
            if (Test-Path -LiteralPath $executable -PathType Leaf) {
                return [pscustomobject]@{ Executable = [System.IO.Path]::GetFullPath($executable); DetectedBy = $candidate.Label }
            }
        }
        $attempts.Add(("{0}: {1}" -f $candidate.Label, (($output | ForEach-Object { [string]$_ }) -join ' ')))
    }

    $details = if ($attempts.Count -gt 0) { $attempts -join "`n" } else { 'Python候補が見つかりませんでした。' }
    throw "Bento Work editorを起動できるPython 3が見つかりません。`nimport bento_converter が成功する環境を用意してください。`n`n$details"
}

function Add-LauncherLog {
    param([Parameter(Mandatory = $true)][string[]]$Lines)
    Add-Content -LiteralPath $logPath -Value $Lines -Encoding utf8
}

try {
    if (-not (Test-Path -LiteralPath $repository -PathType Container)) {
        throw "リポジトリルートが見つかりません: $repository"
    }
    $runner = Join-Path $repository 'scripts\run_bento_work_editor.py'
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
        throw "Work editor起動スクリプトが見つかりません: $runner"
    }

    $sourcePath = Resolve-BentoLauncherPath -Repository $repository -Value $Source
    $targetPath = Resolve-BentoLauncherPath -Repository $repository -Value $Target
    $registryPath = Resolve-BentoLauncherPath -Repository $repository -Value $Registry
    $expectedStatusTarget = Get-BentoDisplayPath -Repository $repository -Value $targetPath
    $url = "http://${hostAddress}:$Port/"

    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($targetPath)) -Force | Out-Null

    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        $display = Get-BentoDisplayPath -Repository $repository -Value $sourcePath
        throw "Bento変換結果が見つかりません。`n`n必要なファイル:`n$display`n`n先にHTML-first変換を実行してください。"
    }
    if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
        $display = Get-BentoDisplayPath -Repository $repository -Value $registryPath
        throw "Bento registryが見つかりません。`n`n必要なファイル:`n$display"
    }

    $lockHandle = Enter-BentoLauncherLock -Repository $repository
    if (-not $lockHandle.Acquired) {
        throw '別のBento Work editorランチャーが起動処理中です。数秒後にもう一度実行してください。'
    }

    $session = $null
    if (Test-Path -LiteralPath $sessionPath -PathType Leaf) {
        try {
            $session = Get-Content -LiteralPath $sessionPath -Raw -Encoding utf8 | ConvertFrom-Json
        }
        catch {
            throw "既存session JSONを読み取れません。安全のため新しいサーバーを起動しません: $sessionPath"
        }
        if ([string]$session.format -ne 'bento/work-editor-session/v1') {
            throw "未知のWork editor session形式です: $($session.format)"
        }
    }

    $status = Get-BentoEditorStatus -HostAddress $hostAddress -Port $Port
    if ($null -ne $status) {
        if (-not (Test-BentoStatusTarget -Status $status -ExpectedTarget $expectedStatusTarget)) {
            throw "port $Port では別のBento Work editorが起動しています（target: $($status.target)）。`n上書きや自動停止は行いません。別のportを指定してください: start_bento_editor.cmd -Port 8766"
        }
        if ($null -ne $session) {
            $sameRequest = ([int]$session.port -eq $Port) -and
                [string]::Equals([string]$session.target, $targetPath, [System.StringComparison]::OrdinalIgnoreCase)
            if (-not $sameRequest) {
                throw '既存sessionは別のportまたはtargetを使用しています。先にstop_bento_editor.cmdで停止してください。'
            }
            $identity = Test-BentoSessionProcessIdentity -Session $session -Repository $repository
            if (-not $identity.Valid) {
                throw "既存sessionのPIDを安全に検証できません: $($identity.Reason)"
            }
        }
        if (-not $NoClipboard -and -not (Copy-BentoUrlToClipboard -Url $url)) {
            Write-Warning 'URLをクリップボードへコピーできませんでした。サーバーは起動済みです。'
        }
        Write-Host "Bento Work editorは既に起動しています。`n$url"
        exit 0
    }

    if (Test-BentoPortOpen -HostAddress $hostAddress -Port $Port) {
        throw "port $Port は別のサービスが使用しています。プロセスは停止しません。`n別のportを指定してください: start_bento_editor.cmd -Port 8766"
    }

    if ($null -ne $session) {
        $identity = Test-BentoSessionProcessIdentity -Session $session -Repository $repository
        if ($identity.Exists) {
            throw "既存sessionのプロセスが残っていますが、Work editor statusを確認できません。安全のため新しいサーバーを起動しません: $($identity.Reason)"
        }
        Remove-Item -LiteralPath $sessionPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    }

    $python = Find-BentoPython -Repository $repository
    foreach ($current in @($logPath, $stdoutLogPath, $errorLogPath)) {
        $previous = $current.Substring(0, $current.Length - 4) + '.previous.log'
        if (Test-Path -LiteralPath $current -PathType Leaf) {
            Move-Item -LiteralPath $current -Destination $previous -Force
        }
    }

    $arguments = @(
        '-u', '-m', 'scripts.run_bento_work_editor',
        '--source', $sourcePath,
        '--target', $targetPath,
        '--registry', $registryPath,
        '--host', $hostAddress,
        '--port', [string]$Port
    )
    $argumentLine = ($arguments | ForEach-Object { ConvertTo-BentoProcessArgument -Argument ([string]$_) }) -join ' '
    $startedAt = [System.DateTimeOffset]::Now.ToString('o')
    $previousPythonUtf8 = $env:PYTHONUTF8
    $previousPythonIoEncoding = $env:PYTHONIOENCODING
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $startedProcess = Start-Process -FilePath $python.Executable -ArgumentList $argumentLine `
            -WorkingDirectory $repository -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $stdoutLogPath -RedirectStandardError $errorLogPath
    }
    finally {
        if ($null -eq $previousPythonUtf8) { Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue }
        else { $env:PYTHONUTF8 = $previousPythonUtf8 }
        if ($null -eq $previousPythonIoEncoding) { Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue }
        else { $env:PYTHONIOENCODING = $previousPythonIoEncoding }
    }

    $deadline = [System.DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $startedStatus = $null
    do {
        Start-Sleep -Milliseconds 250
        $startedProcess.Refresh()
        if ($startedProcess.HasExited) {
            break
        }
        $candidateStatus = Get-BentoEditorStatus -HostAddress $hostAddress -Port $Port -TimeoutSeconds 1
        if ($null -ne $candidateStatus) {
            if (Test-BentoStatusTarget -Status $candidateStatus -ExpectedTarget $expectedStatusTarget) {
                $startedStatus = $candidateStatus
            }
            break
        }
    } while ([System.DateTime]::UtcNow -lt $deadline)

    if ($null -eq $startedStatus) {
        if ($null -ne $startedProcess) {
            $startedProcess.Refresh()
            if (-not $startedProcess.HasExited) {
                Stop-Process -Id $startedProcess.Id -Force -ErrorAction SilentlyContinue
                $startedProcess.WaitForExit(5000) | Out-Null
            }
        }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $sessionPath -Force -ErrorAction SilentlyContinue
        Add-LauncherLog -Lines @(
            "launcherStartedAt=$startedAt", "python=$($python.Executable)", "source=$sourcePath",
            "target=$targetPath", "registry=$registryPath", "host=$hostAddress", "port=$Port",
            "command=$($python.Executable) $argumentLine", "pid=$($startedProcess.Id)", 'status=failed'
        )
        $tail = @()
        if (Test-Path -LiteralPath $errorLogPath -PathType Leaf) {
            $tail = Get-Content -LiteralPath $errorLogPath -Tail 20 -Encoding utf8
        }
        throw "Bento Work editorを${StartupTimeoutSeconds}秒以内に確認できませんでした。起動したプロセスは停止しました。`n`nログ末尾:`n$($tail -join "`n")"
    }

    $snapshot = Get-BentoProcessSnapshot -ProcessId $startedProcess.Id
    if (-not $snapshot.Exists) {
        throw '起動確認後にWork editorプロセスが終了しました。'
    }

    $sessionRecord = [ordered]@{
        format = 'bento/work-editor-session/v1'
        pid = $startedProcess.Id
        startedAt = $startedAt
        processStartTimeUtc = $snapshot.StartTimeUtc
        repository = $repository
        source = $sourcePath
        target = $targetPath
        registry = $registryPath
        host = $hostAddress
        port = $Port
        url = $url
    }
    $sessionTemporary = $sessionPath + '.tmp'
    $sessionRecord | ConvertTo-Json | Set-Content -LiteralPath $sessionTemporary -Encoding utf8
    Move-Item -LiteralPath $sessionTemporary -Destination $sessionPath -Force
    Set-Content -LiteralPath $pidPath -Value ([string]$startedProcess.Id) -Encoding ascii
    Add-LauncherLog -Lines @(
        "launcherStartedAt=$startedAt", "python=$($python.Executable)", "pythonDetectedBy=$($python.DetectedBy)",
        "source=$sourcePath", "target=$targetPath", "registry=$registryPath", "host=$hostAddress", "port=$Port",
        "command=$($python.Executable) $argumentLine", "pid=$($startedProcess.Id)",
        "revision=$($startedStatus.revision)", "validation=$($startedStatus.validation)", 'status=started',
        'workEditorStdout:', @((Get-Content -LiteralPath $stdoutLogPath -Encoding utf8 -ErrorAction SilentlyContinue))
    )

    if (-not $NoClipboard -and -not (Copy-BentoUrlToClipboard -Url $url)) {
        Write-Warning 'URLをクリップボードへコピーできませんでした。サーバー起動自体は成功しています。'
    }

    Write-Host "Bento Work editorを起動しました。`n$url`n`nChatGPT Workの内蔵ブラウザーで上記ページを開くか、`n既に開いているBentoSlideタブを再読み込みしてください。"
    exit 0
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally {
    Exit-BentoLauncherLock -Handle $lockHandle
}
