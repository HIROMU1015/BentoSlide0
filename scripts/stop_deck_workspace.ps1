[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
Set-Location -LiteralPath $repository
$stopped = $false
$failed = $false
if (Test-Path -LiteralPath (Join-Path $repository 'output\html-preview-session.json')) {
    & (Join-Path $PSScriptRoot 'stop_html_preview.ps1')
    if ($LASTEXITCODE -ne 0) { $failed = $true } else { $stopped = $true }
}
if (Test-Path -LiteralPath (Join-Path $repository 'output\work-editor-session.json')) {
    & (Join-Path $PSScriptRoot 'stop_bento_editor.ps1')
    if ($LASTEXITCODE -ne 0) { $failed = $true } else { $stopped = $true }
}
if (-not $failed) {
    try {
        $python = Find-BentoLauncherPython -Repository $repository -RequiredImports @('yaml', 'jsonschema', 'scripts.deck_workflow')
        & $python.Executable -m scripts.deck_workflow --root $repository clear-current-url | Out-Null
    }
    catch { Write-Host 'Could not clear preview.currentUrl; inspect deck.yaml.' -ForegroundColor Yellow }
}
if ($failed) { exit 1 }
if (-not $stopped) { Write-Host 'No recorded BentoSlide workspace server is running.' }
exit 0
