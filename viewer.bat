@echo off
rem LEOデータビューア(Windows)- ダブルクリックでブラウザが開きます
cd /d %~dp0
where py >nul 2>nul
if %errorlevel%==0 ( py -m leo_analyzer --viewer ) else ( python -m leo_analyzer --viewer )
pause
