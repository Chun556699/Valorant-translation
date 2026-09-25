@echo off
chcp 65001 >nul
REM 打包服务端部署产物：dist\vtranslate-server.tar.gz
REM 内含 server 代码 + 管理后台静态页 + deploy 部署脚本，不含 .env / 数据库 / 测试脚本。
REM 上传到服务器后：tar xzf vtranslate-server.tar.gz ^&^& cd vtranslate-server
REM   Docker：   cd deploy ^&^& cp ../server/.env.example ../server/.env ^&^& docker compose up -d --build
REM   裸机：     sudo bash deploy/install.sh
setlocal
cd /d %~dp0
set STAGE=dist\vtranslate-server
if exist %STAGE% rmdir /s /q %STAGE%
mkdir %STAGE%\server\admin %STAGE%\deploy
copy /y requirements-server.txt %STAGE%\ >nul
copy /y .dockerignore %STAGE%\ >nul
for %%f in (app config db epay relay valorant_terms) do copy /y server\%%f.py %STAGE%\server\ >nul
copy /y server\game_terms.json %STAGE%\server\ >nul
copy /y server\.env.example %STAGE%\server\ >nul
copy /y server\Dockerfile %STAGE%\server\ >nul
xcopy /e /q /y server\admin %STAGE%\server\admin\ >nul
xcopy /e /q /y deploy %STAGE%\deploy\ >nul
tar -czf dist\vtranslate-server.tar.gz -C dist vtranslate-server || (echo 打包失败 & exit /b 1)
rmdir /s /q %STAGE%
echo.
echo 输出: dist\vtranslate-server.tar.gz
