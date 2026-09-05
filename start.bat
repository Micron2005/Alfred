@echo off
rem Double-click this on the desktop: starts Alfred and opens his page.
rem Everything else -- machines, jobs, approvals -- happens in the browser.
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
python run_server.py --config configs\desktop.toml --open
pause
