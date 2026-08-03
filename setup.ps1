$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/4] Creating Python environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw "Python 3.11+ is required. Install it from python.org and enable Add Python to PATH."
    }
}

$VenvPython = ".venv\Scripts\python.exe"
Write-Host "[2/4] Installing Python dependencies..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

Write-Host "[3/4] Installing verified model and search indexes..."
& $VenvPython scripts\runtime_assets.py install

if (-not (Test-Path ".env")) {
    Write-Host "[4/4] GLM API key is not configured."
    $SecureKey = Read-Host "Enter ZAI_API_KEY (leave blank to configure later)" -AsSecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
    try { $PlainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
    if ($PlainKey) {
        [IO.File]::WriteAllText((Join-Path $ProjectDir ".env"), "ZAI_API_KEY=$PlainKey`n", [Text.UTF8Encoding]::new($false))
        Write-Host "Saved locally in .env (ignored by Git)."
    } else {
        Write-Host "Skipped. Copy .env.example to .env before using Q&A."
    }
    $PlainKey = $null
} else {
    Write-Host "[4/4] Local .env already exists."
}

& $VenvPython scripts\runtime_assets.py verify
Write-Host "Setup complete. Double-click start.cmd or run .\start.ps1"
