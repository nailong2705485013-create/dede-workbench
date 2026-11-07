@echo off
cd /d "%~dp0"
start "" pythonw -m agent_workbench.launcher
if errorlevel 1 pause
