@echo off
where py >nul 2>nul
if errorlevel 1 (
  python -X utf8 "%~dp0topclass" %*
) else (
  py -3 -X utf8 "%~dp0topclass" %*
)
exit /b %errorlevel%
