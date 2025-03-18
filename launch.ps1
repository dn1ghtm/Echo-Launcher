$host.UI.RawUI.WindowTitle = "Echo Launcher"

Write-Host "Installing dependencies..." -ForegroundColor Cyan
pip install -r requirements.txt

Write-Host "Starting launcher..." -ForegroundColor Green
python launcher.py
Read-Host -Prompt "Press Enter to exit" 