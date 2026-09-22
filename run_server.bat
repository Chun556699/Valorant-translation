@echo off
setlocal
cd /d %~dp0server
rem 先给默认值，.env 仍可覆盖（HOST/PORT 在 .env 里常被注释掉）
set "HOST=0.0.0.0"
set "PORT=8765"
if exist .env (for /f "usebackq tokens=1,* delims== eol=#" %%a in (".env") do set %%a=%%b)
pip install -r ..\requirements-server.txt
uvicorn app:app --host %HOST% --port %PORT%
