@echo off
rem LEO回線測定ツール(Windows用)- ダブルクリックで対話モードが起動します
cd /d %~dp0
where py >nul 2>nul
if %errorlevel%==0 (
    py -m leo_analyzer
) else (
    python -m leo_analyzer
)
pause
