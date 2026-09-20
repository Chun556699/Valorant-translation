@echo off
cd /d %~dp0server
if exist .env (for /f "usebackq tokens=1,* delims== eol=#" %%a in (".env") do set %%a=%%b)
pip install -r ..\requirements-server.txt
uvicorn app:app --host %HOST% --port %PORT%
