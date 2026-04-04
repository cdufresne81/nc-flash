@echo off
set NCFLASH_MCP_MODE=1
"%~dp0NCFlash.exe" --transport stdio %*
