# Solar Aggregator - start everything (local site + public share link)
# Double-click this file. Keep the window open while others use the site.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "Starting Solar Aggregator on http://127.0.0.1:5000 ..." -ForegroundColor Yellow
Start-Process python -ArgumentList "app.py" -WorkingDirectory $here -WindowStyle Minimized

Write-Host "Starting public tunnel (Cloudflare Quick Tunnel) ..." -ForegroundColor Yellow
# The tunnel prints its public URL below - share THAT link with testers.
& "$here\cloudflared.exe" tunnel --url http://127.0.0.1:5000 --no-autoupdate
