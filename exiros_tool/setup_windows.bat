@echo off
rem One-time setup: creates a private Python environment and installs what the tool needs.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
python -m venv .venv || (pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
echo.
echo Setup finished. Double-click "capture_pages.bat" first, then "download_all_rfqs.bat" once the settings are filled in.
pause
