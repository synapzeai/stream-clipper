# One-time setup (Windows PowerShell): creates .venv and installs Python packages.
Set-Location $PSScriptRoot
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Write-Host "ffmpeg not found. Install it: winget install Gyan.FFmpeg"; exit 1 }
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -q --upgrade pip
.\.venv\Scripts\python -m pip install -q -r requirements.txt
Write-Host "Ready. Try: .\.venv\Scripts\python scripts\clipper.py live https://www.twitch.tv/<channel>"
