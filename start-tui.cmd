@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m shu_vod tui %*
) else if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" -m shu_vod tui %*
) else (
  python -m shu_vod tui %*
)
if errorlevel 1 pause
endlocal
