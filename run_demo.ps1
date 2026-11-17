# One command: create a virtual environment, install dependencies, start the dashboard.
# No API key needed: the dashboard reads the pre-built, synthetic data/cinv_demo.db.
#   powershell -ExecutionPolicy Bypass -File .\run_demo.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Test-Python311($exe, $prefix) {
    try {
        & $exe @prefix -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$healthy = (Test-Path $venvPy) -and (Test-Python311 $venvPy @())
if (-not $healthy) {
    $exe = $null; $prefix = @()
    foreach ($c in @(@("py", @("-3")), @("python", @()), @("python3", @()))) {
        if ((Get-Command $c[0] -ErrorAction SilentlyContinue) -and (Test-Python311 $c[0] $c[1])) {
            $exe = $c[0]; $prefix = $c[1]; break
        }
    }
    if (-not $exe) { throw "Python 3.11 or newer is required (developed on 3.13). Install it from python.org and re-run." }
    if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
    Write-Host "Creating .venv ..."
    & $exe @prefix -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}

Write-Host "Installing dependencies ..."
& $venvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

# Make sure a stray key in the environment can never trigger API calls from the demo.
Remove-Item Env:GROQ_API_KEY -ErrorAction SilentlyContinue
Write-Host "Starting the dashboard at http://localhost:8501 (Ctrl+C to stop) ..."
& $venvPy -m streamlit run app.py
