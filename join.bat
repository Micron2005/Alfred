@echo off
rem Double-click this on a laptop or any other machine: it finds the desktop
rem on the network by itself and appears in Alfred's page as a new machine.
rem Give it a job there. Optional: join.bat nats://DESKTOP-IP:4222
cd /d "%~dp0"
if "%~1"=="" (python run_node.py) else (python run_node.py --bus %1)
pause
