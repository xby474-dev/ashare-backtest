param(
    [ValidateSet('demo', 'test')][string]$Command = 'demo',
    [switch]$Research,
    [string]$Output = 'outputs/demo'
)
$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $taskPythonCommand) { throw 'Python 3.11+ is required. Install Python or create .venv first.' }
    $taskPython = $taskPythonCommand.Source
}
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Command -eq 'test') {
        & $taskPython -m unittest discover -s tests -v
    } elseif ($Research) {
        & $taskPython -m ashare demo --output $Output --research
    } else {
        & $taskPython -m ashare demo --output $Output
    }
    $taskExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $taskExitCode
