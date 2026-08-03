$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
$Python = ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) { & "$ProjectDir\setup.ps1" }
& $Python scripts\runtime_assets.py verify 2>$null
if ($LASTEXITCODE -ne 0) { & $Python scripts\runtime_assets.py install }

$Backend = Start-Process -FilePath $Python -ArgumentList "qa-backend\server.py" -WorkingDirectory $ProjectDir -PassThru
$Frontend = Start-Process -FilePath $Python -ArgumentList @("-m", "http.server", "8097") -WorkingDirectory $ProjectDir -PassThru

Start-Sleep -Seconds 2
Start-Process "http://localhost:8097"
Write-Host "Tech-DB is running. Close this window or press Enter to stop both services."
Read-Host
Stop-Process -Id $Backend.Id, $Frontend.Id -ErrorAction SilentlyContinue
