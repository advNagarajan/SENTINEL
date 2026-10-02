@echo off
setlocal
cd /d "%~dp0"
python scripts\run_sentinel_mcp.py
exit /b %ERRORLEVEL%