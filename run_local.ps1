param(
    [ValidateSet("serve", "demo", "test")]
    [string]$Mode = "serve",
    [int]$Port = 8000,
    [switch]$LiveAws
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $RepoRoot "src"

# Default Vertex AI ADC configuration (uses gcloud application_default_credentials.json)
if (-not $env:GOOGLE_CLOUD_PROJECT) {
    $env:GOOGLE_CLOUD_PROJECT = "cog01m1hm7b45680madcxrzh1adja"
}
if (-not $env:GOOGLE_CLOUD_LOCATION) {
    $env:GOOGLE_CLOUD_LOCATION = "global"
}
$env:CLOUD_OPS_USE_VERTEX_AI = "true"

if ($LiveAws) {
    $env:CLOUD_OPS_LIVE_AWS = "true"
}

# Prefer the local venv with google-genai + google-auth installed for Vertex AI ADC
$VenvPython = "C:\googie\.venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host " Cloud Ops Agent Launcher (Local Agent -> Vertex AI ADC + AWS ECS/Logs)" -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host " Python Runtime : $PythonExe"
Write-Host " Vertex Project : $env:GOOGLE_CLOUD_PROJECT ($env:GOOGLE_CLOUD_LOCATION)"
Write-Host " AWS Live Mode  : $($env:CLOUD_OPS_LIVE_AWS -eq 'true')"
Write-Host "------------------------------------------------------------------------"

if ($Mode -eq "test") {
    & $PythonExe -m unittest discover -s (Join-Path $RepoRoot "tests") -v
} elseif ($Mode -eq "demo") {
    if ($LiveAws) {
        & $PythonExe -m cloud_ops demo --live-aws
    } else {
        & $PythonExe -m cloud_ops demo
    }
} else {
    if ($LiveAws) {
        & $PythonExe -m cloud_ops serve --host 127.0.0.1 --port $Port --live-aws
    } else {
        & $PythonExe -m cloud_ops serve --host 127.0.0.1 --port $Port
    }
}
