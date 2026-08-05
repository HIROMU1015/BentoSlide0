[CmdletBinding()]
param([switch]$NoClipboard)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'bento_editor_launcher.common.ps1')

$repository = Get-BentoRepositoryRoot -ScriptsDirectory $PSScriptRoot
Set-Location -LiteralPath $repository
try {
    $python = Find-BentoLauncherPython -Repository $repository -RequiredImports @('bento_converter', 'yaml', 'jsonschema', 'scripts.deck_workflow')
    $previousConsoleEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    try {
        $payload = & $python.Executable -m scripts.deck_workflow --root $repository status --json 2>&1
        $workflowExitCode = $LASTEXITCODE
    }
    finally { [Console]::OutputEncoding = $previousConsoleEncoding }
    if ($workflowExitCode -ne 0) { throw "Cannot read deck workflow state: $($payload -join ' ')" }
    $deck = ($payload -join "`n") | ConvertFrom-Json
    $stage = [string]$deck.workflow.stage
    switch ($stage) {
        { $_ -in @('planning', 'html_authoring', 'html_review') } {
            if ($NoClipboard) {
                & (Join-Path $PSScriptRoot 'start_html_preview.ps1') -Port ([int]$deck.preview.htmlPort) -NoClipboard
            }
            else {
                & (Join-Path $PSScriptRoot 'start_html_preview.ps1') -Port ([int]$deck.preview.htmlPort)
            }
            exit $LASTEXITCODE
        }
        'bento_finalization' {
            $sourcePath = Resolve-BentoLauncherPath -Repository $repository -Value ([string]$deck.outputs.generatedHtml)
            $targetPath = Resolve-BentoLauncherPath -Repository $repository -Value ([string]$deck.outputs.finalHtml)
            $registryPath = Join-Path ([System.IO.Path]::GetDirectoryName($sourcePath)) 'diagnostics\merged-registry.json'
            if ($NoClipboard) {
                & (Join-Path $PSScriptRoot 'start_bento_editor.ps1') `
                    -Source $sourcePath -Target $targetPath -Registry $registryPath `
                    -Port ([int]$deck.preview.bentoPort) -NoClipboard
            }
            else {
                & (Join-Path $PSScriptRoot 'start_bento_editor.ps1') `
                    -Source $sourcePath -Target $targetPath -Registry $registryPath `
                    -Port ([int]$deck.preview.bentoPort)
            }
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            $url = "http://127.0.0.1:$($deck.preview.bentoPort)/"
            & $python.Executable -m scripts.deck_workflow --root $repository set-current-url --url $url | Out-Null
            exit $LASTEXITCODE
        }
        { $_ -in @('ready_for_conversion', 'converting', 'bento_validation') } {
            Write-Host "Current stage is $stage. Codex conversion/validation work is required; no server was started."
            exit 0
        }
        'initialized' {
            Write-Host 'The project is initialized. Add a primary PDF and ask Work to create this deck.'
            exit 0
        }
        'awaiting_plan_approval' {
            Write-Host 'The plan is awaiting a content-level decision. No server was started.'
            exit 0
        }
        'complete' {
            Write-Host 'The deck workflow is complete. No server was started.'
            exit 0
        }
        'blocked' {
            Write-Host "The workflow is blocked and owned by $($deck.workflow.owner): $($deck.workflow.blockingReason)"
            exit 1
        }
        default { throw "Unsupported workflow stage: $stage" }
    }
}
catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
