[CmdletBinding()]
param(
    [ValidateRange(1, 120)][int]$ShutdownTimeoutSeconds = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
$stateDirectory = Join-Path $repository 'output'
$pidPath = Join-Path $stateDirectory 'work-editor.pid'
$sessionPath = Join-Path $stateDirectory 'work-editor-session.json'
$logPath = Join-Path $stateDirectory 'work-editor.log'
$mutexHandle = $null

try {
    $mutexHandle = Enter-BentoLauncherMutex -Repository $repository
    if (-not $mutexHandle.Acquired) {
        throw '別のBento Work editorランチャーが起動または停止処理中です。数秒後にもう一度実行してください。'
    }

    if (-not (Test-Path -LiteralPath $sessionPath -PathType Leaf)) {
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            Remove-Item -LiteralPath $pidPath -Force
        }
        Write-Host 'Bento Work editorは既に停止しています。'
        exit 0
    }

    try {
        $session = Get-Content -LiteralPath $sessionPath -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        throw "session JSONを読み取れません。無関係なプロセスを停止しないため処理を中止します: $sessionPath"
    }
    if ([string]$session.format -ne 'bento/work-editor-session/v1') {
        throw "未知のWork editor session形式です。プロセスは停止しません: $($session.format)"
    }
    if (-not [string]::Equals([string]$session.repository, $repository, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'sessionのrepositoryがこのランチャーと一致しません。プロセスは停止しません。'
    }
    if ([string]$session.host -ne '127.0.0.1' -or [int]$session.port -lt 1 -or [int]$session.port -gt 65535) {
        throw 'sessionのhost/portが安全なloopback設定ではありません。プロセスは停止しません。'
    }

    if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
        throw 'PIDファイルがありません。sessionだけでは停止せず、安全のため処理を中止します。'
    }
    $recordedPidText = (Get-Content -LiteralPath $pidPath -Raw -Encoding ascii).Trim()
    $recordedPid = 0
    if (-not [int]::TryParse($recordedPidText, [ref]$recordedPid) -or $recordedPid -ne [int]$session.pid) {
        throw 'PIDファイルとsession JSONが一致しません。プロセスは停止しません。'
    }

    $identity = Test-BentoSessionProcessIdentity -Session $session -Repository $repository
    if (-not $identity.Exists) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $sessionPath -Force -ErrorAction SilentlyContinue
        Write-Host 'Bento Work editorは既に停止しています。staleなPID/sessionを削除しました。'
        exit 0
    }
    if (-not $identity.Valid) {
        throw "PIDのプロセスをWork editorとして安全に検証できません。プロセスは停止しません: $($identity.Reason)"
    }

    $status = Get-BentoEditorStatus -HostAddress '127.0.0.1' -Port ([int]$session.port)
    $expectedTarget = Get-BentoDisplayPath -Repository $repository -Value ([string]$session.target)
    if ($null -eq $status) {
        throw 'Work editor statusを確認できません。プロセスは停止しません。'
    }
    if (-not (Test-BentoStatusTarget -Status $status -ExpectedTarget $expectedTarget)) {
        throw "Work editor statusのtargetがsessionと一致しません。プロセスは停止しません（actual: $($status.target)）。"
    }

    $finalIdentity = Test-BentoSessionProcessIdentity -Session $session -Repository $repository
    if (-not $finalIdentity.Exists) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $sessionPath -Force -ErrorAction SilentlyContinue
        Write-Host 'Bento Work editorは既に停止しています。staleなPID/sessionを削除しました。'
        exit 0
    }
    if (-not $finalIdentity.Valid) {
        throw "停止直前のPID再検証に失敗しました。プロセスは停止しません: $($finalIdentity.Reason)"
    }

    Stop-Process -Id $recordedPid -Force -ErrorAction Stop
    $deadline = [System.DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 200
        $processStillExists = $null -ne (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)
        $portStillOpen = Test-BentoPortOpen -HostAddress '127.0.0.1' -Port ([int]$session.port) -TimeoutMilliseconds 200
        if (-not $processStillExists -and -not $portStillOpen) {
            break
        }
    } while ([System.DateTime]::UtcNow -lt $deadline)

    if ($processStillExists -or $portStillOpen) {
        throw 'Work editorプロセスまたはportの停止を確認できません。session情報は保持します。'
    }

    Remove-Item -LiteralPath $pidPath -Force
    Remove-Item -LiteralPath $sessionPath -Force
    if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        Add-Content -LiteralPath $logPath -Value @(
            "stoppedAt=$([System.DateTimeOffset]::Now.ToString('o'))", "pid=$recordedPid", 'status=stopped'
        ) -Encoding utf8
    }
    Write-Host 'Bento Work editorを停止しました。final、revision backup、logは保持しています。'
    exit 0
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally {
    Exit-BentoLauncherMutex -Handle $mutexHandle
}
