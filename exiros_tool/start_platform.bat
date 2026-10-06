@echo off
rem Starts the RFQ Platform and opens it in your browser. Close this window to stop it.
cd /d "%~dp0"
.venv\Scripts\python app.py
pause
