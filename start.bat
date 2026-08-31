@echo off
cd /d "%~dp0"
call deeperseeker_env\Scripts\activate.bat
python app.py
pause
