@echo off
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0battery_tool.py"
) else (
    start "" pyw.exe "%~dp0battery_tool.py"
)
