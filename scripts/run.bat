@echo off
cd /d "%~dp0"
call .venv\Scripts\activate
python app_ui.py
pause
