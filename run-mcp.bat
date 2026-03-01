@echo off
cd /d "%~dp0"
"%~dp0venv-windows\Scripts\python.exe" -m src.mcp.server %*
